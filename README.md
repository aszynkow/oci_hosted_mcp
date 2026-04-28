# 🏗️ OCI Inventory MCP Server

> A containerised **Model Context Protocol (MCP) server** that exposes Oracle Cloud Infrastructure (OCI) resource scanning as AI-callable tools — enabling Claude Desktop, Oracle Code Assist (OCA), and other MCP-capable AI clients to query your tenancy in natural language.

---

## 📋 Table of Contents

- [What It Does](#-what-it-does)
- [How It Works (MCP Context)](#-how-it-works-mcp-context)
- [Architecture](#-architecture)
- [Prerequisites](#-prerequisites)
- [Known Issues & Fixes](#%EF%B8%8F-known-issues--fixes)
- [Deployment Steps](#-deployment-steps)
- [OCI Config Setup](#-oci-config-setup)
- [Client Configuration](#-client-configuration)
  - [Claude Desktop](#claude-desktop)
  - [Oracle Code Assist (OCA)](#oracle-code-assist-oca)
- [Available Tools](#-available-tools)
- [Testing](#-testing)
- [Debugging & Logs](#-debugging--logs)
- [OCI Authentication Notes](#-oci-authentication-notes)
- [Oracle Documentation References](#-oracle-documentation-references)

---

## 🔍 What It Does

The OCI Inventory MCP Server connects your AI client directly to your Oracle Cloud tenancy. Instead of logging into the OCI Console or writing scripts, you can ask Claude (or any MCP-capable model) questions like:

> *"What services are running in my us-chicago-1 region?"*  
> *"List all compartments in my tenancy."*  
> *"Summarise resources grouped by service across all regions."*

The server translates those requests into OCI SDK calls and returns structured inventory data — all without leaving your chat session.

---

## 🧠 How It Works (MCP Context)

```
┌─────────────────────┐        SSE / HTTP         ┌──────────────────────────┐
│  AI Client          │ ◄────────────────────────► │  OCI Inventory MCP Server│
│  (Claude Desktop /  │   MCP Protocol (JSON-RPC)  │  FastMCP + OCI Python SDK│
│   OCA / claude.ai)  │                            │  Docker Container        │
└─────────────────────┘                            └──────────┬───────────────┘
                                                              │  OCI API calls
                                                              ▼
                                                   ┌──────────────────────────┐
                                                   │  Oracle Cloud            │
                                                   │  Infrastructure (OCI)    │
                                                   │  REST APIs               │
                                                   └──────────────────────────┘
```

**MCP (Model Context Protocol)** is an open standard that lets AI models call external tools via a structured JSON-RPC interface. This server implements the MCP spec using **[FastMCP](https://github.com/jlowin/fastmcp)** and exposes tools over **SSE (Server-Sent Events)** transport — the recommended transport for network-accessible MCP servers.

When Claude receives a natural-language query about your OCI environment, it:
1. Selects the appropriate MCP tool
2. Sends a JSON-RPC `tools/call` request to this server
3. The server executes the OCI SDK call using your local `~/.oci/config` credentials
4. Results are returned to Claude, which synthesises a human-readable response

---

## 🏛️ Architecture

```
oci-inventory/
├── server.py              # FastMCP server — tool definitions & OCI SDK calls
├── Dockerfile             # Multi-stage build; non-root mcpuser
├── docker-compose.yml     # Service definition, port mapping, volume mounts
├── requirements.txt       # Python deps (fastmcp, oci, etc.)
└── .oci/                  # ⚠️ Do NOT commit — mounted via Docker volume
    └── config             # OCI API key config
```

**Container user:** `mcpuser` (non-root, UID 1000)  
**Transport:** SSE on `http://localhost:8000/sse`  
**Runtime:** Docker via Colima (macOS)

---

## ✅ Prerequisites

| Requirement | Notes |
|---|---|
| [Colima](https://github.com/abiosoft/colima) | macOS container runtime (not Docker Desktop) |
| `docker` + `docker-compose` CLI | Hyphenated `docker-compose`, not the plugin syntax |
| OCI account with API key | See [OCI Auth docs](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm) |
| OCI CLI config (`~/.oci/config`) | Generated via `oci setup config` |
| Python 3.11+ (for local dev only) | Container handles runtime |

---

## ⚠️ Known Issues & Fixes

### 🐛 OCI Config Path Mismatch

**Symptom:** Tools authenticate but can't find `~/.oci/config` inside the container.

**Root cause:** The `mcpuser` was created with home directory `-d /app`:

```dockerfile
# ❌ Current — home resolves to /app
RUN useradd -m -d /app -s /bin/bash mcpuser
```

This means `os.path.expanduser("~/.oci/config")` → `/app/.oci/config`  
But `docker-compose.yml` mounts the OCI config to `/home/mcpuser/.oci` — a **mismatch**.

**Fix Option A — Correct the Dockerfile (recommended):**

```dockerfile
# ✅ Fix: use standard home directory
RUN useradd -m -d /home/mcpuser -s /bin/bash mcpuser
```

Then rebuild:
```bash
docker-compose down
docker-compose up --build
```

**Fix Option B — Change the volume mount in `docker-compose.yml`:**

```yaml
volumes:
  - ~/.oci:/app/.oci:ro    # match what expanduser("~/.oci") resolves to
```

---

## 🚀 Deployment Steps

### 1. Start Colima

```bash
colima start
```

### 2. Clone / navigate to your project

```bash
cd ~/path/to/oci-inventory
```

### 3. Verify OCI config exists on your host

```bash
ls ~/.oci/config
cat ~/.oci/config   # should show [DEFAULT] profile with key_file, tenancy, region, etc.
```

### 4. Build and start the server

```bash
docker-compose up --build
```

You should see:
```
oci-inventory  | INFO:     Started server process
oci-inventory  | INFO:     FastMCP SSE server running on http://0.0.0.0:8000
oci-inventory  | INFO:     MCP endpoint: http://0.0.0.0:8000/sse
```

### 5. Verify the server is reachable

```bash
curl -N http://localhost:8000/sse
# Should return SSE stream headers (content-type: text/event-stream)
```

---

## 🔐 OCI Config Setup

If you haven't set up OCI API key auth yet:

```bash
# Install OCI CLI (if not already installed)
brew install oci-cli

# Interactive setup — generates config + key pair
oci setup config
```

This creates `~/.oci/config` with:

```ini
[DEFAULT]
user=ocid1.user.oc1..aaa...
fingerprint=xx:xx:xx:...
tenancy=ocid1.tenancy.oc1..aaa...
region=us-chicago-1
key_file=~/.oci/oci_api_key.pem
```

📖 **Reference:** [OCI API Key Authentication](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm)  
📖 **Required IAM policies:** [OCI IAM Overview](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/overview.htm)

---

## ⚙️ Client Configuration

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "oci-inventory": {
      "url": "http://localhost:8000/sse",
      "transport": "sse"
    }
  }
}
```

Restart Claude Desktop. You should see the `oci-inventory` tools listed in the tool picker (🔧 icon).

📖 **MCP SSE transport spec:** [MCP Specification — Transports](https://spec.modelcontextprotocol.io/specification/basic/transports/)

---

### Oracle Code Assist (OCA)

OCA supports MCP servers via its tool integration layer. Add the server to your OCA MCP config (location varies by IDE plugin version):

```json
{
  "mcpServers": {
    "oci-inventory": {
      "url": "http://localhost:8000/sse",
      "transport": "sse"
    }
  }
}
```

> **⚠️ Prompt injection warning (OCA-specific):**  
> OCA's underlying model has been observed fabricating error messages that echo content from MCP tool output strings — e.g. inventing *"OCI config not found"* errors when the tool response contains config path strings. If you see an error message that suspiciously mirrors a tool's output text, **treat it as a hallucination** and verify with container logs instead.

---

## 🛠️ Available Tools

| Tool | Description |
|---|---|
| `list_subscribed_regions` | Returns all regions your tenancy is subscribed to |
| `scan_tenancy` | Broad resource scan across all subscribed regions |
| `scan_region` | Targeted resource scan for a specific region |
| `list_compartments` | Lists all compartments in the tenancy hierarchy |
| `get_services_summary` | **Preferred for architecture mapping** — groups resources by service → region → compartment |
| `get_unknown_resource_types` | Surfaces resource types not yet categorised by the server |

> **Tip:** `get_services_summary` is the best tool for generating architecture dashboards and OCI Architecture Center reference lookups. Its grouping (service → region → compartment) maps cleanly to per-service reference architecture searches.

📖 **OCI Resource Types:** [OCI Supported Services](https://docs.oracle.com/en-us/iaas/Content/services.htm)  
📖 **OCI Compartments:** [Managing Compartments](https://docs.oracle.com/en-us/iaas/Content/Identity/compartments/managingcompartments.htm)

---

## 🧪 Testing

### Quick smoke test — list tools via MCP protocol

```bash
curl -s -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}' | jq .
```

Expected: JSON array of tool definitions with `name`, `description`, `inputSchema`.

### Test a tool call directly

```bash
curl -s -X POST http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "list_subscribed_regions",
      "arguments": {}
    },
    "id": 2
  }' | jq .
```

### Test from Claude Desktop

1. Open Claude Desktop
2. Start a new chat
3. Ask: *"Use the oci-inventory tool to list my subscribed OCI regions"*
4. Claude should call `list_subscribed_regions` and return your region list

### Test `get_services_summary`

In Claude: *"Summarise all OCI resources in my tenancy grouped by service"*

This triggers `get_services_summary` — the richest tool for tenancy overviews and architecture dashboards.

---

## 🪵 Debugging & Logs

### View live container logs

```bash
docker-compose logs -f
```

### Check for OCI auth errors

```bash
# Auth only runs on first tool invocation — grep AFTER calling a tool
docker-compose logs | grep -iE "auth|config|oci|error|exception"
```

### Check which path the container resolves for OCI config

```bash
docker-compose exec oci-inventory python3 -c \
  "import os; print(os.path.expanduser('~/.oci/config'))"
```

This reveals the effective path — if it prints `/app/.oci/config` instead of `/home/mcpuser/.oci/config`, the home directory mismatch bug is present (see [Known Issues](#-known-issues--fixes)).

### Inspect running container

```bash
docker-compose exec oci-inventory /bin/bash
ls -la ~/.oci/
cat ~/.oci/config
```

### Full restart (clean state)

```bash
docker-compose down && docker-compose up --build
```

---

## 🔑 OCI Authentication Notes

> **OCI SDK auth is lazy** — authentication happens on the **first tool invocation**, not at container startup. Grepping logs for auth errors is only meaningful after you've actually called a tool from your AI client.

**Auth order of precedence inside the container:**

1. File-based config (`~/.oci/config`) — used by this server
2. Instance Principal (if running on OCI Compute) — not used locally
3. Resource Principal — not used locally

📖 **SDK Auth docs:** [OCI Python SDK Authentication](https://docs.oracle.com/en-us/iaas/tools/python/latest/sdk_behaviors/config.html)  
📖 **Instance Principal (for OCI-hosted deployment):** [Instance Principal Auth](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm)

---

## 📚 Oracle Documentation References

| Topic | Link |
|---|---|
| OCI API Key setup | [docs.oracle.com — API Signing Keys](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/apisigningkey.htm) |
| OCI Python SDK | [docs.oracle.com — Python SDK](https://docs.oracle.com/en-us/iaas/tools/python/latest/) |
| OCI SDK Config file | [docs.oracle.com — SDK Config](https://docs.oracle.com/en-us/iaas/tools/python/latest/sdk_behaviors/config.html) |
| OCI IAM & Compartments | [docs.oracle.com — IAM Overview](https://docs.oracle.com/en-us/iaas/Content/Identity/Concepts/overview.htm) |
| OCI Regions | [docs.oracle.com — Regions & Availability Domains](https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm) |
| OCI Supported Services | [docs.oracle.com — Services](https://docs.oracle.com/en-us/iaas/Content/services.htm) |
| OCI Architecture Center | [docs.oracle.com — Architecture Center](https://docs.oracle.com/solutions/) |
| Instance Principal Auth | [docs.oracle.com — Calling Services from Instances](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm) |
| OCI CLI setup | [docs.oracle.com — OCI CLI Quickstart](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) |
| MCP Specification | [spec.modelcontextprotocol.io](https://spec.modelcontextprotocol.io/) |
| FastMCP | [github.com/jlowin/fastmcp](https://github.com/jlowin/fastmcp) |

---

## 🗺️ What's Next

- [ ] Fix `mcpuser` home directory in Dockerfile (see [Known Issues](#%EF%B8%8F-known-issues--fixes))
- [ ] Deploy to OCI Container Instances for persistent hosting
- [ ] Switch from file-based auth to Instance Principal when OCI-hosted
- [ ] Add API monetisation layer (FastAPI + Stripe + PostgreSQL) for external access
- [ ] Extend tool coverage for additional OCI resource types flagged by `get_unknown_resource_types`

---

*Generated from project context — OCI Inventory MCP Server (us-chicago-1 / FastMCP SSE)*
