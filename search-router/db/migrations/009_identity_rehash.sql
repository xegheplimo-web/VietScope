-- 009_identity_rehash — recompute identity_hash with the P15.1 algorithm.
--
-- 008 backfilled identity_hash = content_hash, but the v2 identity canon
-- differs: {"e","p"} for rows with external_id, {"a","lat","lon","n","p"}
-- for id-less rows (vs the old all-fields content hash). Migrated rows
-- would therefore never conflict on (provider, identity_hash) and would
-- duplicate instead of updating on the next ingest. Recompute in SQL,
-- mirroring RawPlaceRecord.identity_hash():
--   sha256( json.dumps(canon, sort_keys=True, ensure_ascii=False) )
-- → keys sorted, separators ", " and ": ", floats via shortest
-- round-trip with a trailing ".0" for integral values.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION _p151_json_flt(v FLOAT8)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN v IS NULL THEN 'null'
        ELSE regexp_replace(round(v::numeric, 6)::float8::text, '^(-?\d+)$', '\1.0')
    END
$$;

CREATE OR REPLACE FUNCTION _p151_identity_canon(
    provider TEXT, external_id TEXT, raw_name TEXT, raw_address TEXT,
    lat FLOAT8, lon FLOAT8
) RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN external_id IS NOT NULL THEN
            format('{"e": %s, "p": %s}', to_json(external_id::text), to_json(provider))
        ELSE
            format(
                '{"a": %s, "lat": %s, "lon": %s, "n": %s, "p": %s}',
                to_json(COALESCE(btrim(lower(raw_address)), '')),
                _p151_json_flt(lat),
                _p151_json_flt(lon),
                to_json(COALESCE(btrim(lower(raw_name)), '')),
                to_json(provider)
            )
    END
$$;

UPDATE place_source_records
SET identity_hash = encode(
    digest(
        _p151_identity_canon(provider, external_id, raw_name, raw_address, lat, lon),
        'sha256'
    ),
    'hex'
);

DROP FUNCTION _p151_identity_canon(TEXT, TEXT, TEXT, TEXT, FLOAT8, FLOAT8);
DROP FUNCTION _p151_json_flt(FLOAT8);
