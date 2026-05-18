#!/usr/bin/env python3
"""Simple setup for AgentCore Identity OAuth integration."""

import os
import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
WORKLOAD_NAME = os.getenv("AGENTCORE_WORKLOAD_NAME", "web-search-agent")
CREDENTIAL_PROVIDER_NAME = os.getenv("AGENTCORE_CREDENTIAL_PROVIDER_NAME", "cognito-oauth-provider")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_REGION = os.getenv("COGNITO_REGION", "us-west-2")


def setup():
    """Setup AgentCore Identity resources."""
    print("Setting up AgentCore Identity...")

    control_client = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)

    # 1. Create workload identity
    try:
        control_client.create_workload_identity(name=WORKLOAD_NAME)
        print(f"✓ Created workload identity: {WORKLOAD_NAME}")
    except Exception as e:
        if "already exists" in str(e).lower():
            print(f"✓ Workload identity already exists: {WORKLOAD_NAME}")
        else:
            print(f"✗ Error creating workload identity: {e}")
            return

    # 2. Create OAuth credential provider
    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID:
        print("✗ Missing COGNITO_USER_POOL_ID or COGNITO_CLIENT_ID")
        return

    # Get client secret from Cognito
    cognito_client = boto3.client("cognito-idp", region_name=COGNITO_REGION)
    try:
        response = cognito_client.describe_user_pool_client(
            UserPoolId=COGNITO_USER_POOL_ID,
            ClientId=COGNITO_CLIENT_ID
        )
        client_secret = response['UserPoolClient']['ClientSecret']
    except Exception as e:
        print(f"✗ Error getting client secret: {e}")
        return

    discovery_url = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/openid-configuration"

    try:
        response = control_client.create_oauth2_credential_provider(
            name=CREDENTIAL_PROVIDER_NAME,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput={
                "customOauth2ProviderConfig": {
                    "oauthDiscovery": {"discoveryUrl": discovery_url},
                    "clientId": COGNITO_CLIENT_ID,
                    "clientSecret": client_secret
                }
            }
        )
        callback_url = response.get('callbackUrl')
        print(f"✓ Created credential provider: {CREDENTIAL_PROVIDER_NAME}")
        print(f"  Callback URL: {callback_url}")
    except Exception as e:
        if "already exists" in str(e).lower():
            print(f"✓ Credential provider already exists: {CREDENTIAL_PROVIDER_NAME}")
        else:
            print(f"✗ Error creating credential provider: {e}")

    print("\n✓ Setup complete!")
    print(f"\nAdd to .env:")
    print(f"AGENTCORE_WORKLOAD_NAME={WORKLOAD_NAME}")
    print(f"AGENTCORE_CREDENTIAL_PROVIDER_NAME={CREDENTIAL_PROVIDER_NAME}")


def cleanup():
    """Cleanup AgentCore Identity resources."""
    control_client = boto3.client("bedrock-agentcore-control", region_name=AWS_REGION)

    try:
        control_client.delete_workload_identity(name=WORKLOAD_NAME)
        print(f"✓ Deleted workload identity: {WORKLOAD_NAME}")
    except Exception as e:
        print(f"Warning: {e}")

    try:
        control_client.delete_oauth2_credential_provider(name=CREDENTIAL_PROVIDER_NAME)
        print(f"✓ Deleted credential provider: {CREDENTIAL_PROVIDER_NAME}")
    except Exception as e:
        print(f"Warning: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        cleanup()
    else:
        setup()
