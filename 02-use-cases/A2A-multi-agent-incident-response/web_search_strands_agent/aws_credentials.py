"""AWS credentials via GCP Workload Identity Federation (OIDC).

Based on: https://aws.amazon.com/blogs/security/access-aws-using-a-google-cloud-platform-native-workload-identity/

Flow:
1. Fetch identity token from GCP metadata server (audience=sts.amazonaws.com, format=full)
2. Call AWS STS AssumeRoleWithWebIdentity with the token
3. Get temporary credentials (1 hour), auto-refresh before expiry

Trust policy field mapping (Google tokens with azp field):
- accounts.google.com:aud  → token's azp (authorized party = service account numeric ID)
- accounts.google.com:oaud → token's aud (audience = "sts.amazonaws.com")
- accounts.google.com:sub  → token's sub (subject = service account numeric ID)

Environment variables (required):
- AWS_ROLE_ARN: IAM role ARN to assume
- AWS_REGION: Target region (default: us-west-2)
"""

import os
import time
import logging
import urllib.request
import boto3

logger = logging.getLogger(__name__)

AWS_ROLE_ARN = os.environ["AWS_ROLE_ARN"]
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "sts.amazonaws.com")

_cached_session = None
_cache_expiry = 0


def _get_gcp_identity_token() -> str:
    """Fetch identity token from GCP metadata server."""
    url = (
        "http://metadata.google.internal/computeMetadata/v1/"
        f"instance/service-accounts/default/identity?audience={OIDC_AUDIENCE}&format=full"
    )
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8")


def _assume_role() -> boto3.Session:
    """Exchange GCP identity token for temporary AWS credentials."""
    token = _get_gcp_identity_token()
    sts = boto3.client("sts", region_name=AWS_REGION)
    response = sts.assume_role_with_web_identity(
        RoleArn=AWS_ROLE_ARN,
        RoleSessionName="web-search-agent-gcp",
        WebIdentityToken=token,
        DurationSeconds=3600,
    )
    creds = response["Credentials"]
    logger.info(f"[OIDC] Assumed role, expires {creds['Expiration']}")
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=AWS_REGION,
    )


def get_aws_session() -> boto3.Session:
    """Get cached AWS session, refresh 5 min before expiry."""
    global _cached_session, _cache_expiry
    if _cached_session and time.time() < (_cache_expiry - 300):
        return _cached_session
    _cached_session = _assume_role()
    _cache_expiry = time.time() + 3600
    return _cached_session


def get_boto3_client(service_name: str, **kwargs):
    """Drop-in replacement for boto3.client() using OIDC credentials."""
    return get_aws_session().client(service_name, region_name=AWS_REGION, **kwargs)
