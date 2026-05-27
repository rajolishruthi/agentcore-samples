# Cognito-Native OBO for the Multi-Agent System

**Date:** 2026-05-22
**Status:** Draft, pending implementation plan
**Branch:** `fix/code-review` (current); implementation will land on a new branch off `main`

## Goal

Add user identity propagation across the multi-agent system so that the monitoring agent (AWS AgentCore Runtime) and the web search agent (GCP Cloud Run) can:

1. Know which Cognito user originated a request, for audit and role-based authorization.
2. Do this without a second consent popup. The user already authenticated via Cognito at the frontend.
3. Work cross-cloud. The web search agent on GCP must be able to read the user identity from the agent's bearer token.

## Why this design (and what it is NOT)

This design uses **Cognito's native Pre-Token-Generation v3 trigger plus `aws_client_metadata`** to copy user identity into a custom `onBehalfOf` claim on the agent's M2M access token. The pattern is taken from the working `~/Downloads/demo_export` (`lambdas/PreTokenGen_code.py` + `agents/hardened/agent.py`).

This is **not** AgentCore Identity's documented `ON_BEHALF_OF_TOKEN_EXCHANGE` API. That API requires an OAuth2 IdP that implements RFC 8693 token exchange or RFC 7523 §2.1 JWT bearer grant. Cognito implements neither. The supported out-of-the-box OBO providers in AgentCore Identity are Microsoft Entra ID and any custom RFC-compliant IdP (Keycloak, Auth0, etc.).

We considered three real-OBO options and rejected each:

- **Entra ID:** would work but adds a second IdP for the demo and requires a Microsoft tenant.
- **GCP STS (`sts.googleapis.com/v1/token`):** does not work. Three independent reasons: (1) AgentCore custom OAuth2 providers always send HTTP client authentication, but Google STS forbids the `Authorization` header; (2) Google STS does not accept the RFC 8693 `actor_token` parameter; (3) `clientAuthenticationMethod` enum has no `NONE` value, so client auth cannot be suppressed.
- **Keycloak (already deployed at `internal.hardikvt.people.aws.dev/realms/agentcore`):** would work for AWS-side hops, but the web search agent on Cloud Run can't reach an internal-only Keycloak host, and Cognito-as-trusted-issuer is not yet configured in the realm. Out of scope for now.

The README will state the mechanism honestly: "Cognito-native delegated identity. AgentCore Identity's `ON_BEHALF_OF_TOKEN_EXCHANGE` API requires an RFC 8693 / RFC 7523 IdP, which Cognito is not. We use Cognito's Pre-Token-Generation feature to achieve the same outcome on the existing identity provider."

## Architecture

```
Frontend (Cognito JS SDK)
   │   user logs in
   ▼
Cognito User Pool ──► user JWT (idToken / accessToken)
   │
   ▼
Frontend calls host agent runtime
   Authorization: Bearer <user JWT>
   │
   ▼
Host agent (AWS AgentCore Runtime, ADK)
   │  AgentCore Runtime authorizer validates user JWT (existing)
   │  entrypoint reads context.request_headers["authorization"]
   │  extracts <user JWT>
   │
   │  outbound A2A call (replaces the current @requires_access_token):
   │    POST https://<cognito-domain>/oauth2/token
   │      grant_type=client_credentials
   │      client_id=<agent-obo-client-id>
   │      client_secret=<agent-obo-client-secret>
   │      scope=<agent scopes>
   │      aws_client_metadata={
   │        "onBehalfOfToken": "<user JWT>",
   │        "callerApp": "host-agent"
   │      }
   │
   ▼
Cognito User Pool
   │  fires Pre-Token-Gen v3 Lambda
   │  Lambda reads event.request.clientMetadata.onBehalfOfToken
   │  decodes user JWT (no signature check; gateway already validated)
   │  returns claimsToAddOrOverride:
   │    onBehalfOf = '{"email": "...", "sub": "...", "username": "..."}'
   │    callerApp  = "host-agent"
   │  ─► returns agent M2M token with onBehalfOf claim
   │
   ▼
Host agent receives agent M2M token, uses as Bearer for A2A:
   │
   ├──► Monitoring agent (AWS AgentCore Runtime, Strands)
   │     │ AgentCore Runtime authorizer validates JWT
   │     │ agent code: decode token payload, read onBehalfOf
   │     │ apply role map: alice→admin, bob→manager, charlie→analyst
   │     │ filter CloudWatch results by role
   │     │ log: "[OBO] acting on behalf of alice@..."
   │     │ include {"_audit": {...}} in response
   │     ▼
   │   tool calls to monitoring gateway (existing M2M flow, unchanged)
   │
   └──► Web search agent (GCP Cloud Run, Strands)
         │ AgentCoreIdentityMiddleware calls
         │   GetWorkloadAccessTokenForJWT(userToken=<incoming bearer>)
         │ middleware additionally decodes the *incoming* token payload
         │ (not the workload access token) to read onBehalfOf
         │ stash on request.state.user_on_behalf_of
         │ log: "[OBO] acting on behalf of alice@..."
         │ include in response metadata
```

The user JWT itself never leaves the host agent. Only the host agent's exchanged M2M token (with `onBehalfOf` claim) flows downstream. Downstream agents trust that claim because AgentCore Runtime / `GetWorkloadAccessTokenForJWT` has already validated the token signature; the claim is signed by Cognito.

## Components

### New files

- `cloudformation/lambdas/pre_token_gen.py`
  Cognito Pre-Token-Gen v3 handler. Port of `demo_export/lambdas/PreTokenGen_code.py`. Reads `onBehalfOfToken` from `event.request.clientMetadata`, decodes the JWT payload (no signature verification — gateway already validated), copies `email/sub/cognito:username` into a JSON-stringified `onBehalfOf` claim plus a `callerApp` claim.

- `host_adk_agent/obo_token.py`
  New module. Exposes `async fetch_obo_token(user_jwt: str) -> str` that does a direct POST to Cognito `/oauth2/token` with `client_credentials` + `aws_client_metadata`. Uses the new `agent-obo-client` credentials from SSM. Caches per-user-token (5 min TTL) to avoid hammering Cognito within a single request.

- `monitoring_strands_agent/obo_claims.py`
  Helper to decode the inbound JWT payload (no verification — runtime authorizer already validated) and return `{"email", "sub", "role"}`. Static role map: `alice@demo.com→admin`, `bob@demo.com→manager`, `charlie@demo.com→analyst`, default→`viewer`.

- `web_search_strands_agent/obo_claims.py`
  Same helper, lives in the GCP-hosted package. Decodes inbound bearer payload after `GetWorkloadAccessTokenForJWT` succeeds (we use the original token for claim reading, not the workload access token).

### Modified files

- `cloudformation/cognito.yaml`
  - Add second app client `agent-obo-client` with:
    - `GenerateSecret: true`
    - `ExplicitAuthFlows`: client credentials only
    - `AllowedOAuthFlows: ["client_credentials"]`
    - Custom scopes inherited from existing agent client (do not change scopes that monitoring agent and web search agent already trust)
  - Add Lambda trigger:
    - `LambdaConfig.PreTokenGenerationConfig.LambdaArn = !GetAtt PreTokenGenLambda.Arn`
    - `LambdaConfig.PreTokenGenerationConfig.LambdaVersion = "V3_0"`
  - Add Lambda function `PreTokenGenLambda` + invoke permission
  - Output the new client ID + client secret ARN as SSM parameters under `/hostagent/cognito/obo/...`

- `host_adk_agent/agent.py`
  - `get_root_agent(session_id, actor_id, user_jwt)` — add `user_jwt` parameter
  - `_create_client_factory(...)` — replace the `@requires_access_token` block with a call to `obo_token.fetch_obo_token(user_jwt)` to populate the bearer token
  - The `LazyClientFactory.create()` path threads `user_jwt` into each fresh client

- `host_adk_agent/main.py`
  - `call_agent`: extract `Authorization` header from `context.request_headers`, strip `Bearer `, pass to `get_agent_and_card(session_id, actor_id, user_jwt)`

- `monitoring_strands_agent/agent.py` (or wherever inbound is handled)
  - On entry, decode token payload via `obo_claims.decode_user_identity()`
  - Pass `role` into the CloudWatch query path: filter log groups by role
    - `admin`: no filter
    - `manager`: `/aws/lambda/*`, `/aws/apigateway/*`
    - `analyst`: `/aws/lambda/*`
    - `viewer`: deny
  - Log a structured audit line: `logger.info("[OBO] user=%s role=%s tools=%s", email, role, tools)`
  - Include `{"_audit": {"on_behalf_of": email, "role": role}}` in the A2A response payload

- `web_search_strands_agent/agentcore_identity_auth.py`
  - After successful `GetWorkloadAccessTokenForJWT`, also call `obo_claims.decode_user_identity(bearer_token)` (the original token, not the workload access token) and stash on `request.state.user_on_behalf_of`
  - Log audit line

- `web_search_strands_agent/agent_executor.py`
  - Read `request.state.user_on_behalf_of`, log it, include in response metadata

- `frontend/src/...` (one component, exact path TBD during implementation)
  - Render an "On behalf of" badge per agent response, sourced from `_audit.on_behalf_of`

- `cloudformation/host_agent.yaml`
  - SSM parameter dependencies for `/hostagent/cognito/obo/client-id` and `/hostagent/cognito/obo/client-secret-arn`
  - Execution role: `secretsmanager:GetSecretValue` on the new client secret

- `DEMO_GUIDE.md`
  - New section "Demo: Delegated User Identity (OBO)"
  - Two prompts: one as Alice (admin, sees prod alarms), one as Charlie (analyst, sees lambda logs only)
  - Show the audit log line in CloudWatch with the user's email
  - Explicit note about the mechanism: Cognito-native, not AgentCore Identity OBO API

### Unchanged

- Gmail 3LO. The `email_tool` keeps the existing `@requires_access_token(auth_flow="USER_FEDERATION")` flow.
- Monitoring agent's outbound to its gateway. Stays M2M via `get_resource_oauth2_token`.
- Web search agent's outbound to Tavily. Unchanged.

## Data flow per request

1. User logs into frontend → Cognito returns user JWT.
2. Frontend POSTs to host agent runtime URL with `Authorization: Bearer <user JWT>`. AgentCore Runtime validates.
3. Host agent entrypoint reads `context.request_headers["authorization"]`, extracts `<user JWT>`.
4. Host agent's `obo_token.fetch_obo_token(user_jwt)` POSTs to `https://<cognito-domain>/oauth2/token`:
   - Body (URL-encoded): `grant_type=client_credentials&scope=<scopes>&aws_client_metadata={"onBehalfOfToken":"<user JWT>","callerApp":"host-agent"}`
   - Header: `Authorization: Basic <base64(client_id:client_secret)>`
5. Cognito invokes `PreTokenGenLambda`. Lambda parses `event.request.clientMetadata.onBehalfOfToken`, decodes its payload, builds:
   ```json
   {"onBehalfOf": "{\"email\":\"alice@demo.com\",\"sub\":\"...\",\"username\":\"alice\"}", "callerApp": "host-agent"}
   ```
   and returns it under `claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride`.
6. Cognito returns the agent M2M token. Token now contains `onBehalfOf` claim.
7. Host agent uses this token as Bearer for both A2A calls.
8. Monitoring agent (AWS): runtime authorizer validates the JWT signature. Agent code decodes payload, reads `onBehalfOf`, looks up role, filters tool results, logs audit, returns response with `_audit` metadata.
9. Web search agent (GCP): middleware calls `GetWorkloadAccessTokenForJWT` to validate. Same `onBehalfOf` claim is read from the original token. Logs audit, includes in response metadata.

## Why a second app client

The existing `/hostagent/cognito/m2m-client-id` is referenced from many places, has a Pre-Token-Gen-free history, and is used by `@requires_access_token` for non-OBO calls if any remain. Adding `agent-obo-client` separately:

- Lets us attach the Pre-Token-Gen trigger only to OBO traffic
- Keeps the existing `@requires_access_token` paths working unchanged for backward compatibility
- Makes the OBO surface auditable: any token issued by the OBO client is, by definition, an OBO call
- Minimizes blast radius if anything goes wrong with the new client

## Trust and security

- The user JWT lives only in the host agent process and the `aws_client_metadata` payload to Cognito. It's not logged, not forwarded to downstream agents.
- The Pre-Token-Gen Lambda decodes the user JWT *without* signature verification. This is safe because:
  - The Lambda runs inside Cognito's invocation, only triggered by trusted Cognito flow
  - The user JWT was already validated upstream by AgentCore Runtime authorizer before reaching the host agent
  - A forged user JWT in `aws_client_metadata` would still produce a Cognito-signed agent token, but the embedded `onBehalfOf` claim would carry whatever identity the attacker put in the JWT — meaning the attacker would need to already have valid host agent credentials AND already be a confused-deputy victim. Not a new attack surface; it's the same trust boundary.
- The `onBehalfOf` claim is signed by Cognito as part of the agent token. Downstream agents that validate the token signature can trust the claim transitively.
- Per-user `aws_client_metadata` size is well under Cognito's 5 KB clientMetadata limit (full Cognito JWT is 1-2 KB).

## Risks

1. **Cross-cloud claim visibility.** `GetWorkloadAccessTokenForJWT` returns a *new* workload access token; the `onBehalfOf` claim is on the *incoming* token, not the workload access token. The web search agent middleware must decode the *original* incoming bearer for claim reading, not the workload access token. The current design does this correctly. Verify in implementation.
2. **Cognito Pre-Token-Gen v3 enablement.** If the user pool was created with v1/v2, upgrading the trigger to v3 may require a stack update; CloudFormation supports this directly via `LambdaVersion: V3_0` in the trigger config.
3. **Rate limits.** Cognito `/oauth2/token` is rate-limited per client. The host agent caches per-user-JWT (5 min TTL) to avoid one Cognito call per A2A hop. Cache key is the user JWT itself; cache value is the agent token + expiry. Cache lives in the `obo_token` module as a process-local `dict` guarded by an `asyncio.Lock` per cache key (single-flight) to avoid stampedes when two parallel A2A calls happen for the same request.
4. **Token TTL mismatch.** User JWT expires before agent token: the cached agent token is still valid until its own expiry, no issue. Agent token expires before user JWT: cache miss, refetch transparently.
5. **Demo personas.** demo_export defines `alice/bob/charlie` in a separate burner. The current pool is in account 211175818413 (Isengard). Need to seed the same three users with the role mapping in this pool. New step in `deploy.py`.

## Out of scope

- Real RFC 8693 OBO via AgentCore Identity (Entra/Keycloak path). Documented as future work in `DEMO_GUIDE.md`.
- Cedar/AVP for tool-level authorization. Role enforcement lives in monitoring agent code (a 5-line dict). If the demo grows, Cedar can be added later.
- Replacing or modifying Gmail 3LO. It stays as is.
- Modifying any other agent's outbound auth (monitoring agent → gateway stays M2M).

## Testing

- **Unit:** `cloudformation/lambdas/test_pre_token_gen.py` — sample Cognito v3 event with a known user JWT, assert claim shape.
- **Unit:** `host_adk_agent/test_obo_token.py` — mock httpx, assert request body contains `aws_client_metadata` with the right user JWT, assert cache hits skip the second call.
- **Unit:** `monitoring_strands_agent/test_obo_claims.py` — sample agent JWTs with various `onBehalfOf` shapes, assert role mapping.
- **Integration:** end-to-end run from frontend with each persona, assert:
  - CloudWatch results differ by role (Alice sees prod, Charlie doesn't)
  - Response metadata `_audit.on_behalf_of` matches the logged-in user
  - Web search agent log line contains the user's email
- **Negative:** request with no `Bearer` token in inbound → host agent rejects, never reaches OBO path.
- **Negative:** request with malformed `aws_client_metadata` → Pre-Token-Gen Lambda logs and returns the original event with no `onBehalfOf` claim. Downstream agents see no `onBehalfOf`, default to `viewer` role (deny). Test this path.

## Deployment plan (high level — full sequencing in writing-plans)

1. Add `pre_token_gen.py` Lambda + CFN.
2. Update `cognito.yaml` to add `agent-obo-client` + trigger.
3. Update `deploy.py` to seed personas with role-tagged custom attributes (or just use email matching).
4. Implement `obo_token.py` and rewire `host_adk_agent/agent.py` + `main.py`.
5. Implement `obo_claims.py` in both Strands agents.
6. Wire `_audit` metadata through A2A responses.
7. Frontend "On behalf of" badge.
8. Update `DEMO_GUIDE.md`.

## Success criteria

- Logging in as Alice, asking "list all log groups," yields all groups; as Charlie, only `/aws/lambda/*`.
- Both agents' CloudWatch logs contain `[OBO] user=alice@demo.com role=admin` for the same request.
- Frontend shows "On behalf of: alice@demo.com" next to each agent's response.
- No new IdP introduced. No second consent popup. Gmail 3LO still works.
