#!/bin/bash
# Fetch OIDC credentials and export as env vars before starting OTEL-instrumented app.
# ADOT's aws_auth_session.py reads AWS_ACCESS_KEY_ID/SECRET/TOKEN from environment.

CREDS=$(uv run python -c "
import urllib.request, os, boto3, json

audience = os.getenv('OIDC_AUDIENCE', 'sts.amazonaws.com')
role_arn = os.environ['AWS_ROLE_ARN']
region = os.getenv('AWS_REGION', 'us-west-2')

url = f'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}&format=full'
req = urllib.request.Request(url, headers={'Metadata-Flavor': 'Google'})
token = urllib.request.urlopen(req, timeout=5).read().decode()

sts = boto3.client('sts', region_name=region)
resp = sts.assume_role_with_web_identity(
    RoleArn=role_arn,
    RoleSessionName='web-search-agent-gcp',
    WebIdentityToken=token,
    DurationSeconds=3600,
)
c = resp['Credentials']
print(json.dumps({'ak': c['AccessKeyId'], 'sk': c['SecretAccessKey'], 'st': c['SessionToken']}))
")

export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['ak'])")
export AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['sk'])")
export AWS_SESSION_TOKEN=$(echo "$CREDS" | python3 -c "import sys,json; print(json.load(sys.stdin)['st'])")
export AWS_DEFAULT_REGION="${AWS_REGION:-us-west-2}"

echo "[ENTRYPOINT] ✅ AWS credentials exported via OIDC"
echo "[ENTRYPOINT] AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:0:4}..."

# Start with OTEL auto-instrumentation
exec uv run opentelemetry-instrument python -m main
