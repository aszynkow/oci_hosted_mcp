# ── Stage 1: builder ──────────────────────────────────────────────────────────
# python:3.12-slim is used here purely for its build toolchain convenience.
# The builder stage is DISCARDED after the build — it is never scanned by
# OCI VSS. Only the runtime stage (Stage 2) is present in the final image.
FROM --platform=linux/amd64 python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt \
 \
 # ── CVE-2018-20834 fix ────────────────────────────────────────────────────
 # mcp/cli/cli.py references `npx @modelcontextprotocol/inspector`.
 # OCI VSS (Trivy) resolves that npm package's dependency tree at scan time
 # and finds node-tar <4.4.2 (CVE-2018-20834, High) as a transitive dep —
 # even though npx is never called at runtime.
 # mcp.cli is not imported by mcp.server.fastmcp; deleting it is safe.
 && find /install -type d -name "cli" -path "*/mcp/cli" -exec rm -rf {} + 2>/dev/null; true \
 && find /install -type d -name "node_modules" -exec rm -rf {} + 2>/dev/null; true \
 && find /install -name "package.json" -delete 2>/dev/null; true \
 && find /install -name "package-lock.json" -delete 2>/dev/null; true


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Oracle Linux 9 slim — this is the layer OCI VSS scans.
#   • No Debian packages → eliminates coreutils CVE-2016-2781, dpkg CVE-2025-6297
#   • Oracle recommends container-registry.oracle.com images for VSS compliance.
#     Ref: https://docs.oracle.com/en-us/iaas/Content/generative-ai/prepare-artifacts.htm
#   • oraclelinux:9-slim on Docker Hub is identical, no OCIR auth needed.
FROM --platform=linux/amd64 oraclelinux:9-slim

# openssl-libs provides libssl.so.3 required by the cryptography package at runtime.
RUN microdnf install -y \
        python3.12 \
        openssl-libs \
    && microdnf clean all

LABEL maintainer="arul.ganeswaran@oracle.com" \
      description="OCI Inventory MCP Server – OCI GenAI Hosted Deployment" \
      org.opencontainers.image.title="oci-inventory-mcp" \
      org.opencontainers.image.source="https://github.com/your-org/oci-inventory-mcp"

RUN groupadd -r mcpuser \
 && useradd -r -g mcpuser -d /home/mcpuser -s /sbin/nologin mcpuser

WORKDIR /app

# Copy only the installed Python packages — build toolchain stays in builder.
COPY --from=builder /install /usr/local

COPY server.py .

# NOTE: In hosted deployment NO ~/.oci/config is available (no volume mounts).
#       Auth is via Resource Principal injected by the platform.
# NOTE: Container filesystem is READ-ONLY except /tmp.
RUN mkdir -p /home/mcpuser \
 && chown -R mcpuser:mcpuser /app /home/mcpuser

USER mcpuser

# ── Runtime environment ───────────────────────────────────────────────────────
# OCI_AUTH: resource_principal — OCI Hosted Deployment (default)
#           instance_principal — OCI VM / Container Instance
#           auto               — local dev (~/.oci/config fallback)
#
# RESERVED by OCI platform — do NOT redefine:
#   PORT, K_SERVICE, K_CONFIGURATION, K_REVISION,
#   OCI_RESOURCE_PRINCIPAL_VERSION, OCI_RESOURCE_PRINCIPAL_PRIVATE_PEM,
#   OCI_RESOURCE_PRINCIPAL_RPST, KUBERNETES_*
ENV OCI_AUTH=resource_principal \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python3.12 -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=5)" \
    || exit 1

ENTRYPOINT ["python3.12", "server.py"]
