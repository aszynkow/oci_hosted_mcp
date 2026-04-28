#!/usr/bin/env python3
"""
destroy.py — OCI MCP Server: full teardown (reverse of deploy.py).

Reads deploy_output.json and deploy_config.yaml to find and delete all
resources created by deploy.py, in reverse dependency order:

  1. GenAI Hosted Deployment
  2. GenAI Hosted Application
  3. IAM Policy
  4. IAM Dynamic Group          (skipped if existing_dynamic_group set in config)
  5. Identity Domain OAuth App
  6. OCIR image + repository    (only with --delete-image)

Usage:
    python destroy.py [--config deploy_config.yaml] [options]

    python destroy.py                      # dry-run: show what would be deleted
    python destroy.py --confirm            # actually delete everything
    python destroy.py --confirm --delete-image   # also delete OCIR repo + image
    python destroy.py --step genai_deploy  # delete one resource only (dry-run)
    python destroy.py --step genai_deploy --confirm   # delete one resource
"""

import argparse
import json
import sys
import time
from pathlib import Path

# ── Lazy imports (same pattern as deploy.py — --help works without deps) ──────

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

oci = requests = yaml = None

# ── Output file — same location as deploy.py ─────────────────────────────────

OUTPUT_FILE = str(Path(__file__).parent / "deploy_output.json")

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def load_output(path=OUTPUT_FILE) -> dict:
    if Path(path).exists():
        with open(path) as f:
            return json.load(f)
    return {}

def save_output(data: dict, path=OUTPUT_FILE):
    existing = load_output(path)
    existing.update(data)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)

def clear_output_key(key: str, path=OUTPUT_FILE):
    """Remove a single key from deploy_output.json after successful deletion."""
    existing = load_output(path)
    existing.pop(key, None)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)

def ok(msg):      print(f"  \033[92m✓\033[0m {msg}")
def err(msg):     print(f"  \033[91m✗\033[0m {msg}"); sys.exit(1)
def info(msg):    print(f"  \033[96m→\033[0m {msg}")
def warn(msg):    print(f"  \033[93m⚠\033[0m {msg}")
def dry(msg):     print(f"  \033[90m~\033[0m [DRY RUN] {msg}")
def section(msg): print(f"\n\033[1m{'─'*60}\033[0m\n\033[1m{msg}\033[0m\n{'─'*60}")

def wait_active_to_deleted(poll_fn, resource_id: str, label: str, timeout=300):
    """Poll until resource reaches DELETED/FAILED or disappears (404)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resource = poll_fn(resource_id)
            state    = getattr(resource.data, "lifecycle_state", "UNKNOWN")
            info(f"  {label} state: {state}")
            if state in ("DELETED", "FAILED"):
                return
        except oci.exceptions.ServiceError as e:
            if e.status == 404:
                return  # gone
            raise
        time.sleep(10)
    warn(f"{label} did not reach DELETED state within {timeout}s — continuing anyway")

def _make_signer(oci_cfg: dict):
    from oci.signer import Signer
    return Signer(
        tenancy=oci_cfg["tenancy"],
        user=oci_cfg["user"],
        fingerprint=oci_cfg["fingerprint"],
        private_key_file_location=oci_cfg["key_file"],
    )

def _idcs_request(method: str, path: str, domain_url: str, oci_cfg: dict, body=None):
    """Signed request to Identity Domain REST API."""
    from oci.signer import Signer
    url  = f"{domain_url.rstrip('/')}{path}"
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    sgn  = Signer(
        tenancy=oci_cfg.get("tenancy", ""),
        user=oci_cfg.get("user", ""),
        fingerprint=oci_cfg.get("fingerprint", ""),
        private_key_file_location=oci_cfg.get("key_file", ""),
    )
    resp = requests.request(method, url, json=body, headers=hdrs, auth=sgn)
    # 404 on DELETE is fine — already gone
    if not resp.ok and not (method == "DELETE" and resp.status_code == 404):
        warn(f"IDCS {method} {path} → {resp.status_code}: {resp.text}")
        return None
    return resp


# ── Destroy steps (reverse order) ─────────────────────────────────────────────

def destroy_genai_deployment(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 1: Delete GenAI Hosted Deployment")

    dep_id = out.get("genai_deployment_id", "")
    if not dep_id:
        warn("No genai_deployment_id in deploy_output.json — skipping")
        return

    info(f"Deployment OCID: {dep_id[:50]}...")
    if not confirm:
        dry(f"Would delete GenAI Hosted Deployment: {dep_id[:50]}...")
        return

    region     = cfg["oci"]["region"]
    genai_cfg  = dict(oci_cfg)
    genai_cfg["region"] = region
    genai_ep   = f"https://generativeai.{region}.oci.oraclecloud.com"
    genai      = oci.generative_ai.GenerativeAiClient(genai_cfg, service_endpoint=genai_ep)

    app_id = out.get("genai_app_id", "")

    try:
        # OCI won't delete an active deployment — must deactivate it on the
        # application first. Try SDK update, then REST PUT, then REST action endpoint.
        if app_id:
            info("Deactivating deployment on application (trying SDK update)...")
            deactivated = False

            # Attempt 1: SDK UpdateHostedApplication with activeDeploymentId=None
            try:
                genai.update_hosted_application(
                    hosted_application_id=app_id,
                    update_hosted_application_details=oci.generative_ai.models.UpdateHostedApplicationDetails(
                        active_deployment_id=""
                    ),
                )
                ok("Active deployment cleared via SDK")
                time.sleep(5)
                deactivated = True
            except Exception as sdk_err:
                warn(f"SDK update failed ({sdk_err}) — trying REST PUT...")

            # Attempt 2: GET full app object then PUT back with activeDeploymentId nulled
            if not deactivated:
                for api_ver in ("20251112", "20231130"):
                    signer  = _make_signer(oci_cfg)
                    app_url = (
                        f"https://generativeai.{region}.oci.oraclecloud.com"
                        f"/{api_ver}/hostedApplications/{app_id}"
                    )
                    # GET current state
                    get_resp = requests.get(
                        app_url, auth=signer,
                        headers={"Accept": "application/json"},
                    )
                    if not get_resp.ok:
                        warn(f"GET app {api_ver} failed ({get_resp.status_code}) — trying next version")
                        continue

                    app_body = get_resp.json()
                    info(f"Current activeDeploymentId: {app_body.get('activeDeploymentId', 'NOT SET')}")

                    # Null out the active deployment and PUT back full object
                    app_body["activeDeploymentId"] = None
                    put_resp = requests.put(
                        app_url,
                        json=app_body,
                        auth=signer,
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                    if put_resp.ok:
                        ok(f"Active deployment cleared via full PUT ({api_ver})")
                        info("Waiting 10s for state propagation...")
                        time.sleep(10)
                        deactivated = True
                        break
                    else:
                        warn(f"Full PUT {api_ver} failed ({put_resp.status_code}: {put_resp.text[:200]})")

            # Attempt 3: dedicated deactivate action endpoint
            if not deactivated:
                for api_ver in ("20251112", "20231130"):
                    signer  = _make_signer(oci_cfg)
                    act_url = (
                        f"https://generativeai.{region}.oci.oraclecloud.com"
                        f"/{api_ver}/hostedDeployments/{dep_id}/actions/deactivate"
                    )
                    resp = requests.post(
                        act_url, json={}, auth=signer,
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                    )
                    if resp.ok:
                        ok(f"Deployment deactivated via action endpoint ({api_ver})")
                        time.sleep(5)
                        deactivated = True
                        break
                    else:
                        warn(f"Action endpoint {api_ver} failed ({resp.status_code}: {resp.text[:120]})")

            if not deactivated:
                warn("All deactivation attempts failed — attempting delete anyway")

        # Try delete — retry once on 403 then fall through to app deletion
        deleted = False
        for attempt in range(2):
            try:
                genai.delete_hosted_deployment(hosted_deployment_id=dep_id)
                info("Waiting for deployment deletion...")
                wait_active_to_deleted(
                    lambda d: genai.get_hosted_deployment(hosted_deployment_id=d),
                    dep_id, "Deployment"
                )
                ok(f"GenAI Deployment deleted: {dep_id[:50]}...")
                clear_output_key("genai_deployment_id")
                deleted = True
                break
            except oci.exceptions.ServiceError as e:
                if e.status == 404:
                    ok("Deployment already deleted (404)")
                    clear_output_key("genai_deployment_id")
                    deleted = True
                    break
                elif e.status == 403 and attempt == 0:
                    warn(f"Delete blocked (403) — retrying in 15s...")
                    time.sleep(15)
                else:
                    warn(
                        f"Could not delete deployment ({e.status}) — "
                        f"skipping to application deletion which should cascade"
                    )
                    break

        if not deleted:
            warn("Deployment deletion skipped — application deletion will follow and should cascade")

    except Exception as e:
        warn(f"Unexpected error in deployment step: {e}")


def destroy_genai_app(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 2: Delete GenAI Hosted Application")

    app_id = out.get("genai_app_id", "")
    if not app_id:
        warn("No genai_app_id in deploy_output.json — skipping")
        return

    info(f"Application OCID: {app_id[:50]}...")
    if not confirm:
        dry(f"Would delete GenAI Hosted Application: {app_id[:50]}...")
        return

    region     = cfg["oci"]["region"]
    genai_cfg  = dict(oci_cfg)
    genai_cfg["region"] = region
    genai_ep   = f"https://generativeai.{region}.oci.oraclecloud.com"
    genai      = oci.generative_ai.GenerativeAiClient(genai_cfg, service_endpoint=genai_ep)

    try:
        genai.delete_hosted_application(hosted_application_id=app_id)
        info("Waiting for application deletion...")
        wait_active_to_deleted(
            lambda a: genai.get_hosted_application(hosted_application_id=a),
            app_id, "Application"
        )
        ok(f"GenAI Application deleted: {app_id[:50]}...")
        clear_output_key("genai_app_id")
        clear_output_key("endpoint_url")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            ok("Application already deleted (404)")
            clear_output_key("genai_app_id")
            clear_output_key("endpoint_url")
        else:
            err(f"Failed to delete application: {e.status} {e.code} — {e.message}")


def destroy_iam_policy(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 3: Delete IAM Policy")

    policy_name = out.get("iam_policy", "oci-mcp-genai-policy")
    tenancy_id  = cfg["oci"]["tenancy_id"]
    identity    = oci.identity.IdentityClient(oci_cfg)

    policies = identity.list_policies(compartment_id=tenancy_id, name=policy_name).data
    if not policies:
        warn(f"Policy '{policy_name}' not found — skipping")
        return

    policy_id = policies[0].id
    info(f"Policy: {policy_name} ({policy_id[:40]}...)")
    if not confirm:
        dry(f"Would delete IAM policy: {policy_name}")
        return

    try:
        identity.delete_policy(policy_id=policy_id)
        ok(f"IAM policy deleted: {policy_name}")
        clear_output_key("iam_policy")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            ok("Policy already deleted (404)")
            clear_output_key("iam_policy")
        else:
            err(f"Failed to delete policy: {e.status} {e.code} — {e.message}")


def destroy_iam_dynamic_group(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 4: Delete IAM Dynamic Group")

    # Skip if the DG was pre-existing (not created by deploy.py)
    existing_dg = cfg.get("iam", {}).get("existing_dynamic_group", "").strip()
    if existing_dg:
        warn(
            f"Dynamic group '{existing_dg}' is set as existing_dynamic_group in config "
            f"— skipping deletion to avoid removing a shared resource"
        )
        return

    dg_name    = out.get("dynamic_group", "oci-mcp-genai-dg")
    tenancy_id = cfg["oci"]["tenancy_id"]
    identity   = oci.identity.IdentityClient(oci_cfg)

    dgs = identity.list_dynamic_groups(tenancy_id, name=dg_name).data
    if not dgs:
        warn(f"Dynamic group '{dg_name}' not found — skipping")
        return

    dg_id = dgs[0].id
    info(f"Dynamic group: {dg_name} ({dg_id[:40]}...)")
    if not confirm:
        dry(f"Would delete IAM dynamic group: {dg_name}")
        return

    try:
        identity.delete_dynamic_group(dynamic_group_id=dg_id)
        ok(f"Dynamic group deleted: {dg_name}")
        clear_output_key("dynamic_group")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            ok("Dynamic group already deleted (404)")
            clear_output_key("dynamic_group")
        else:
            err(f"Failed to delete dynamic group: {e.status} {e.code} — {e.message}")


def destroy_oauth_app(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 5: Delete Identity Domain OAuth App")

    app_id     = out.get("idcs_app_id", "")
    domain_url = out.get("domain_url", cfg["identity_domain"]["url"])
    app_name   = cfg["oauth"]["app_name"]

    if not app_id:
        # Try to look it up by name
        info(f"No idcs_app_id in deploy_output.json — looking up '{app_name}' by name...")
        resp = _idcs_request(
            "GET",
            f'/admin/v1/Apps?filter=displayName+eq+"{app_name}"',
            domain_url, oci_cfg,
        )
        if resp:
            resources = resp.json().get("Resources", [])
            if resources:
                app_id = resources[0]["id"]
                info(f"Found app id: {app_id}")
            else:
                warn(f"OAuth app '{app_name}' not found in Identity Domain — skipping")
                return
        else:
            warn("Could not reach Identity Domain — skipping OAuth app deletion")
            return

    info(f"OAuth App: {app_name} (id={app_id})")
    if not confirm:
        dry(f"Would deactivate + delete Identity Domain OAuth app: {app_name}")
        return

    # Must deactivate before delete
    info("Deactivating app...")
    _idcs_request(
        "PUT",
        f"/admin/v1/AppStatusChanger/{app_id}",
        domain_url, oci_cfg,
        body={
            "schemas": ["urn:ietf:params:scim:schemas:oracle:idcs:AppStatusChanger"],
            "active":  False,
            "id":      app_id,
        },
    )

    info("Deleting app...")
    resp = _idcs_request("DELETE", f"/admin/v1/Apps/{app_id}", domain_url, oci_cfg)
    if resp is not None:
        ok(f"OAuth app deleted: {app_name}")
        for key in ("idcs_app_id", "client_id", "client_secret", "domain_url",
                    "audience", "scope", "full_scope"):
            clear_output_key(key)
    else:
        warn("OAuth app deletion may have failed — check Identity Domain console")


def destroy_ocir_image(cfg: dict, oci_cfg: dict, out: dict, confirm: bool):
    section("Step 6: Delete OCIR Repository + Image")

    c        = cfg["container"]
    registry = c["registry"]
    ns       = c["tenancy_namespace"]
    repo     = c["repository"]
    tag      = c["tag"]
    comp_id  = cfg["oci"]["compartment_id"]

    short    = registry.split(".")[0]
    from deploy import OCIR_REGION_MAP
    region   = OCIR_REGION_MAP.get(short, cfg["oci"]["region"])

    artifacts = oci.artifacts.ArtifactsClient(
        dict(oci_cfg, region=region)
    )

    info(f"OCIR repository: {ns}/{repo}")
    info(f"Image tag: {tag}")

    if not confirm:
        dry(f"Would delete OCIR repository '{repo}' and all its images in namespace '{ns}'")
        return

    # Find the repository
    try:
        repos = artifacts.list_container_repositories(
            compartment_id=comp_id,
            display_name=repo,
        ).data.items
    except oci.exceptions.ServiceError as e:
        warn(f"Could not list OCIR repositories: {e.message} — skipping")
        return

    if not repos:
        warn(f"OCIR repository '{repo}' not found — skipping")
        return

    repo_id = repos[0].id
    info(f"Repository OCID: {repo_id[:50]}...")

    # Delete all images first
    try:
        images = artifacts.list_container_images(
            compartment_id=comp_id,
            repository_id=repo_id,
        ).data.items
        for img in images:
            info(f"Deleting image: {img.display_name}")
            artifacts.delete_container_image(container_image_id=img.id)
            ok(f"  Image deleted: {img.display_name}")
    except oci.exceptions.ServiceError as e:
        warn(f"Error deleting images: {e.message} — attempting repo deletion anyway")

    # Delete the repository
    try:
        artifacts.delete_container_repository(container_repository_id=repo_id)
        ok(f"OCIR repository deleted: {repo}")
    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            ok("Repository already deleted (404)")
        else:
            err(f"Failed to delete repository: {e.status} {e.code} — {e.message}")


# ── Steps registry ────────────────────────────────────────────────────────────

DESTROY_STEPS = {
    "genai_deploy": destroy_genai_deployment,
    "genai_app":    destroy_genai_app,
    "iam_policy":   destroy_iam_policy,
    "iam_dg":       destroy_iam_dynamic_group,
    "oauth":        destroy_oauth_app,
    "ocir":         destroy_ocir_image,
}

# Ordered for full teardown (reverse of deploy)
ALL_STEPS = ["genai_deploy", "genai_app", "iam_policy", "iam_dg", "oauth"]


# ── Summary ───────────────────────────────────────────────────────────────────

def print_plan(out: dict, cfg: dict, delete_image: bool):
    """Show what will be deleted before asking for confirmation."""
    section("Destruction Plan")

    rows = [
        ("GenAI Deployment",  out.get("genai_deployment_id", "NOT FOUND")[:60]),
        ("GenAI Application", out.get("genai_app_id",        "NOT FOUND")[:60]),
        ("IAM Policy",        out.get("iam_policy",          "NOT FOUND")),
        ("IAM Dynamic Group", out.get("dynamic_group",       "NOT FOUND")
                              + (" [SKIP — pre-existing]"
                                 if cfg.get("iam", {}).get("existing_dynamic_group", "").strip()
                                 else "")),
        ("OAuth App",         out.get("idcs_app_id",         "NOT FOUND")[:60]),
    ]
    if delete_image:
        c = cfg["container"]
        rows.append(("OCIR Repository", f"{c['tenancy_namespace']}/{c['repository']}"))

    for label, value in rows:
        colour = "\033[91m" if "NOT FOUND" in value else "\033[93m"
        print(f"  {colour}▸\033[0m  {label:<22} {value}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="destroy.py",
        description="OCI MCP Server — full teardown (reverse of deploy.py)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Steps (--step):
  genai_deploy  Delete GenAI Hosted Deployment
  genai_app     Delete GenAI Hosted Application
  iam_policy    Delete IAM Policy
  iam_dg        Delete IAM Dynamic Group  (skipped if pre-existing)
  oauth         Delete Identity Domain OAuth App
  ocir          Delete OCIR repository + all images  (requires --delete-image)

Examples:
  python destroy.py                          # dry-run: show what would be deleted
  python destroy.py --confirm                # delete all resources
  python destroy.py --confirm --delete-image # delete all resources + OCIR repo
  python destroy.py --step genai_deploy      # dry-run single step
  python destroy.py --step genai_deploy --confirm   # delete single resource

Notes:
  - Default is DRY RUN — nothing is deleted without --confirm
  - Resources are deleted in reverse dependency order
  - Each deleted resource is removed from deploy_output.json
  - Dynamic group is skipped if existing_dynamic_group is set in config
  - OCIR repository deletion requires explicit --delete-image flag
        """,
    )

    parser.add_argument(
        "--config", default="deploy_config.yaml", metavar="FILE",
        help="Path to deploy_config.yaml  (default: deploy_config.yaml)",
    )
    parser.add_argument(
        "--step", default="all", metavar="STEP",
        help="Single step to destroy: " + " | ".join(DESTROY_STEPS),
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually delete resources (default is dry-run)",
    )
    parser.add_argument(
        "--delete-image", action="store_true",
        help="Also delete the OCIR repository and all its images",
    )

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if not Path(args.config).exists():
        parser.error(
            f"Config file '{args.config}' not found.\n"
            "  destroy.py must be run from the same folder as deploy_config.yaml"
        )

    _require_deps()

    cfg     = load_config(args.config)
    out     = load_output()
    oci_cfg = oci.config.from_file(profile_name=cfg["oci"]["profile"])
    oci.config.validate_config(oci_cfg)
    ok(f"OCI profile '{cfg['oci']['profile']}' loaded")

    if not out:
        err(
            "deploy_output.json is empty or missing.\n"
            "  Nothing to destroy — deploy.py has not been run yet."
        )

    # Resolve steps
    if args.step == "all":
        run_steps = ALL_STEPS + (["ocir"] if args.delete_image else [])
    else:
        if args.step not in DESTROY_STEPS:
            parser.error(f"Unknown step '{args.step}'. Valid: {sorted(DESTROY_STEPS)}")
        if args.step == "ocir" and not args.delete_image:
            parser.error("--step ocir requires --delete-image flag")
        run_steps = [args.step]

    # Show plan
    print_plan(out, cfg, args.delete_image and args.step == "all")

    if not args.confirm:
        print("  \033[93m⚠\033[0m  DRY RUN — nothing will be deleted.")
        print("       Add --confirm to actually delete these resources.")
        print()

    # Confirm prompt for full destroy
    if args.confirm and args.step == "all":
        print("  \033[91m⚠  This will permanently delete all resources listed above.\033[0m")
        answer = input("  Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("  Aborted.")
            sys.exit(0)
        print()

    # Execute
    for step_name in run_steps:
        fn = DESTROY_STEPS[step_name]
        try:
            fn(cfg, oci_cfg, out, confirm=args.confirm)
            # Reload out after each step (keys get cleared as resources are deleted)
            out = load_output()
        except oci.exceptions.ServiceError as e:
            err(f"OCI API error in step '{step_name}': {e.status} {e.code} — {e.message}")

    print()
    if args.confirm:
        ok("Teardown complete.")
        if args.step == "all":
            # Clear completed_steps so deploy.py starts fresh
            save_output({"completed_steps": []})
            ok("deploy_output.json reset — deploy.py will re-run all steps fresh.")
    else:
        print("  Run with --confirm to execute the above deletions.")
    print()


if __name__ == "__main__":
    main()
