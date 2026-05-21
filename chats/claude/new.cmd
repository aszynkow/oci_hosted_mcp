@echo off
setlocal
set "DOMAIN_URL=https://idcs-36ee04102b0c49b0a656aa0be89db14a.identity.oraclecloud.com"
set "CLIENT_ID=oci-mcp-inventory"
set "CLIENT_SECRET=idcscs-0ba1a043-3694-4ca8-af96-73227e590213"
set "FULL_SCOPE=oci-mcp-inventoryinvoke"
set "ENDPOINT_URL=https://inference.generativeai.us-phoenix-1.oci.oraclecloud.com/20251112/hostedApplications/ocid1.generativeaihostedapplication.oc1.phx.amaaaaaajlkbyliagqwaithtlj4ho6xpnkivavfk4aff5z5scz635d4llcpq/actions/invoke"

for /f "delims=" %%T in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "(Invoke-RestMethod -Method Post -Uri '%DOMAIN_URL%/oauth2/v1/token' -Body @{grant_type='client_credentials';client_id='%CLIENT_ID%';client_secret='%CLIENT_SECRET%';scope='%FULL_SCOPE%'} -ContentType 'application/x-www-form-urlencoded').access_token"') do set "TOKEN=%%T"

if "%TOKEN%"=="" (
  echo ERROR: token fetch failed 1>&2
  exit /b 1
)

npx -y mcp-remote "%ENDPOINT_URL%" --header "Authorization: Bearer %TOKEN%"