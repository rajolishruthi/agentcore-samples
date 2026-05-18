#!/bin/bash
# Deploy Web Search Strands Agent with AgentCore Identity to GCP Cloud Run

set -e

echo "====================================================================="
echo "  Deploying Web Search Agent with AgentCore Identity to Cloud Run"
echo "====================================================================="
echo

# Configuration
GCP_PROJECT=$(gcloud config get-value project)
REGION=${REGION:-us-central1}
SERVICE_NAME="web-search-strands-agent"
IMAGE_NAME="gcr.io/$GCP_PROJECT/$SERVICE_NAME"

echo "Configuration:"
echo "  GCP Project: $GCP_PROJECT"
echo "  Region: $REGION"
echo "  Service: $SERVICE_NAME"
echo

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ .env file not found!"
    echo "   Create it from template: cp .env.template .env"
    exit 1
fi

# Load environment variables
source .env

# Validate required variables
REQUIRED_VARS=(
    "AGENTCORE_WORKLOAD_NAME"
    "AGENTCORE_CREDENTIAL_PROVIDER_NAME"
    "COGNITO_USER_POOL_ID"
    "COGNITO_CLIENT_ID"
    "AWS_REGION"
    "MEMORY_ID"
    "TAVILY_API_KEY"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ Missing required variable: $var"
        exit 1
    fi
done

echo "✓ All required variables present"
echo

# Step 1: Build and push container
echo "Step 1: Building and pushing container..."
gcloud builds submit --tag $IMAGE_NAME
echo "✓ Container built and pushed"
echo

# Step 2: Deploy to Cloud Run with AgentCore Identity configuration
echo "Step 2: Deploying to Cloud Run..."
gcloud run deploy $SERVICE_NAME \
  --image $IMAGE_NAME \
  --region $REGION \
  --allow-unauthenticated \
  --platform managed \
  --memory 1Gi \
  --timeout 300 \
  --update-env-vars "\
AGENTCORE_WORKLOAD_NAME=$AGENTCORE_WORKLOAD_NAME,\
AGENTCORE_CREDENTIAL_PROVIDER_NAME=$AGENTCORE_CREDENTIAL_PROVIDER_NAME,\
AWS_REGION=$AWS_REGION,\
COGNITO_REGION=${COGNITO_REGION:-us-west-2},\
MCP_REGION=${MCP_REGION:-us-west-2},\
MODEL_ID=${MODEL_ID:-gemini/gemini-2.5-flash}"

echo "✓ Deployed to Cloud Run"
echo

# Step 3: Get service URL
SERVICE_URL=$(gcloud run services describe $SERVICE_NAME \
  --region $REGION \
  --format 'value(status.url)')

echo "====================================================================="
echo "  ✓ Deployment Complete!"
echo "====================================================================="
echo
echo "Service URL: $SERVICE_URL"
echo
echo "Next steps:"
echo "1. Test the agent card:"
echo "   curl $SERVICE_URL/.well-known/agent-card.json"
echo
echo "2. Test the health endpoint:"
echo "   curl $SERVICE_URL/ping"
echo
echo "3. Update your orchestrator agent with this URL:"
echo "   WEBSEARCH_GCP_URL=$SERVICE_URL"
echo
