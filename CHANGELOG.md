# Changelog

All notable changes to the deployment automation in [hosted_app/](hosted_app/) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
