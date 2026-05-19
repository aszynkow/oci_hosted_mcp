# Changelog

All notable changes to the deployment automation in [hosted_app/](hosted_app/) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.5] — 2026-05-19

### Fixed — `get_token.py`

- **Generated `claude_wrapper.cmd` no longer dies with `+ was unexpected at this time.` on Windows.** The inline PowerShell that fetches the OAuth token previously built the token URL as `($env:DOMAIN_URL.TrimEnd('/') + '/oauth2/v1/token')`. Inside cmd's `for /f '…'` command delimiter, the embedded single quotes around `'/'` and `'/oauth2/v1/token'` closed cmd's quoting early, leaving the `+` operator exposed to the cmd parser — which rejected it before PowerShell ever started. The template now pre-computes the URL as a cmd variable (`set "TOKEN_URL=%DOMAIN_URL%/oauth2/v1/token"`) and the PowerShell call references `$env:TOKEN_URL` directly, removing both the `+` and the inline single-quoted literals from cmd's view.

### Why this matters

Until 1.0.5, every Windows Claude Desktop launch using the generated `claude_wrapper.cmd` failed at startup — the MCP server transport closed immediately with `Server transport closed unexpectedly` because the wrapper exited before fetching a token. The fix restores the Windows-native flow added in 1.0.3 / documented in 1.0.4 so that `get_token.py --setup-claude` produces a wrapper that runs on first invocation without manual editing.

## [1.0.4] — 2026-05-18

### Added — `README.md`

- **Windows Claude Desktop setup documented.** The README now covers the Windows-native client flow alongside macOS / Linux: which wrapper file to use (`claude_wrapper.cmd`), where Claude Desktop reads its config from on Windows (`%APPDATA%\Claude\claude_desktop_config.json`), and the exact `mcpServers` JSON block that wires `cmd.exe /c` to the wrapper.
- **Unified `mcp-remote` prerequisite for all platforms** — documented `npm install -g mcp-remote@latest` as a single global-install step that applies to macOS, Linux, and Windows alike, called out at the top of step 3 so both wrappers share the same setup contract. Windows users also get the one-line `winget install OpenJS.NodeJS.LTS` to install Node.js first.
- **Secret-handling warning** — explicit callout that `claude_wrapper.cmd` embeds the Identity Domain `client_secret` and must be stored outside synced / shared folders and never committed.
- **Architecture diagram updated.** The Mermaid diagram in the *Architecture* section now groups Claude with its two Project Skills (`oci-tenancy-dashboard`, `orcl-docs-search`), labels the Claude ↔ Hosted Deployment edge as `SSE / Streamable HTTP + JWT Bearer Token`, and labels the FastMCP ↔ OCI API edge with the auth mode used inside the container. A new *Skill usage* paragraph below the diagram explains how each skill consumes the MCP tools.

### Why this matters

Until 1.0.4 the README only described the macOS / Linux Claude Desktop flow, even though `get_token.py --setup-claude` has been generating `claude_wrapper.cmd` alongside `claude_wrapper.sh` since 1.0.3. Windows users had to read the script source to discover the setup. The architecture diagram now also matches the actual data flow (JWT-authenticated MCP traffic + skill orchestration), so new contributors can understand the system end-to-end from the README alone.

## [1.0.3] — 2026-05-01

### Changed — `server.py`, `requirements.txt`

- **SSE keepalive compatibility:** Disabled FastMCP SSE keepalive pings to prevent mcp-remote client crashes. This is done by patching the session manager's ping interval after instantiating the FastMCP server.
- **Dependency update:** MCP SDK bumped from 1.9.0 to 1.27.0 in requirements.txt for improved protocol support and bugfixes.
- **Version bump:** Internal server version incremented to 19.

### Why this matters

The server now works reliably with mcp-remote and Claude Desktop, avoiding protocol errors caused by SSE keepalive pings. The MCP SDK upgrade brings protocol and stability improvements.

### Added — `deploy.py`

- **Artifact management improvements:**
  - New two-step artifact registration and activation flow for GenAI deployments, matching Console UI behavior. This prevents 400 errors when updating artifacts.
  - Added `--activate-only` option to activate an already-registered artifact without re-registering it.
  - Tag override support: `--image-only --tag v1` and related flows now ensure the pushed image tag matches the artifact update.
  - More robust work request handling and status output for artifact operations.

### Changed — `deploy.py`

- Usage and help output updated with new artifact management and recovery examples.
- Improved internal structure for artifact and deployment update steps.

### Why this matters

Artifact updates and rollbacks are now reliable and match the Console's behavior, with clear CLI flows for registering, activating, and rolling back container images in GenAI deployments. Tag handling is more predictable, and error messages are clearer for all artifact operations.

## [1.0.2] — 2026-04-30

### Fixed — `get_token.py`

- **Generated `claude_wrapper.sh` now resolves the venv at the repo root instead of inside `hosted_app/`.** The wrapper template's `PYTHON` line was changed from `"$DEPLOY_DIR/.venv/bin/python3"` to `"$DEPLOY_DIR/../.venv/bin/python3"`, matching the README's recommended layout where the virtualenv lives one directory above `hosted_app/`. Without this fix, Claude Desktop launched the wrapper with the system Python, which lacks the `oci`, `requests` and `pyyaml` packages and fails with an import error before any token can be minted.

### Why this matters

Claude Desktop integrations stopped working out of the box for fresh checkouts whose venv was created at the repo root (the documented setup). 1.0.2 brings the wrapper template back in line with the documented venv location so `--setup-claude` produces a working wrapper on first run.

## [1.0.1] — 2026-04-29

### Changed — `deploy.py`

- **GenAI API now receives the OCIR image reference with the full region hostname instead of the short-code form.** A new helper `_api_image_ref()` rewrites `phx.ocir.io` (and any other OCIR short code) to `us-phoenix-1.ocir.io` using the existing `OCIR_REGION_MAP`, and strips the tag (the GenAI API requires the tag in a separate `tag` field, not in `containerUri`).
- **`step_genai_deploy()`** — `containerUri` in the create-deployment payload now uses `_api_image_ref(cfg)` instead of the short-code `image_ref`.
- **`step_add_artifact()`** — the same short-code → full-region-hostname normalisation is applied when updating an existing deployment, and `containerUri` is now passed without the tag (tag stays in the dedicated `tag` field).

### Fixed — `deploy.py`

- **Existing-deployment lookup is no longer brittle when the SDK omits `display_name`.** `step_genai_deploy()` previously matched on `d.display_name` only, which raised when the SDK populated `name` instead. The lookup now falls back to `name` via `getattr(d, "display_name", None) or getattr(d, "name", None)`.

### Changed — `destroy.py`

- **`destroy_genai_deployment()` is now resilient to transient `403 Forbidden` responses and no longer hard-exits the whole teardown.**
  - The delete call is wrapped in a 2-attempt retry loop. On the first `403` it waits 15 s and retries.
  - On any non-`404` error (including a second `403`) it now emits a `warn(...)` and falls through, so the subsequent application deletion can cascade to the orphaned deployment.
  - The outer `except` now catches `Exception` (was `oci.exceptions.ServiceError`) and downgrades the failure to a warning, replacing the previous `err(...)` call that would `sys.exit(1)`.

### Why this matters

End-to-end teardown previously aborted whenever the GenAI control plane returned `403` while the deployment was still de-activating — leaving the operator to clean up the IAM, OAuth and OCIR resources by hand. After 1.0.1, the script logs the warning and continues; deleting the parent application cascades to the child deployment, completing the teardown in a single run.

## [1.0.0] — Initial release

- Full deploy automation: Docker build + push to OCIR → Identity Domain OAuth app → IAM dynamic group + policy → GenAI Hosted Application → GenAI Hosted Deployment.
- Full teardown automation in reverse dependency order (dry-run by default).
- Resume tracking via `deploy_output.json`.
- `get_token.py` helper for fetching OAuth bearer tokens and generating Claude Desktop / Cline MCP client configs.
