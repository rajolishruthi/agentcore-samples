# Host ADK Agent — Orchestrator

## What It Does

The Host Agent is the **orchestrator** of the multi-agent system. It receives user queries and delegates them to the right specialist agent. It never answers AWS questions itself — it acts purely as a router and synthesizer.

- Built with **Google ADK** (Agent Development Kit)
- Runs on **AWS Bedrock AgentCore Runtime**
- Uses **Claude Sonnet 4** on Amazon Bedrock via LiteLLM (default: `global.anthropic.claude-sonnet-4-20250514-v1:0`)
- Communicates with sub-agents via the **A2A protocol** (Agent-to-Agent)
- Sends emails on behalf of users via **Gmail 3LO** (Authorization Code Grant with AgentCore Identity)

## Architecture Position

```
User / Frontend
      │
      ▼
┌─────────────────────────────────┐
│  Host ADK Agent (Orchestrator)  │  ← AgentCore Runtime (us-west-2)
│  Google ADK + Gemini/Bedrock    │
│                                 │
│  Sub-agents:                    │
│  ├── monitor_agent  ──A2A──►  AWS AgentCore Runtime
│  └── websearch_agent ──A2A──► GCP Cloud Run
└─────────────────────────────────┘
```

## Code Flow

### 1. Entry Point: `main.py`

```
Request arrives at AgentCore Runtime
  → @app.entrypoint (call_agent)
    → Extracts session_id from context
    → Extracts actor_id from request headers
    → On first request: calls get_agent_and_card() to initialize root agent
    → Yields agent cards info (sub-agent metadata)
    → Creates/retrieves InMemorySession for the user
    → Creates a Runner and streams responses via runner.run_async()
```

Key detail: The agent is initialized **lazily** on the first request, inside the entrypoint. This is because AgentCore Identity (workload identity) is only available inside the entrypoint context.

### 2. Agent Setup: `agent.py`

```
get_root_agent(session_id, actor_id)
  │
  ├── Build monitor_agent_card_url
  │   URL: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded-ARN}/invocations/.well-known/agent-card.json
  │   → RemoteA2aAgent("monitor_agent", agent_card=url, a2a_client_factory=...)
  │
  ├── Build websearch_agent_card_url
  │   URL: {WEBSEARCH_GCP_URL}/.well-known/agent-card.json
  │   → RemoteA2aAgent("websearch_agent", agent_card=url, a2a_client_factory=...)
  │
  └── Create root Agent(model, instruction=SYSTEM_PROMPT, sub_agents=[monitor, websearch])
```

### 3. OBO Token Exchange: `obo_token.py`

Before creating A2A clients, the host agent exchanges the inbound user JWT for an OBO-enriched M2M token:

```
main.py: extracts "Authorization: Bearer <user JWT>" from request headers
  → agent.py: _OBOAuth(httpx.Auth) attaches per-request via httpx auth=
    → obo_token.OBOTokenExchanger.fetch(user_jwt)
      → POST Cognito /oauth2/token with:
          grant_type=client_credentials
          aws_client_metadata={"onBehalfOfToken": <user JWT>, "callerApp": "host-agent"}
      → Pre-Token-Gen v3 Lambda fires, copies user identity into onBehalfOf claim
      → Returns M2M token carrying: sub, email, role of the original user
      → Cached per user JWT (single-flight for concurrent requests)
```

The result: downstream agents receive a token they can decode to learn *who* the action is on behalf of — without the user JWT itself ever leaving the host agent.

### 4. Authentication (Zero-Trust): `_create_client_factory()`

This is where **AgentCore Identity's zero-trust model** comes to life. Each sub-agent gets its own `LazyClientFactory` that:

1. Uses `_OBOAuth(httpx.Auth)` — fetches an OBO-exchanged Cognito token per request
2. Creates an `httpx.AsyncClient` with the OBO Bearer token in headers
3. Also passes `session_id` and `actor_id` as custom headers

The same token-based auth pattern works for both sub-agents regardless of where they run:
- **Monitor agent (AWS)**: AgentCore Runtime validates the token automatically via `CustomJWTAuthorizer`
- **Web search agent (GCP)**: `auth_middleware.py` validates the same Cognito token against JWKS public keys ( Verifies the token's signature, expiration, and scopes)

This is zero-trust in action — no VPC peering, no shared secrets, no network-level trust between clouds. Just cryptographically verifiable identity tokens.

The factory creates **fresh httpx clients on each A2A call** to avoid event loop issues. This is critical because the agent card resolution and actual A2A invocations may happen in different async contexts.

### 5. Routing Logic: `prompt/__init__.py`

The system prompt defines strict delegation rules:

| Query Type | Delegated To |
|---|---|
| CloudWatch, logs, metrics, alarms, monitoring, AWS resources | `monitor_agent` |
| Questions about previous sessions or past investigations | `monitor_agent` (has memory) |
| AWS troubleshooting guides, documentation, solutions, "search for", "look up" | `websearch_agent` |
| "Save to memory", "remember this", "save that for later" | `websearch_agent` (owns memory tools) |
| Error messages and resolution steps | `websearch_agent` |

For troubleshooting requests, the orchestration strategy is:
1. First → `monitor_agent` to gather current metrics/logs
2. Then → `websearch_agent` with specific context to find solutions
3. Finally → Synthesize findings into actionable steps

## Tools

| Tool | Purpose |
|---|---|
| `send_email_to_user` | Send findings/recommendations via Gmail (3LO — user consent required on first use) |

The email tool uses `@requires_access_token(auth_flow="USER_FEDERATION")` with AgentCore Identity. First call returns a consent URL; after user authorizes, subsequent calls send emails directly using the cached token from the Token Vault.

## Memory

None. The host agent uses `InMemorySessionService` from Google ADK for session state within a single runtime lifecycle, but has no persistent memory. The sub-agents handle their own memory.

## Configuration

| Env Variable | Purpose |
|---|---|
| `GOOGLE_MODEL_ID` | Gemini model ID (default: `gemini-2.5-flash`) |
| `BEDROCK_MODEL_ID` | Optional Bedrock model via LiteLlm |
| `GOOGLE_API_KEY` | Google API key for Gemini |
| `WEBSEARCH_GCP_URL` | GCP Cloud Run URL for web search agent |

SSM Parameters:
- `/monitoragent/agentcore/runtime-id` — Monitor agent's runtime ID
- `/monitoragent/agentcore/provider-name` — Cognito credential provider for monitor agent
- `/websearchagent/agentcore/provider-name` — Cognito credential provider for web search agent

## Deployment

- **Runtime**: AWS Bedrock AgentCore Runtime
- **Container**: Docker (Python 3.13, uv, OpenTelemetry instrumented)
- **CloudFormation**: `cloudformation/host_agent.yaml`

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Entry point, BedrockAgentCoreApp, extracts user JWT for OBO |
| `agent.py` | Root agent creation, sub-agent wiring, OBO A2A client factory |
| `obo_token.py` | OBO token exchanger — Cognito client_credentials + onBehalfOfToken |
| `email_tool.py` | Gmail 3LO send-email tool using AgentCore Identity USER_FEDERATION |
| `oauth2_callback_server.py` | Local callback server for Gmail consent; `/oauth2/status` for frontend polling |
| `prompt/__init__.py` | System prompt with delegation rules |
| `utils.py` | SSM parameter retrieval, AWS account/region info |
| `Dockerfile` | Container build with OpenTelemetry |
