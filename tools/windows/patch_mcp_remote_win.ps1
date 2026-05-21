# patch-mcp-remote.ps1

$chunk = Get-ChildItem "$env:APPDATA\npm\node_modules\mcp-remote\dist\chunk-*.js" |
         Sort-Object Length -Descending |
         Select-Object -First 1 -ExpandProperty FullName

if (-not $chunk) {
    Write-Host "ERROR: mcp-remote chunk file not found" -ForegroundColor Red
    exit 1
}

Write-Host "Found: $chunk"

$content = Get-Content $chunk -Raw

if ($content -match "event\.data\.startsWith") {
    Write-Host "Already patched - nothing to do" -ForegroundColor Green
    exit 0
}

$old = "          if (!event.data) {`n            continue;`n          }"
$new = "          if (!event.data) {`n            continue;`n          }`n          if (event.data.startsWith(':')) {`n            continue;`n          }"

$patched = $content.Replace($old, $new)

if ($patched -eq $content) {
    Write-Host "ERROR: patch location not found - mcp-remote may have changed" -ForegroundColor Red
    exit 1
}

Set-Content $chunk $patched -NoNewline
Write-Host "Patched OK" -ForegroundColor Green

if (Select-String -Path $chunk -Pattern "event.data.startsWith" -Quiet) {
    Write-Host "Verified" -ForegroundColor Green
} else {
    Write-Host "WARNING: verification failed" -ForegroundColor Yellow
}
