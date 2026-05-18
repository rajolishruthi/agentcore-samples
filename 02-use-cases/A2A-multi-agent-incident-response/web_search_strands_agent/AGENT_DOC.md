# Web Search Agent — AWS Troubleshooting Specialist

## What It Does

The Web Search Agent finds AWS documentation, troubleshooting guides, and solutions by searching the web. It's the "knowledge" arm of the incident response system — when the monitoring agent finds a problem, this agent finds the fix.

- Built with **Strands Agents SDK**
- Runs on **GCP Cloud Run** (cross-cloud deployment)
- Uses **Gemini 2.5 Flash** via LiteLLM (default: `gemini/gemini-2.5-flash`)
- Searches the web via **Tavily API**
- Has **memory tools** via AgentCore Memory (cross-cloud call back to AWS)
- Validates incoming JWTs via **AgentCore Identity's `GetWorkloadAccessTokenForJWT`** (not direct JWKS validation)

## Architecture Position

```
Host ADK Agent (AWS)
      │ A2A (Cognito M2M Bearer token)
      ▼
┌──────────────────────────────────────┐
│  Web Search Agent                    │  ← GCP Cloud Run
│  Strands SDK + Claude on Bedrock     │
│                                      │
│  Tools:                              │
│  ├── web_search (Tavily API)         │
│  ├── retrieve_monitoring_context     │
│  ├── save_interaction_to_memory      │
│  ├── get_recent_conversation_history │
│  ├── save_custom_memory              │
│  └── search_memory_by_namespace      │
│                                      │
│  Auth: Cognito JWT middleware        │
│  Memory: AgentCore Memory (AWS)      │
│  LLM: Bedrock (AWS, cross-cloud)    │
└──────────────────────────────────────┘
```

## Code Flow

### 1. Entry Point: `main.py`

```
A2A request arrives at GCP Cloud Run
  → CognitoAuthMiddleware validates JWT (skips for /ping, /.well-known/agent-card.json)
  → A2AStarletteApplication serves /.well-known/agent-card.json
  → DefaultRequestHandler routes to WebSearchAgentExecutor
  → /ping endpoint for health checks
```

The `AgentCard` defines 2 skills:
- `websearch` — General web search for AWS documentation and solutions
- `aws-documentation` — Targeted AWS documentation and best practices search

### 2. Authentication: `auth_middleware.py`

Since this agent runs on GCP (not AgentCore Runtime), it handles its own inbound auth:

```
CognitoAuthMiddleware.dispatch(request)
  │
  ├── Skip auth for: /ping, /health, /.well-known/agent-card.json
  │
  ├── Extract Bearer token from Authorization header
  │
  └── validate_cognito_token(token)
      ├── Fetch JWKS from Cognito: https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json
      ├── Get signing key for the JWT
      ├── Decode and verify: RS256, issuer check, expiry check
      └── Return claims (or 401 if invalid)
```

### 3. Request Execution: `agent_executor.py`

1. WHO is asking?
   → Read session_id and actor_id from headers

2. Is this a valid request?
   → If session_id or actor_id missing → reject

3. Create the agent (first time only)
   → Set up Strands agent with Tavily + memory tools

4. Create a task to track the work
   → A2A protocol uses "tasks" to track request → response

5. Do the actual work
   → Send the user's question to the Strands agent
   → Stream the response back chunk by chunk
   → Mark the task as complete

event_queue is how the agent sends responses back — it pushes status updates and the final answer onto this queue, which the A2A server streams back to the caller.
context -- context parameter carries the incoming A2A message (the user's question and metadata).
```
WebSearchAgentExecutor.execute(context, event_queue)
  │ 
  ├── Extract from headers:
  │   ├── session_id (x-amzn-bedrock-agentcore-runtime-session-id)
  │   └── actor_id (x-amzn-bedrock-agentcore-runtime-custom-actorid)
  │   (No workload_token — this agent uses AWS credentials directly)
  │
  ├── Validate session_id and actor_id are present (  it's just checking that required headers are present. That's it. It's a simple null check — "did the caller send these headers?" If not, reject with InvalidParamsError.)
  │
  ├── Lazily create WebSearchAgent (first request only)
  │
  ├── Create/get A2A Task
  │
  └── _execute_streaming(agent, message, updater, task_id, session_id)
      ├── Stream chunks from agent.stream()
      ├── Update task status (TaskState.working) with accumulated text
      ├── Add final artifact on completion
      └── updater.complete()
```

Request arrives
    │
    ▼
auth_middleware.py          ← SECURITY check (is the JWT valid?)
    │ Valid token?
    ├── No → 401 Unauthorized (request never reaches executor)
    ├── Yes ▼
    │
agent_executor.py           ← PARAMETER check (are required fields present?)
    │ session_id and actor_id present?
    ├── No → InvalidParamsError (valid caller, but bad request)
    ├── Yes ▼
    │
    Do the actual work


### 4. Agent Creation: `agent.py`

```
WebSearchAgent.__init__(memory_id, model_id, region_name, actor_id, session_id)
  │
  ├── Create BedrockModel(model_id, region_name)
  │   (Cross-cloud: GCP → AWS Bedrock via AWS credentials)
  │
  ├── Build tool list:
  │   ├── web_search (Tavily tool from tools.py)
  │   └── get_memory_tools(memory_id, actor_id, session_id)
  │       └── create_memory_tools() → 5 memory tools
  │
  └── Create Strands Agent(
        model=bedrock_model,
        tools=[web_search] + memory_tools,
        system_prompt=SYSTEM_PROMPT
      )
```

### 5. Web Search Tool: `tools.py`

```
@tool
web_search(query, top_k=5, recency_days=0)
  │
  ├── Create TavilyClient(api_key=TAVILY_API_KEY)
  │
  ├── Map recency_days to time_range:
  │   ├── ≤1 day → "day"
  │   ├── ≤7 days → "week"
  │   ├── ≤30 days → "month"
  │   └── >30 days → "year"
  │
  └── tavily.search(query, max_results=min(top_k, 10))
      → Returns: [{title, url, snippet, score}, ...]
```

## Memory Architecture

Memory is implemented as **explicit tools** (not hooks like the monitoring agent). The agent decides when to use memory.

### Memory Tools: `memory_tools.py`

All 5 tools are created via `AgentMemoryTools.create_tools()`:

| Tool | Purpose |
|---|---|
| `retrieve_monitoring_context` | Semantic search across all memory namespaces |
| `save_interaction_to_memory` | Save user query + agent response as a conversation turn |
| `get_recent_conversation_history` | Get last K conversation turns |
| `save_custom_memory` | Save arbitrary content to memory |
| `search_memory_by_namespace` | Search a specific namespace (e.g., search-queries, knowledge) |

```
Memory Flow (tool-driven, not automatic):
  Agent decides → retrieve_monitoring_context("EC2 high CPU")
    → Searches all namespaces: /technical-issues/{actorId}, /knowledge/{actorId}, etc.
    → Returns matching memories as context

  Agent decides → save_interaction_to_memory(user_msg, response)
    → client.create_event(memory_id, actor_id, session_id, messages)

  Agent decides → search_memory_by_namespace("CPU issues", "SemanticMemory")
    → Searches specific namespace only
```

Key difference from the monitoring agent: Memory here is **tool-based** (agent explicitly calls tools), while the monitoring agent uses **hooks** (automatic on every message).

## Tools Summary

| Tool | Source | Purpose |
|---|---|---|
| `web_search` | Tavily API | Search web for AWS docs and solutions |
| `retrieve_monitoring_context` | AgentCore Memory | Semantic search across memory |
| `save_interaction_to_memory` | AgentCore Memory | Save conversation turns |
| `get_recent_conversation_history` | AgentCore Memory | Get recent turns |
| `save_custom_memory` | AgentCore Memory | Save arbitrary content |
| `search_memory_by_namespace` | AgentCore Memory | Search specific namespace |

## Configuration

| Env Variable | Purpose |
|---|---|
| `MODEL_ID` | Bedrock model ID (default: Claude Sonnet) |
| `MEMORY_ID` | AgentCore Memory ID |
| `MCP_REGION` | AWS region for Bedrock and Memory |
| `TAVILY_API_KEY` | Tavily API key for web search |
| `COGNITO_REGION` | Cognito region for JWT validation |
| `COGNITO_USER_POOL_ID` | Cognito User Pool ID for JWT validation |
| `SERVICE_URL` | Cloud Run service URL (for agent card) |
| `PORT` | Server port (default: 9000, Cloud Run uses 8080) |
| `AWS_ACCESS_KEY_ID` | AWS credentials for cross-cloud access |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials for cross-cloud access |

## Deployment

- **Runtime**: GCP Cloud Run (not AgentCore Runtime)
- **Container**: Docker (Python 3.13, uv)
- **Auth**: Self-managed Cognito JWT validation middleware
- **Cross-cloud**: Calls AWS Bedrock for LLM, AWS AgentCore Memory for persistence

## Zero-Trust Identity (Cross-Cloud)

The web search agent demonstrates that AgentCore Identity's trust model works **across cloud boundaries** without AgentCore Runtime.

### The Challenge

This agent runs on **GCP Cloud Run** — outside the AgentCore ecosystem. There's no AgentCore Runtime to automatically validate incoming tokens. Yet it must securely accept requests from the host agent on AWS.

### The Solution: `GetWorkloadAccessTokenForJWT`

Instead of manually validating JWTs with JWKS (which requires ~110 lines of custom crypto code), the agent calls AgentCore Identity's `GetWorkloadAccessTokenForJWT` API. This single call:
1. Validates the incoming JWT (signature, expiry, issuer)
2. Returns a workload access token (for potential outbound calls)

```python
# agentcore_identity_auth.py — single API call replaces manual JWKS validation
response = client.get_workload_access_token_for_jwt(
    workloadName="web-search-agent",
    userToken=bearer_token,  # The Cognito M2M JWT from the host agent
)
# If this succeeds → token is valid, request proceeds
# If this fails → 401 Unauthorized
```

### Why This Works

- **Same trust anchor** — Both AWS and GCP agents rely on the same Cognito User Pool's JWKS keys
- **No custom crypto** — AgentCore Identity handles JWKS fetching, key rotation, signature verification
- **Equivalent to Runtime** — Same validation that `customJWTAuthorizer` does on Runtime, but called as an API
- **Single API call** — Validates the token AND returns a workload access token in one call

## Key Differences from Monitoring Agent

| Aspect | Monitoring Agent | Web Search Agent |
|---|---|---|
| **Runs on** | AWS AgentCore Runtime | GCP Cloud Run |
| **Inbound auth** | AgentCore Runtime (automatic) | Cognito JWT middleware (self-managed) |
| **Trust model** | Same (Cognito JWKS) | Same (Cognito JWKS) |
| **Tools source** | MCP Gateway (dynamic) | Hardcoded (Tavily + memory tools) |
| **Memory approach** | Hooks (automatic on every message) | Tools (agent decides when to use) |
| **Workload token** | Yes (for gateway auth) | No (uses AWS credentials directly) |
| **Zero-trust proof** | AgentCore handles it transparently | Proves the model works without AgentCore infra |

## Key Files

| File | Purpose |
|---|---|
| `main.py` | A2A server setup, agent card, Cognito middleware, health endpoint |
| `agent_executor.py` | A2A request handling, streaming, task management |
| `agent.py` | Strands agent creation with Tavily + memory tools |
| `tools.py` | `web_search` tool (Tavily) + memory tool factory |
| `memory_tools.py` | 5 memory tools: retrieve, save, history, custom, namespace search |
| `auth_middleware.py` | Cognito JWT validation (replaces AgentCore Runtime inbound auth) |
| `prompt/__init__.py` | System prompt — troubleshooting specialist with memory guidelines |
| `Dockerfile` | Container build for GCP Cloud Run |
