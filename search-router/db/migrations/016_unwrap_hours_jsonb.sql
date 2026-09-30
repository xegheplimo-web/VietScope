-- 016_unwrap_hours_jsonb — P2.0.3 repair for double-encoded opening_hours.
--
-- Rows written while the resolver re-serialized staged jsonb text hold a
-- JSON *string* containing the hours object (jsonb_typeof = 'string'), and
-- serving projects them to NULL — every such place lost open_now. Unwrap
-- each string row whose inner text parses to a jsonb object.
--
-- Idempotent: once unwrapped a row is jsonb_typeof 'object' and is never
-- re-selected. Per-row safe: a string whose inner text is not valid jsonb
-- fails the cast inside the sub-block and is left untouched instead of
-- aborting the migration.
--
-- Repaired rows bump updated_at so the incremental index cursor —
-- (updated_at, place_id) in serving_index_state — re-scans them; without
-- the bump a repair would stay invisible to sync() until a manual full
-- rebuild.

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT place_id, opening_hours #>> '{}' AS inner_text
        FROM canonical_places
        WHERE jsonb_typeof(opening_hours) = 'string'
          AND (opening_hours #>> '{}') LIKE '{%'
    LOOP
        BEGIN
            IF jsonb_typeof(r.inner_text::jsonb) = 'object' THEN
                UPDATE canonical_places
                SET opening_hours = r.inner_text::jsonb,
                    updated_at = now()
                WHERE place_id = r.place_id;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
    END LOOP;
END $$;
