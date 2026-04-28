#!/usr/bin/env python3
"""
get_token.py — Get an OAuth bearer token for the MCP endpoint.
Reads credentials from deploy_output.json (written by deploy.py).

Usage:
    python get_token.py                  # prints token
    python get_token.py --export         # prints: export MCP_TOKEN=...
    python get_token.py --test           # gets token then tests /sse endpoint
"""

import argparse
import json
import sys
import urllib.request
import urllib.parse

def load_output(path="deploy_output.json") -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print("ERROR: deploy_output.json not found — run deploy.py first")
        sys.exit(1)

def get_token(domain_url: str, client_id: str, client_secret: str, scope: str) -> str:
    token_url = f"{domain_url.rstrip('/')}/oauth2/v1/token"
    payload = urllib.parse.urlencode({
        "grant_type":    "client_credentials",
        "client_id":     client_id,
        "client_secret": client_secret,
        "scope":         scope,
    }).encode()

    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: Token request failed ({e.code}): {body}")
        raise SystemExit(1)

    token = data.get("access_token")
    if not token:
        print(f"ERROR: No access_token in response: {data}")
        sys.exit(1)

    expires_in = data.get("expires_in", "?")
    print(f"Token obtained (expires in {expires_in}s)", file=sys.stderr)
    return token

def test_endpoint(endpoint_url: str, token: str):
    url     = endpoint_url.rstrip("/")
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id":      "1",
        "method":  "initialize",
        "params":  {
            "protocolVersion": "2025-06-18",
            "capabilities":    {},
            "clientInfo":      {"name": "Test", "version": "1.0.0"},
        },
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Accept":        "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    print(f"\nTesting: POST {url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"HTTP {resp.status}", file=sys.stderr)
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    print(line, file=sys.stderr)
                    break
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} — {e.read().decode()}", file=sys.stderr)
    except Exception as e:
        print(f"Connection error: {e}", file=sys.stderr)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="deploy_output.json")
    parser.add_argument("--export", action="store_true", help="Print export statement")
    parser.add_argument("--test",        action="store_true", help="Test SSE endpoint after getting token")
    parser.add_argument("--setup-claude", action="store_true", help="Generate claude_wrapper.sh")
    parser.add_argument("--setup-cline",  action="store_true", help="Generate cline_mcp_settings.json")
    parser.add_argument("--dir",          default=None,        help="Output dir override (default varies per --setup-* flag)")
    parser.add_argument("--client-secret", default=None,
                        help="Override client_secret (if not stored in deploy_output.json)")
    args = parser.parse_args()

    out = load_output(args.output)
    client_secret = args.client_secret or out.get("client_secret") or ""

    # Show what we are about to use
    print(f"Domain URL : {out.get('domain_url', '')}", file=sys.stderr)
    print(f"Client ID  : {out.get('client_id', '')}", file=sys.stderr)
    print(f"Scope      : {out.get('full_scope', '')}", file=sys.stderr)


    if not client_secret:
        print("ERROR: client_secret not found in deploy_output.json")
        print("       Pass it with: python get_token.py --client-secret <secret>")
        sys.exit(1)

    # Try full_scope first; if 400 invalid_scope, try scope alone then audience alone
    scope_candidates = [
        out["full_scope"],                                           # stored fqs
        out.get("audience", "").rstrip("/") + out.get("scope", ""), # audience+scope no sep
        out.get("audience", "").rstrip("/") + "/" + out.get("scope",""), # audience/scope
        out.get("scope", ""),                                        # scope only
        out["full_scope"].replace("/", ""),                          # no separator
        out["full_scope"].replace("/", "."),                         # dot separator
    ]
    token = None
    for scope_try in scope_candidates:
        if not scope_try:
            continue
        print(f"Trying scope: {scope_try}", file=sys.stderr)
        try:
            token = get_token(
                domain_url=out["domain_url"],
                client_id=out["client_id"],
                client_secret=client_secret,
                scope=scope_try,
            )
            print(f"  ✓ Scope worked: {scope_try}", file=sys.stderr)
            break
        except SystemExit:
            print(f"  ✗ Failed: {scope_try}", file=sys.stderr)
            continue
    if not token:
        print("ERROR: All scope variants failed", file=sys.stderr)
        sys.exit(1)

    if args.export:
        print(f"export MCP_TOKEN='{token}'")
    else:
        print(token)

    if args.setup_claude:
        import os
        deploy_dir   = os.path.dirname(os.path.abspath(__file__))
        endpoint     = out.get("endpoint_url", "").rstrip("/")
        default_dir  = os.path.join(deploy_dir, "..", "chats", "claude")
        output_dir   = os.path.abspath(args.dir if args.dir else default_dir)
        os.makedirs(output_dir, exist_ok=True)
        wrapper      = os.path.join(output_dir, "claude_wrapper.sh")
        script = f"""#!/bin/bash

# Use the venv python which has correct SSL certs and dependencies
DEPLOY_DIR="{deploy_dir}"
PYTHON="$DEPLOY_DIR/.venv/bin/python3"

TOKEN=$(cd "$DEPLOY_DIR" && "$PYTHON" get_token.py | tail -1)

if [ -z "$TOKEN" ]; then
    echo "ERROR: Failed to get token" >&2
    exit 1
fi

exec npx -y mcp-remote \\
  "{endpoint}" \\
  --header "Authorization: Bearer $TOKEN"
"""
        with open(wrapper, "w") as f:
            f.write(script)
        os.chmod(wrapper, 0o755)
        print(f"Created: {wrapper}", file=sys.stderr)

    if args.setup_cline:
        import os
        deploy_dir  = os.path.dirname(os.path.abspath(__file__))
        endpoint    = out.get("endpoint_url", "").rstrip("/")
        default_dir = os.path.join(deploy_dir, "..", "chats", "cline")
        output_dir  = os.path.abspath(args.dir if args.dir else default_dir)
        os.makedirs(output_dir, exist_ok=True)
        settings    = os.path.join(output_dir, "cline_mcp_settings.json")
        config = {
            "mcpServers": {
                "oci-inventory": {
                    "autoApprove": [],
                    "disabled":    False,
                    "timeout":     60,
                    "type":        "streamableHttp",
                    "url":         endpoint,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                }
            }
        }
        with open(settings, "w") as f:
            json.dump(config, f, indent=2)
        print(f"Created: {settings}", file=sys.stderr)
        print("NOTE: token expires in 24h — re-run --setup-cline to refresh", file=sys.stderr)

    if args.test:
        endpoint = out.get("endpoint_url")
        if not endpoint or "check Console" in endpoint:
            print("endpoint_url not set in deploy_output.json — skipping test", file=sys.stderr)
        else:
            test_endpoint(endpoint, token)

if __name__ == "__main__":
    main()
