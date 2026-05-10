"""Setup script for Gmail Gateway with 3LO (Authorization Code Grant).

This script provisions the AgentCore Gateway-based approach for Gmail send:
1. Creates a Google OAuth2 credential provider in AgentCore Identity
2. Creates a Gateway with MCP protocol version 2025-11-25 and CUSTOM_JWT authorizer
3. Creates a Gmail target on the Gateway with the OpenAPI spec and outbound auth
4. Stores the Gateway URL and provider name in SSM Parameter Store
5. Prints the AgentCore Identity callback URL for Google Cloud Console registration

Prerequisites:
- A Google Cloud project with Gmail API enabled
- OAuth 2.0 credentials (Client ID + Client Secret) from Google Cloud Console
- A Cognito User Pool for inbound auth (discovery URL + client ID)

Usage:
    python setup_gmail_gateway.py \\
        --google-client-id YOUR_GOOGLE_CLIENT_ID \\
        --google-client-secret YOUR_GOOGLE_CLIENT_SECRET \\
        --cognito-discovery-url https://cognito-idp.<region>.amazonaws.com/<pool-id>/.well-known/openid-configuration \\
        --cognito-client-id YOUR_COGNITO_CLIENT_ID \\
        --region us-west-2
"""

import argparse
import json
import logging
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Resource names
PROVIDER_NAME = "gmail-3lo-provider"
GATEWAY_NAME = "gmail-send-gateway"
TARGET_NAME = "GmailSend"
IAM_ROLE_NAME = "BedrockAgentCoreGmailGatewayRole"

# SSM parameter paths
SSM_GATEWAY_URL = "/hostagent/agentcore/gmail-gateway-url"
SSM_PROVIDER_NAME = "/hostagent/agentcore/gmail-provider-name"
SSM_COGNITO_DISCOVERY_URL = "/hostagent/agentcore/cognito-discovery-url"
SSM_COGNITO_CLIENT_ID = "/hostagent/agentcore/cognito-client-id"

# Gmail scope
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

# Default callback URL for the OAuth2 callback server
DEFAULT_RETURN_URL = "http://localhost:9090/oauth2/callback"


def _load_openapi_spec() -> str:
    """Load the Gmail OpenAPI spec from the yaml file next to the host agent."""
    # Look for gmail_openapi.yaml relative to this script's location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    spec_path = os.path.join(script_dir, "..", "gmail_openapi.yaml")
    spec_path = os.path.normpath(spec_path)

    if not os.path.exists(spec_path):
        print(f"  ERROR: Gmail OpenAPI spec not found at: {spec_path}")
        sys.exit(1)

    with open(spec_path, "r") as f:
        return f.read()


def create_credential_provider(client_id: str, client_secret: str, region: str) -> dict:
    """Create or update the Google OAuth2 credential provider in AgentCore Identity."""
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    try:
        response = client.create_oauth2_credential_provider(
            name=PROVIDER_NAME,
            credentialProviderVendor="GoogleOauth2",
            oauth2ProviderConfigInput={
                "googleOauth2ProviderConfig": {
                    "clientId": client_id,
                    "clientSecret": client_secret,
                }
            },
        )
        print(f"  ✅ Created credential provider: {PROVIDER_NAME}")
        return response

    except client.exceptions.ConflictException:
        print(f"  ℹ️  Credential provider '{PROVIDER_NAME}' already exists. Updating...")
        response = client.update_oauth2_credential_provider(
            name=PROVIDER_NAME,
            credentialProviderVendor="GoogleOauth2",
            oauth2ProviderConfigInput={
                "googleOauth2ProviderConfig": {
                    "clientId": client_id,
                    "clientSecret": client_secret,
                }
            },
        )
        print(f"  ✅ Updated credential provider: {PROVIDER_NAME}")
        return response


def _ensure_gateway_iam_role(region: str) -> str:
    """Create or retrieve the IAM role for the Gateway."""
    iam_client = boto3.client("iam", region_name=region)

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        iam_response = iam_client.create_role(
            RoleName=IAM_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for Bedrock AgentCore Gmail Gateway",
        )
        role_arn = iam_response["Role"]["Arn"]
        print(f"  ✅ Created IAM role: {role_arn}")

        # Attach policy for AgentCore Identity access
        iam_client.attach_role_policy(
            RoleName=IAM_ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess",
        )
        print("  ✅ Attached AdministratorAccess policy to Gateway role")

        # Wait briefly for IAM propagation
        time.sleep(10)
        return role_arn

    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            iam_response = iam_client.get_role(RoleName=IAM_ROLE_NAME)
            role_arn = iam_response["Role"]["Arn"]
            print(f"  ℹ️  IAM role already exists: {role_arn}")
            return role_arn
        raise


def create_gateway(
    cognito_discovery_url: str,
    cognito_client_id: str,
    region: str,
) -> tuple:
    """Create or retrieve the Gateway with MCP protocol and CUSTOM_JWT authorizer.

    Returns (gateway_id, gateway_url).
    """
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Check if gateway already exists by listing and finding by name
    try:
        list_response = client.list_gateways()
        for gw in list_response.get("items", []):
            if gw.get("name") == GATEWAY_NAME:
                gateway_id = gw["gatewayId"]
                # Get full details for the URL
                detail = client.get_gateway(gatewayIdentifier=gateway_id)
                gateway_url = detail.get("gatewayUrl", "")
                print(f"  ℹ️  Gateway '{GATEWAY_NAME}' already exists: {gateway_id}")
                print(f"  Gateway URL: {gateway_url}")
                return gateway_id, gateway_url
    except Exception:
        pass  # If listing fails, try to create

    # Ensure IAM role exists
    role_arn = _ensure_gateway_iam_role(region)

    print("  Creating Gateway with MCP protocol (2025-11-25)...")
    response = client.create_gateway(
        name=GATEWAY_NAME,
        protocolType="MCP",
        protocolConfiguration={
            "mcp": {
                "supportedVersions": ["2025-03-26", "2025-11-25"],
            }
        },
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": cognito_discovery_url,
                "allowedClients": [cognito_client_id],
            }
        },
        roleArn=role_arn,
    )

    gateway_id = response["gatewayId"]
    gateway_url = response.get("gatewayUrl", "")
    print(f"  ✅ Gateway created: {gateway_id}")

    # Wait for gateway to be ready
    print("  Waiting for Gateway to become READY...")
    for _ in range(30):
        status_response = client.get_gateway(gatewayIdentifier=gateway_id)
        current_status = status_response.get("status", "UNKNOWN")
        if current_status == "READY":
            gateway_url = status_response.get("gatewayUrl", gateway_url)
            print(f"  ✅ Gateway is READY. URL: {gateway_url}")
            return gateway_id, gateway_url
        print(f"    Status: {current_status}...")
        time.sleep(10)

    print("  ⚠️  Gateway did not reach READY state within timeout. Continuing anyway.")
    return gateway_id, gateway_url


def create_gateway_target(
    gateway_id: str,
    provider_arn: str,
    region: str,
) -> str:
    """Create the Gmail target on the Gateway with OpenAPI spec and outbound auth.

    Returns the target ID.
    """
    client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Check if target already exists
    try:
        list_response = client.list_gateway_targets(gatewayIdentifier=gateway_id)
        for target in list_response.get("items", []):
            if target.get("name") == TARGET_NAME:
                target_id = target["targetId"]
                print(f"  ℹ️  Target '{TARGET_NAME}' already exists: {target_id}")
                return target_id
    except Exception:
        pass  # If listing fails, try to create

    # Load the OpenAPI spec
    openapi_spec = _load_openapi_spec()

    credential_provider_config = {
        "credentialProviderType": "OAUTH",
        "credentialProvider": {
            "oauthCredentialProvider": {
                "providerArn": provider_arn,
                "grantType": "AUTHORIZATION_CODE",
                "defaultReturnUrl": DEFAULT_RETURN_URL,
                "scopes": [GMAIL_SEND_SCOPE],
            }
        },
    }

    target_config = {
        "mcp": {
            "openApiSchema": {
                "inlinePayload": openapi_spec,
            }
        }
    }

    print("  Creating Gmail target on Gateway...")
    response = client.create_gateway_target(
        name=TARGET_NAME,
        description="Gmail Send API target with 3LO outbound auth",
        gatewayIdentifier=gateway_id,
        targetConfiguration=target_config,
        credentialProviderConfigurations=[credential_provider_config],
    )

    target_id = response["targetId"]
    print(f"  ✅ Created Gateway target: {target_id}")
    return target_id


def store_ssm_parameters(gateway_url: str, region: str):
    """Store Gateway URL and provider name in SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=region)

    params = {
        SSM_GATEWAY_URL: gateway_url,
        SSM_PROVIDER_NAME: PROVIDER_NAME,
    }

    for name, value in params.items():
        ssm.put_parameter(
            Name=name,
            Value=value,
            Type="String",
            Overwrite=True,
        )
        print(f"  ✅ Stored SSM parameter: {name} = {value}")


def _read_ssm_param(name: str, region: str) -> str:
    """Try to read an SSM parameter, return empty string if not found."""
    try:
        ssm = boto3.client("ssm", region_name=region)
        response = ssm.get_parameter(Name=name, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser(
        description="Setup Gmail Gateway with 3LO for AgentCore"
    )
    parser.add_argument(
        "--google-client-id",
        required=True,
        help="Google OAuth2 Client ID",
    )
    parser.add_argument(
        "--google-client-secret",
        required=True,
        help="Google OAuth2 Client Secret",
    )
    parser.add_argument(
        "--region",
        default="us-west-2",
        help="AWS region (default: us-west-2)",
    )
    parser.add_argument(
        "--cognito-discovery-url",
        default=None,
        help="Cognito OpenID Connect discovery URL (reads from SSM if not provided)",
    )
    parser.add_argument(
        "--cognito-client-id",
        default=None,
        help="Cognito App Client ID for Gateway inbound auth (reads from SSM if not provided)",
    )
    parser.add_argument(
        "--gateway-id",
        default=None,
        help="Existing Gateway ID to reuse (creates new if not provided)",
    )
    args = parser.parse_args()

    # Resolve Cognito params from SSM if not provided via CLI
    cognito_discovery_url = args.cognito_discovery_url
    cognito_client_id = args.cognito_client_id

    if not cognito_discovery_url:
        cognito_discovery_url = _read_ssm_param(SSM_COGNITO_DISCOVERY_URL, args.region)
    if not cognito_client_id:
        cognito_client_id = _read_ssm_param(SSM_COGNITO_CLIENT_ID, args.region)

    if not cognito_discovery_url or not cognito_client_id:
        print("ERROR: --cognito-discovery-url and --cognito-client-id are required")
        print("       (or store them in SSM at {} and {})".format(
            SSM_COGNITO_DISCOVERY_URL, SSM_COGNITO_CLIENT_ID
        ))
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  Gmail Gateway Setup (3LO via AgentCore Gateway)")
    print("=" * 60 + "\n")

    # Step 1: Create credential provider
    print("Step 1: Creating Gmail credential provider...")
    cred_response = create_credential_provider(
        client_id=args.google_client_id,
        client_secret=args.google_client_secret,
        region=args.region,
    )
    provider_arn = cred_response.get("credentialProviderArn", "")
    callback_url = cred_response.get("callbackUrl", "")
    print()

    # Step 2: Create or reuse Gateway
    print("Step 2: Creating Gateway with CUSTOM_JWT authorizer...")
    if args.gateway_id:
        # Reuse existing gateway
        client = boto3.client("bedrock-agentcore-control", region_name=args.region)
        detail = client.get_gateway(gatewayIdentifier=args.gateway_id)
        gateway_id = args.gateway_id
        gateway_url = detail.get("gatewayUrl", "")
        print(f"  ℹ️  Using existing Gateway: {gateway_id}")
        print(f"  Gateway URL: {gateway_url}")
    else:
        gateway_id, gateway_url = create_gateway(
            cognito_discovery_url=cognito_discovery_url,
            cognito_client_id=cognito_client_id,
            region=args.region,
        )
    print()

    # Step 3: Create Gmail target on Gateway
    print("Step 3: Creating Gmail target on Gateway...")
    create_gateway_target(
        gateway_id=gateway_id,
        provider_arn=provider_arn,
        region=args.region,
    )
    print()

    # Step 4: Store in SSM
    print("Step 4: Storing configuration in SSM Parameter Store...")
    store_ssm_parameters(gateway_url=gateway_url, region=args.region)
    print()

    # Step 5: Print callback URL for Google Cloud Console
    print("=" * 60)
    print("  IMPORTANT: Register this callback URL in Google Cloud Console")
    print("=" * 60)
    if callback_url:
        print(f"\n  Callback URL: {callback_url}\n")
    else:
        # Construct it from the known pattern
        constructed_url = (
            f"https://bedrock-agentcore.{args.region}.amazonaws.com"
            f"/identities/oauth2/callback/{PROVIDER_NAME}"
        )
        print(f"\n  Callback URL: {constructed_url}\n")

    print("  Go to: Google Cloud Console > APIs & Services > Credentials")
    print("  Edit your OAuth 2.0 Client > Authorized redirect URIs")
    print("  Add the callback URL above")
    print()

    # Step 6: Print environment variables
    print("=" * 60)
    print("  Environment variables to set for the Host Agent:")
    print("=" * 60)
    print(f"\n  export GMAIL_PROVIDER_NAME={PROVIDER_NAME}")
    print(f"  export GMAIL_GATEWAY_URL={gateway_url}")
    print(f"  export GMAIL_CALLBACK_URL={DEFAULT_RETURN_URL}")
    print()
    print("  (The agent also reads Gateway URL from SSM at runtime)")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
