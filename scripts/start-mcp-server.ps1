# Search Hub — Start MCP Server (HTTP mode :8901)
# Runs adapters/mcp_server.py in background (hidden window).
# Re-run this script after reboot or if MCP server dies.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/start-mcp-server.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$routerDir = Join-Path $root "search-router"

# ─── Environment (loaded up front: the port-busy path's credential probe
#     needs the key BEFORE it decides to reuse) ───────────────────────────

$env:MCP_TRANSPORT = "http"
$env:MCP_PORT = "8901"
if (-not $env:SEARCH_ROUTER_URL) { $env:SEARCH_ROUTER_URL = "http://localhost:8888" }
$routerUrl = $env:SEARCH_ROUTER_URL

# Default Hermes surface: search / fetch_evidence / code_search / search_places.
# Opt-in deep-research lane (fetch + research, source cap 8 -> 15):
#   $env:MCP_ENABLE_DEEP_RESEARCH = "true"
if (-not $env:MCP_ENABLE_DEEP_RESEARCH) { $env:MCP_ENABLE_DEEP_RESEARCH = "false" }

# Router auth (P11.1): only the scoped SEARCH_HUB_ROUTER_KEY is honored — the
# admin key must never sit on the normal MCP path (least privilege for Hermes
# retrieval). Create one with:
#   python search-router/manage_keys.py create --tenant hermes --name hermes-mcp --scopes "search:read,read:use"
if (-not $env:SEARCH_HUB_ROUTER_KEY) {
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile) {
            if ($line -match '^\s*SEARCH_HUB_ROUTER_KEY\s*=\s*(\S+)\s*$') {
                $env:SEARCH_HUB_ROUTER_KEY = $Matches[1]
            }
        }
    }
}
if ($env:SEARCH_HUB_ROUTER_KEY) {
    Write-Host "Router API key: configured (scoped SEARCH_HUB_ROUTER_KEY)" -ForegroundColor DarkGray
} else {
    Write-Host "Router API key: not set - running without Authorization header (dev auth-off)" -ForegroundColor DarkGray
    if ($env:HUB_ADMIN_KEY) {
        Write-Host "  HUB_ADMIN_KEY present but ignored - set a scoped SEARCH_HUB_ROUTER_KEY instead" -ForegroundColor Yellow
    }
}

function Read-McpJson([string]$content) {
    # streamable-http may answer as SSE ("data: {...}") or plain JSON
    $data = ($content -split "`n" | Where-Object { $_ -match "^data:" } | ForEach-Object { $_.Substring(5).Trim() }) -join ""
    if (-not $data) { $data = $content }
    return $data | ConvertFrom-Json
}

function Test-McpSurface {
    # $true only when :8901 is an MCP server exposing exactly the expected
    # tool surface. A LISTEN port alone proves nothing — the stale-MCP
    # failure mode (old code/dummy key/wrong tools) also listens on 8901.
    $expected = @("search", "fetch_evidence", "code_search", "search_places")
    if ($env:MCP_ENABLE_DEEP_RESEARCH -match '^(1|true|yes|on)$') {
        $expected += @("fetch", "research")
    }
    $mcpUrl = "http://127.0.0.1:8901/mcp"
    $headers = @{
        "Content-Type" = "application/json"
        "Accept"       = "application/json, text/event-stream"
    }
    try {
        $initBody = @{
            jsonrpc = "2.0"; id = 1; method = "initialize"
            params = @{
                protocolVersion = "2025-03-26"
                capabilities    = @{}
                clientInfo      = @{ name = "start-mcp-verify"; version = "1.0" }
            }
        } | ConvertTo-Json -Depth 10 -Compress
        $init = Invoke-WebRequest -Uri $mcpUrl -Method Post -Headers $headers -Body $initBody -TimeoutSec 10
        # server-issued; never self-generate. Headers[...] returns a string
        # array on pwsh 7 — unwrap so the value isn't sent as "System.String[]".
        $sessionId = @($init.Headers["Mcp-Session-Id"]) | Select-Object -First 1
        if (-not $sessionId) { return $false }
        $sessionHeaders = $headers + @{ "Mcp-Session-Id" = $sessionId }
        $notify = @{ jsonrpc = "2.0"; method = "notifications/initialized" } | ConvertTo-Json -Compress
        Invoke-WebRequest -Uri $mcpUrl -Method Post -Headers $sessionHeaders -Body $notify -TimeoutSec 10 | Out-Null
        $listBody = @{ jsonrpc = "2.0"; id = 2; method = "tools/list" } | ConvertTo-Json -Compress
        $list = Invoke-WebRequest -Uri $mcpUrl -Method Post -Headers $sessionHeaders -Body $listBody -TimeoutSec 10
        $result = Read-McpJson $list.Content
        $names = @($result.result.tools | ForEach-Object { $_.name } | Sort-Object)
        return (($names -join ",") -eq ((@($expected) | Sort-Object) -join ","))
    } catch {
        return $false
    }
}

function Test-RouterCredential {
    # Probes GET /v1/auth/check on the router with the SAME key this script
    # would (re)start the MCP server with. The MCP handshake alone cannot
    # see a dummy/wrong/revoked key — initialize + tools/list never touch
    # the router. Fails closed: unreachable router or rejected key is NOT
    # verified. $script:probeError carries the failure detail for callers.
    $script:probeError = ""
    $headers = @{}
    if ($env:SEARCH_HUB_ROUTER_KEY) {
        $headers["Authorization"] = "Bearer $env:SEARCH_HUB_ROUTER_KEY"
    }
    try {
        $resp = Invoke-WebRequest -Uri "$routerUrl/v1/auth/check" -Method Get -Headers $headers -TimeoutSec 10
        $info = $resp.Content | ConvertFrom-Json
    } catch {
        $code = $_.Exception.Response.StatusCode.value__
        $detail = if ($code) { "HTTP $code" } else { $_.Exception.Message }
        $script:probeError = "credential probe failed at $routerUrl ($detail) - is the stack up?"
        return $false
    }
    if (-not $info.auth_enabled) {
        Write-Host "Router auth: disabled (dev mode) - nothing to verify" -ForegroundColor DarkGray
        return $true
    }
    if (-not $env:SEARCH_HUB_ROUTER_KEY) {
        $script:probeError = "router requires auth but SEARCH_HUB_ROUTER_KEY is not set"
        return $false
    }
    if (-not $info.authenticated) {
        $script:probeError = "SEARCH_HUB_ROUTER_KEY rejected (dummy/wrong/revoked)"
        return $false
    }
    $required = @("search:read", "read:use")
    if ($env:MCP_ENABLE_DEEP_RESEARCH -match '^(1|true|yes|on)$') {
        $required += "research:use"
    }
    $have = @($info.scopes)
    if ($have -notcontains "*") {
        $missing = @($required | Where-Object { $have -notcontains $_ })
        if ($missing.Count -gt 0) {
            $script:probeError = "key missing required scopes: $($missing -join ', ')"
            return $false
        }
    }
    return $true
}

# Port already in use? -> verify it is really OUR MCP server before reusing:
# (1) owning process is adapters.mcp_server, (2) initialize + tools/list
# returns the expected surface, (3) the router credential probes valid.
# Anything else = stale/foreign process — fail loudly with the PID instead
# of claiming READY (no auto-kill: process ownership stays with the operator).
$portBusy = Get-NetTCPConnection -LocalPort 8901 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portBusy) {
    $busyPid = $portBusy.OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$busyPid" -ErrorAction SilentlyContinue
    if (-not ($proc -and $proc.CommandLine -match "adapters\.mcp_server")) {
        Write-Host "Port 8901 is held by PID $busyPid which is NOT adapters.mcp_server" -ForegroundColor Red
        # ExecutablePath only — a raw CommandLine can carry secrets.
        if ($proc) { Write-Host "  process: $($proc.Name) — $($proc.ExecutablePath)" -ForegroundColor DarkGray }
        Write-Host "  Free the port or stop that process (kill -Id $busyPid), then re-run." -ForegroundColor Yellow
        exit 1
    }
    $surfaceOk = Test-McpSurface
    $credOk = Test-RouterCredential
    if ($surfaceOk -and $credOk) {
        Write-Host "Port 8901 already serves a verified MCP server (PID $busyPid) - reusing." -ForegroundColor Yellow
        exit 0
    }
    if (-not $surfaceOk) {
        Write-Host "PID $busyPid looks like adapters.mcp_server but failed initialize/tools-list verification" -ForegroundColor Red
        Write-Host "  (stale MCP: old code or wrong tool surface)." -ForegroundColor DarkGray
    } else {
        Write-Host "PID $busyPid serves the right tools but the router credential probe failed:" -ForegroundColor Red
        Write-Host "  $script:probeError" -ForegroundColor DarkGray
    }
    Write-Host "  Stop it manually (kill -Id $busyPid) and re-run this script." -ForegroundColor Yellow
    exit 1
}

# Verify the router credential BEFORE spawning — no point starting a server
# that will 401/403 on every tool call.
if (-not (Test-RouterCredential)) {
    Write-Host "Router credential probe failed: $script:probeError" -ForegroundColor Red
    Write-Host "  Fix the key (manage_keys.py hint above) or bring the stack up, then re-run." -ForegroundColor Yellow
    exit 1
}

# Pin the repo venv interpreter — `Get-Command python` can resolve a global
# or Store-shim Python without mcp/httpx/project deps installed.
$python = Join-Path $routerDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Search-Hub venv Python not found: $python" -ForegroundColor Red
    Write-Host "  Create it first: cd search-router; uv sync --extra mcp" -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting Search Hub MCP server on http://127.0.0.1:8901/mcp ..." -ForegroundColor Cyan
Push-Location $routerDir

# NOTE: pythonw.exe exits immediately on this setup — with no console attached
# the MCP server dies during startup. Use python.exe with a hidden window.
Start-Process -FilePath $python -ArgumentList "-m adapters.mcp_server" -WorkingDirectory $routerDir -WindowStyle Hidden
Pop-Location

# Poll for readiness (uvicorn can take a few seconds to bind), then
# verify the tool surface before claiming READY — same check as the
# port-busy path, so "READY" is never asserted on a wrong/stale server.
$check = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    $check = Get-NetTCPConnection -LocalPort 8901 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($check) { break }
}
if (-not $check) {
    Write-Host "MCP server not listening after 30s - check: python -m adapters.mcp_server (foreground) for errors" -ForegroundColor Red
    exit 1
}
if (Test-McpSurface) {
    Write-Host "MCP server READY at http://127.0.0.1:8901/mcp (PID $($check.OwningProcess))" -ForegroundColor Green
} else {
    Write-Host "MCP server listening but tool-surface verification FAILED (PID $($check.OwningProcess))" -ForegroundColor Red
    Write-Host "  Check: python -m adapters.mcp_server (foreground) for errors" -ForegroundColor Yellow
    exit 1
}
