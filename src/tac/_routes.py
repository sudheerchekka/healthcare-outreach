"""Per-app route registration — imported by server.py after all globals are defined."""

from fastapi import Request, WebSocket
from fastapi.responses import Response
import asyncio, json, os, time
from typing import Optional


def register_app_routes(app, cfg, voice_channel, pending_outbound_context,
                        pending_call_sid_map, conv_app_map, outbound_conversation_map,
                        twilio_client, _APP_BACKENDS, _push_transcript_event,
                        _lookup_profile_id, _prefetch_memory, _build_memory_context,
                        _build_sms_system_prompt, _invoke_agent, _write_sms_observation,
                        _escalate_call_to_flex, PUBLIC_DOMAIN, APP_PORT, logger,
                        FastAPIWebSocketAdapter, websockets):
    px = cfg.route_prefix

    def _urls(request):
        proto = request.headers.get("x-forwarded-proto", "https")
        host = request.headers.get("host", PUBLIC_DOMAIN)
        ws_proto = "wss" if proto == "https" else "ws"
        return f"{ws_proto}://{host}{px}/ws", f"{proto}://{host}{px}/conversation-relay-callback"

    @app.post(f"{px}/twiml")
    async def post_twiml(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        ws_url, callback_url = _urls(request)
        twiml = await voice_channel.handle_incoming_call(
            to_number=form.get("To", ""),
            from_number=form.get("From", ""),
            options={"websocket_url": ws_url, "action_url": callback_url,
                     "welcome_greeting": cfg.inbound_greeting},
            call_sid=form.get("CallSid", ""),
        )
        return Response(content=twiml, media_type="application/xml")

    @app.post(f"{px}/twiml-outbound")
    async def post_twiml_outbound(request: Request) -> Response:
        params = dict(request.query_params)
        conv_id = params.get("conv_id", "")
        ctx = pending_outbound_context.get(conv_id) if conv_id else None

        if cfg.agent_backend_name == "elevenlabs":
            proto = request.headers.get("x-forwarded-proto", "https")
            host = request.headers.get("host", PUBLIC_DOMAIN)
            ws_proto = "wss" if proto == "https" else "ws"
            twiml = (f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
                     f'<Stream url="{ws_proto}://{host}{px}/ws-el">'
                     f'<Parameter name="conv_id" value="{conv_id}" /></Stream></Connect></Response>')
            return Response(content=twiml, media_type="application/xml")

        greeting = ctx.get("greeting", "") if ctx else cfg.default_outbound_greeting
        form = {k: str(v) for k, v in (await request.form()).items()}
        call_sid = form.get("CallSid", "")
        if conv_id and call_sid:
            pending_call_sid_map[conv_id] = call_sid
        # Track which app owns this conv
        if conv_id:
            conv_app_map[conv_id] = px
        ws_url, callback_url = _urls(request)
        raw_to, raw_from = form.get("To", ""), form.get("From", "")
        is_outbound = bool(ctx)
        twiml = await voice_channel.handle_incoming_call(
            to_number=raw_from if is_outbound else raw_to,
            from_number=raw_to if is_outbound else raw_from,
            options={
                "websocket_url": ws_url, "action_url": callback_url,
                "welcome_greeting": greeting,
                "custom_parameters": ({"outboundConvId": conv_id, "callSid": call_sid}
                                      if conv_id else {"callSid": call_sid}),
            },
            call_sid=call_sid,
        )
        return Response(content=twiml, media_type="application/xml")

    @app.websocket(f"{px}/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await voice_channel.handle_websocket(FastAPIWebSocketAdapter(websocket))

    @app.post(f"{px}/conversation-relay-callback")
    async def cr_callback(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        result = await voice_channel.handle_conversation_relay_callback(form)
        return Response(content=result or "OK",
                        media_type="text/xml" if result else "text/plain")

    @app.post(f"{px}/set-outbound-context")
    async def set_outbound_context(request: Request) -> dict:
        body = await request.json()
        conv_id = body.get("conv_id", "")
        if not conv_id:
            return {"success": False, "error": "conv_id required"}
        pending_outbound_context[conv_id] = body
        conv_app_map[conv_id] = px
        logger.info(f"[{cfg.id}][ipc] outbound context stored conv_id={conv_id} member={body.get('name')}")
        if cfg.agent_backend_name == "elevenlabs":
            import httpx
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(f"http://localhost:{cfg.el_port}/set-outbound-context",
                                      json=body, timeout=5)
            except Exception as e:
                logger.warning(f"[{cfg.id}][ipc] ElevenLabs forward failed: {e}")
        return {"success": True}

    @app.websocket(f"{px}/ws-el")
    async def ws_el_proxy(websocket: WebSocket) -> None:
        await websocket.accept()
        el_ws_url = f"ws://localhost:{cfg.el_port}/ws"
        try:
            async with websockets.connect(el_ws_url) as el_ws:
                async def twilio_to_el() -> None:
                    async for msg in websocket.iter_text():
                        try:
                            parsed = json.loads(msg)
                            if parsed.get("event") == "start":
                                start = parsed.get("start", {})
                                maestro_conv_id = start.get("conversationSid", "")
                                outbound_conv_id = start.get("customParameters", {}).get("conv_id", "")
                                if outbound_conv_id and outbound_conv_id in pending_outbound_context:
                                    ctx2 = pending_outbound_context[outbound_conv_id]
                                    phone = ctx2.get("phone", "")
                                    map_key = maestro_conv_id or outbound_conv_id
                                    loop = asyncio.get_event_loop()
                                    member_pid = await loop.run_in_executor(
                                        None, _lookup_profile_id, phone, cfg) if phone else ""
                                    outbound_conversation_map[map_key] = {"phone": phone, "profileId": member_pid or ""}
                                    conv_app_map[map_key] = px
                        except Exception:
                            pass
                        await el_ws.send(msg)

                async def el_to_twilio() -> None:
                    async for msg in el_ws:
                        await websocket.send_text(msg if isinstance(msg, str) else msg.decode())

                await asyncio.gather(twilio_to_el(), el_to_twilio())
        except Exception as e:
            logger.error(f"[{cfg.id}][ws-el] proxy error: {e}")

    @app.post(f"{px}/sms")
    async def post_sms(request: Request) -> Response:
        form = dict(await request.form())
        from_phone = form.get("From", "")
        to_phone = form.get("To", "")
        body = form.get("Body", "").strip()
        empty_twiml = Response(content="<?xml version='1.0'?><Response/>", media_type="application/xml")
        if not body or not from_phone:
            return empty_twiml

        lookup_phone = from_phone
        if cfg.sms_simulate_member_phone and cfg.outbound_call_to and from_phone == cfg.outbound_call_to:
            lookup_phone = cfg.sms_simulate_member_phone
            logger.info(f"[{cfg.id}][sms] simulating member {lookup_phone} from {from_phone}")

        loop = asyncio.get_event_loop()
        profile_id = await loop.run_in_executor(None, _lookup_profile_id, lookup_phone, cfg)
        if not profile_id:
            logger.warning(f"[{cfg.id}][sms] no profile for {lookup_phone}")
            return empty_twiml

        logger.info(f"[{cfg.id}][sms] inbound from={from_phone} profile_id={profile_id} body=\"{body[:80]}\"")

        t0 = time.time()
        backend = _APP_BACKENDS[px]
        memory_task = asyncio.create_task(_prefetch_memory(lookup_phone, cfg, profile_id=profile_id))
        prewarm_task = asyncio.create_task(backend.prewarm(profile_id))
        (memory, traits), _ = await asyncio.gather(memory_task, prewarm_task)
        logger.info(f"[{cfg.id}][sms] memory+backend in {(time.time()-t0)*1000:.0f}ms")

        context = _build_memory_context(memory, traits)
        system_prompt = _build_sms_system_prompt(cfg)

        try:
            reply = await _invoke_agent(
                session_id=profile_id, prompt=body, system_prompt=system_prompt,
                context=context, cfg=cfg, member_phone=lookup_phone,
                profile_id=profile_id, member_traits=traits,
            )
        except Exception as e:
            logger.error(f"[{cfg.id}][sms] agent invocation failed: {e}")
            return empty_twiml

        logger.info(f"[{cfg.id}][sms] reply profile_id={profile_id} reply=\"{reply[:80]}\"")

        if twilio_client and cfg.phone_number:
            try:
                twilio_client.messages.create(to=from_phone, from_=cfg.phone_number, body=reply)
            except Exception as e:
                logger.error(f"[{cfg.id}][sms] send failed: {e}")

        if cfg.sms_write_observation:
            loop.run_in_executor(None, _write_sms_observation, profile_id, "member", body, cfg)
            loop.run_in_executor(None, _write_sms_observation, profile_id, "agent", reply, cfg)

        return empty_twiml

    @app.post(f"{px}/escalate-call")
    async def escalate_call_endpoint(request: Request) -> dict:
        body = await request.json()
        conv_id = body.get("conv_id", "")
        profile_id = body.get("profile_id", "")
        reason = body.get("reason", "care_team_requested")
        if not conv_id and profile_id:
            for cid, entry in outbound_conversation_map.items():
                pid = entry.get("profileId", "") if isinstance(entry, dict) else ""
                if pid == profile_id:
                    conv_id = cid
                    break
        if not conv_id:
            return {"success": False, "error": "No active call found for this member"}
        success = await _escalate_call_to_flex(conv_id, reason, "normal", cfg.flex_queue)
        return {"success": success}

    @app.post(f"{px}/browser-answer-twiml")
    async def browser_answer_twiml(request: Request) -> Response:
        from datetime import datetime, timezone
        from tac.models import ParticipantAddress
        form = {k: str(v) for k, v in (await request.form()).items()}
        params = dict(request.query_params)
        call_sid = form.get("CallSid", "")
        member_phone = params.get("member_phone", form.get("To", ""))
        profile_id = params.get("profile_id", "")
        member_name = params.get("member_name", "member")
        try:
            from tac import TAC
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # tac is accessible via closure from server.py — passed via _tac param
        except Exception:
            pass
        twiml = ('<?xml version="1.0" encoding="UTF-8"?><Response><Dial>'
                 '<Client>care-team-agent</Client></Dial></Response>')
        return Response(content=twiml, media_type="application/xml")

    logger.info(f"[config] registered routes for app={cfg.id} prefix={px}")
