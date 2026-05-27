# A2A Multi-Agent Incident Response — Demo Guide

## Elevator Pitch (30 seconds)

This is a multi-agent system for AWS incident response that runs across two clouds. Three specialized agents coordinate using the A2A protocol — a host orchestrator on Google ADK, a CloudWatch monitoring agent on Strands, and a web search agent on GCP Cloud Run. All secured by AgentCore Identity with zero-trust authentication — no VPCs, no shared secrets, just cryptographically verifiable identity tokens across cloud boundaries.

---

## Key Talking Points

### 1. Multi-Agent Coordination via A2A Protocol

- Three agents, three different frameworks (Google ADK, Strands SDK x2), communicating via the open A2A standard
- The host agent is a pure orchestrator — it never answers AWS questions itself, it delegates to specialists
- Each agent has its own runtime, its own tools, its own memory — loosely coupled, independently deployable

### 2. Zero-Trust Identity Across Clouds

- **This is the headline feature.** AgentCore Identity provides the trust fabric
- Cognito is the single identity provider — tokens are verifiable on AWS and GCP alike
- On AWS: AgentCore Runtime validates JWTs automatically (infrastructure-level, zero code)
- On GCP: Same Cognito tokens validated via standard OIDC (auth_middleware.py)
- No VPC peering between clouds. No IP whitelisting. The network topology is irrelevant — the identity is everything

### 3. AgentCore Primitives Working Together

- **Identity**: Inbound JWT auth + outbound token vaulting via `@requires_access_token`
- **Memory**: Short-term conversation turns + long-term semantic search across sessions
- **Gateway**: CloudWatch tools exposed via MCP Gateway — agent discovers tools dynamically
- **Runtime**: Containerized agents with auto-scaling, observability, and A2A protocol support
- **Registry**: Agents registered with agent cards for discovery via semantic search

### 4. Cross-Cloud Architecture

- Host Agent + Monitoring Agent: AWS AgentCore Runtime (us-west-2)
- Web Search Agent: GCP Cloud Run (us-central1)
- Memory + LLM: Both GCP and AWS agents call back to AWS Bedrock and AgentCore Memory
- Single Cognito User Pool as the trust anchor across both clouds

---

## Demo Flow

### Pre-Demo Setup

```bash
# Terminal 1: Start the frontend
cd frontend
npm run dev
# Opens at http://localhost:5173 (or 5174)
```

Have these ready in browser tabs:
- Frontend chat UI
- AWS Console → AgentCore Runtime (show the 3 runtimes)
- AWS Console → AgentCore Registry (show registered agents)
- (Optional) AWS Console → CloudWatch Logs (to show real logs being queried)

---

### Demo Script

#### Scene 1: Introduction (2 min)

**Show the architecture diagram** (`images/architecture.png`)

> "We have three agents working together for incident response. A host orchestrator built with Google ADK decides who to delegate to. A monitoring agent built with Strands SDK queries CloudWatch logs and metrics. And a web search agent — also Strands — that finds AWS documentation and troubleshooting guides."
>
> "The interesting part: the web search agent runs on GCP Cloud Run, not AWS. All three agents communicate using the A2A protocol, and they're all secured by AgentCore Identity using Cognito as the trust anchor."

#### Scene 2: Simple Monitoring Query (3 min)

**Type in the chat UI:**

> "Show me the log groups in my account"

**What happens behind the scenes (explain while waiting):**
1. Your message goes to the Host Agent (Google ADK on AgentCore Runtime)
2. Host Agent recognizes this is a monitoring question → delegates to Monitor Agent via A2A
3. Monitor Agent authenticates to MCP Gateway using its workload identity token
4. Gateway calls CloudWatch DescribeLogGroups
5. Results stream back through the A2A chain

**Talking point:** "Notice the host agent didn't try to answer this itself. The system prompt enforces strict delegation — it's a router, not an answerer. The monitoring agent has the CloudWatch tools via MCP Gateway, and it discovered those tools dynamically at startup."

#### Scene 3: Cross-Cloud Web Search (3 min)

**Type in the chat UI:**

> "Find best practices for CloudWatch alarm configuration"

**What happens:**
1. Host Agent delegates to Web Search Agent via A2A
2. AgentCore Identity fetches a Cognito M2M token from its vault (`@requires_access_token`)
3. Token is sent as Bearer header to GCP Cloud Run
4. GCP middleware validates the JWT against Cognito's JWKS public keys
5. Web Search Agent calls Tavily API, returns results

**Talking point:** "This request just crossed cloud boundaries. The host agent on AWS called an agent on GCP Cloud Run. No VPC peering, no shared secrets. AgentCore Identity fetched a Cognito token, and the GCP agent validated it using standard OIDC. Same trust model, different infrastructure."

#### Scene 4: Multi-Agent Orchestration (4 min)

**Type in the chat UI:**

> "I'm seeing errors in my Lambda functions. Can you investigate and find solutions?"

**What happens (the full orchestration):**
1. Host Agent recognizes this needs BOTH agents
2. First → Monitor Agent: "Check Lambda log groups for recent errors"
3. Monitor Agent queries CloudWatch, finds error patterns
4. Then → Web Search Agent: "Find solutions for [specific errors found]"
5. Web Search Agent searches for troubleshooting guides
6. Host Agent synthesizes: "Here's what I found in your logs + here's how to fix it"

**Talking point:** "This is the power of multi-agent orchestration. The host agent gathered real data from CloudWatch, then used that specific context to search for solutions. It's not just routing — it's synthesizing insights from multiple specialists."

#### Scene 5: Memory in Action (2 min)

**Type in the chat UI (same session):**

> "What did we just find about the Lambda errors?"

**What happens:**
1. Host Agent delegates to Monitor Agent (it has memory)
2. Monitor Agent's memory hooks load the previous conversation context
3. It references the earlier findings without re-querying CloudWatch

**Talking point:** "The monitoring agent remembers our previous investigation. It uses AgentCore Memory with two layers — short-term conversation turns that load automatically via hooks, and long-term semantic memory that persists across sessions. If you come back tomorrow and ask about this incident, it'll still have context."

#### Scene 6: Delegated User Identity / OBO (3 min)

**Log in as alice@demo.com**, then type:

> "Show me the log groups in my account"

**Then log out, log in as charlie@demo.com**, ask the same question. Then again as the default `viewer`.

**What to point at in the UI:**
- Each agent response carries a small **"On behalf of: alice@demo.com (admin)"** badge near the avatar
- Alice (admin) sees every log group; Charlie (analyst) sees only `/aws/lambda/*`; viewer sees none
- The badge comes from the agent itself — it's an audit assertion, not a frontend guess

**What happens behind the scenes:**
1. Frontend sends the user's Cognito JWT as Bearer to the host agent
2. Host agent extracts the JWT and calls Cognito `/oauth2/token` with `aws_client_metadata.onBehalfOfToken=<user JWT>`
3. A Pre-Token-Gen v3 Lambda fires inside Cognito, decodes the user JWT from the metadata, and stamps an `onBehalfOf` claim onto the agent's M2M access token
4. Host uses that token as Bearer for the A2A call to monitoring agent
5. Monitoring agent decodes `onBehalfOf` from its inbound bearer, applies the role map (`alice→admin`, `bob→manager`, `charlie→analyst`, default→`viewer`), and filters CloudWatch results
6. Audit assertion comes back in the artifact as `<!--AUDIT:{...}-->`, which the frontend strips and renders as the badge

**Talking point:** "Multi-agent systems usually lose user identity at the M2M boundary. The downstream agent sees a workload token and audit logs say 'the agent did it.' We solved that with a Cognito-native OBO flow. AgentCore Identity has an `ON_BEHALF_OF_TOKEN_EXCHANGE` API, but it requires RFC 8693 token exchange — which Cognito doesn't speak. Instead, we use Cognito's Pre-Token-Generation v3 trigger plus `aws_client_metadata` to copy user identity into a custom claim on the agent's M2M token. The user JWT never leaves the host agent. Downstream agents read the claim and apply role-based policy. Same trust model, no second IdP."

#### Scene 7: Zero-Trust Deep Dive (2 min — if audience is technical)

**Show the CloudFormation snippet:**

```yaml
AuthorizerConfiguration:
  CustomJWTAuthorizer:
    DiscoveryUrl: <Cognito OIDC discovery URL>
    AllowedClients:
      - <client-id>
```

> "On AWS, inbound auth is infrastructure-level. AgentCore Runtime validates every JWT before the agent code even runs. Zero lines of auth code in the monitoring agent."

**Show auth_middleware.py:**

> "On GCP, we do the same validation in application code — same Cognito JWKS, same trust model. The identity layer is portable even when the infrastructure isn't. That's what zero-trust looks like when your agents operate in a world without walls."

---

## Sample Test Queries

### Monitoring Queries (→ Monitor Agent)
- "Show me the log groups in my account"
- "List the log streams for /aws/lambda/my-function"
- "Search for ERROR in the last hour across all log groups"
- "What CloudWatch alarms are currently in ALARM state?"

### Web Search Queries (→ Web Search Agent)
- "Find best practices for CloudWatch alarm configuration"
- "Search for AWS Lambda cold start optimization techniques"
- "What are the recommended EC2 instance types for memory-intensive workloads?"

### Multi-Agent Queries (→ Both Agents)
- "I'm seeing high CPU on my EC2 instances. Investigate and suggest fixes."
- "Check my Lambda error rates and find troubleshooting guides for any issues"
- "Are there any issues in my CloudWatch logs? Search for solutions."

### Memory Queries (→ Monitor Agent with context)
- "What did we discuss earlier?"
- "Summarize the issues we found in the last investigation"
- "Is that Lambda error still happening?"

### OBO / Role-Based Queries (log in as different users)
Demo users (seeded in the same Cognito pool, password set during deploy):
- `alice@demo.com` → `admin` (sees all log groups)
- `bob@demo.com` → `manager` (lambda + apigateway)
- `charlie@demo.com` → `analyst` (lambda only)
- any other email → `viewer` (no access)

Run the same query as each user to show different results:
- "Show me the log groups in my account"
- "List the log streams for /aws/apigateway/access"

---

## Troubleshooting During Demo

| Issue | Fix |
|---|---|
| Frontend won't load | `cd frontend && npm run dev` |
| Login fails | Check Cognito user exists — may need to confirm email first |
| Agent timeout | AgentCore Runtime may have cold-started — wait 30s and retry |
| "Actor ID not set" error | Make sure the frontend is sending the custom headers |
| Monitor agent returns empty | Check that CloudWatch log groups exist in us-west-2 |
| Web search agent fails | Check GCP Cloud Run service is running and TAVILY_API_KEY is set |

---

## Architecture Summary (for slides)

| Component | Framework | Runs On | Model | Auth |
|---|---|---|---|---|
| Host Agent | Google ADK | AgentCore Runtime | Claude Sonnet 4 (Bedrock via LiteLLM) | AgentCore Identity (outbound) |
| Monitoring Agent | Strands SDK | AgentCore Runtime | Claude Sonnet 4.5 (Bedrock) | AgentCore Runtime JWT Authorizer (inbound) |
| Web Search Agent | Strands SDK | GCP Cloud Run | Gemini 2.5 Flash (Google AI) | AgentCore Identity (GetWorkloadAccessTokenForJWT) |
| Identity Provider | Cognito | AWS | — | Trust anchor for all agents |
| Delegated User Identity | Cognito Pre-Token-Gen v3 + `aws_client_metadata` | AWS | — | Stamps `onBehalfOf` claim onto agent M2M token |
| Tool Access | MCP Gateway | AWS | — | OAuth2 via workload identity |
| Memory | AgentCore Memory | AWS | — | IAM / workload identity |
