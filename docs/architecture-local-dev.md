# Healthcare Outreach — System Architecture (Local Dev)

```mermaid
flowchart LR
    careTeam(["Care Team"])

    subgraph local["Local Machine"]
        subgraph appServer["App Server · port 8001"]
            dashboard["Dashboard\nmembers.html"]
            apiRoutes["REST API\n/api/members\n/api/outbound-call\n/api/send-sms\n/ci-webhook"]
        end

        subgraph tacServer["TAC Server · port 8000"]
            twiml["TwiML\n/twiml · /twiml-outbound"]
            ws["ConversationRelay WS\n/ws"]
            relayCallback["/conversation-relay-callback"]
        end

        subgraph state["In-process state (Node.js)"]
            ctxBridge["outboundCallerToCallSid\ntwiloFrom → callSid"]
            pendingCtx["pendingOutboundContext\ncallSid → OutboundContext"]
            caches["systemPromptCache\nmemberPhoneCache\nmemoryContextCache"]
        end
    end

    subgraph ngrok["ngrok tunnel"]
        tunnel["https://abc.ngrok-free.app\n→ localhost:8000"]
    end

    subgraph twilio["Twilio Sierra Platform"]
        voice["Voice API"]
        sms["SMS API"]
        relay["ConversationRelay\nTTS + STT"]
        memory["Memory Store\nProfiles · Observations · Summaries"]
        orch["Conversation Orchestrator\nconv_conversation_*"]
        ci["Conversation Intelligence\npost-call summary"]
    end

    subgraph aws["AWS"]
        agentcore["AgentCore Runtime\nStrands Agent (Python)"]
        stm["AgentCore STM\nwithin-call history"]
        bedrock["Claude Opus 4.6"]
    end

    careTeam -- "view members" --> dashboard
    careTeam -- "trigger call / SMS" --> apiRoutes
    apiRoutes -- "read profiles" --> memory
    apiRoutes -- "place call" --> voice
    apiRoutes -- "send SMS" --> sms

    voice -- "fetch TwiML" --> tunnel --> twiml
    twiml -- "welcomeGreeting + WS URL" --> relay
    relay <--> ws

    ws -- "setup: store from→callSid bridge" --> ctxBridge
    ws -- "setup: stash OutboundContext" --> pendingCtx
    ws -- "turn 1: resolve ctx via bridge\ncache system prompt + phone" --> caches
    ws -- "turn 1: enrich context + invoke" --> agentcore
    ws -- "turn 2+: invoke (context in STM)" --> agentcore

    agentcore <--> stm
    agentcore --> bedrock
    bedrock --> agentcore
    agentcore -- "streaming reply" --> ws
    ws -- "text token" --> relay
    relay -- "TTS" --> careTeam

    relay -- "call ends" --> orch
    orch -- "close conversation" --> ci
    ci -- "summary webhook" --> apiRoutes
    apiRoutes -- "write lastCallSummary" --> memory

    classDef person fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e
    classDef appNode fill:#dbeafe,stroke:#3b82f6,color:#1e3a5f
    classDef tacNode fill:#ede9fe,stroke:#7c3aed,color:#3b0764
    classDef stateNode fill:#f5f3ff,stroke:#a78bfa,color:#4c1d95
    classDef tunnelNode fill:#f0fdf4,stroke:#16a34a,color:#14532d
    classDef twilioNode fill:#fce7f3,stroke:#ec4899,color:#831843
    classDef awsNode fill:#fef3c7,stroke:#f59e0b,color:#78350f

    class careTeam person
    class dashboard,apiRoutes appNode
    class twiml,ws,relayCallback tacNode
    class ctxBridge,pendingCtx,caches stateNode
    class tunnel tunnelNode
    class voice,sms,relay,memory,orch,ci twilioNode
    class agentcore,stm,bedrock awsNode
```
