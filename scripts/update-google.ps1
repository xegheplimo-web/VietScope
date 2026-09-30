# update-google.ps1 — Google Maps discovery → staging → canonical pipeline.
#
#   gosom/google-maps-scraper → NDJSON → scripts.ingest (P15 staging)
#     → scripts.resolve (P16 canonical graph)
#
# Scraping is offline/batch only — user search never touches Google.
# Do NOT pass -resume for a scheduled refresh; -resume is only for
# restarting a run that died mid-flight. Each refresh produces a new
# observation row, and observation_hash comparison is what surfaces
# phone/hours/status changes on existing places.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\update-google.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\update-google.ps1 `
#       -QueriesFile data\gmaps\queries\health.txt -Region bac_ninh -Vertical health

[CmdletBinding()]
param(
    [string]$QueriesFile = "data\gmaps\queries.txt",
    [string]$Region = "",
    [string]$Vertical = "",
    [int]$Concurrency = 4,
    [int]$Depth = 10
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $Root "data\gmaps"
$Queries = if ([IO.Path]::IsPathRooted($QueriesFile)) { $QueriesFile } else { Join-Path $Root $QueriesFile }
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$FileName = "google-$Stamp.ndjson"
$Output = Join-Path $Data $FileName

New-Item -ItemType Directory -Force $Data | Out-Null

if (!(Test-Path $Queries)) {
    throw "Queries file not found: $Queries"
}

Write-Host "=== 1. Google Maps discovery ==="
Write-Host "queries: $Queries"
Write-Host "output:  $Output"

docker run --rm `
    -v gmaps-playwright-cache:/opt `
    -v "${Queries}:/queries.txt:ro" `
    -v "${Data}:/out" `
    gosom/google-maps-scraper `
    -input /queries.txt `
    -json `
    -results "/out/$FileName" `
    -lang vi `
    -depth $Depth `
    -c $Concurrency `
    -exit-on-inactivity 3m

if ($LASTEXITCODE -ne 0) {
    throw "Google Maps scraper failed."
}

if (!(Test-Path $Output)) {
    throw "Output file was not created: $Output"
}

Write-Host "=== 2. Search-Hub raw ingestion ==="

Set-Location (Join-Path $Root "search-router")

$IngestArgs = @(
    "run", "python", "-m", "scripts.ingest",
    "--provider", "google_maps",
    "--file", $Output,
    "--batch", "2000"
)
if ($Region)   { $IngestArgs += @("--param", "region=$Region") }
if ($Vertical) { $IngestArgs += @("--param", "vertical=$Vertical") }

uv @IngestArgs

if ($LASTEXITCODE -ne 0) {
    throw "Search-Hub ingestion failed."
}

Write-Host "=== 3. Canonical resolution ==="
# No --since-id: re-scrapes update existing source records in place, so a
# cursor would skip exactly the records whose observations changed.

uv run python -m scripts.resolve `
    --provider google_maps `
    --batch 200

if ($LASTEXITCODE -ne 0) {
    throw "Canonical resolution failed."
}

Write-Host ""
Write-Host "========================================"
Write-Host "Google update completed successfully."
Write-Host "Source file: $Output"
Write-Host "========================================"
