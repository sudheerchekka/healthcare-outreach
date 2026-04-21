graph LR
    subgraph CareTeam["Care Team"]
        AGENT_USER["Care Team Member\n(Browser)"]
    end

    subgraph Member["Member"]
        PHONE_USER["Member\n(Phone)"]
    end

    subgraph App["Owl Health App (Local / EC2)"]
        direction TB
        NODE["Node.js App Server\nPort 8001\nDashboard · APIs · CI webhook"]
        TAC["Python TAC Server\nPort 8000\nTwiML · ConversationRelay · Memory"]
        NODE <-->|IPC\noutbound ctx · phone lookup| TAC
    end

    subgraph TwilioCloud["Twilio"]
        direction TB
        CR["ConversationRelay\nVoice Bridge"]
        MEMSTORE["TAC Memory Store\nMember Profiles\nObservations · Summaries"]
        CI_SVC["Conversation Intelligence\nPost-call Summarization"]
    end

    subgraph AWSCloud["AWS"]
        direction TB
        AC["AgentCore Runtime\nStrands Agent\nPersistent WS per call"]
        BR["Amazon Bedrock\nClaude Opus 4"]
        AC <-->|LLM inference| BR
    end

    AGENT_USER <-->|HTTPS| NODE
    PHONE_USER <-->|PSTN| CR

    NODE <-->|Twilio REST API\ncalls · SMS · profiles| TwilioCloud
    TAC <-->|TwiML webhooks\nConversationRelay WS| CR
    TAC <-->|member profiles\nobservations · summaries| MEMSTORE

    TAC <-->|presigned WebSocket\ntoken streaming| AC

    CI_SVC -->|post-call summary\n→ /ci-webhook| NODE
    NODE -->|write summary\nto member profile| MEMSTORE
    CR -->|call audio| CI_SVC