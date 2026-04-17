# Deploying Web Search Strands Agent to GCP Cloud Run

## Architecture

```
AWS AgentCore Runtime                    GCP Cloud Run
┌─────────────────────┐                 ┌──────────────────────────┐
│ Host Agent (ADK)    │──A2A + Cognito──│ WebSearch Agent (Strands)│
│ Monitoring Agent    │   M2M token     │ + Cognito JWT validation │
│                     │                 │ + Tavily web search      │
│ Cognito User Pool   │                 │ + AgentCore Memory (AWS) │
│ AgentCore Identity  │                 └──────────────────────────┘
└─────────────────────┘
```

## Prerequisites

- GCP project with Cloud Run enabled
- `gcloud` CLI installed and authenticated
- AWS Cognito User Pool deployed (from the cognito.yaml stack)
- Cognito client credentials for the websearch client

## Step 1: Store secrets in GCP Secret Manager

```bash
# Get these from your Cognito stack outputs or Secrets Manager
gcloud secrets create cognito-user-pool-id \
  --data-file=- <<< "us-west-2_xxxxxxxx"

gcloud secrets create cognito-client-id \
  --data-file=- <<< "your-websearch-client-id"

gcloud secrets create cognito-client-secret \
  --data-file=- <<< "your-websearch-client-secret"

gcloud secrets create tavily-api-key \
  --data-file=- <<< "your-tavily-api-key"
```

## Step 2: Set up AWS credentials for AgentCore Memory access

The agent calls AgentCore Memory (AWS) for context. Set up
GCP-to-AWS credential access using Workload Identity Federation:

```bash
# Create a GCP service account for the Cloud Run service
gcloud iam service-accounts create websearch-agent-sa \
  --display-name="WebSearch Agent Service Account"

# On AWS side: create an IAM role that trusts the GCP service account
# via OIDC federation. The role needs:
# - bedrock-agentcore:* (for Memory access)
# - bedrock:InvokeModel* (for Bedrock model access)
```

Alternatively, for a quick start, use AWS access keys as env vars
(not recommended for production).

## Step 3: Build and deploy to Cloud Run

```bash
# From the web_search_strands_agent/ directory
GCP_PROJECT=$(gcloud config get-value project)
REGION=us-central1

# Build and push
gcloud builds submit \
  --tag gcr.io/$GCP_PROJECT/web-search-strands-agent

# Deploy
gcloud run deploy web-search-strands-agent \
  --image gcr.io/$GCP_PROJECT/web-search-strands-agent \
  --region $REGION \
  --allow-unauthenticated \
  --set-env-vars "COGNITO_REGION=us-west-2,MCP_REGION=us-west-2" \
  --set-env-vars "MODEL_ID=global.anthropic.claude-sonnet-4-20250514-v1:0" \
  --set-secrets "COGNITO_USER_POOL_ID=cognito-user-pool-id:latest" \
  --set-secrets "TAVILY_API_KEY=tavily-api-key:latest" \
  --set-secrets "MEMORY_ID=memory-id:latest" \
  --set-secrets "AWS_ACCESS_KEY_ID=aws-access-key:latest" \
  --set-secrets "AWS_SECRET_ACCESS_KEY=aws-secret-key:latest" \
  --memory 1Gi \
  --timeout 300
```

## Step 4: Get the Cloud Run URL

```bash
SERVICE_URL=$(gcloud run services describe web-search-strands-agent \
  --region $REGION --format 'value(status.url)')
echo $SERVICE_URL
```

## Step 5: Update Host Agent environment variables

Add these to the Host Agent's CloudFormation stack or .env:

```bash
WEBSEARCH_GCP_URL=https://web-search-strands-agent-xxxxx-uc.a.run.app
WEBSEARCH_COGNITO_TOKEN_ENDPOINT=https://your-domain.auth.us-west-2.amazoncognito.com/oauth2/token
WEBSEARCH_COGNITO_CLIENT_ID=your-websearch-client-id
WEBSEARCH_COGNITO_CLIENT_SECRET=your-websearch-client-secret
```

## Step 6: Test

```bash
# Test the agent card endpoint (no auth needed)
curl $SERVICE_URL/.well-known/agent-card.json

# Test with a Cognito M2M token
TOKEN=$(curl -s -X POST \
  "https://your-domain.auth.us-west-2.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -u "client_id:client_secret" | jq -r .access_token)

curl -X POST $SERVICE_URL/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","params":{"message":{"role":"user","parts":[{"text":"Find AWS CloudWatch best practices"}]}},"id":"1"}'
```

## Auth Flow Summary

1. Host Agent (AWS) fetches Cognito M2M token directly (no AgentCore Identity)
2. Host Agent calls GCP Cloud Run URL with Bearer token
3. GCP Cloud Run middleware validates JWT against Cognito JWKS
4. Request reaches the Strands A2A agent
5. Agent uses Bedrock (via AWS creds) for LLM + AgentCore Memory
