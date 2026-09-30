# Search Hub — Start All Services
# Starts all 9 containers with a single docker compose command.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "=== Search Hub — Starting All Services ===" -ForegroundColor Cyan

# Single command starts everything
Push-Location $root
docker compose up -d
Pop-Location

# Wait for health
Write-Host "`nWaiting for services to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 15

# Health check
Write-Host "`n=== Health Check ===" -ForegroundColor Cyan
try {
    $health = Invoke-RestMethod -Uri "http://localhost:8888/health" -TimeoutSec 10
    Write-Host "  SearXNG:    $($health.searxng)" -ForegroundColor $(if($health.searxng -eq 'ok'){'Green'}else{'Red'})
    Write-Host "  Firecrawl:  $($health.firecrawl)" -ForegroundColor $(if($health.firecrawl -eq 'ok'){'Green'}else{'Red'})
    Write-Host "  LLM:        $($health.llm)" -ForegroundColor Green
} catch {
    Write-Host "  Search Router not ready yet — check logs: docker compose logs search-router" -ForegroundColor Red
}

Write-Host "`n=== Containers ===" -ForegroundColor Cyan
Push-Location $root
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
Pop-Location

Write-Host "`n=== Endpoints ===" -ForegroundColor Cyan
Write-Host "  Search Router:  http://localhost:8888"
Write-Host "  SearXNG:        http://localhost:8080"
Write-Host "  Firecrawl:      http://localhost:3002"
Write-Host ""
Write-Host "  POST /search       — web/news/image search"
Write-Host "  POST /fetch        — scrape/crawl/map a URL"
Write-Host "  POST /v1/research  — AI research (internal pipeline)"
Write-Host "  POST /code_search  — code search via GitHub + grep.app"
Write-Host "  POST /answer       — full pipeline + evidence + citations"
Write-Host "  GET  /health       — check all providers"
