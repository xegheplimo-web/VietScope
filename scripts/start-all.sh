#!/usr/bin/env bash
# Search Hub — Start All Services
set -e
cd "$(dirname "$0")/.."

echo "=== Search Hub — Starting All Services ==="

# Single command starts all 9 containers
docker compose up -d

# Wait for health
echo -e "\nWaiting for services to be ready..."
sleep 15

# Health check
echo -e "\n=== Health Check ==="
curl -s http://localhost:8888/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Search Router not ready yet"

echo -e "\n=== Containers ==="
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"

echo -e "\n=== Endpoints ==="
echo "  Search Router:  http://localhost:8888"
echo "  SearXNG:        http://localhost:8080"
echo "  Firecrawl:      http://localhost:3002"
echo ""
echo "  POST /search       — web/news/image search"
echo "  POST /fetch        — scrape/crawl/map a URL"
echo "  POST /v1/research  — AI research (internal pipeline)"
echo "  POST /code_search  — code search via GitHub + grep.app"
echo "  POST /answer       — full pipeline + evidence + citations"
echo "  GET  /health       — check all providers"
