"""Memory helpers — profile lookup, trait fetch, memory fetch, context builder."""

import asyncio
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MEMORY_BASE = "https://memory.twilio.com"

_TRAIT_SKIP = {"firstName", "lastName", "name"}


class MemoryResponse:
    def __init__(self, observations, summaries):
        self.observations = observations
        self.summaries = summaries


class _Obs:
    def __init__(self, content): self.content = content


class _Sum:
    def __init__(self, content): self.content = content


def lookup_profile_id(phone: str, memory_store_id: str, api_key: str, api_token: str) -> Optional[str]:
    try:
        res = requests.post(
            f"{MEMORY_BASE}/v1/Stores/{memory_store_id}/Profiles/Lookup",
            json={"idType": "phone", "value": phone},
            auth=(api_key, api_token),
            timeout=5,
        )
        profiles = res.json().get("profiles", [])
        return profiles[0] if profiles else None
    except Exception as e:
        logger.warning(f"[memory] lookup_profile_id failed: {e}")
        return None


def fetch_profile_traits(profile_id: str, memory_store_id: str, api_key: str, api_token: str) -> dict:
    try:
        res = requests.get(
            f"{MEMORY_BASE}/v1/Stores/{memory_store_id}/Profiles/{profile_id}",
            auth=(api_key, api_token),
            timeout=5,
        )
        data = res.json()
        traits: dict = {}
        raw_traits = data.get("traits") or {}
        if isinstance(raw_traits, dict):
            for group_val in raw_traits.values():
                if isinstance(group_val, dict):
                    traits.update(group_val)
        elif isinstance(raw_traits, list):
            for trait_group in raw_traits:
                if not isinstance(trait_group, dict):
                    continue
                attrs = trait_group.get("attributes") or {}
                if isinstance(attrs, dict):
                    traits.update(attrs)
                elif isinstance(attrs, list):
                    for attr in attrs:
                        if isinstance(attr, dict) and "name" in attr:
                            traits[attr["name"]] = attr.get("value", "")
        return traits
    except Exception as e:
        logger.warning(f"[memory] fetch_profile_traits failed: {e}")
        return {}


def fetch_memory(profile_id: str, memory_store_id: str, api_key: str, api_token: str) -> Optional[MemoryResponse]:
    try:
        base = f"{MEMORY_BASE}/v1/Stores/{memory_store_id}/Profiles/{profile_id}"
        auth = (api_key, api_token)
        obs_res = requests.get(f"{base}/Observations", auth=auth, timeout=5)
        sum_res = requests.get(f"{base}/ConversationSummaries", auth=auth, timeout=5)
        observations = [_Obs(o["content"]) for o in (obs_res.json() or {}).get("observations") or []]
        summaries = [_Sum(s["content"]) for s in (sum_res.json() or {}).get("summaries") or []]
        return MemoryResponse(observations=observations, summaries=summaries)
    except Exception as e:
        logger.warning(f"[memory] fetch_memory failed: {e}")
        return None


async def prefetch_memory(
    phone: str, memory_store_id: str, api_key: str, api_token: str,
    profile_id: Optional[str] = None,
) -> tuple[Optional[MemoryResponse], dict, Optional[str]]:
    """Returns (memory, traits, profile_id)."""
    loop = asyncio.get_event_loop()
    if not profile_id:
        profile_id = await loop.run_in_executor(
            None, lookup_profile_id, phone, memory_store_id, api_key, api_token
        )
    if profile_id:
        memory, traits = await asyncio.gather(
            loop.run_in_executor(None, fetch_memory, profile_id, memory_store_id, api_key, api_token),
            loop.run_in_executor(None, fetch_profile_traits, profile_id, memory_store_id, api_key, api_token),
        )
    else:
        memory, traits = None, {}
    obs = len(memory.observations) if memory else 0
    sums = len(memory.summaries) if memory else 0
    logger.info(f"[memory] prefetch phone={phone} profileId={profile_id or 'none'} obs={obs} summaries={sums} traits={len(traits)}")
    return memory, traits, profile_id


def build_memory_context(memory: Optional[MemoryResponse], traits: Optional[dict] = None) -> str:
    sections = []
    if traits:
        trait_lines = []
        full_name = (f"{traits.get('firstName', '')} {traits.get('lastName', '')}".strip()
                     or traits.get("name", ""))
        if full_name:
            trait_lines.append(f"- Name: {full_name}")
        for key, val in traits.items():
            if key in _TRAIT_SKIP or not val:
                continue
            label = key.replace("_", " ").replace("-", " ")
            label = re.sub(r'([A-Z])', r' \1', label).strip().capitalize()
            trait_lines.append(f"- {label}: {val}")
        if trait_lines:
            sections.append("### Member Profile\n" + "\n".join(trait_lines))
    if memory and memory.observations:
        lines = [f"- {o.content}" for o in memory.observations]
        sections.append("### Previous Observations\n" + "\n".join(lines))
    if memory and memory.summaries:
        lines = [f"- {s.content}" for s in memory.summaries]
        sections.append("### Previous Summaries\n" + "\n".join(lines))
    if not sections:
        return ""
    return "The following is from previous interactions with this member.\n\n" + "\n\n".join(sections)


def write_sms_observation(profile_id: str, role: str, content: str,
                          memory_store_id: str, api_key: str, api_token: str) -> None:
    try:
        obs_content = f"[SMS {role}] {content}"
        requests.post(
            f"{MEMORY_BASE}/v1/Stores/{memory_store_id}/Profiles/{profile_id}/Observations",
            json={"observations": [{"content": obs_content, "source": "sms-conversation"}]},
            auth=(api_key, api_token),
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"[memory] write_sms_observation failed: {e}")
