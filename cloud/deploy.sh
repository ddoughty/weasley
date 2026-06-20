#!/usr/bin/env bash
# Deploy the Weasley cloud pipeline to AWS.
#
# Usage:
#   cd cloud/
#   ./deploy.sh
#
# Requires: aws-cli, sam-cli, and valid AWS credentials.
# Secrets are read from the parent .env file.

set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="../.env"

read_env() {
    sed -n "s/^${1}=//p" "$ENV_FILE" | tail -n 1 | tr -d '"'
}

AMAZON_PLACES_API_KEY="$(read_env WEASLEY_AMAZON_PLACES_API_KEY)"
TRMNL_API_KEY="$(read_env WEASLEY_TRMNL_API_KEY)"
TRMNL_PLUGIN_UUID="$(read_env WEASLEY_TRMNL_PLUGIN_UUID)"
API_KEY="$(read_env WEASLEY_API_KEY)"
SESSION_SIGNING_KEY="$(read_env WEASLEY_SESSION_SIGNING_KEY)"
ADMIN_DOMAIN_NAME="$(read_env WEASLEY_ADMIN_DOMAIN_NAME)"
ADMIN_CERTIFICATE_ARN="$(read_env WEASLEY_ADMIN_CERTIFICATE_ARN)"
DISABLE_EXECUTE_API_ENDPOINT="$(read_env WEASLEY_DISABLE_EXECUTE_API_ENDPOINT)"

ADMIN_DOMAIN_NAME="${ADMIN_DOMAIN_NAME:-weasley-admin.doughty.org}"
DISABLE_EXECUTE_API_ENDPOINT="${DISABLE_EXECUTE_API_ENDPOINT:-false}"

if [ -z "$API_KEY" ]; then
    echo "Error: WEASLEY_API_KEY not found in $ENV_FILE"
    echo "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
    exit 1
fi

if [ -z "$SESSION_SIGNING_KEY" ]; then
    echo "Error: WEASLEY_SESSION_SIGNING_KEY not found in $ENV_FILE"
    echo "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
    exit 1
fi

if [ "$DISABLE_EXECUTE_API_ENDPOINT" = "true" ] && [ -z "$ADMIN_CERTIFICATE_ARN" ]; then
    echo "Error: cannot disable the default endpoint without a custom-domain certificate"
    exit 1
fi

sam build --template template.yaml --use-container

sam deploy \
    --parameter-overrides \
        "AmazonPlacesApiKey=${AMAZON_PLACES_API_KEY}" \
        "TrmnlApiKey=${TRMNL_API_KEY}" \
        "TrmnlPluginUuid=${TRMNL_PLUGIN_UUID}" \
        "ApiKeyValue=${API_KEY}" \
        "SessionSigningKey=${SESSION_SIGNING_KEY}" \
        "AdminDomainName=${ADMIN_DOMAIN_NAME}" \
        "AdminCertificateArn=${ADMIN_CERTIFICATE_ARN}" \
        "DisableExecuteApiEndpoint=${DISABLE_EXECUTE_API_ENDPOINT}"
