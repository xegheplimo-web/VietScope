-- 003_businesses — local-business geo table (canonical DDL).
--
-- storage/business_store.py reads/writes this table via storage.pg_client.
-- Previously the DDL only existed in BusinessStore.ensure_schema(), which no
-- production path calls — a freshly provisioned hub-postgres had no table
-- and every query degraded to []. This migration is the single source of
-- truth; ensure_schema() was removed.
--
-- Data note: any DB where ensure_schema() ran creates the identical table —
-- IF NOT EXISTS makes this a no-op there. coords are DOUBLE PRECISION and
-- location is a PostGIS geography-capable GEOMETRY(Point,4326) (T2 types).

CREATE TABLE IF NOT EXISTS businesses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    category TEXT,
    address TEXT,
    phone TEXT,
    hours TEXT,
    rating REAL,
    price_level TEXT,
    website TEXT,
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    location GEOMETRY(Point, 4326),
    description TEXT,
    source_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(lat, lon, name)
);

CREATE INDEX IF NOT EXISTS idx_businesses_location
    ON businesses USING GIST(location);
