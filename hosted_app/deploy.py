#!/usr/bin/env python3
"""
deploy.py — OCI MCP Server: full deploy automation (build → push → infra).

Automates end-to-end:
  0. Docker build + push to OCIR
  1. Validate config and OCI connectivity
  2. Create Identity Domain confidential app (OAuth resource server + client)
  3. Configure OAuth: audience, scope, client_credentials grant
  4. Activate the Identity Domain app
  5. Create IAM dynamic group for the GenAI deployment
  6. Create IAM policy granting resource scan permissions
  7. Create GenAI Hosted Application (scaling, auth, env vars)
  8. Create and activate GenAI Hosted Deployment (container image)
  9. Write connection details to deploy_output.json

Usage:
    python deploy.py [--config deploy_config.yaml] [--step STEP] [options]

    Steps:
        all           Run everything: docker build+push + all infra steps (default)
        docker        Docker build + push to OCIR only (same as --image-only)
        validate      Validate config and OCI connectivity only
        oauth         Create/activate Identity Domain OAuth app
        iam           Create IAM dynamic group + policy
        genai_app     Create GenAI Hosted Application
        genai_deploy  Create GenAI Hosted Deployment

    Common shortcuts:
        python deploy.py                               # full deploy (build + all infra)
        python deploy.py --skip-docker                 # infra steps only, no docker
        python deploy.py --image-only                  # build + push only, stop before infra
        python deploy.py --step docker                 # same as --image-only
        python deploy.py --step genai_deploy           # re-deploy container only
        python deploy.py --add-artifact                # push new image + update existing deployment
        python deploy.py --add-artifact --skip-docker  # update existing deployment (no new build)
        python deploy.py --add-artifact \\
            --deployment-id <ocid> \\
            --image <registry/ns/repo> \\
            --tag <new-tag>                            # explicit overrides for artifact update

    After deploy:
        python get_token.py --test
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# stdlib-only at import time so --help works without deps installed
# oci, requests, yaml are imported lazily inside _require_deps()
def _require_deps():
    global oci, requests, yaml
    missing = []
    try:
        import oci as _oci; oci = _oci
    except ImportError:
        missing.append("oci")
    try:
        import requests as _req; requests = _req
    except ImportError:
        missing.append("requests")
    try:
        import yaml as _yaml; yaml = _yaml
    except ImportError:
        missing.append("pyyaml")
    if missing:
        print(f"  \033[91m✗\033[0m Missing packages: {', '.join(missing)}")
        print(f"    Run: pip install {' '.join(missing)}")
        sys.exit(1)

# placeholders so IDEs don't complain
oci = requests = yaml = None


# ── OCIR short-code → region map ─────────────────────────────────────────────

OCIR_REGION_MAP = {
    "phx": "us-phoenix-1",    "iad": "us-ashburn-1",   "fra": "eu-frankfurt-1",
    "lhr": "uk-london-1",     "syd": "ap-sydney-1",    "mel": "ap-melbourne-1",
    "nrt": "ap-tokyo-1",      "bom": "ap-mumbai-1",    "sin": "ap-singapore-1",
    "yyz": "ca-toronto-1",    "gru": "sa-saopaulo-1",  "icn": "ap-seoul-1",
    "dxb": "me-dubai-1",      "jed": "me-jeddah-1",    "sjc": "us-sanjose-1",
    "ord": "us-chicago-1",    "yny": "ap-chuncheon-1", "kix": "ap-osaka-1",
    "cdg": "eu-paris-1",      "ams": "eu-amsterdam-1", "mtz": "il-jerusalem-1",
    "vcp": "sa-vinhedo-1",    "bog": "sa-bogota-1",    "mxq": "mx-queretaro-1",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def save_output(data: dict, path="deploy_output.json"):
    existing = {}
    if Path(path).exists():
        with open(path) as f:
            existing = json.load(f)
    existing.update(data)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    return existing

def load_output(path="deploy_output.json") -> dict:
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return {}

def ok(msg):      print(f"  \033[92m✓\033[0m {msg}")
def err(msg):     print(f"  \033[91m✗\033[0m {msg}"); sys.exit(1)
def info(msg):    print(f"  \033[96m→\033[0m {msg}")
def warn(msg):    print(f"  \033[93m⚠\033[0m {msg}")
def section(msg): print(f"\n\033[1m{'─'*60}\033[0m\n\033[1m{msg}\033[0m\n{'─'*60}")

def skip(msg):    print(f"  \033[90m↷\033[0m {msg}")

# ── Resume tracking ───────────────────────────────────────────────────────────

def mark_complete(step_name: str, path="deploy_output.json"):
    """Record a step as successfully completed in deploy_output.json."""
    out  = load_output(path)
    done = out.get("completed_steps", [])
    if step_name not in done:
        done.append(step_name)
    save_output({"completed_steps": done}, path)

def is_complete(step_name: str, path="deploy_output.json") -> bool:
    """Return True if this step was previously completed successfully."""
    return step_name in load_output(path).get("completed_steps", [])

def reset_step(step_name: str, path="deploy_output.json"):
    """Remove a step from the completed list so it will re-run."""
    out  = load_output(path)
    done = [s for s in out.get("completed_steps", []) if s != step_name]
    save_output({"completed_steps": done}, path)

def reset_all_steps(path="deploy_output.json"):
    """Clear all resume tracking (keeps all other output values)."""
    save_output({"completed_steps": []}, path)

def print_status(path="deploy_output.json"):
    """Print which steps have been completed."""
    out  = load_output(path)
    done = out.get("completed_steps", [])
    all_steps = ["docker", "oauth", "iam", "genai_app", "genai_deploy"]
    print("\n  Resume status (deploy_output.json):")
    for s in all_steps:
        mark = "\033[92m✓\033[0m" if s in done else "\033[90m○\033[0m"
        print(f"    {mark}  {s}")
    print()


def run(cmd: list[str], check=True, capture=False, **kwargs):
    """Run a subprocess command, streaming output unless capture=True."""
    result = subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=capture,
        **kwargs,
    )
    return result

def wait_for_work_request(genai_client, work_request_id: str, timeout=300):
    """Poll a GenAI work request until it succeeds or fails."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        wr = genai_client.get_work_request(work_request_id).data
        info(f"Work request status: {wr.status} ({wr.percent_complete:.0f}%)")
        if wr.status == "SUCCEEDED":
            return wr
        if wr.status in ("FAILED", "CANCELED"):
            err(f"Work request {work_request_id} {wr.status}: {wr.resources}")
        time.sleep(10)
    err(f"Work request {work_request_id} timed out after {timeout}s")

def _image_ref(cfg: dict) -> str:
    """Build the full OCIR image reference from config."""
    c = cfg["container"]
    return f"{c['registry']}/{c['tenancy_namespace']}/{c['repository']}:{c['tag']}"

def _api_image_ref(cfg: dict) -> str:
    """Build OCIR image reference with full region hostname for GenAI API (no tag)."""
    c = cfg["container"]
    short    = c['registry'].split(".")[0]
    registry = f"{OCIR_REGION_MAP[short]}.ocir.io" if short in OCIR_REGION_MAP else c['registry']
    return f"{registry}/{c['tenancy_namespace']}/{c['repository']}"

def _ocir_region(registry: str, fallback: str) -> str:
    """Derive OCI region identifier from OCIR registry hostname."""
    short = registry.split(".")[0]
    return OCIR_REGION_MAP.get(short, fallback)


# ── Step 0: Docker build + push ───────────────────────────────────────────────

def _check_docker():
    """Verify docker binary is present and daemon/machine is reachable."""
    if not shutil.which("docker"):
        err(
            "docker not found.\n"
            "  Install: brew install docker\n"
            "  Then:    colima start   (or: docker machine init && docker machine start)"
        )
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        err(
            "Docker daemon not running.\n"
            "  Start with: colima start   (or: docker machine start)"
        )
    import re
    ver_result = subprocess.run(["docker", "--version"], capture_output=True, text=True)
    ver = re.search(r"[\d.]+", ver_result.stdout)
    ok(f"Docker {ver.group() if ver else 'OK'}")

def _ocir_login(cfg: dict):
    """docker login to OCIR — uses token from config or prompts if empty."""
    import getpass
    c     = cfg["container"]
    reg   = c["registry"]
    ns    = c["tenancy_namespace"]
    usr   = c["username"]
    token = c.get("ocir_token", "").strip()

    print()
    print(f"  Logging into OCIR ({reg})...")
    print(f"  Username: {ns}/{usr}")

    if not token:
        print("  No ocir_token in deploy_config.yaml — enter it now.")
        print("    (Console → Identity → Users → <your user> → Auth Tokens → Generate)")
        print()
        token = getpass.getpass("  OCIR auth token: ").strip()
        if not token:
            err("No auth token provided — aborting docker login")

    run(
        ["docker", "login", reg, "--username", f"{ns}/{usr}", "--password-stdin"],
        input=token.encode(),
        capture=False,
    )
    ok(f"OCIR login OK ({reg})")

def _ensure_ocir_repo(cfg: dict, profile: str):
    """Create the OCIR container repository if it doesn't already exist."""
    c        = cfg["container"]
    registry = c["registry"]
    repo     = c["repository"]
    comp_id  = cfg["oci"]["compartment_id"]
    region   = _ocir_region(registry, cfg["oci"]["region"])

    info(f"Ensuring OCIR repository '{repo}' exists (region: {region})...")
    run(
        [
            "oci", "artifacts", "container", "repository", "create",
            "--compartment-id", comp_id,
            "--display-name",   repo,
            "--region",         region,
            "--profile",        profile,
        ],
        check=False,   # exits non-zero if repo already exists — that's fine
    )
    ok(f"Repository ready: {repo}")

def step_docker(cfg: dict, oci_cfg: dict, skip_login=False):
    """Step 0: Docker build + push to OCIR."""
    section("Step 0: Docker build + push to OCIR")

    # build_context is relative to deploy.py's location
    build_context_rel = cfg["container"].get("build_context", ".")
    script_dir        = Path(__file__).parent.resolve()
    build_context     = (script_dir / build_context_rel).resolve()
    dockerfile        = build_context / "Dockerfile"
    if not build_context.exists():
        err(f"build_context directory not found: {build_context}")
    if not dockerfile.exists():
        err(f"Dockerfile not found at {dockerfile}")

    _check_docker()

    profile = cfg["oci"]["profile"]
    image   = _image_ref(cfg)

    if not skip_login:
        _ocir_login(cfg)

    _ensure_ocir_repo(cfg, profile)

    info(f"Build context: {build_context}")
    info(f"Building image: {image}")
    run(["docker", "build", "-t", image, str(build_context)])
    ok("Image built")

    info(f"Pushing: {image}")
    run(["docker", "push", image])
    ok(f"Image pushed: {image}")

    return image


# ── Step 1: Validate ──────────────────────────────────────────────────────────

def step_validate(cfg: dict) -> dict:
    section("Step 1: Validate config and OCI connectivity")
    oci_cfg = oci.config.from_file(profile_name=cfg["oci"]["profile"])
    oci.config.validate_config(oci_cfg)
    ok(f"OCI profile '{cfg['oci']['profile']}' loaded")

    identity = oci.identity.IdentityClient(oci_cfg)
    tenancy  = identity.get_tenancy(cfg["oci"]["tenancy_id"]).data
    ok(f"Tenancy: {tenancy.name} ({cfg['oci']['tenancy_id'][:30]}...)")

    if not cfg["identity_domain"]["url"] or "XXXXXXX" in cfg["identity_domain"]["url"]:
        err("identity_domain.url not set in deploy_config.yaml")
    ok(f"Identity Domain URL: {cfg['identity_domain']['url']}")

    if not cfg["container"]["tenancy_namespace"]:
        ns = oci.object_storage.ObjectStorageClient(oci_cfg).get_namespace().data
        cfg["container"]["tenancy_namespace"] = ns
        info(f"Auto-detected tenancy namespace: {ns}")
    ok(f"Container namespace: {cfg['container']['tenancy_namespace']}")

    return oci_cfg


# ── Step 2-4: Identity Domain OAuth App ───────────────────────────────────────

def step_oauth(cfg: dict, oci_cfg: dict):
    section("Step 2-4: Identity Domain OAuth app (confidential app + client credentials)")

    import re

    domain_url = cfg["identity_domain"]["url"].rstrip("/")
    audience   = cfg["oauth"]["audience"]
    scope_name = cfg["oauth"]["scope"]
    app_name   = cfg["oauth"]["app_name"]

    from oci.signer import Signer

    def idcs_request(method, path, body=None):
        url  = f"{domain_url}{path}"
        hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
        sgn  = Signer(
            tenancy=oci_cfg.get("tenancy", ""),
            user=oci_cfg.get("user", ""),
            fingerprint=oci_cfg.get("fingerprint", ""),
            private_key_file_location=oci_cfg.get("key_file", ""),
        )
        resp = requests.request(method, url, json=body, headers=hdrs, auth=sgn)
        if not resp.ok:
            err(f"IDCS {method} {path} → {resp.status_code}: {resp.text}")
        return resp.json()

    existing   = idcs_request("GET", f'/admin/v1/Apps?filter=displayName+eq+"{app_name}"')
    resources  = existing.get("Resources", [])
    if resources:
        app_id    = resources[0]["id"]
        client_id = resources[0].get("name", "")
        info(f"App '{app_name}' already exists (id={app_id}) — skipping creation")
    else:
        info(f"Creating confidential app '{app_name}'...")
        client_name = re.sub(r"[^a-zA-Z0-9_-]", "-", app_name)
        tmpl_resp   = idcs_request("GET", "/admin/v1/AppTemplates?filter=wellKnownId+eq+%22CustomWebAppTemplateId%22")
        tmpl_res    = tmpl_resp.get("Resources", [])
        if not tmpl_res:
            err("Could not find CustomWebAppTemplateId template in Identity Domain")
        tmpl_id = tmpl_res[0]["id"]
        info(f"Template OCID: {tmpl_id}")

        payload = {
            "schemas":       ["urn:ietf:params:scim:schemas:oracle:idcs:App"],
            "displayName":   app_name,
            "name":          client_name,
            "description":   "OCI Inventory MCP Server — OAuth endpoint protection",
            "basedOnTemplate": {"value": tmpl_id},
            "isOAuthClient": True,
            "clientType":    "confidential",
            "allowedGrants": ["client_credentials"],
            "isLoginTarget": False,
            "showInMyApps":  False,
            "isOAuthResource": True,
            "audience":      audience.rstrip("/"),
            "scopes": [{"value": scope_name, "displayName": "Invoke", "requiresConsent": False}],
            "accessTokenExpiry": 86400,
        }
        result    = idcs_request("POST", "/admin/v1/Apps", payload)
        app_id    = result["id"]
        client_id = result.get("name", client_name)
        ok(f"App created: id={app_id}")

    app_data      = idcs_request("GET", f"/admin/v1/Apps/{app_id}?attributes=name,clientSecret,audience,scopes")
    client_secret = app_data.get("clientSecret", "")
    scopes        = app_data.get("scopes", [])
    actual_fqs    = scopes[0].get("fqs", f"{audience}/{scope_name}") if scopes else f"{audience}/{scope_name}"

    ok(f"Client ID: {client_id}")
    ok(f"Audience:  {audience}")
    ok(f"FQS scope: {actual_fqs}")

    info("Activating app...")
    idcs_request("PUT", f"/admin/v1/AppStatusChanger/{app_id}", {
        "schemas": ["urn:ietf:params:scim:schemas:oracle:idcs:AppStatusChanger"],
        "active":  True,
        "id":      app_id,
    })
    ok("App activated")

    out = save_output({
        "idcs_app_id":   app_id,
        "client_id":     client_id,
        "client_secret": client_secret,
        "domain_url":    domain_url,
        "audience":      audience,
        "scope":         scope_name,
        "full_scope":    audience.rstrip("/") + scope_name, #scope_name,
    })
    ok("OAuth details saved to deploy_output.json")
    return out


# ── Step 5-6: IAM dynamic group + policy ─────────────────────────────────────

def step_iam(cfg: dict, oci_cfg: dict):
    section("Step 5-6: IAM dynamic group + policy for Resource Principal")

    identity       = oci.identity.IdentityClient(oci_cfg)
    tenancy_id     = cfg["oci"]["tenancy_id"]
    compartment_id = cfg["oci"]["compartment_id"]
    dg_name        = "oci-mcp-genai-dg"
    policy_name    = "oci-mcp-genai-policy"

    matching_rule = (
        f"ALL {{"
        f"resource.compartment.id = '{compartment_id}', "
        f"resource.type = 'genaihosteddeployment'"
        f"}}"
    )

    existing_dg_name = cfg.get("iam", {}).get("existing_dynamic_group", "")
    if existing_dg_name:
        dg_name = existing_dg_name
        info(f"Using existing dynamic group '{dg_name}' from config")

    existing_dgs = identity.list_dynamic_groups(tenancy_id, name=dg_name).data
    if existing_dgs:
        dg_id = existing_dgs[0].id
        info(f"Dynamic group '{dg_name}' already exists — skipping creation")
    else:
        info(f"Creating dynamic group '{dg_name}'...")
        try:
            dg    = identity.create_dynamic_group(
                oci.identity.models.CreateDynamicGroupDetails(
                    compartment_id=tenancy_id,
                    name=dg_name,
                    description="Grants GenAI hosted deployments Resource Principal for OCI inventory scan",
                    matching_rule=matching_rule,
                )
            ).data
            dg_id = dg.id
            ok(f"Dynamic group created: {dg_id}")
        except oci.exceptions.ServiceError as e:
            if "quota" in str(e.message).lower() or "limit" in str(e.message).lower():
                err(
                    f"Dynamic group limit hit. Add an existing group to deploy_config.yaml:\n"
                    f"  iam:\n    existing_dynamic_group: <name>\n"
                    f"Run: oci iam dynamic-group list --all --query 'data[*].name'"
                )
            raise

    policy_statements = [
        f"Allow dynamic-group {dg_name} to read all-resources in tenancy",
    ]
    existing_policies = identity.list_policies(compartment_id=tenancy_id, name=policy_name).data
    if existing_policies:
        info(f"Policy '{policy_name}' already exists — skipping")
    else:
        info(f"Creating policy '{policy_name}'...")
        identity.create_policy(
            oci.identity.models.CreatePolicyDetails(
                compartment_id=tenancy_id,
                name=policy_name,
                description="OCI MCP Server GenAI hosted deployment — resource scan",
                statements=policy_statements,
            )
        )
        ok(f"Policy created: {policy_name}")
        ok(f"  → {policy_statements[0]}")

    save_output({"dynamic_group": dg_name, "iam_policy": policy_name})


# ── Step 7: GenAI Application ─────────────────────────────────────────────────

def step_genai_app(cfg: dict, oci_cfg: dict):
    section("Step 7: Create GenAI Hosted Application")

    out = load_output()
    if not out.get("domain_url"):
        err("OAuth step not completed — run with --step oauth first")

    genai_cfg  = dict(oci_cfg)
    region     = cfg["oci"]["region"]
    genai_cfg["region"] = region
    genai_ep   = f"https://generativeai.{region}.oci.oraclecloud.com"
    genai      = oci.generative_ai.GenerativeAiClient(genai_cfg, service_endpoint=genai_ep)
    m          = oci.generative_ai.models

    app_name       = cfg["genai_application"]["name"]
    compartment_id = cfg["oci"]["compartment_id"]

    existing     = genai.list_hosted_applications(compartment_id=compartment_id).data
    existing_app = next(
        (a for a in existing.items
         if a.display_name == app_name
         and getattr(a, "lifecycle_state", "") in ("ACTIVE", "CREATING", "UPDATING")),
        None,
    )
    if existing_app:
        app_id = existing_app.id
        info(
            f"GenAI Application '{app_name}' already exists and is "
            f"{existing_app.lifecycle_state} (id={app_id[:30]}...) — skipping creation"
        )
    else:
        info(f"Creating GenAI Application '{app_name}'...")
        scfg           = cfg["genai_application"]
        scaling_metric = scfg["scaling_metric"]
        threshold      = scfg["scaling_threshold"]
        scaling_kwargs = {}
        if scaling_metric in ("CONCURRENT_REQUESTS", "CONCURRENCY"):
            scaling_kwargs["target_concurrency_threshold"] = threshold
            scaling_type = "CONCURRENCY"
        elif scaling_metric == "CPU":
            scaling_kwargs["target_cpu_threshold"] = threshold
            scaling_type = "CPU"
        elif scaling_metric == "MEMORY":
            scaling_kwargs["target_memory_threshold"] = threshold
            scaling_type = "MEMORY"
        else:
            scaling_kwargs["target_rps_threshold"] = threshold
            scaling_type = "REQUESTS_PER_SECOND"

        details = m.CreateHostedApplicationDetails(
            display_name=app_name,
            compartment_id=compartment_id,
            description="OCI Inventory MCP Server — resource scan agent",
            scaling_config=m.ScalingConfig(
                min_replica=scfg["min_replicas"],
                max_replica=scfg["max_replicas"],
                scaling_type=scaling_type,
                **scaling_kwargs,
            ),
            inbound_auth_config=m.InboundAuthConfig(
                inbound_auth_config_type="IDCS_AUTH_CONFIG",
                idcs_config=m.IdcsAuthConfig(
                    domain_url=out["domain_url"],
                    audience=out["audience"],
                    scope=out["scope"],
                ),
            ),
            environment_variables=[
                m.EnvironmentVariable(name="OCI_AUTH",                      type="PLAINTEXT", value="resource_principal"),
                m.EnvironmentVariable(name="MCP_HOST",                      type="PLAINTEXT", value="0.0.0.0"),
                m.EnvironmentVariable(name="MCP_PORT",                      type="PLAINTEXT", value="8080"),
                m.EnvironmentVariable(name="OCI_RESOURCE_PRINCIPAL_REGION", type="PLAINTEXT", value=region),
            ],
        )

        resp  = genai.create_hosted_application(create_hosted_application_details=details)
        wr_id = resp.headers.get("opc-work-request-id")
        if wr_id:
            info(f"Waiting for application provisioning (work request: {wr_id[:30]}...)")
            wait_for_work_request(genai, wr_id)

        apps   = genai.list_hosted_applications(compartment_id=compartment_id).data
        app    = next(a for a in apps.items if a.display_name == app_name)
        app_id = app.id
        ok(f"GenAI Application created: {app_id[:40]}...")

    app_detail = genai.get_hosted_application(hosted_application_id=app_id).data
    endpoint   = (
        getattr(app_detail, "endpoint_url", None)
        or getattr(app_detail, "endpoint", None)
        or getattr(app_detail, "url", None)
        or getattr(app_detail, "invoke_endpoint", None)
    )
    if not endpoint:
        endpoint = (
            f"https://inference.generativeai.{region}.oci.oraclecloud.com"
            f"/20251112/hostedApplications/{app_id}/actions/invoke"
        )
        info("Endpoint attribute not in SDK response — constructed from pattern")

    save_output({"genai_app_id": app_id, "endpoint_url": endpoint})
    ok(f"Endpoint: {endpoint}")
    return app_id


# ── Step 8: GenAI Deployment ──────────────────────────────────────────────────

def _make_genai_signer(oci_cfg: dict):
    from oci.signer import Signer
    return Signer(
        tenancy=oci_cfg["tenancy"],
        user=oci_cfg["user"],
        fingerprint=oci_cfg["fingerprint"],
        private_key_file_location=oci_cfg["key_file"],
    )

def step_genai_deploy(cfg: dict, oci_cfg: dict):
    section("Step 8: Create and activate GenAI Hosted Deployment")

    out    = load_output()
    app_id = out.get("genai_app_id")
    if not app_id:
        err("GenAI Application step not completed — run --step genai_app first")

    genai_cfg  = dict(oci_cfg)
    region     = cfg["oci"]["region"]
    genai_cfg["region"] = region
    genai_ep   = f"https://generativeai.{region}.oci.oraclecloud.com"
    genai      = oci.generative_ai.GenerativeAiClient(genai_cfg, service_endpoint=genai_ep)

    ccfg      = cfg["container"]
    image_ref = _image_ref(cfg)
    info(f"Container image: {image_ref}")

    existing = genai.list_hosted_deployments(
        compartment_id=cfg["oci"]["compartment_id"],
        application_id=app_id,
    ).data
    existing_dep = next(
        (d for d in existing.items
         if (getattr(d, "display_name", None) or getattr(d, "name", None)) == cfg["genai_application"]["name"]),
        None,
    )
    if existing_dep and existing_dep.lifecycle_state == "ACTIVE":
        info(f"Active deployment already exists ({existing_dep.id[:30]}...) — skipping")
        save_output({"genai_deployment_id": existing_dep.id})
        return

    info("Creating deployment with container image (REST)...")
    signer  = _make_genai_signer(oci_cfg)
    url     = f"https://generativeai.{region}.oci.oraclecloud.com/20231130/hostedDeployments"
    payload = {
        "displayName":        cfg["genai_application"]["name"],
        "compartmentId":      cfg["oci"]["compartment_id"],
        "hostedApplicationId": app_id,
        "activeArtifact": {
            "artifactType":  "SIMPLE_DOCKER_ARTIFACT",
            "containerUri":  _api_image_ref(cfg),  # full region hostname, no tag
            "tag":           ccfg["tag"],
        },
    }
    resp = requests.post(
        url, json=payload, auth=signer,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if not resp.ok:
        err(f"create_hosted_deployment failed {resp.status_code}: {resp.text}")

    dep_data = resp.json()
    dep_id   = dep_data.get("id", "")
    wr_id    = resp.headers.get("opc-work-request-id", "")
    if wr_id:
        info(f"Waiting for deployment (work request: {wr_id[:30]}...)")
        wait_for_work_request(genai, wr_id, timeout=600)

    ok(f"Deployment created: {dep_id[:40]}...")
    save_output({"genai_deployment_id": dep_id})


# ── Add artifact to existing deployment ───────────────────────────────────────

def step_add_artifact(cfg: dict, oci_cfg: dict, deployment_id: str = "", image: str = "", tag: str = ""):
    """Update an existing GenAI Hosted Deployment with a new container image."""
    section("Add Artifact: Update Existing GenAI Hosted Deployment")

    out    = load_output()
    dep_id = deployment_id or out.get("genai_deployment_id", "")
    if not dep_id:
        err(
            "No deployment ID found.\n"
            "  Pass --deployment-id <ocid>  or run a full deploy first (deploy_output.json)"
        )

    ccfg      = cfg["container"]
    short     = ccfg['registry'].split(".")[0]
    registry  = f"{OCIR_REGION_MAP[short]}.ocir.io" if short in OCIR_REGION_MAP else ccfg['registry']
    image_ref = image or f"{registry}/{ccfg['tenancy_namespace']}/{ccfg['repository']}"
    use_tag   = tag or ccfg["tag"]
    full_ref  = f"{image_ref}:{use_tag}" if ":" not in image_ref else image_ref
    info(f"Updating deployment {dep_id[:30]}... → {full_ref}")

    region  = cfg["oci"]["region"]
    signer  = _make_genai_signer(oci_cfg)
    url     = f"https://generativeai.{region}.oci.oraclecloud.com/20231130/hostedDeployments/{dep_id}"
    payload = {
        "activeArtifact": {
            "artifactType": "SIMPLE_DOCKER_ARTIFACT",
            "containerUri": image_ref,  # no tag — passed separately
            "tag":          use_tag,
        }
    }
    resp = requests.put(
        url, json=payload, auth=signer,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if not resp.ok:
        err(f"update_hosted_deployment failed {resp.status_code}: {resp.text}")

    genai_cfg  = dict(oci_cfg)
    genai_cfg["region"] = region
    genai_ep   = f"https://generativeai.{region}.oci.oraclecloud.com"
    genai      = oci.generative_ai.GenerativeAiClient(genai_cfg, service_endpoint=genai_ep)
    wr_id      = resp.headers.get("opc-work-request-id", "")
    if wr_id:
        info(f"Waiting for artifact update (work request: {wr_id[:30]}...)")
        wait_for_work_request(genai, wr_id, timeout=600)

    ok(f"Deployment updated with new artifact: {full_ref}")
    save_output({"last_artifact_image": full_ref, "last_artifact_tag": use_tag})


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(cfg: dict):
    section("Deployment Complete — Connection Details")
    out         = load_output()
    domain_url  = out.get("domain_url", "")
    full_scope  = out.get("full_scope", "")
    endpoint    = out.get("endpoint_url", "check Console")
    client_id   = out.get("client_id", "")

    print(f"""
Token endpoint:   {domain_url}/oauth2/v1/token
Client ID:        {client_id}
Client secret:    (in deploy_output.json — store in OCI Vault for production)
Scope:            {full_scope}
MCP endpoint:     {endpoint}/sse

Get a token:
  python get_token.py

Add to Claude Desktop / OCA:
  URL:    {endpoint}/sse
  Header: Authorization: Bearer <token from get_token.py>
""")


# ── Steps registry ────────────────────────────────────────────────────────────

STEPS = {
    "validate":     step_validate,
    "docker":       step_docker,
    "oauth":        step_oauth,
    "iam":          step_iam,
    "genai_app":    step_genai_app,
    "genai_deploy": step_genai_deploy,
}

INFRA_STEPS = ["oauth", "iam", "genai_app", "genai_deploy"]


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description="OCI MCP Server — full deploy automation (build → push → infra)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps (--step):
  all           Docker build+push + all infra steps  (default)
  docker        Docker build+push only
  validate      Config + OCI connectivity check only
  oauth         Identity Domain OAuth app
  iam           IAM dynamic group + policy
  genai_app     GenAI Hosted Application
  genai_deploy  GenAI Hosted Deployment (container)

Examples:
  python deploy.py                              # full deploy
  python deploy.py --skip-docker               # infra only (image already in OCIR)
  python deploy.py --image-only                # build+push, stop before infra
  python deploy.py --step genai_deploy         # re-deploy container only
  python deploy.py --step docker               # alias for --image-only
  python deploy.py --add-artifact              # push updated image + update deployment
  python deploy.py --add-artifact --skip-docker \\
      --deployment-id ocid1.xxx --tag v2       # update deployment, no new build


Resume / recovery:
  python deploy.py --status                    # show which steps completed
  python deploy.py                             # re-run: skips completed steps automatically
  python deploy.py --force-step oauth          # force re-run one step even if complete
  python deploy.py --reset-step genai_deploy   # clear one step's completion flag
  python deploy.py --reset                     # clear all completion flags
After deploy:
  python get_token.py --test
        """,
    )

    parser.add_argument(
        "--config", default="deploy_config.yaml", metavar="FILE",
        help="Path to deploy_config.yaml  (default: deploy_config.yaml)",
    )
    parser.add_argument(
        "--step", default="all",
        metavar="STEP",
        help="Step to run: all | docker | validate | oauth | iam | genai_app | genai_deploy",
    )

    # Docker control
    docker_grp = parser.add_argument_group("Docker options")
    docker_grp.add_argument(
        "--skip-docker", action="store_true",
        help="Skip docker build+push (image must already be in OCIR)",
    )
    docker_grp.add_argument(
        "--image-only", action="store_true",
        help="Build+push image only — do not run any infra steps",
    )
    docker_grp.add_argument(
        "--skip-login", action="store_true",
        help="Skip 'docker login' to OCIR (already logged in this session)",
    )

    # Add-artifact shortcut
    art_grp = parser.add_argument_group("Add artifact (update existing deployment)")
    art_grp.add_argument(
        "--add-artifact", action="store_true",
        help="Push new image (unless --skip-docker) and update an existing deployment",
    )
    art_grp.add_argument(
        "--deployment-id", default="", metavar="OCID",
        help="Deployment OCID to update (reads deploy_output.json if omitted)",
    )
    art_grp.add_argument(
        "--image", default="", metavar="REGISTRY/NS/REPO",
        help="Override container image path (reads deploy_config.yaml if omitted)",
    )
    art_grp.add_argument(
        "--tag", default="", metavar="TAG",
        help="Override container image tag (reads deploy_config.yaml if omitted)",
    )

    # Resume tracking
    resume_grp = parser.add_argument_group("Resume tracking")
    resume_grp.add_argument(
        "--status", action="store_true",
        help="Show which steps have completed then exit",
    )
    resume_grp.add_argument(
        "--force-step", default="", metavar="STEP",
        help="Force re-run a specific step even if already marked complete",
    )
    resume_grp.add_argument(
        "--reset", action="store_true",
        help="Clear all resume tracking in deploy_output.json then exit",
    )
    resume_grp.add_argument(
        "--reset-step", default="", metavar="STEP",
        help="Clear resume tracking for one step then exit",
    )

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # ── Quick status/reset commands (no OCI auth needed) ─────────────────────
    if args.status:
        print_status()
        return

    if args.reset:
        reset_all_steps()
        ok("Resume tracking cleared — all steps will re-run on next deploy.")
        return

    if args.reset_step:
        if args.reset_step not in STEPS:
            parser.error(f"Unknown step '{args.reset_step}'. Valid: {sorted(STEPS)}")
        reset_step(args.reset_step)
        ok(f"Step '{args.reset_step}' cleared — it will re-run on next deploy.")
        return

    if not Path(args.config).exists():
        parser.error(
            f"Config file '{args.config}' not found.\n"
            "  Copy and edit the template before running:\n"
            "    cp deploy_config.yaml.example deploy_config.yaml"
        )

    _require_deps()
    cfg     = load_config(args.config)
    oci_cfg = step_validate(cfg)

    # ── --add-artifact shortcut ───────────────────────────────────────────────
    if args.add_artifact:
        print()
        print("━" * 58)
        print(" OCI MCP Server — Add Artifact to Existing Deployment")
        print("━" * 58)

        if not args.skip_docker:
            step_docker(cfg, oci_cfg, skip_login=args.skip_login)

        step_add_artifact(
            cfg, oci_cfg,
            deployment_id=args.deployment_id,
            image=args.image,
            tag=args.tag,
        )
        ok("Done — artifact updated.")
        return

    # ── --image-only or --step docker: build+push then stop ──────────────────
    if args.image_only or args.step == "docker":
        step_docker(cfg, oci_cfg, skip_login=args.skip_login)
        mark_complete("docker")
        print()
        info(
            "Image pushed. To create a new deployment run:\n"
            "    python deploy.py --skip-docker --step genai_deploy"
        )
        return

    # ── Resolve steps list ────────────────────────────────────────────────────
    if args.step == "all":
        run_steps = ([] if args.skip_docker else ["docker"]) + INFRA_STEPS
    else:
        if args.step not in STEPS:
            parser.error(f"Unknown step '{args.step}'. Valid: {sorted(STEPS)}")
        run_steps = [args.step]

    # Determine which step to force re-run regardless of completion status
    force = args.force_step  # e.g. "oauth"

    # ── Execute steps ─────────────────────────────────────────────────────────
    print()
    print("━" * 58)
    print(" OCI MCP Server — GenAI Hosted Deployment")
    print("━" * 58)
    print_status()

    for step_name in run_steps:
        fn = STEPS[step_name]
        if fn is step_validate:
            continue   # already ran above

        # Skip if already complete — unless this is the forced step or an explicit single step
        if args.step == "all" and step_name != force and is_complete(step_name):
            skip(f"Skipping '{step_name}' — already completed  (--force-step {step_name} to re-run)")
            continue

        try:
            if fn is step_docker:
                fn(cfg, oci_cfg, skip_login=args.skip_login)
            else:
                fn(cfg, oci_cfg)
            mark_complete(step_name)
        except oci.exceptions.ServiceError as e:
            err(f"OCI API error in step '{step_name}': {e.status} {e.code} — {e.message}")

    print_summary(cfg)
    print_status()


if __name__ == "__main__":
    main()
