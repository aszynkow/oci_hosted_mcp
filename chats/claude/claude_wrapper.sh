#!/bin/bash
# Use the venv python which has correct SSL certs and dependencies
DEPLOY_DIR="<path_to_hosted_app>"
PYTHON="$DEPLOY_DIR/../.venv/bin/python3"

TOKEN=$(cd "$DEPLOY_DIR" && "$PYTHON" get_token.py | tail -1)

if [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to get token" >&2
    exit 1
fi

exec npx -y mcp-remote \
  "https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com/20251112/hostedApplications/ocid1.generativeaihostedapplication.oc1.phx.amaaaaaajlkbyliagqwaithtlj4ho6xpnkivavfk4aff5z5scz635d4llcpq/actions/invoke" \
  --header "Authorization: Bearer $TOKEN"
