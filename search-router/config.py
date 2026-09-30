import os
from dataclasses import dataclass, field
from typing import Any


def _user_data_dir(app_name: str) -> str:
    """Per-user data dir without a platformdirs dependency.

    Windows → %LOCALAPPDATA%/<app_name>; POSIX → $XDG_DATA_HOME/<app_name>
    falling back to ~/.local/share/<app_name>.
    """
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local"
        )
    else:
        base = os.getenv("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share"
        )
    return os.path.join(base, app_name)


@dataclass
class Settings:
    # --- SearXNG ---
    searxng_url: str = field(
        default_factory=lambda: os.getenv("SEARXNG_URL", "http://localhost:8080")
    )

    # --- Firecrawl ---
    firecrawl_url: str = field(
        default_factory=lambda: os.getenv("FIRECRAWL_URL", "http://localhost:3002")
    )
    firecrawl_api_key: str = field(default_factory=lambda: os.getenv("FIRECRAWL_API_KEY", ""))
    # Internal JS-render service (Firecrawl stack's playwright container).
    playwright_url: str = field(
        default_factory=lambda: os.getenv("PLAYWRIGHT_URL", "http://firecrawl-playwright:3000")
    )

    # --- LLM for RAG synthesis (OpenAI-compatible) ---
    llm_base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    )
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    # Large local models (e.g. 27B Q4) need a generous read timeout.
    llm_timeout: int = field(default_factory=lambda: int(os.getenv("LLM_TIMEOUT", "180")))
    # Per-role model overrides — empty = use llm_model. Lets a LiteLLM proxy
    # (or any OpenAI-compatible backend) route e.g. planner→cheap model,
    # synthesizer→strong model, verifier→precise model without code changes.
    llm_planner_model: str = field(default_factory=lambda: os.getenv("LLM_PLANNER_MODEL", ""))
    llm_synth_model: str = field(default_factory=lambda: os.getenv("LLM_SYNTH_MODEL", ""))
    llm_verify_model: str = field(default_factory=lambda: os.getenv("LLM_VERIFY_MODEL", ""))
    llm_extract_model: str = field(default_factory=lambda: os.getenv("LLM_EXTRACT_MODEL", ""))
    # A9: per-role backend endpoints — empty = fall back to the global
    # LLM_BASE_URL/LLM_API_KEY above. Lets cheap roles (planner/extractor/
    # verifier) hit a local runtime (LocalAI/llama.cpp) while hard roles
    # stay on a paid API. Each distinct base_url gets its own HTTP client
    # and circuit breaker, so one endpoint's outage never trips another's.
    llm_planner_base_url: str = field(default_factory=lambda: os.getenv("LLM_PLANNER_BASE_URL", ""))
    llm_planner_api_key: str = field(default_factory=lambda: os.getenv("LLM_PLANNER_API_KEY", ""))
    llm_synth_base_url: str = field(default_factory=lambda: os.getenv("LLM_SYNTH_BASE_URL", ""))
    llm_synth_api_key: str = field(default_factory=lambda: os.getenv("LLM_SYNTH_API_KEY", ""))
    llm_verify_base_url: str = field(default_factory=lambda: os.getenv("LLM_VERIFY_BASE_URL", ""))
    llm_verify_api_key: str = field(default_factory=lambda: os.getenv("LLM_VERIFY_API_KEY", ""))
    llm_extract_base_url: str = field(default_factory=lambda: os.getenv("LLM_EXTRACT_BASE_URL", ""))
    llm_extract_api_key: str = field(default_factory=lambda: os.getenv("LLM_EXTRACT_API_KEY", ""))
    # A9 per-role failure fallback: "default" retries the call once on the
    # global backend (using LLM_MODEL) when the role's own endpoint fails;
    # anything else = return None so callers take their deterministic
    # fallback path. Only meaningful for roles with a *_BASE_URL override.
    llm_planner_fallback: str = field(default_factory=lambda: os.getenv("LLM_PLANNER_FALLBACK", ""))
    llm_synth_fallback: str = field(default_factory=lambda: os.getenv("LLM_SYNTH_FALLBACK", ""))
    llm_verify_fallback: str = field(default_factory=lambda: os.getenv("LLM_VERIFY_FALLBACK", ""))
    llm_extract_fallback: str = field(default_factory=lambda: os.getenv("LLM_EXTRACT_FALLBACK", ""))

    # --- AI reranker (cross-encoder, lazy-loaded) ---
    # Qwen3-Reranker-0.6B via sentence-transformers CrossEncoder. When the
    # model or its dependencies are unavailable the pipeline falls back to
    # the deterministic heuristic scorer — never a hard failure.
    reranker_enabled: bool = field(
        default_factory=lambda: os.getenv("RERANKER_ENABLED", "true").lower() == "true"
    )
    reranker_model: str = field(
        default_factory=lambda: os.getenv("RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B")
    )
    reranker_device: str = field(default_factory=lambda: os.getenv("RERANKER_DEVICE", "cpu"))
    reranker_top_n: int = field(default_factory=lambda: int(os.getenv("RERANKER_TOP_N", "20")))
    reranker_batch_size: int = field(
        default_factory=lambda: int(os.getenv("RERANKER_BATCH_SIZE", "16"))
    )

    # --- Research engine search modes ---
    # fast | balanced | deep — see pipeline/search_modes.py for budgets.
    search_mode_default: str = field(
        default_factory=lambda: os.getenv("SEARCH_MODE_DEFAULT", "balanced")
    )
    # Passages kept after the second-stage rerank before LLM synthesis.
    passage_top_n: int = field(default_factory=lambda: int(os.getenv("PASSAGE_TOP_N", "16")))

    # --- Semantic embedding rerank (optional, OpenAI-compatible /embeddings) ---
    # Leave EMBEDDING_MODEL empty to disable semantic reranking entirely
    # (keyword-only fallback keeps current behavior).
    embedding_base_url: str = field(default_factory=lambda: os.getenv("EMBEDDING_BASE_URL", ""))
    embedding_api_key: str = field(default_factory=lambda: os.getenv("EMBEDDING_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", ""))

    # --- Pipeline defaults ---
    max_results: int = field(default_factory=lambda: int(os.getenv("MAX_RESULTS", "10")))
    scrape_top_n: int = field(default_factory=lambda: int(os.getenv("SCRAPE_TOP_N", "5")))
    scrape_timeout: int = field(default_factory=lambda: int(os.getenv("SCRAPE_TIMEOUT", "60")))
    chunk_size: int = field(default_factory=lambda: int(os.getenv("CHUNK_SIZE", "800")))
    chunk_overlap: int = field(default_factory=lambda: int(os.getenv("CHUNK_OVERLAP", "200")))
    max_chunks_per_source: int = field(
        default_factory=lambda: int(os.getenv("MAX_CHUNKS_PER_SOURCE", "3"))
    )

    # --- Server ---
    host: str = field(
        default_factory=lambda: os.getenv(
            "HOST",
            "0.0.0.0",  # noqa: S104 — in-container the router must bind all interfaces
        )
    )
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8888")))
    # Allowed CORS origins — comma-separated in CORS_ORIGINS. Defaults cover
    # the local dev frontends (Next.js :3000, Vite :5173) on both localhost
    # and 127.0.0.1; set explicit origins before exposing the router behind
    # a public gateway.
    cors_origins: list[str] = field(
        default_factory=lambda: [
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:3000,http://127.0.0.1:3000,"
                "http://localhost:5173,http://127.0.0.1:5173",
            ).split(",")
            if origin.strip()
        ]
    )

    # --- GitHub code search ---
    github_token: str = field(default_factory=lambda: os.getenv("GITHUB_TOKEN", ""))
    # --- Exa (premium semantic search, optional, deep_research only) ---
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", ""))

    # --- BGE Reranker Service (Phase 1) ---
    reranker_service_url: str = field(
        default_factory=lambda: os.getenv("RERANKER_SERVICE_URL", "http://localhost:8891")
    )
    reranker_service_enabled: bool = field(
        default_factory=lambda: os.getenv("RERANKER_SERVICE_ENABLED", "true").lower() == "true"
    )

    # --- BGE Embedding Service (Phase 1) ---
    embedding_service_url: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_SERVICE_URL", "http://localhost:8892")
    )
    embedding_service_enabled: bool = field(
        default_factory=lambda: os.getenv("EMBEDDING_SERVICE_ENABLED", "true").lower() == "true"
    )

    # --- Qdrant (Phase 6B) ---
    qdrant_url: str = field(
        default_factory=lambda: os.getenv("QDRANT_URL", "http://localhost:6333")
    )
    qdrant_grpc_url: str = field(
        default_factory=lambda: os.getenv("QDRANT_GRPC_URL", "http://localhost:6334")
    )
    qdrant_timeout_ms: int = field(
        default_factory=lambda: int(os.getenv("QDRANT_TIMEOUT_MS", "300"))
    )
    qdrant_top_k: int = field(default_factory=lambda: int(os.getenv("QDRANT_TOP_K", "60")))
    qdrant_enabled: bool = field(
        default_factory=lambda: os.getenv("QDRANT_ENABLED", "false").lower() == "true"
    )
    qdrant_dense_enabled: bool = field(
        default_factory=lambda: os.getenv("QDRANT_DENSE_ENABLED", "false").lower() == "true"
    )
    qdrant_sparse_enabled: bool = field(
        default_factory=lambda: os.getenv("QDRANT_SPARSE_ENABLED", "false").lower() == "true"
    )
    qdrant_multivector_enabled: bool = field(
        default_factory=lambda: os.getenv("QDRANT_MULTIVECTOR_ENABLED", "false").lower() == "true"
    )
    qdrant_images_enabled: bool = field(
        default_factory=lambda: os.getenv("QDRANT_IMAGES_ENABLED", "false").lower() == "true"
    )
    qdrant_collection_passages: str = field(
        default_factory=lambda: os.getenv("QDRANT_COLLECTION_PASSAGES", "web_passages_v1")
    )
    qdrant_collection_images: str = field(
        default_factory=lambda: os.getenv("QDRANT_COLLECTION_IMAGES", "web_images_v1")
    )
    qdrant_collection_user_docs: str = field(
        default_factory=lambda: os.getenv("QDRANT_COLLECTION_USER_DOCS", "user_documents_v1")
    )

    # --- OpenSearch (Phase 2/6A) — lexical/web index ---
    opensearch_enabled: bool = field(
        default_factory=lambda: os.getenv("OPENSEARCH_ENABLED", "false").lower() == "true"
    )
    opensearch_host: str = field(default_factory=lambda: os.getenv("OPENSEARCH_HOST", "localhost"))
    opensearch_port: int = field(default_factory=lambda: int(os.getenv("OPENSEARCH_PORT", "9200")))
    opensearch_user: str = field(default_factory=lambda: os.getenv("OPENSEARCH_USER", "admin"))
    opensearch_password: str = field(
        default_factory=lambda: os.getenv("OPENSEARCH_PASSWORD", "admin")
    )
    opensearch_use_ssl: bool = field(
        default_factory=lambda: os.getenv("OPENSEARCH_USE_SSL", "false").lower() == "true"
    )
    opensearch_index_documents: str = field(
        default_factory=lambda: os.getenv("OPENSEARCH_INDEX_DOCUMENTS", "web_documents")
    )
    opensearch_index_passages: str = field(
        default_factory=lambda: os.getenv("OPENSEARCH_INDEX_PASSAGES", "web_passages")
    )
    opensearch_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("OPENSEARCH_TIMEOUT_S", "5"))
    )
    # L17 indexing worker — post-response backfill into OpenSearch + Qdrant.
    indexing_enabled: bool = field(
        default_factory=lambda: os.getenv("INDEXING_ENABLED", "true").lower() == "true"
    )

    # --- Hybrid federated retrieval (P11) ---
    # Extra candidate lane in /v1/search mode pipelines: OpenSearch BM25 +
    # Qdrant dense fused with RRF, merged into the pool before reranking.
    # The dense lane also needs QDRANT_DENSE_ENABLED=true.
    hybrid_retrieval_enabled: bool = field(
        default_factory=lambda: os.getenv("HYBRID_RETRIEVAL_ENABLED", "true").lower() == "true"
    )
    hybrid_top_k: int = field(default_factory=lambda: int(os.getenv("HYBRID_TOP_K", "60")))
    hybrid_rrf_k: int = field(default_factory=lambda: int(os.getenv("HYBRID_RRF_K", "60")))
    hybrid_fused_top: int = field(default_factory=lambda: int(os.getenv("HYBRID_FUSED_TOP", "40")))

    # --- Advanced Settings v2 ---
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379"))

    # --- Semantic answer cache (P5) ---
    # 4-layer cache (exact/semantic/evidence/page) on the research lane.
    # Redis-backed when reachable, in-memory fallback; the semantic layer
    # additionally needs the BGE embedding service.
    semantic_cache_enabled: bool = field(
        default_factory=lambda: os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() == "true"
    )
    semantic_cache_sim_threshold: float = field(
        default_factory=lambda: float(os.getenv("SEMANTIC_CACHE_SIM_THRESHOLD", "0.94"))
    )
    postgres_url: str = field(
        default_factory=lambda: os.getenv(
            "POSTGRES_URL", "postgresql://postgres:password@localhost:5432/search"
        )
    )

    # --- Conversation context (P4) ---
    # Redis-backed per-session state for follow-up resolution on the
    # research/answer lanes. Degrades to in-process memory when Redis is
    # unreachable; resolution itself is fail-open (heuristic/pass-through).
    conversation_context_enabled: bool = field(
        default_factory=lambda: os.getenv("CONVERSATION_CONTEXT_ENABLED", "true").lower() == "true"
    )
    conversation_context_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("CONVERSATION_CONTEXT_TTL_SECONDS", "86400"))
    )
    conversation_context_max_turns: int = field(
        default_factory=lambda: int(os.getenv("CONVERSATION_CONTEXT_MAX_TURNS", "6"))
    )
    conversation_context_max_sources: int = field(
        default_factory=lambda: int(os.getenv("CONVERSATION_CONTEXT_MAX_SOURCES", "5"))
    )
    conversation_context_resolution_timeout: float = field(
        default_factory=lambda: float(os.getenv("CONVERSATION_CONTEXT_RESOLUTION_TIMEOUT", "2.0"))
    )

    # --- Public API (P10) ---
    # hub-postgres DSN for api_keys / usage / query_logs (empty = disabled).
    hub_database_url: str = field(default_factory=lambda: os.getenv("HUB_DATABASE_URL", ""))

    # --- MinIO raw snapshot store (Phase 1/T2) ---
    # S3-compatible object store for immutable raw page snapshots
    # (document_snapshots.storage_key). Empty endpoint = disabled.
    minio_endpoint: str = field(default_factory=lambda: os.getenv("MINIO_ENDPOINT", ""))
    minio_access_key: str = field(default_factory=lambda: os.getenv("MINIO_ACCESS_KEY", ""))
    minio_secret_key: str = field(default_factory=lambda: os.getenv("MINIO_SECRET_KEY", ""))
    minio_bucket_raw: str = field(
        default_factory=lambda: os.getenv("MINIO_BUCKET_RAW", "sh-raw-snapshots")
    )
    minio_region: str = field(default_factory=lambda: os.getenv("MINIO_REGION", "us-east-1"))

    # --- Crawler engine (Phase 2) ---
    # Off by default: dev compose must never crawl the live web by accident.
    crawler_enabled: bool = field(
        default_factory=lambda: os.getenv("CRAWLER_ENABLED", "false").lower() == "true"
    )
    crawler_interval: float = field(
        default_factory=lambda: float(os.getenv("CRAWLER_INTERVAL_S", "5"))
    )
    crawler_batch_size: int = field(
        default_factory=lambda: int(os.getenv("CRAWLER_BATCH_SIZE", "10"))
    )
    crawler_concurrency: int = field(
        default_factory=lambda: int(os.getenv("CRAWLER_CONCURRENCY", "5"))
    )

    # --- Extraction engine (Phase 3) ---
    # Trafilatura main-content extraction on crawled snapshots + quality
    # gate feeding OpenSearch/Qdrant indexing. Off → crawl stores raw
    # snapshots only (no extraction, no indexing from the crawler lane).
    extraction_enabled: bool = field(
        default_factory=lambda: os.getenv("EXTRACTION_ENABLED", "true").lower() == "true"
    )
    extraction_min_chars: int = field(
        default_factory=lambda: int(os.getenv("EXTRACTION_MIN_CHARS", "100"))
    )
    extraction_min_quality: float = field(
        default_factory=lambda: float(os.getenv("EXTRACTION_MIN_QUALITY", "0.30"))
    )
    # API-key auth on /v1/* POST endpoints. Off by default for local/dev;
    # enable behind the reverse proxy in production.
    api_auth_enabled: bool = field(
        default_factory=lambda: os.getenv("API_AUTH_ENABLED", "false").lower() == "true"
    )
    # Bootstrap admin key plaintext (read once at startup → hashed + stored).
    hub_admin_key: str = field(default_factory=lambda: os.getenv("HUB_ADMIN_KEY", ""))
    api_default_rpm: int = field(default_factory=lambda: int(os.getenv("API_DEFAULT_RPM", "60")))
    api_default_daily_quota: int = field(
        default_factory=lambda: int(os.getenv("API_DEFAULT_DAILY_QUOTA", "1000"))
    )
    # --- OpenAI-compat public surface (P1) ---
    # Comma-separated model ids exposed on GET /v1/models and accepted by
    # POST /v1/chat/completions.
    openai_models: str = field(default_factory=lambda: os.getenv("OPENAI_MODELS", "duyai-search"))
    # Ceiling for a non-streamed chat completion (the research pipeline's
    # own budgets are tighter; this is the outer request bound).
    openai_timeout_s: float = field(
        default_factory=lambda: float(os.getenv("OPENAI_TIMEOUT_S", "300"))
    )
    default_mode: str = field(default_factory=lambda: os.getenv("DEFAULT_MODE", "normal"))
    budget_defaults: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "fast": {
                "max_queries": int(os.getenv("BUDGET_FAST_MAX_QUERIES", "2")),
                "max_fetches": int(os.getenv("BUDGET_FAST_MAX_FETCHES", "3")),
                "max_followups": int(os.getenv("BUDGET_FAST_MAX_FOLLOWUPS", "0")),
            },
            "normal": {
                "max_queries": int(os.getenv("BUDGET_NORMAL_MAX_QUERIES", "5")),
                "max_fetches": int(os.getenv("BUDGET_NORMAL_MAX_FETCHES", "8")),
                "max_followups": int(os.getenv("BUDGET_NORMAL_MAX_FOLLOWUPS", "1")),
            },
            "deep": {
                "max_queries": int(os.getenv("BUDGET_DEEP_MAX_QUERIES", "12")),
                "max_fetches": int(os.getenv("BUDGET_DEEP_MAX_FETCHES", "20")),
                "max_followups": int(os.getenv("BUDGET_DEEP_MAX_FOLLOWUPS", "3")),
            },
        }
    )
    provider_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "searxng": os.getenv("PROVIDER_SEARXNG_ENABLED", "true").lower() == "true",
            "firecrawl": os.getenv("PROVIDER_FIRECRAWL_ENABLED", "true").lower() == "true",
            "github": os.getenv("PROVIDER_GITHUB_ENABLED", "true").lower() == "true",
            "hn": os.getenv("PROVIDER_HN_ENABLED", "true").lower() == "true",
            "arxiv": os.getenv("PROVIDER_ARXIV_ENABLED", "true").lower() == "true",
        }
    )

    # --- Source Federation Layer (Phase 2) ---
    # Optional JSON file overriding PROVIDER_SPECS entries (enabled, priority,
    # countries, languages, source_types, timeout_s): {"providers": {"name": {...}}}
    providers_config_path: str = field(
        default_factory=lambda: os.getenv("HUB_PROVIDERS_CONFIG", "")
    )
    # Adaptive fan-out caps (external providers per query; the internal index
    # lane never counts against these).
    provider_fanout_max_providers: int = field(
        default_factory=lambda: int(os.getenv("PROVIDER_FANOUT_MAX_PROVIDERS", "6"))
    )
    provider_fanout_max_providers_fast: int = field(
        default_factory=lambda: int(os.getenv("PROVIDER_FANOUT_MAX_PROVIDERS_FAST", "3"))
    )
    # Provider health scoring / circuit breaker.
    provider_health_window: int = field(
        default_factory=lambda: int(os.getenv("PROVIDER_HEALTH_WINDOW", "64"))
    )
    provider_health_max_window_age_s: float = field(
        default_factory=lambda: float(os.getenv("PROVIDER_HEALTH_MAX_WINDOW_AGE_S", "1800"))
    )
    provider_health_cooldown_s: float = field(
        default_factory=lambda: float(os.getenv("PROVIDER_HEALTH_COOLDOWN_S", "300"))
    )
    provider_health_max_cooldown_s: float = field(
        default_factory=lambda: float(os.getenv("PROVIDER_HEALTH_MAX_COOLDOWN_S", "1800"))
    )
    provider_health_max_consecutive_failures: int = field(
        default_factory=lambda: int(os.getenv("PROVIDER_HEALTH_MAX_CONSECUTIVE_FAILURES", "3"))
    )
    provider_health_open_score: float = field(
        default_factory=lambda: float(os.getenv("PROVIDER_HEALTH_OPEN_SCORE", "0.30"))
    )
    provider_health_captcha_trip_rate: float = field(
        default_factory=lambda: float(os.getenv("PROVIDER_HEALTH_CAPTCHA_TRIP_RATE", "0.5"))
    )

    # --- P17 local-place serving ---
    # Read path: canonical_places → PlaceDocumentV1 → OpenSearch "places"
    # index + PostGIS geo lane + Redis caches. Ordinary local search never
    # touches an LLM.
    places_serving_enabled: bool = field(
        default_factory=lambda: os.getenv("PLACES_SERVING_ENABLED", "true").lower() == "true"
    )
    opensearch_index_places: str = field(
        default_factory=lambda: os.getenv("OPENSEARCH_INDEX_PLACES", "places")
    )
    # Candidate pool pulled from a lane before fusion ranking.
    places_candidate_topk: int = field(
        default_factory=lambda: int(os.getenv("PLACES_CANDIDATE_TOPK", "100"))
    )
    places_cache_ttl_search: int = field(
        default_factory=lambda: int(os.getenv("PLACES_CACHE_TTL_SEARCH", "300"))
    )
    places_cache_ttl_place: int = field(
        default_factory=lambda: int(os.getenv("PLACES_CACHE_TTL_PLACE", "3600"))
    )
    places_cache_ttl_suggest: int = field(
        default_factory=lambda: int(os.getenv("PLACES_CACHE_TTL_SUGGEST", "300"))
    )
    # JSON object overriding ranking component weights, e.g.
    # '{"distance":0.4,"text":0.3}' — unknown keys ignored.
    places_rank_weights: str = field(default_factory=lambda: os.getenv("PLACES_RANK_WEIGHTS", ""))

    # --- Metrics / feedback loop ---
    metrics_db_path: str = field(
        default_factory=lambda: os.getenv(
            "METRICS_DB_PATH",
            os.path.join(_user_data_dir("search-hub"), "search_metrics.db"),
        )
    )
    metrics_latency_target_ms: int = field(
        default_factory=lambda: int(os.getenv("METRICS_LATENCY_TARGET_MS", "5000"))
    )
    metrics_coverage_min_results: int = field(
        default_factory=lambda: int(os.getenv("METRICS_COVERAGE_MIN_RESULTS", "3"))
    )


settings = Settings()
