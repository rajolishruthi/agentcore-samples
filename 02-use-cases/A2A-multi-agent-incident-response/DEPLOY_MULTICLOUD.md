# Multi-Cloud Deployment Guide

Deploy 2 agents on AWS AgentCore Runtime + 1 agent on GCP Cloud Run,
with Cognito as the single identity provider across both clouds.

## Architecture

```
AWS (us-west-2)                              GCP (Cloud Run)
┌──────────────────────────────┐            ┌───────────────────────────┐
│ Cognito User Pool            │            │                           │
│ (OAuth2 M2M tokens)         │            │ WebSearch Agent (Strands) │
│                              │            │ • Cognito JWT validation  │
│ Host Agent (Google ADK)      │───A2A────▶│ • Tavily web search       │
│ on AgentCore Runtime         │  Bearer   │ • AgentCore Memory (AWS)  │
│ • @requires_access_token     │  token    │ • Bedrock LLM (AWS)       │
│                              │            │                           │
│ Monitoring Agent (Strands)   │            └───────────────────────────┘
│ on AgentCore Runtime         │
│                              │
│ AgentCore Identity           │
│ AgentCore Memory             │
│ MCP Gateway                  │
└──────────────────────────────┘
```

## Deployment Order

```
Step 1: Deploy Cognito stack (AWS)
Step 2: Deploy Monitoring Agent (AWS AgentCore)
Step 3: Deploy WebSearch Agent (GCP Cloud Run)  ← NEW
Step 4: Deploy Host Agent (AWS AgentCore) with WEBSEARCH_GCP_URL
```

---

## Step 1: Deploy Cognito + Monitoring Agent (AWS)

Use the existing deploy script. It will prompt for parameters interactively:

```bash
cd 02-use-cases/A2A-multi-agent-incident-response
uv run deploy.py
```

When prompted, deploy only the Cognito stack and Monitoring Agent.
Skip the Web Search Agent and Host Agent for now.

Alternatively, deploy them manually:

```bash
# Set region
aws configure set region us-west-2

# Deploy Cognito
aws cloudformation create-stack \
  --stack-name cognito-stack-a2a \
  --template-body file://cloudformation/cognito.yaml \
  --parameters \
    ParameterKey=DomainName,ParameterValue=agentcore-m2m-$(uuidgen | cut -c1-8) \
    ParameterKey=AdminUserEmail,ParameterValue=your-email@example.com \
  --capabilities CAPABILITY_IAM

aws cloudformation wait stack-create-complete --stack-name cognito-stack-a2a

# Deploy Monitoring Agent
aws cloudformation create-stack \
  --stack-name monitor-agent-a2a \
  --template-body file://cloudformation/monitoring_agent.yaml \
  --parameters \
    ParameterKey=CognitoStackName,ParameterValue=cognito-stack-a2a \
    ParameterKey=SmithyModelS3Bucket,ParameterValue=YOUR_BUCKET \
  --capabilities CAPABILITY_IAM

aws cloudformation wait stack-create-complete --stack-name monitor-agent-a2a
```

## Step 2: Get Cognito details for GCP deployment

```bash
# Get the Cognito User Pool ID
COGNITO_USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name cognito-stack-a2a \
  --query "Stacks[0].Outputs[?OutputKey=='WebSearchClientId'].OutputValue" \
  --output text)

# Get the Cognito domain
COGNITO_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name cognito-stack-a2a \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoDomain'].OutputValue" \
  --output text)

# Get the Discovery URL (needed for JWT validation on GCP)
DISCOVERY_URL=$(aws cloudformation describe-stacks \
  --stack-name cognito-stack-a2a \
  --query "Stacks[0].Outputs[?OutputKey=='DiscoveryUrl'].OutputValue" \
  --output text)

# Extract User Pool ID from Discovery URL
USER_POOL_ID=$(echo $DISCOVERY_URL | grep -oP 'amazonaws.com/\K[^/]+')

echo "User Pool ID: $USER_POOL_ID"
echo "Cognito Domain: $COGNITO_DOMAIN"
echo "Discovery URL: $DISCOVERY_URL"
```

---

## Step 3: Deploy WebSearch Agent to GCP Cloud Run

### 3a. Set up GCP project

```bash
# Set your GCP project
export GCP_PROJECT=$(gcloud config get-value project)
export GCP_REGION=us-central1

# Enable required APIs
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable secretmanager.googleapis.com
```

### 3b. Store secrets in GCP Secret Manager

```bash
# Cognito User Pool ID (for JWT validation)
echo -n "$USER_POOL_ID" | \
  gcloud secrets create cognito-user-pool-id --data-file=-

# Tavily API key
echo -n "YOUR_TAVILY_API_KEY" | \
  gcloud secrets create tavily-api-key --data-file=-

# AgentCore Memory ID (from monitoring agent stack or create new)
echo -n "YOUR_MEMORY_ID" | \
  gcloud secrets create memory-id --data-file=-

# AWS credentials for cross-cloud access (Bedrock + Memory)
echo -n "YOUR_AWS_ACCESS_KEY_ID" | \
  gcloud secrets create aws-access-key-id --data-file=-

echo -n "YOUR_AWS_SECRET_ACCESS_KEY" | \
  gcloud secrets create aws-secret-access-key --data-file=-
```

### 3c. Build and deploy

```bash
# From the project root
cd web_search_strands_agent/

# Build container image
gcloud builds submit --tag gcr.io/$GCP_PROJECT/web-search-strands-agent

# Deploy to Cloud Run
gcloud run deploy web-search-strands-agent \
  --image gcr.io/$GCP_PROJECT/web-search-strands-agent \
  --region $GCP_REGION \
  --allow-unauthenticated \
  --set-env-vars "\
COGNITO_REGION=us-west-2,\
MCP_REGION=us-west-2,\
MODEL_ID=global.anthropic.claude-sonnet-4-20250514-v1:0" \
  --set-secrets "\
COGNITO_USER_POOL_ID=cognito-user-pool-id:latest,\
TAVILY_API_KEY=tavily-api-key:latest,\
MEMORY_ID=memory-id:latest,\
AWS_ACCESS_KEY_ID=aws-access-key-id:latest,\
AWS_SECRET_ACCESS_KEY=aws-secret-access-key:latest" \
  --memory 1Gi \
  --timeout 300
```

### 3d. Get the Cloud Run URL

```bash
WEBSEARCH_GCP_URL=$(gcloud run services describe web-search-strands-agent \
  --region $GCP_REGION --format 'value(status.url)')
echo "WebSearch GCP URL: $WEBSEARCH_GCP_URL"
```

### 3e. Verify the deployment

```bash
# Test agent card (no auth needed)
curl $WEBSEARCH_GCP_URL/.well-known/agent-card.json

# Test health
curl $WEBSEARCH_GCP_URL/ping
```

---

## Step 4: Deploy Host Agent on AWS with GCP URL

The Host Agent needs the `WEBSEARCH_GCP_URL` environment variable.
Add it to the CloudFormation template's `EnvironmentVariables` section,
or pass it as a parameter.

### Option A: Add parameter to host_agent.yaml

Add to the `Parameters` section:
```yaml
  WebSearchGcpUrl:
    Type: String
    Description: GCP Cloud Run URL for the WebSearch agent
```

Add to the `AgentRuntime` resource's `EnvironmentVariables`:
```yaml
      EnvironmentVariables:
        GOOGLE_MODEL_ID: !Ref GoogleModelId
        GOOGLE_API_KEY: !Ref GoogleApiKey
        GOOGLE_GENAI_USE_VERTEXAI: 0
        MCP_REGION: !Sub '${AWS::Region}'
        WEBSEARCH_GCP_URL: !Ref WebSearchGcpUrl    # ← Add this
```

Then deploy:
```bash
aws cloudformation create-stack \
  --stack-name host-agent-a2a \
  --template-body file://cloudformation/host_agent.yaml \
  --parameters \
    ParameterKey=GoogleApiKey,ParameterValue=YOUR_GOOGLE_API_KEY \
    ParameterKey=GoogleModelId,ParameterValue=gemini-2.5-flash \
    ParameterKey=CognitoStackName,ParameterValue=cognito-stack-a2a \
    ParameterKey=WebSearchGcpUrl,ParameterValue=$WEBSEARCH_GCP_URL \
  --capabilities CAPABILITY_IAM
```

### Option B: Update after deployment

If the Host Agent is already deployed, update the runtime env vars:
```bash
# Get the runtime ID
HOST_RUNTIME_ID=$(aws ssm get-parameter \
  --name /hostagent/agentcore/runtime-id \
  --query Parameter.Value --output text)

# Update with the GCP URL (via AWS CLI or console)
# The ECR notification Lambda handles runtime updates on new image pushes
```

---

## Step 5: End-to-end test

```bash
# Test the Host Agent (which orchestrates both sub-agents)
uv run test/connect_agent.py --agent host

# Or test individual agents
uv run test/connect_agent.py --agent monitor

# Test GCP agent directly with a Cognito token
TOKEN=$(curl -s -X POST \
  "https://$COGNITO_DOMAIN/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -u "CLIENT_ID:CLIENT_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -X POST $WEBSEARCH_GCP_URL/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","params":{"message":{"role":"user","parts":[{"text":"Find AWS CloudWatch best practices"}]}},"id":"1"}'
```

---

## Summary

| Component | Where | Auth | Deploy method |
|---|---|---|---|
| Cognito User Pool | AWS | Issues all tokens | CloudFormation |
| Monitoring Agent | AWS AgentCore Runtime | Inbound: Cognito JWT (auto) | CloudFormation |
| WebSearch Agent | GCP Cloud Run | Inbound: Cognito JWT (middleware) | gcloud run deploy |
| Host Agent | AWS AgentCore Runtime | Outbound: @requires_access_token | CloudFormation |

## Cleanup

```bash
# AWS
uv run cleanup.py

# GCP
gcloud run services delete web-search-strands-agent --region $GCP_REGION
gcloud secrets delete cognito-user-pool-id
gcloud secrets delete tavily-api-key
gcloud secrets delete memory-id
gcloud secrets delete aws-access-key-id
gcloud secrets delete aws-secret-access-key
```
