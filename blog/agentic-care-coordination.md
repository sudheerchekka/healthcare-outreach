# From Callbacks to Care Conversations: Building an AI-Powered Outreach Agent with Twilio and AWS AgentCore

Healthcare has a follow-up problem. Not a technology problem — a bandwidth problem. And for the 133 million Americans living with at least one chronic condition, the gap between the care they need and the care they actually receive often comes down to one thing: whether a care coordinator had time to make a phone call.

---

## The Bottleneck Nobody Talks About

Every care team knows the stat: roughly 40% of patients with chronic conditions miss recommended follow-up care. High-risk members skip PCP appointments. Medication adherence drops off between visits. Blood pressure goes unchecked for months. And when something preventable becomes an emergency, the cost — human and financial — is significant.

The instinct is to blame the patients. But spend a day with a care coordinator and you'll see the real problem. They are working through a list of 30 names. They dial manually. They leave voicemails. They wait for callbacks that don't come. When someone does pick up, they're flipping between tabs trying to remember what the last call was about — "Was it the A1C numbers? The PCP referral? The medication refill?" — while the member is already on the line.

That context scramble isn't a small inefficiency. It's the difference between a member feeling known and cared for, and a member who feels like one more item on a checklist.

The tools haven't solved this. Electronic health records digitized the paperwork but didn't change the workflow. Automated phone trees get hung up on or ignored. What care teams need isn't faster dialing. They need an agent that can hold the context, initiate the conversation, and document the outcome — so the coordinator can focus on the members who need the most.

---

## A Typical Morning for a Care Coordinator

It's 9am. Sarah opens her care management platform and pulls up the list of members due for follow-up. There are 22 of them today.

She starts with Maria — a 67-year-old with Type 2 diabetes who missed her last PCP appointment. Sarah opens Maria's chart, scans the last visit notes, switches to the phone, dials, and waits. Voicemail. She leaves a message, switches back to the chart, and logs the attempt. That's three minutes, four tab switches, and one entry in an activity log that nobody reads.

She moves to the next member. Then the next. By noon she's made 11 calls, reached 3 people, and spent 40 minutes on documentation. The afternoon is the same pattern in reverse — waiting for callbacks, re-establishing context each time someone picks up, trying to remember what she was calling about when she left the voicemail three hours ago.

The members who answered? Great conversations. The other 19? They're still on the list. They'll roll to tomorrow, or they'll fall off entirely if something more urgent comes in.

This isn't a staffing problem — Sarah is good at her job. It's a leverage problem. The most valuable thing a care coordinator does is the conversation itself: understanding what's going on with a member, building trust, guiding them toward the next step. Everything around that conversation — the dialing, the context lookup, the documentation — is overhead that compounds every day.

---

## What Agentic Outreach Changes

What if the call could start itself? What if the agent already knew Maria's name, her follow-up goal, her last visit summary, and the two observations the care team had flagged — before the first word was spoken?

Not a robocall with a press-1-to-confirm. Not a scripted chatbot. A real voice conversation, driven by an AI agent that has Maria's history and knows how to steer toward a clear next step.

That's what we built with Owl Health's Care Team Portal — an outreach demo built on **Twilio Agent Connect (TAC)**, **AWS AgentCore**, and **Conversation Intelligence**. The care team sees a dashboard. One click triggers an outbound call with a personalized greeting. The AI agent handles the conversation. When the call ends, the summary writes itself back to the member's profile.

The coordinator's job shifts from making calls to reviewing outcomes.

---

## The Care Team Dashboard

The starting point is simple: a list of members, a goal for each one, and two buttons.

[SCREENSHOT: Care Team Portal showing member cards with name, phone, next follow-up goal, status badge (pending/scheduled/completed), and last call summary column]

Each member card shows the information that matters for outreach: name, phone number, the next follow-up goal the care team set ("Medication refill discussion"), additional context ("Patient has been missing doses"), current status, and — after any call has happened — the AI-generated summary of what was discussed.

The follow-up fields are inline-editable. A coordinator can update the goal for the next call right in the dashboard without touching a separate system.

When they click the voice button, the call goes out immediately. The greeting is built from the goal and context fields — personalized before the member even picks up:

```
Hi Maria, this is the Owl Health Care Team calling.
I'm reaching out about your medication refill. Do you have a moment to chat?
```

The SMS button sends a templated message with the same context, for members who prefer text.

After the call, Conversation Intelligence processes the recording and the `Last Call Summary` column updates automatically. The coordinator didn't type a single word of documentation.

---

## What Maria Experiences

Maria's phone rings. She answers and hears her name and a specific reason for the call — not a generic "we're calling about your account." The agent knows she was flagged for a medication refill discussion and that she's been missing doses.

When Maria responds, the AI agent takes over. It already has:

- Her last call summary ("Confirmed PCP appointment. Mentioned difficulty remembering evening doses.")
- Care team observations ("Member prefers morning calls. Has diabetes type 2.")
- The specific follow-up goal ("Medication refill discussion")

It doesn't ask "Can you remind me why you were flagged for a call today?" It picks up where the last conversation left off. When Maria says she forgot to refill her metformin, the agent doesn't ask for her date of birth to look up her record — it already knows who she is. It steers toward a next step: confirming the refill was sent, checking if she needs a reminder, asking about her appointment.

The conversation feels like it's coming from someone who knows her case. Because it is.

[SCREENSHOT: TAC server logs showing pre-warm timing, turn-1 TTFT, and token streaming to ConversationRelay]

---

## How It's Built

The architecture has two servers running in parallel, connected by Twilio in the middle.

```
Care Team Browser (port 8001)
  └── Node.js App Server
        ├── /api/members           ← member profiles from TAC Memory Store
        ├── /api/outbound-call     ← triggers the call
        └── /ci-webhook            ← receives post-call summary from CI

Python TAC Server (port 8000, exposed via ngrok)
  └── FastAPI + Twilio Agent Connect SDK
        ├── /twiml-outbound        ← personalized TwiML with greeting
        ├── /ws                    ← ConversationRelay WebSocket
        └── AgentCore WS pool      ← persistent connection per call session

AWS AgentCore Runtime
  └── Strands Agent (@app.websocket)
        └── Amazon Bedrock (Claude Opus 4)
```

**The Python TAC server** handles everything Twilio touches: TwiML generation, the ConversationRelay WebSocket, and the connection to AgentCore. It's the bridge between real-time voice and the AI agent.

**The Node.js app server** handles everything the care team touches: the dashboard, the member API, and the post-call CI webhook. These two servers communicate via simple HTTP — the Node server POSTs the outbound context to the TAC server before the call is placed, and queries it afterward for the CI webhook routing.

**AWS AgentCore** hosts the Strands-based AI agent. It exposes a persistent WebSocket endpoint — one connection per call, kept open for the entire session. The Strands `Agent` object lives in memory for the call's lifetime, so turns 2, 3, and 4 don't need any API calls to load conversation history. They're just inference.

### The Pre-Warm Trick

The most important latency optimization happens before the member says a word.

When Twilio's ConversationRelay WebSocket first connects (the `setup` event), it fires before the greeting even plays. The TAC server uses that window to do two things in parallel:

1. Open the AgentCore WebSocket connection (presigned URL → TLS handshake)
2. Fetch the member's profile from TAC Memory Store (profile lookup + observations + summaries)

Both complete while Twilio is speaking the greeting. By the time Maria says "Yes, I have a moment," the WebSocket is open and the memory context is cached. The first turn's response time is pure model inference — no connection setup, no memory API calls on the critical path.

```python
async def _prewarm(conv_id: str, session_id: str, phone: str) -> None:
    ws_task = asyncio.create_task(get_or_create_agent_ws(session_id))
    mem_task = asyncio.create_task(_prefetch_memory(phone))

    ws = await ws_task
    memory = await mem_task

    if memory:
        memory_context_cache[conv_id] = _build_memory_context(memory)
```

The context the agent receives on turn 1 looks like this:

```
[Greeting already spoken to member]
Hi Maria, this is the Owl Health Care Team calling about your medication refill.

### Previous Observations
- Member prefers morning calls
- Has Type 2 diabetes, prescribed metformin

### Previous Summaries
- [voice, Apr 15] Confirmed PCP appointment. Member mentioned difficulty with evening doses.
```

The agent's system prompt is built from the care team's goal and reason fields, merged with a base persona prompt. The agent knows exactly why it's calling and what the care team needs to know.

---

## The Memory Loop

Each call makes the next one better. Here's how.

The member profile in TAC Memory Store has two sets of traits:

**Contact traits** — name, phone, member ID  
**Outreach traits** — `nextFollowUp`, `nextFollowUpReason`, `status`, `lastCallSummary`

When a call ends, Twilio closes the Maestro conversation, which triggers Conversation Intelligence to process the recording. CI runs a language summary operator over the transcript and fires a webhook to the app server. The app server looks up the member's phone number, queries the TAC Memory Store for their profile ID, and patches the `lastCallSummary` trait:

```
[voice, Apr 20 02:32 PM] Discussed metformin refill. Member confirmed dose schedule.
Follow up in 2 weeks on blood pressure check.
```

The next time the care team pulls up the dashboard, that summary is already there. The next time they — or the member — initiates a call, the agent opens with that context already loaded. No lookup, no scramble.

This is what makes the system compound over time. It's not just automating today's call — it's building a richer record with every interaction that makes every future call faster, more personal, and more effective.

---

## What's Next

The code for this demo is [open source on GitHub](https://github.com/sudheerchekka/healthcare-outreach). It's designed to be a starting point, not a finished product.

A few obvious extensions:

- **Add tools to the Strands agent** — give it access to a scheduling API so it can book the follow-up appointment directly during the call
- **Inbound call handling** — the same architecture works for inbound calls, the agent identifies the member by phone and picks up with full context
- **Escalation routing** — detect when a conversation needs a human and hand off to a live coordinator via Twilio Flex
- **SMS follow-up** — send a post-call summary or appointment confirmation via the same TAC memory context

The care team portal pattern isn't unique to healthcare. Any industry with high-touch, relationship-driven outreach — financial services, insurance, real estate — runs into the same bandwidth problem. The same stack applies.

**Resources:**
- [Twilio Agent Connect documentation](https://www.twilio.com/docs/agent-connect)
- [AWS AgentCore documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
- [Strands Agents SDK](https://github.com/strands-agents/sdk-python)
- [Source code for this demo](https://github.com/sudheerchekka/healthcare-outreach)
