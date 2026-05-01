"""
OCI Inventory MCP Server
Exposes OCI resource scanning as MCP tools for Claude / OCA.

Deployment targets:
  - OCI Generative AI Hosted Deployment (resource_principal auth, port 8080)
  - OCI Container Instance / VM (instance_principal auth)
  - Local dev (~/.oci/config, port 8080)
"""

import os
import json
import logging
from typing import Optional
from collections import defaultdict

import oci
from mcp.server.fastmcp import FastMCP

from starlette.routing import Route
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SERVER_VERSION = 19  # increment on every change

# Disable MCP SDK DNS-rebinding protection — required when accessed via IP or
# OCI load-balancer hostname.  The SDK's TransportSecurityMiddleware rejects
# non-hostname Host headers; this patch allows any host.
try:
    from mcp.server.transport_security import TransportSecurityMiddleware

    async def _allow_all(self, request, is_post=False):
        return None  # no error — allow any host

    TransportSecurityMiddleware.validate_request = _allow_all
except Exception:
    pass

# ── Resource type → service mapping ──────────────────────────────────────────
RESOURCE_TYPE_TO_SERVICE = {
    # Compute
    'instance': 'Compute',
    'instanceconfiguration': 'Compute',
    'instancepool': 'Compute',
    'dedicatedvmhost': 'Compute',
    'consolehistory': 'Compute',
    'bootvolume': 'Compute',
    'bootvolumebackup': 'Compute',
    'bootvolumereplica': 'Compute',
    'volume': 'Compute',
    'volumebackup': 'Compute',
    'volumebackuppolicy': 'Compute',
    'volumegroup': 'Compute',
    'volumegroupbackup': 'Compute',
    'volumegroupreplica': 'Compute',
    'volumereplica': 'Compute',
    'autonomouscontainerdatabase': 'Compute',
    'computecapacityreservation': 'Compute',
    'vmclusternetwork': 'Compute',

    # Networking
    'vcn': 'Networking',
    'subnet': 'Networking',
    'securitylist': 'Networking',
    'routetable': 'Networking',
    'internetgateway': 'Networking',
    'natgateway': 'Networking',
    'servicegateway': 'Networking',
    'drg': 'Networking',
    'drgattachment': 'Networking',
    'drgroutedistribution': 'Networking',
    'drgroutetable': 'Networking',
    'cpe': 'Networking',
    'ipsecconnection': 'Networking',
    'localpeeringgateway': 'Networking',
    'remotepeeringconnection': 'Networking',
    'virtualcircuit': 'Networking',
    'networksecuritygroup': 'Networking',
    'dhcpoptions': 'Networking',
    'dnsresolver': 'Networking',
    'dnsview': 'Networking',
    'customerdnszone': 'Networking',
    'privateip': 'Networking',
    'publicip': 'Networking',
    'publicippool': 'Networking',
    'vnic': 'Networking',
    'ipv6': 'Networking',
    'vlan': 'Networking',
    'httpredirect': 'Networking',
    'networkfirewall': 'Networking',
    'networkfirewallpolicy': 'Networking',
    'bastion': 'Networking',
    'byoiprange': 'Networking',
    'privateserviceaccess': 'Networking',

    # Object Storage
    'bucket': 'Object Storage',

    # Database
    'autonomousdatabase': 'Database',
    'autonomousdatabasebackup': 'Database',
    'dbinstance': 'Database',
    'database': 'Database',
    'dbnode': 'Database',
    'dbsystem': 'Database',
    'dbhome': 'Database',
    'pluggabledatabase': 'Database',
    'mysqldb': 'Database',
    'mysqldbbackup': 'Database',
    'mysqldbsystem': 'Database',
    'postgresqldb': 'Database',
    'postgresqldbbackup': 'Database',
    'postgresqldbsystem': 'Database',
    'rediscluster': 'Database',
    'externalcontainerdatabase': 'Database',
    'externaldatabaseconnector': 'Database',
    'externalplugabledatabase': 'Database',
    'dbcloudexecutionaction': 'Database',
    'dbcloudexecutionwindow': 'Database',
    'dbcloudscheduledaction': 'Database',
    'dbcloudschedulingplan': 'Database',
    'dbcloudschedulingpolicy': 'Database',
    'dbcloudschedulingwindow': 'Database',

    # Load Balancer
    'loadbalancer': 'Load Balancer',

    # File Storage
    'mounttarget': 'File Storage',
    'filesystem': 'File Storage',
    'export': 'File Storage',
    'fssreplicationtarget': 'File Storage',
    'fssreplication': 'File Storage',
    'filesystemsnapshotpolicy': 'File Storage',
    'protectionpolicy': 'File Storage',

    # Functions
    'function': 'Functions',
    'application': 'Functions',
    'functionsapplication': 'Functions',
    'functionsfunction': 'Functions',

    # Container Engine
    'containercluster': 'Container Engine',
    'nodepool': 'Container Engine',
    'container': 'Container Engine',
    'containerimage': 'Container Engine',
    'containerrepo': 'Container Engine',
    'containerinstance': 'Container Engine',

    # Logging
    'loggroup': 'Logging',
    'log': 'Logging',
    'loganalyticsentity': 'Logging',
    'logsavdsearch': 'Logging',
    'logsavedsearch': 'Logging',
    'unifiedagentconfiguration': 'Logging',

    # Monitoring
    'metric': 'Monitoring',
    'alarm': 'Monitoring',

    # Notifications
    'notificationtopic': 'Notifications',
    'subscription': 'Notifications',
    'onstopic': 'Notifications',
    'onssubscription': 'Notifications',

    # Key Management
    'vault': 'Key Management',
    'key': 'Key Management',
    'secret': 'Key Management',
    'vaultsecret': 'Key Management',

    # Identity
    'policy': 'Identity',
    'group': 'Identity',
    'user': 'Identity',
    'compartment': 'Identity',
    'tagnamespace': 'Identity',
    'tag': 'Identity',
    'tagdefault': 'Identity',
    'securityattributenamespace': 'Identity',
    'dynamicresourcegroup': 'Resource Manager',

    # DevOps
    'devopsproject': 'DevOps',
    'devopsrepository': 'DevOps',
    'devopsbuildpipeline': 'DevOps',
    'devopsbuildpipelinestage': 'DevOps',
    'devopsbuildrun': 'DevOps',
    'devopsconnection': 'DevOps',
    'devopsdeployartifact': 'DevOps',
    'devopsdeployenvironment': 'DevOps',
    'devopsdeploypipeline': 'DevOps',
    'devopsdeploystage': 'DevOps',
    'devopsdeployment': 'DevOps',
    'devopstrigger': 'DevOps',

    # Resource Manager
    'ormjob': 'Resource Manager',
    'ormstack': 'Resource Manager',
    'ormprivateendpoint': 'Resource Manager',
    'ormconfigsourceprovider': 'Resource Manager',
    'ormtemplate': 'Resource Manager',

    # Analytics
    'analyticsinstance': 'Analytics',

    # API Gateway
    'apigateway': 'API Gateway',
    'apideployment': 'API Gateway',
    'apigatewayapi': 'API Gateway',
    'apigatewaysubscriber': 'API Gateway',
    'apigatewayusageplan': 'API Gateway',

    # AI Services
    'aidataplatform': 'AI Services',
    'ailanguageproject': 'AI Services',
    'ailanguagemodel': 'AI Services',
    'ailanguageendpoint': 'AI Services',
    'aivisionproject': 'AI Services',
    'aivisionmodel': 'AI Services',
    'aidocumentproject': 'AI Services',
    'aidocumentmodel': 'AI Services',
    'aianomalydetectionproject': 'AI Services',
    'aianomalydetectionmodel': 'AI Services',
    'aianomalydetectiondataasset': 'AI Services',

    # Data Science
    'datascienceproject': 'Data Science',
    'datasciencenotebooksession': 'Data Science',
    'datasciencemodel': 'Data Science',
    'datasciencemodeldeployment': 'Data Science',
    'datasciencemodelversionset': 'Data Science',
    'datasciencejob': 'Data Science',
    'datasciencejobrun': 'Data Science',
    'datasciencepipeline': 'Data Science',
    'datasciencepipelinerun': 'Data Science',
    'datascienceprivateendpoint': 'Data Science',

    # Integration
    'integrationinstance': 'Integration',

    # Process Automation
    'processautomationinstance': 'Process Automation',

    # Visual Builder
    'visualbuilderinstance': 'Visual Builder',
    'vbsinstance': 'Visual Builder Studio',

    # Email Delivery
    'emaildomain': 'Email Delivery',
    'emaildkim': 'Email Delivery',
    'emailsender': 'Email Delivery',

    # Events
    'eventrule': 'Events',

    # Queues
    'queue': 'Queues',

    # Streams
    'stream': 'Streams',
    'streamcdnconfig': 'Streams',
    'streamdistributionchannel': 'Streams',
    'streampackagingconfig': 'Streams',

    # Service Connector
    'serviceconnector': 'Service Connector',

    # Data Safe
    'datasafeprivateendpoint': 'Data Safe',
    'datasafeauditpolicy': 'Data Safe',
    'datasafeauditprofile': 'Data Safe',
    'datasafeaudittrail': 'Data Safe',
    'datasafeuserassessment': 'Data Safe',
    'datasafesecurityassessment': 'Data Safe',
    'datasafetargetdatabase': 'Data Safe',
    'datasafedatabasesecurityconfig': 'Data Safe',
    'datasafeonpremconnector': 'Data Safe',
    'datasafeprivatesendpoint': 'Data Safe',
    'datasafealertpolicy': 'Data Safe',
    'datasafealertpolicylibrary': 'Data Safe',
    'datasafesqlcollection': 'Data Safe',
    'datasafesensitivetype': 'Data Safe',
    'datasafesecuritypolicymanagement': 'Data Safe',
    'datasafesecuritypolicydeployment': 'Data Safe',
    'datasafesecuritypolicy': 'Data Safe',
    'datasafereport': 'Data Safe',
    'datasafereportdefinition': 'Data Safe',
    'datasafelibrarymaskingformat': 'Data Safe',
    'datasafesensitivedatamodel': 'Data Safe',
    'datasafemaskingreport': 'Data Safe',
    'datasafemaskingpolicy': 'Data Safe',
    'datasafetargetalertpolicyassociation': 'Data Safe',
    'datasafediscoveryjob': 'Data Safe',
    'datasafemaskpolicyhealthreport': 'Data Safe',
    'datasafesdmmaskingpolicydifference': 'Data Safe',

    # VMware
    'vmwaresddc': 'VMware',
    'vmwarecluster': 'VMware',
    'vmwareesxihost': 'VMware',
    'vmwarevmasset': 'VMware',

    # Blockchain
    'blockchainplatform': 'Blockchain',

    # Digital Assistant
    'odainstance': 'Digital Assistant',
    'odaprivateendpoint': 'Digital Assistant',

    # Content Management
    'oceinstance': 'Content Management',

    # IoT
    'iotdomain': 'IoT',
    'iotdomaingroup': 'IoT',

    # Media Services
    'mediaasset': 'Media Services',
    'mediaworkflow': 'Media Services',

    # Web Application Firewall
    'waaspolicy': 'Web Application Firewall',
    'waascertificate': 'Web Application Firewall',
    'webappfirewall': 'Web Application Firewall',
    'webappfirewallpolicy': 'Web Application Firewall',
    'webappaccelerationpolicy': 'Web Application Firewall',
    'webappacceleration': 'Web Application Firewall',

    # Certificates
    'certificate': 'Certificates',
    'certificateauthority': 'Certificates',
    'certificateassociation': 'Certificates',
    'certificateauthorityassociation': 'Certificates',
    'cabundle': 'Certificates',

    # NoSQL
    'nosqltable': 'NoSQL',

    # Kafka
    'kafkacluster': 'Kafka',
    'kafkaclusterconfig': 'Kafka',

    # Golden Gate
    'goldengateconnection': 'Golden Gate',
    'goldengatedeployment': 'Golden Gate',
    'goldengatedeploymentbackup': 'Golden Gate',

    # Migration
    'migration': 'Migration',
    'migrationplan': 'Migration',
    'odmsconnection': 'Database Migration',
    'odmsjob': 'Database Migration',
    'odmsmigration': 'Database Migration',

    # Disaster Recovery
    'drplan': 'Disaster Recovery',
    'drplanexecution': 'Disaster Recovery',
    'drprotectiongroup': 'Disaster Recovery',

    # GenAI
    'genaiagent': 'Generative AI',
    'genaiagentendpoint': 'Generative AI',
    'genaiagentknowledgebase': 'Generative AI',
    'genaiagentdatasource': 'Generative AI',
    'genaiagentdataingestionjob': 'Generative AI',

    # OS Management Hub
    'osmhprofile': 'OS Management Hub',
    'osmhscheduledjob': 'OS Management Hub',
    'osmhlifecycleenvironment': 'OS Management Hub',
    'osmhmangedinstancegroup': 'OS Management Hub',
    'osmhmanagedinstancegroup': 'OS Management Hub',
    'osmhsoftwaresource': 'OS Management Hub',

    # Data Integration
    'disworkspace': 'Data Integration',

    # Data Catalog
    'datacatalog': 'Data Catalog',
    'datacatalogprivateendpoint': 'Data Catalog',

    # Data Labeling
    'datalabelingdataset': 'Data Labeling',

    # Data Lake
    'datalake': 'Data Lake',

    # Data Flow
    'dataflowapplication': 'Data Flow',
    'dataflowpool': 'Data Flow',
    'dataflowrun': 'Data Flow',

    # ADM
    'admknowledgebase': 'ADM',

    # APM
    'apmdomain': 'APM',

    # Limits
    'limitsincreaserequest': 'Limits',
    'quota': 'Limits',

    # Management Agent
    'managementagent': 'Management Agent',
    'managementagentinstallkey': 'Management Agent',

    # Management Dashboard
    'managementdashboard': 'Management Dashboard',
    'managementsavedsearch': 'Management Dashboard',

    # Console
    'consoleresourcecollection': 'Console',
    'consoledashboard': 'Console',
    'consoledashboardgroup': 'Console',
    'consoleResourceCollection': 'Console',

    # Path Analyzer
    'pathanalyzertest': 'Path Analyzer',

    # Recovery Service
    'recoveryservicesubnet': 'Recovery Service',
    'protecteddatabase': 'Protected Database',

    # ZPR
    'zprpolicy': 'ZPR',

    # App
    'app': 'App',

    # Auto Scaling
    'autoscalingconfiguration': 'Auto Scaling',

    # Clusters
    'clusterscluster': 'Clusters',

    # Desktop
    'desktoppool': 'Desktop',

    # Image
    'image': 'Image',

    # Connect Harness
    'connectharness': 'Connect Harness',

    # MySQL
    'mysqlbackup': 'MySQL',
    'mysqlconfiguration': 'MySQL',

    # PostgreSQL
    'postgresqlbackup': 'PostgreSQL',
    'postgresqlconfiguration': 'PostgreSQL',

    # Database Tools
    'databasetoolsconnection': 'Database Tools',
    'databasetoolsprivateendpoint': 'Database Tools',

    # Database Management
    'dbmgmtmanageddatabase': 'Database Management',
    'dbmgmtmanageddatabasegroup': 'Database Management',
    'dbmgmtnamedcredential': 'Database Management',
    'dbmgmtprivateendpoint': 'Database Management',
    'dbmgmtexternalcluster': 'Database Management',
    'dbmgmtexternalclusterinstance': 'Database Management',
    'dbmgmtexternaldbhome': 'Database Management',
    'dbmgmtexternaldbnode': 'Database Management',
    'dbmgmtexternaldbsystem': 'Database Management',
    'dbmgmtexternaldbsystemconnector': 'Database Management',
    'dbmgmtexternallistener': 'Database Management',
    'dbmgmtexternalmysqldb': 'Database Management',
    'dbmgmtmysqldbconnector': 'Database Management',

    # Operations Insights
    'opsidatabaseinsight': 'Operations Insights',

    # Cloud Guard
    'cloudguardtarget': 'Cloud Guard',
    'cloudguarddetectorrecipe': 'Cloud Guard',
    'cloudguardresponderrecipe': 'Cloud Guard',
    'cloudguardmanagedlist': 'Cloud Guard',

    # Security Zones
    'securityzonessecurityrecipe': 'Security Zones',
    'securityzonessecurityzone': 'Security Zones',

    # License Manager
    'licensemanagerproductlicense': 'License Manager',
    'licensemanagerlicenserecord': 'License Manager',

    # Cost Management
    'budget': 'Cost Management',

    # Organizations
    'organizationsgovernancerule': 'Organizations',

    # Batch
    'batchcontext': 'Batch',

    # Stack Monitoring
    'stackmonitoringresource': 'Stack Monitoring',

    # Vulnerability Scanning
    'vsscontainerscanrecipe': 'Vulnerability Scanning',
    'vsscontainerscantarget': 'Vulnerability Scanning',
    'vsshostscanrecipe': 'Vulnerability Scanning',
    'vsshostscantarget': 'Vulnerability Scanning',

    # Resource Analytics
    'resanalyticsinstance': 'Resource Analytics',

    # Cloud Compute Capacity
    'cccinfrastructure': 'Cloud Compute Capacity',

    # Exadata Database
    'exadbvmcluster': 'Exadata Database',
    'exascaledbstoragevault': 'Exadata Database',
    'cloudautonomousvmcluster': 'Exadata Database',
    'cloudexadatainfrastructure': 'Exadata Database',
    'cloudvmcluster': 'Exadata Database',
    'dbserver': 'Exadata Database',

    # Resource Scheduler
    'resourceschedule': 'Resource Scheduler',

    # Access Governance
    'agcsgovernanceinstance': 'Access Governance',

    # Oracle Cloud Bridge
    'ocbagent': 'Oracle Cloud Bridge',
    'ocbagentdependency': 'Oracle Cloud Bridge',
    'ocbassetsource': 'Oracle Cloud Bridge',
    'ocbdiscoveryschedule': 'Oracle Cloud Bridge',
    'ocbenvironment': 'Oracle Cloud Bridge',
    'ocbinventory': 'Oracle Cloud Bridge',
    'ocbvmwarevmasset': 'Oracle Cloud Bridge',
}


# ── OCI client factory ────────────────────────────────────────────────────────

def _make_config():
    """
    Auth priority:
    1. Resource Principal  -- OCI GenAI Hosted Deployments (OCI_AUTH=resource_principal)
                             OCI_RESOURCE_PRINCIPAL_* env vars are injected by the platform.
    2. Instance Principal  -- VM on OCI (OCI_AUTH=instance_principal or auto-detected)
    3. ~/.oci/config       -- local dev fallback only (not available in hosted deployment)
    """
    auth_mode = os.getenv("OCI_AUTH", "resource_principal").lower()

    if auth_mode == "resource_principal":
        signer = oci.auth.signers.get_resource_principals_signer()
        log.info("Auth: Resource Principal")
        return {'region': _resolve_region(signer)}, signer

    if auth_mode == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        log.info("Auth: Instance Principal")
        return None, signer

    # auto: try Resource Principal -> Instance Principal -> config file
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        log.info("Auth: Resource Principal (auto)")
        return {'region': _resolve_region(signer)}, signer
    except Exception:
        pass
    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        log.info("Auth: Instance Principal (auto)")
        return None, signer
    except Exception:
        pass

    log.info("Auth: ~/.oci/config (local dev only)")
    config = oci.config.from_file(os.path.expanduser("~/.oci/config"))
    return config, None


def _resolve_region(signer) -> str:
    """
    Region resolution priority:
    1. OCI_RESOURCE_PRINCIPAL_REGION env var — explicit override
    2. Tenancy home region — looked up via Identity API using deployment region as bootstrap
    3. signer.region (deployment region) — last resort fallback
    """
    explicit = os.getenv('OCI_RESOURCE_PRINCIPAL_REGION', '').strip()
    if explicit:
        log.info(f"Region: OCI_RESOURCE_PRINCIPAL_REGION={explicit}")
        return explicit

    deployment_region = getattr(signer, 'region', None) or ''
    try:
        bootstrap = oci.identity.IdentityClient(
            config={'region': deployment_region}, signer=signer
        )
        subscriptions = bootstrap.list_region_subscriptions(signer.tenancy_id).data
        for sub in subscriptions:
            if sub.is_home_region:
                log.info(f"Region: home region resolved as {sub.region_name}")
                return sub.region_name
    except Exception as e:
        log.warning(f"Region: home region lookup failed, falling back to deployment region '{deployment_region}': {e}")

    return deployment_region


def _identity_client(config, signer):
    if signer:
        return oci.identity.IdentityClient(config=config or {}, signer=signer)
    return oci.identity.IdentityClient(config)


def _search_client(config, signer, region: str):
    if signer:
        c = oci.resource_search.ResourceSearchClient(config={}, signer=signer)
        c.base_client.set_region(region)
        return c
    cfg = dict(config)
    cfg["region"] = region
    return oci.resource_search.ResourceSearchClient(cfg)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_compartments(identity, tenancy_id: str) -> dict:
    """Return {compartment_id: name} for entire tenancy."""
    comp_map = {}

    def _recurse(parent_id):
        try:
            resp = identity.list_compartments(parent_id, lifecycle_state="ACTIVE")
            for c in resp.data:
                comp_map[c.id] = c.name
                _recurse(c.id)
        except Exception as e:
            log.warning(f"list_compartments error under {parent_id}: {e}")

    tenancy = identity.get_tenancy(tenancy_id).data
    comp_map[tenancy_id] = tenancy.name
    _recurse(tenancy_id)
    return comp_map


def _get_subscribed_regions(identity, tenancy_id: str) -> list[str]:
    return [r.region_name for r in identity.list_region_subscriptions(tenancy_id).data]


def _scan_region(config, signer, region: str, comp_map: dict) -> list[dict]:
    """Return list of resource dicts for one region."""
    search = _search_client(config, signer, region)
    details = oci.resource_search.models.StructuredSearchDetails(
        query="query all resources",
        type="Structured",
        matching_context_type="NONE",
    )
    items = []
    try:
        page = None
        while True:
            resp = search.search_resources(details, page=page) if page else search.search_resources(details)
            for r in resp.data.items:
                service = RESOURCE_TYPE_TO_SERVICE.get(r.resource_type.lower(), "Unknown")
                items.append({
                    "region": region,
                    "service": service,
                    "compartment": comp_map.get(r.compartment_id, "Unknown"),
                    "resource_type": r.resource_type,
                    "resource_name": r.display_name or "",
                    "resource_id": r.identifier or "",
                    "lifecycle_state": r.lifecycle_state or "",
                })
            page = resp.next_page
            if not page:
                break
    except Exception as e:
        log.error(f"scan_region {region} error: {e}")
    return items


# ── MCP Server ────────────────────────────────────────────────────────────────

# streamable_http_path="/" — endpoint at root, client URL needs no /mcp suffix
# stateless_http=True      — no session-ID handshake; each POST is independent,
#                            fixes "Internal Server Error" on bare tools/list calls
# Both passed as constructor kwargs (settings object is immutable after construction)
mcp = FastMCP("OCI Inventory", streamable_http_path="/", stateless_http=True)

# Disable SSE keepalive pings — mcp-remote cannot handle SSE comment lines
# (": ping - N") and crashes with SyntaxError when they arrive mid-stream.
# FastMCP older versions don't accept ping_interval in the constructor so we
# patch the session manager's ping interval directly after instantiation.
try:
    mcp._session_manager.ping_interval = None
except Exception:
    pass  # safe to ignore — pings are cosmetic, not functional



@mcp.tool()
def list_subscribed_regions() -> str:
    """List all regions subscribed in the OCI tenancy."""
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])
    regions = _get_subscribed_regions(identity, tenancy_id)
    return json.dumps({"regions": regions, "count": len(regions)}, indent=2)


@mcp.tool()
def scan_region(region: str) -> str:
    """
    Scan a single OCI region and return all resources.

    Args:
        region: OCI region identifier, e.g. 'ap-sydney-1'
    """
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])
    comp_map = _get_compartments(identity, tenancy_id)
    items = _scan_region(config, signer, region, comp_map)

    by_service = defaultdict(list)
    for r in items:
        by_service[r["service"]].append(r)

    summary = {svc: len(rs) for svc, rs in sorted(by_service.items())}
    return json.dumps({
        "region": region,
        "total_resources": len(items),
        "by_service": summary,
        "resources": items,
    }, indent=2)


@mcp.tool()
def scan_tenancy(regions: Optional[str] = None) -> str:
    """
    Scan all (or specified) OCI regions and return a summary of resources.

    Args:
        regions: Optional comma-separated list of regions to scan.
                 Defaults to all subscribed regions.
    """
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])

    all_regions = _get_subscribed_regions(identity, tenancy_id)
    target_regions = (
        [r.strip() for r in regions.split(",")]
        if regions
        else all_regions
    )

    comp_map = _get_compartments(identity, tenancy_id)
    all_items = []
    region_summaries = {}

    for region in target_regions:
        log.info(f"Scanning {region}...")
        items = _scan_region(config, signer, region, comp_map)
        all_items.extend(items)

        by_service = defaultdict(int)
        for r in items:
            by_service[r["service"]] += 1
        region_summaries[region] = {
            "total": len(items),
            "by_service": dict(sorted(by_service.items())),
        }

    total_by_service = defaultdict(int)
    for r in all_items:
        total_by_service[r["service"]] += 1

    return json.dumps({
        "regions_scanned": target_regions,
        "total_resources": len(all_items),
        "by_service_totals": dict(sorted(total_by_service.items())),
        "by_region": region_summaries,
        "resources": all_items,
    }, indent=2)


@mcp.tool()
def list_compartments() -> str:
    """List all compartments in the tenancy."""
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])
    comp_map = _get_compartments(identity, tenancy_id)
    return json.dumps({
        "compartment_count": len(comp_map),
        "compartments": [{"id": k, "name": v} for k, v in sorted(comp_map.items(), key=lambda x: x[1])],
    }, indent=2)


@mcp.tool()
def get_services_summary(regions: Optional[str] = None) -> str:
    """
    Scan the tenancy and return a deduplicated summary of OCI services in use,
    grouped by service → regions → compartments.
    Optimised as input for Oracle Architecture Center reference architecture lookups.

    Args:
        regions: Optional comma-separated regions. Defaults to all subscribed regions.
    """
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])

    all_regions = _get_subscribed_regions(identity, tenancy_id)
    target = [r.strip() for r in regions.split(",")] if regions else all_regions
    comp_map = _get_compartments(identity, tenancy_id)

    all_items: list = []
    for region in target:
        log.info(f"Scanning {region} ...")
        all_items.extend(_scan_region(config, signer, region, comp_map))

    svc_map: dict = defaultdict(lambda: {
        "regions": set(), "compartments": set(), "resource_types": set(), "count": 0
    })
    for r in all_items:
        svc = r["service"]
        svc_map[svc]["regions"].add(r["region"])
        svc_map[svc]["compartments"].add(r["compartment"])
        svc_map[svc]["resource_types"].add(r["resource_type"])
        svc_map[svc]["count"] += 1

    summary = {}
    for svc, data in sorted(svc_map.items()):
        summary[svc] = {
            "resource_count": data["count"],
            "regions": sorted(data["regions"]),
            "compartments": sorted(data["compartments"]),
            "resource_types": sorted(data["resource_types"]),
        }

    return json.dumps({
        "tenancy_regions_scanned": target,
        "total_resources": len(all_items),
        "services_in_use": sorted(summary.keys()),
        "service_detail": summary,
    }, indent=2)


@mcp.tool()
def get_unknown_resource_types(region: str) -> str:
    """
    Scan a region and return any resource types not in the service mapping.
    Useful for finding gaps in the classification dict.

    Args:
        region: OCI region identifier, e.g. 'ap-sydney-1'
    """
    config, signer = _make_config()
    identity = _identity_client(config, signer)
    tenancy_id = (signer.tenancy_id if signer else config["tenancy"])
    comp_map = _get_compartments(identity, tenancy_id)
    items = _scan_region(config, signer, region, comp_map)

    unknown = sorted({r["resource_type"] for r in items if r["service"] == "Unknown"})
    return json.dumps({
        "region": region,
        "unknown_resource_types": unknown,
        "count": len(unknown),
    }, indent=2)


# ── ASGI Application ──────────────────────────────────────────────────────────
#
# The MCP streamable_http_app() returns a Starlette app with a lifespan that
# runs session_manager.run(). Mounting it inside FastAPI breaks this because
# Starlette does NOT propagate lifespan events to sub-apps — causing:
#   RuntimeError: Task group is not initialized. Make sure to use run().
#
# Fix: use mcp.streamable_http_app() as the ROOT app and inject /ready + /health
# via FastMCP's built-in _custom_starlette_routes list, which gets included
# in the same Starlette app that owns the lifespan.
#
# Transport: Streamable HTTP
#   - streamable_http_path="/" → MCP endpoint at /, no /mcp suffix needed
#   - stateless_http=True      → no session-ID handshake per request
#   - Client config: type=streamable_http, url=<base invoke url>
#
# NOTE: Do NOT define the PORT env var here — it is reserved by the platform.
#       Use MCP_PORT for local/compose overrides (defaults to 8080).

def build_app():
    """Return the ASGI app: MCP Streamable HTTP with /ready and /health probes."""

    async def ready(request):
        """Readiness probe — OCI platform routes traffic only when this returns 200."""
        return JSONResponse({"status": "ready", "version": SERVER_VERSION})

    async def health(request):
        """Liveness probe — OCI platform restarts container if this returns non-200."""
        return JSONResponse({"status": "healthy", "version": SERVER_VERSION})

    # Inject probe routes into FastMCP's own Starlette app.
    # These are included in streamable_http_app() via self._custom_starlette_routes
    # so they share the same lifespan (session_manager.run()) — the root cause fix.
    mcp._custom_starlette_routes = [
        Route("/ready",  ready),
        Route("/health", health),
    ]

    return mcp.streamable_http_app()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    # MCP_PORT: local override only.
    # In hosted deployment the platform controls port binding via PORT (reserved).
    pub_host = os.getenv("MCP_HOST", "0.0.0.0")
    pub_port = int(os.getenv("MCP_PORT", "8080"))

    log.info(f"Starting OCI Inventory MCP Server v{SERVER_VERSION} — {pub_host}:{pub_port}")
    log.info(f"Auth mode: {os.getenv('OCI_AUTH', 'resource_principal')}")

    # uvicorn correctly fires ASGI lifespan startup events so FastMCP's
    # StreamableHttpSessionManager initialises its task group before any
    # requests arrive. hypercorn does not fire lifespan the same way,
    # causing: RuntimeError: Task group is not initialized. Make sure to use run().
    uvicorn.run(build_app(), host=pub_host, port=pub_port, log_level="info")
