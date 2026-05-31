# Monitoring Agent — CloudWatch Specialist

## What It Does

The Monitoring Agent is a **CloudWatch specialist** that queries AWS logs, metrics, and alarms. It's the "eyes" of the incident response system — it gathers real-time observability data from your AWS infrastructure.

- Built with **Strands Agents SDK**
- Runs on **AWS Bedrock AgentCore Runtime**
- Uses **Claude Sonnet** on Amazon Bedrock (default: `global.anthropic.claude-sonnet-4-20250514-v1:0`)
- Accesses CloudWatch via **MCP Gateway** (AgentCore Gateway)
- Has **long-term and short-term memory** via AgentCore Memory

## Architecture Position

```
Host ADK Agent
      │ A2A (Bearer token)
      ▼
┌──────────────────────────────────────┐
│  Monitoring Agent                    │  ← AgentCore Runtime (us-west-2)
│  Strands SDK + Claude on Bedrock     │
│                                      │
│  Tools (via MCP Gateway):            │
│  ├── DescribeLogGroups               │
│  ├── DescribeLogStreams              │
│  ├── FilterLogEvents                 │
│  └── GetLogEvents                    │
│                                      │
│  Memory (AgentCore Memory):          │
│  ├── Short-term: conversation turns  │
│  └── Long-term: semantic search      │
└──────────────────────────────────────┘
```

## Code Flow

### 1. Entry Point: `main.py`

```
A2A request arrives at AgentCore Runtime
  → A2AStarletteApplication serves /.well-known/agent-card.json
  → DefaultRequestHandler routes to MonitoringAgentExecutor
  → /ping endpoint for health checks
```

The `AgentCard` is defined in `main.py` with 5 skills:
- `x_amz_bedrock_agentcore_search` — Tool discovery (AgentCore built-in)
- `DescribeLogGroups` — List CloudWatch log groups
- `DescribeLogStreams` — List log streams in a group
- `FilterLogEvents` — Search/filter log events
- `GetLogEvents` — Retrieve specific log entries

### 2. Request Execution: `agent_executor.py`

```
MonitoringAgentExecutor.execute(context, event_queue)
  │
  ├── Extract from headers:
  │   ├── session_id (x-amzn-bedrock-agentcore-runtime-session-id)
  │   ├── actor_id (x-amzn-bedrock-agentcore-runtime-custom-actorid)
  │   ├── workload_token (x-amzn-bedrock-agentcore-runtime-workload-accesstoken)
  │   └── bearer_token (authorization) — carries OBO claim
  │
  ├── Decode OBO identity: obo_claims.decode_user_identity(bearer_token)
  │   └── Returns {email, sub, role} or None if no onBehalfOf claim
  │
  ├── Apply role-based filter to user_message:
  │   ├── admin  → no filter (pass through)
  │   ├── manager → prepend "[ROLE: manager] Only return /aws/lambda/ and /aws/apigateway/ results"
  │   ├── analyst → prepend "[ROLE: analyst] Only return /aws/lambda/ results"
  │   └── viewer  → replace message with "inform user their role does not permit access"
  │
  ├── Validate session_id, actor_id, workload_token present (else InvalidParamsError)
  │
  ├── Lazily create MonitoringAgent (first request only)
  │
  ├── Create/get A2A Task
  │
  └── _execute_streaming(agent, message, updater, task_id, session_id)
      ├── Stream chunks from agent.stream()
      ├── Update task status (TaskState.working) with accumulated text
      ├── Add final artifact on completion
      └── updater.complete()
```

The `workload_token` is unique to this agent — it's the AgentCore Runtime workload identity token used to authenticate with the MCP Gateway.

### 3. Agent Creation: `agent.py`

```
MonitoringAgent.__init__(memory_id, model_id, region_name, actor_id, session_id, workload_token)
  │
  ├── Create BedrockModel(model_id, region_name)
  │
  ├── Create MemoryClient(region_name)
  │
  ├── Create MonitoringMemoryHooks(memory_id, client, actor_id, session_id)
  │
  ├── Create MCP Gateway client:
  │   └── create_gateway_client(workload_token)
  │       ├── Get OAuth2 token via agentcore_client.get_resource_oauth2_token()
  │       ├── Get gateway URL from SSM (/monitoragent/agentcore/gateway/gateway_url)
  │       └── Return MCPClient with streamable HTTP transport + Bearer auth
  │
  ├── gateway_client.start() → Connect to MCP Gateway
  ├── gateway_tools = gateway_client.list_tools_sync() → Get CloudWatch tools
  │
  └── Create Strands Agent(
        model=bedrock_model,
        tools=gateway_tools,      ← CloudWatch tools from MCP Gateway
        hooks=[monitoring_hooks],  ← Memory hooks
        system_prompt=SYSTEM_PROMPT
      )
```

### 4. MCP Gateway Connection: `utils.py`

The monitoring agent accesses CloudWatch through the **AgentCore MCP Gateway**, not directly:

```
create_gateway_client(workload_token)
  │
  ├── agentcore_client.get_resource_oauth2_token(
  │     workloadIdentityToken=workload_token,
  │     resourceCredentialProviderName=GATEWAY_PROVIDER_NAME,
  │     oauth2Flow="M2M"
  │   )
  │   → Returns gateway_access_token
  │
  ├── SSM: /monitoragent/agentcore/gateway/gateway_url
  │   → Returns gateway_url
  │
  └── MCPClient(streamablehttp_client(url=gateway_url, headers={Authorization: Bearer ...}))
```

This means the CloudWatch tools (DescribeLogGroups, etc.) are defined in the MCP Gateway configuration, not in this agent's code. The agent discovers them dynamically via `list_tools_sync()`.

## Memory Architecture

Memory is implemented via **Strands hooks** in `memory_hook.py`. Three hooks fire at different lifecycle points:

### Hook 1: `on_agent_initialized` (AgentInitializedEvent)
**When**: Agent starts up
**What**: Loads the last 5 conversation turns from short-term memory and appends them to the system prompt as `<recent-conversation>` context.

### Hook 2: `retrieve_monitoring_context` (MessageAddedEvent)
**When**: Every time a user message is added
**What**: Searches long-term memory across two namespaces:
- `/technical-issues/{actorId}` — CustomMemoryStrategy (top_k=3, min score 0.3)
- `/knowledge/{actorId}` — SemanticMemoryStrategy (top_k=5, min score 0.2)

Injects matching memories into the system prompt as `<memory-context>`.

### Hook 3: `save_monitoring_interaction` (AfterInvocationEvent)
**When**: After the agent completes a response
**What**: Saves the user query + agent response as a conversational turn to short-term memory via `create_event()`.

```
Memory Flow:
  Agent Init → Load last 5 turns into system prompt
  User Message → Search long-term memory → Inject into system prompt
  Agent Response → Save interaction to short-term memory
                   (Long-term extraction happens automatically via CloudFormation-defined strategies)
```

## Tools

All tools come from the **MCP Gateway** (not hardcoded):

| Tool | Description |
|---|---|
| `DescribeLogGroups` | List CloudWatch log groups, filter by prefix |
| `DescribeLogStreams` | List log streams within a log group |
| `FilterLogEvents` | Search log events with filter patterns |
| `GetLogEvents` | Retrieve events from a specific log stream |
| `x_amz_bedrock_agentcore_search` | AgentCore built-in tool discovery |

## Configuration

| Env Variable | Purpose |
|---|---|
| `MODEL_ID` | Bedrock model ID (default: Claude Sonnet) |
| `MEMORY_ID` | AgentCore Memory ID |
| `MCP_REGION` | AWS region for Bedrock and Memory |
| `GATEWAY_PROVIDER_NAME` | Credential provider for MCP Gateway auth |
| `AGENTCORE_RUNTIME_URL` | Runtime URL for agent card |

SSM Parameters:
- `/monitoragent/agentcore/gateway/gateway_url` — MCP Gateway endpoint

## Zero-Trust Identity

The monitoring agent demonstrates **two layers of AgentCore Identity's zero-trust model**:

### Inbound: Automatic JWT Validation

AgentCore Runtime validates every incoming request before the agent code runs. Configured in CloudFormation:

```yaml
AuthorizerConfiguration:
  CustomJWTAuthorizer:
    DiscoveryUrl: <Cognito OIDC discovery URL>
    AllowedClients:
      - <Cognito MonitoringClientId>
      - <Cognito AgentOBOClientId>   # OBO-exchanged tokens must also be accepted
RequestHeaderConfiguration:
  RequestHeaderAllowlist:
    - X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actorid
    - Authorization   # Required so the OBO bearer token reaches agent_executor.py
```

This means:
- The agent code never sees an unauthenticated request
- Both the standard M2M client and the OBO client are accepted
- The `Authorization` header must be in the allowlist — without it, `agent_executor.py` cannot read the OBO bearer token to decode the `onBehalfOf` claim and apply role-based filtering
- JWT signature, expiry, and issuer are all verified at the infrastructure layer

### Outbound: Workload Identity for Tool Access

The monitoring agent uses its **workload identity token** (injected by AgentCore Runtime) to obtain OAuth2 credentials for the MCP Gateway. No static credentials are stored in code or environment variables:

```python
# utils.py — workload identity → gateway token
response = agentcore_client.get_resource_oauth2_token(
    workloadIdentityToken=workload_token,  # injected by Runtime
    resourceCredentialProviderName=GATEWAY_PROVIDER_NAME,
    oauth2Flow="M2M"
)
```

This is zero-trust end-to-end: the host agent proves its identity to reach this agent, and this agent proves its identity to reach CloudWatch tools.

## Deployment

- **Runtime**: AWS Bedrock AgentCore Runtime
- **Container**: Docker (Python 3.13, uv, OpenTelemetry instrumented)
- **CloudFormation**: `cloudformation/monitoring_agent.yaml`
- **Inbound Auth**: Handled automatically by AgentCore Runtime (Cognito JWT)

## Key Files

| File | Purpose |
|---|---|
| `main.py` | A2A server setup, agent card definition, health endpoint |
| `agent_executor.py` | A2A request handling, OBO role filtering, streaming, task management |
| `agent.py` | Strands agent creation, MCP gateway connection |
| `obo_claims.py` | Decode `onBehalfOf` JWT claim; role map; log group prefix filter |
| `memory_hook.py` | Memory hooks: load context, retrieve long-term, save interactions |
| `utils.py` | MCP gateway client creation with OAuth2 auth |
| `prompt/__init__.py` | System prompt — CloudWatch specialist with memory guidelines |
| `Dockerfile` | Container build with OpenTelemetry |
