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
    grep "^${1}=" "$ENV_FILE" | cut -d= -f2- | tr -d '"'
}

AMAZON_PLACES_API_KEY="$(read_env WEASLEY_AMAZON_PLACES_API_KEY)"
TRMNL_API_KEY="$(read_env WEASLEY_TRMNL_API_KEY)"
TRMNL_PLUGIN_UUID="$(read_env WEASLEY_TRMNL_PLUGIN_UUID)"
API_KEY="$(read_env WEASLEY_API_KEY)"

if [ -z "$API_KEY" ]; then
    echo "Error: WEASLEY_API_KEY not found in $ENV_FILE"
    echo "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
    exit 1
fi

sam build --template template.yaml

sam deploy \
    --parameter-overrides \
        "AmazonPlacesApiKey=${AMAZON_PLACES_API_KEY}" \
        "TrmnlApiKey=${TRMNL_API_KEY}" \
        "TrmnlPluginUuid=${TRMNL_PLUGIN_UUID}" \
        "ApiKeyValue=${API_KEY}"
