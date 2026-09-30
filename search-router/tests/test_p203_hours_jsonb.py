"""P2.0.3 — double-encoded ``opening_hours`` / ``images`` in canonical_places.

asyncpg returns jsonb as ``str`` (no codec registered). The resolver passed
that str through ``json.dumps`` a second time on write, so
``canonical_places.opening_hours`` held a JSON *string* containing the hours
object — serving's ``_jsonb_obj`` then projected it to NULL and every place
with hours served ``opening_hours: null`` / ``open_now: null``.

Covers the three-layer fix: tolerant write params (store), str→dict
normalization where staged rows enter fusion (runner), a two-layer unwrap
on read (projection), and the 016 repair migration.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from db import migrate
from resolution.runner import run_resolution
from resolution.store import DictCanonicalStore, PgCanonicalStore, _jsonb_param
from serving.places.indexer import DictIndexState, PlaceIndexer
from serving.places.projection import PLACE_COLS, _jsonb_obj, project_row

VN_HOURS = {"Thứ Ba": ["09:00–17:00"]}  # Tuesday 09:00–17:00
VN_HOURS_STR = json.dumps(VN_HOURS, ensure_ascii=False)
VN_HOURS_DOUBLE = json.dumps(VN_HOURS_STR, ensure_ascii=False)
IMAGES = ["https://img.example/a.jpg", "https://img.example/b.jpg"]
IMAGES_STR = json.dumps(IMAGES)

# Naive `now` args to open_now are read as UTC+7 wall time (P2.0.2 convention).
TUE_10AM = datetime(2026, 9, 29, 10, 0)


# ─── projection: two-layer unwrap ────────────────────────────────────────


class TestJsonbObjUnwrap:
    def test_double_encoded_str_yields_dict(self):
        assert _jsonb_obj(VN_HOURS_DOUBLE) == VN_HOURS

    def test_single_encoded_str_still_works(self):
        assert _jsonb_obj(VN_HOURS_STR) == VN_HOURS

    def test_non_json_str_is_none(self):
        assert _jsonb_obj("not json") is None
        assert _jsonb_obj("{broken") is None

    def test_json_scalar_and_list_are_none(self):
        # a JSON string/number/list is valid JSON but not an object
        assert _jsonb_obj('"abc"') is None
        assert _jsonb_obj("5") is None
        assert _jsonb_obj("[1, 2]") is None

    def test_dict_passthrough_and_null(self):
        assert _jsonb_obj(VN_HOURS) is VN_HOURS
        assert _jsonb_obj(None) is None


# ─── store: tolerant write params ────────────────────────────────────────


class TestJsonbParam:
    def test_dict_and_list_dump_once(self):
        assert json.loads(_jsonb_param(VN_HOURS)) == VN_HOURS
        assert json.loads(_jsonb_param(IMAGES)) == IMAGES

    def test_json_str_unwraps_before_dump(self):
        out = _jsonb_param(VN_HOURS_STR)
        # single-encoding: the bound param parses to the dict, not to a str
        assert json.loads(out) == VN_HOURS
        out = _jsonb_param(IMAGES_STR)
        assert json.loads(out) == IMAGES

    def test_garbage_and_wrong_types_become_none(self):
        assert _jsonb_param("not json") is None
        assert _jsonb_param('"abc"') is None  # parses to str — not obj/list
        assert _jsonb_param("5") is None
        assert _jsonb_param(5) is None
        assert _jsonb_param(None) is None


class _ArgPool:
    """Captures every bound argument; returns canned RETURNING rows."""

    def __init__(self):
        self.calls: list[tuple[str, str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return {"place_id": 9}

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        return "UPDATE 1"


def _insert_arg(pool: _ArgPool, index: int):
    call = next(c for c in pool.calls if "INSERT INTO canonical_places" in c[1])
    return call[2][index]


class TestStoreWritePath:
    """Both write sites must bind a single-encoded JSON param even when the
    incoming value is still the str asyncpg handed back."""

    _ROW = {
        "business_id": 3,
        "canonical_name": "Quán X",
        "normalized_name": "quan x",
        "canonical_category": "food",
        "address": None,
        "normalized_address": None,
        "phone": None,
        "website": None,
        "lat": 21.03,
        "lon": 105.85,
        "admin_unit_id": None,
        "status": "open",
        "confidence": 0.0,
        "source_count": 1,
        "resolution_run_id": 1,
    }

    def test_create_place_str_hours_binds_object(self):
        pool = _ArgPool()
        store = PgCanonicalStore(pool)
        asyncio.run(store.create_place({**self._ROW, "opening_hours": VN_HOURS_STR}))
        assert json.loads(_insert_arg(pool, 9)) == VN_HOURS  # $10

    def test_create_place_dict_hours_unchanged(self):
        pool = _ArgPool()
        store = PgCanonicalStore(pool)
        asyncio.run(store.create_place({**self._ROW, "opening_hours": VN_HOURS}))
        assert json.loads(_insert_arg(pool, 9)) == VN_HOURS

    def test_create_place_garbage_hours_binds_null(self):
        pool = _ArgPool()
        store = PgCanonicalStore(pool)
        asyncio.run(store.create_place({**self._ROW, "opening_hours": "not json"}))
        assert _insert_arg(pool, 9) is None

    def test_update_place_str_jsonb_args(self):
        pool = _ArgPool()
        store = PgCanonicalStore(pool)
        asyncio.run(store.update_place(9, {"opening_hours": VN_HOURS_STR, "images": IMAGES_STR}))
        sql, args = pool.calls[0][1], pool.calls[0][2]
        assert "opening_hours = $1" in sql and "images = $2" in sql
        assert json.loads(args[0]) == VN_HOURS
        assert json.loads(args[1]) == IMAGES

    def test_update_place_bad_str_binds_null(self):
        pool = _ArgPool()
        store = PgCanonicalStore(pool)
        asyncio.run(store.update_place(9, {"opening_hours": "{broken"}))
        assert pool.calls[0][2][0] is None


# ─── runner: normalization at the fusion boundary ────────────────────────


def _staged(i: int, **kw):
    base = {
        "id": i,
        "provider": "google_maps",
        "external_id": f"ChIJ{i}",
        "raw_name": f"Quán {i}",
        "raw_address": None,
        "raw_phone": None,
        "raw_website": None,
        "raw_category": None,
        "raw_hours": None,
        "raw_status": None,
        "lat": None,
        "lon": None,
        "admin_unit_id": None,
        "observed_at": None,
        "raw_payload": None,
    }
    return {**base, **kw}


async def _feed(rows):
    for r in rows:
        yield r


class TestRunnerHoursNormalization:
    """Staged ``raw_hours`` arrives as str from asyncpg; the pipeline must
    carry a dict so canonical writes never re-serialize text."""

    def test_str_raw_hours_lands_as_dict(self):
        store = DictCanonicalStore()
        asyncio.run(
            run_resolution(
                None,
                store=store,
                sources_feed=_feed([_staged(1, raw_hours=VN_HOURS_STR)]),
            )
        )
        assert store.places[1].opening_hours == VN_HOURS
        prov = next(r for r in store.provenance if r.field == "hours")
        assert prov.value == VN_HOURS

    def test_dict_raw_hours_unchanged(self):
        store = DictCanonicalStore()
        asyncio.run(
            run_resolution(
                None,
                store=store,
                sources_feed=_feed([_staged(1, raw_hours=VN_HOURS)]),
            )
        )
        assert store.places[1].opening_hours == VN_HOURS

    def test_pg_path_insert_and_update_bind_objects(self):
        """The full defect chain: _PAGE_SQL str → create INSERT + provenance
        UPDATE both bind single-encoded jsonb."""

        class _Acquire:
            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, *a):
                return False

            def __await__(self):
                async def _c():
                    return self._conn

                return _c().__await__()

        class _Pool:
            def __init__(self):
                self.calls = []
                self.row = _staged(1, raw_hours=VN_HOURS_STR)
                self.staged = [[self.row], []]

            def acquire(self):
                return _Acquire(self)

            async def release(self, conn):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def transaction(self):
                return self

            async def fetch(self, sql, *args):
                self.calls.append(("fetch", sql, args))
                if "place_sources" in sql and "JOIN" in sql.upper():
                    return [self.row]
                if "place_source_records" in sql:
                    return self.staged.pop(0) if self.staged else []
                return []

            async def fetchrow(self, sql, *args):
                self.calls.append(("fetchrow", sql, args))
                if "pg_try_advisory_lock" in sql:
                    return {"ok": True}
                if "RETURNING run_id" in sql:
                    return {"run_id": 1}
                if "RETURNING business_id" in sql:
                    return {"business_id": 3}
                if "RETURNING place_id" in sql:
                    return {"place_id": 9}
                return None

            async def execute(self, sql, *args):
                self.calls.append(("execute", sql, args))
                if "place_sources" in sql:
                    return "INSERT 0 1"
                return "OK"

        pool = _Pool()
        out = asyncio.run(run_resolution(pool, batch_size=10))
        assert out["status"] == "done" and out["created"] == 1

        ins = next(c for c in pool.calls if "INSERT INTO canonical_places" in c[1])
        assert json.loads(ins[2][9]) == VN_HOURS  # create: $10 single-encoded
        upd = next(
            c
            for c in pool.calls
            if c[0] == "execute"
            and "UPDATE canonical_places" in c[1]
            and "opening_hours = $" in c[1]
        )
        m = re.search(r"opening_hours = \$(\d+)", upd[1])
        assert m and json.loads(upd[2][int(m.group(1)) - 1]) == VN_HOURS


# ─── service end-to-end: broken row → document → open_now ────────────────


def _canon_row(**kw):
    base = {
        "place_id": 7,
        "business_id": None,
        "canonical_name": "Quán X",
        "normalized_name": "quan x",
        "status": "open",
        "confidence": 0.5,
        "source_count": 1,
    }
    return {**base, **kw}


class TestServingEndToEnd:
    def test_double_encoded_row_serves_hours_and_open_now(self):
        from serving.places.ranking import Candidate
        from serving.places.service import PlaceService

        doc = project_row(_canon_row(opening_hours=VN_HOURS_DOUBLE))
        assert doc.opening_hours == VN_HOURS  # was NULL before the fix

        row = PlaceService._out_row(
            Candidate(doc=doc, os_score=1.0, lane="postgis"),
            debug=False,
            now=TUE_10AM,
        )
        assert row["opening_hours"] == VN_HOURS
        assert isinstance(row["open_now"], bool) and row["open_now"] is True

    def test_garbage_hours_still_null_not_crash(self):
        doc = project_row(_canon_row(opening_hours="{broken"))
        assert doc.opening_hours is None
        assert project_row(_canon_row(opening_hours='"plain"')).opening_hours is None


# ─── migration 016 ───────────────────────────────────────────────────────


class TestMigration016:
    def test_file_registered_and_safe(self):
        files = dict(migrate.migration_files())
        path = files.get("016_unwrap_hours_jsonb")
        assert path is not None
        sql = path.read_text(encoding="utf-8")
        assert "jsonb_typeof(opening_hours) = 'string'" in sql
        assert "#>> '{}'" in sql
        assert "EXCEPTION WHEN OTHERS" in sql  # one bad row can't kill it
        assert "DO $$" in sql


# ─── migration 016: repaired rows must re-enter the incremental scan ────
#
# The indexer's durable cursor is (updated_at, place_id). An UPDATE that
# leaves updated_at alone keeps a repaired row at-or-behind the cursor —
# sync() can never select it again, so the place keeps serving
# opening_hours/open_now=null until a manual full rebuild.

MIGRATION_016 = Path(migrate.MIGRATIONS_DIR) / "016_unwrap_hours_jsonb.sql"


def _migration_016_set_clause() -> list[str]:
    """Assignment list of 016's repair UPDATE, parsed from the real file —
    the simulation below writes exactly what the migration writes."""
    sql = MIGRATION_016.read_text(encoding="utf-8")
    m = re.search(
        r"UPDATE\s+canonical_places\s+SET\s+(.*?)\s+WHERE\s+place_id\s*=\s*r\.place_id",
        sql,
        re.S | re.I,
    )
    assert m, "016 must repair rows via UPDATE ... WHERE place_id = r.place_id"
    return [a.strip() for a in m.group(1).split(",")]


def _simulate_migration_016(rows: list[dict], *, now: datetime) -> list[dict]:
    """Apply the 016 DO block to fake canonical_places rows.

    Row model: ``opening_hours`` holds the jsonb *value* — a ``str`` is a
    jsonb string (the double-encoded defect), a ``dict`` a real object.
    Only 'string' rows whose inner text starts with '{' enter the FOR
    loop; a failed inner cast hits EXCEPTION WHEN OTHERS and the row is
    left untouched.
    """
    assignments = _migration_016_set_clause()
    out = []
    for row in rows:
        hours = row.get("opening_hours")
        if not isinstance(hours, str) or not hours.startswith("{"):
            out.append(row)
            continue
        try:
            parsed = json.loads(hours)  # r.inner_text::jsonb — may throw
        except ValueError:
            out.append(row)  # EXCEPTION WHEN OTHERS — row left as-is
            continue
        if not isinstance(parsed, dict):  # jsonb_typeof(inner::jsonb) != 'object'
            out.append(row)
            continue
        repaired = dict(row)
        for a in assignments:
            col, _, expr = a.partition("=")
            col, expr = col.strip(), expr.strip()
            if expr == "r.inner_text::jsonb":
                repaired[col] = parsed
            elif expr.lower() == "now()":
                repaired[col] = now
            else:
                raise AssertionError(f"unmodeled 016 assignment: {a}")
        out.append(repaired)
    return out


# asyncpg parity for the sqlite stand-in: timestamp columns read back as
# datetime; jsonb columns read back as str (no codec registered).
_TS_COLS = {"last_seen", "updated_at"}
_SQLITE_COL_TYPES = {
    "place_id": "INTEGER PRIMARY KEY",
    "business_id": "INTEGER",
    "admin_unit_id": "INTEGER",
    "source_count": "INTEGER",
    "review_count": "INTEGER",
    "lat": "REAL",
    "lon": "REAL",
    "confidence": "REAL",
    "rating": "REAL",
}


def _sqlite_encode(v):
    """datetime → ISO text; dict/list → JSON text (jsonb comes back str)."""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


class _DeltaPool:
    """``canonical_places`` read side over an in-memory sqlite table.

    ``fetch`` executes the real SQL text it is handed — SCAN_DELTA_SQL's
    ``(updated_at, place_id)`` cursor predicate, ORDER BY and LIMIT are all
    evaluated by sqlite, not re-implemented in Python. Postgres ``$n``
    placeholders are rebound to ``?`` in order of appearance. SQL aimed at
    other tables returns no rows (this fixture links no source records).
    """

    def __init__(self, rows: list[dict]):
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row
        decl = ", ".join(f"{c} {_SQLITE_COL_TYPES.get(c, 'TEXT')}" for c in PLACE_COLS)
        self._db.execute(f"CREATE TABLE canonical_places ({decl})")
        cols, marks = ", ".join(PLACE_COLS), ", ".join("?" for _ in PLACE_COLS)
        for row in rows:
            self._db.execute(
                f"INSERT INTO canonical_places ({cols}) VALUES ({marks})",
                [_sqlite_encode(row.get(c)) for c in PLACE_COLS],
            )

    @staticmethod
    def _rebind(sql: str, args: tuple) -> tuple[str, list]:
        idxs = [int(m.group(1)) for m in re.finditer(r"\$(\d+)", sql)]
        return re.sub(r"\$\d+", "?", sql), [_sqlite_encode(args[i - 1]) for i in idxs]

    async def fetch(self, sql, *args):
        if "canonical_places" not in sql:
            return []  # ALIASES_SQL — no source links in this fixture
        q, binds = self._rebind(sql, args)
        rows = [dict(r) for r in self._db.execute(q, binds)]
        for row in rows:
            for col in _TS_COLS:
                v = row.get(col)
                if isinstance(v, str):
                    try:
                        row[col] = datetime.fromisoformat(v)
                    except ValueError:
                        pass
        return rows


class _MemIndex:
    """Just the PlaceIndexer → OpenSearch surface used by sync()."""

    def __init__(self):
        self.docs: dict = {}

    async def ensure(self):
        return "places"

    async def upsert_docs(self, docs, index=None):
        for d in docs:
            self.docs[d.place_id] = d
        return {"indexed": len(docs), "failed": 0, "errors": []}

    async def refresh(self, index=None):
        return None


class _NoopCache:
    async def invalidate_place(self, place_id):
        return None

    async def invalidate_all(self):
        return None


class TestMigration016DeltaVisibility:
    """Regression (R5): a row repaired by 016 must sort AFTER the durable
    (updated_at, place_id) cursor so the next incremental sync re-indexes
    it — proving the migration bumps updated_at, not just opening_hours."""

    T0 = datetime(2026, 9, 28, 12, 0)  # last index pass before the repair
    T1 = datetime(2026, 9, 30, 12, 0)  # migration run time

    def _repaired_rows(self):
        rows = [
            _canon_row(
                place_id=42,
                opening_hours=VN_HOURS_STR,  # jsonb string — the defect
                updated_at=self.T0,
            ),
            _canon_row(
                place_id=43,
                opening_hours="{broken",  # inner cast fails → row untouched
                updated_at=self.T0,
            ),
        ]
        return _simulate_migration_016(rows, now=self.T1)

    def _indexer(self, rows):
        state = DictIndexState()
        # ledger: the T0 pass already indexed every row up to place_id 43 —
        # nothing short of a strict (updated_at, place_id) advance rescans.
        asyncio.run(state.advance(cursor_updated_at=self.T0, cursor_place_id=43, docs_delta=0))
        return PlaceIndexer(_DeltaPool(rows), os_index=_MemIndex(), cache=_NoopCache(), state=state)

    def test_repaired_row_reenters_delta_scan(self):
        rows = self._repaired_rows()
        # the repair itself still happened — guards/idempotency unchanged
        assert rows[0]["opening_hours"] == VN_HOURS
        assert rows[1]["opening_hours"] == "{broken"  # exception-safe

        idx = self._indexer(rows)
        res = asyncio.run(idx.sync(batch_size=10))
        assert res["scanned"] == 1
        doc = idx._os.docs["42"]
        assert doc.opening_hours == VN_HOURS

    def test_untouched_row_stays_behind_cursor(self):
        # A row the DO block skips keeps its updated_at — the cursor must
        # NOT pick it up (only genuinely repaired rows get re-indexed).
        idx = self._indexer(self._repaired_rows())
        asyncio.run(idx.sync(batch_size=10))
        assert "43" not in idx._os.docs


@pytest.mark.skipif(
    os.getenv("E2E") != "1",
    reason="migration live check — set E2E=1 with hub-postgres up",
)
class TestMigration016Live:
    """Runs the DO block and the real incremental sync inside a transaction
    that is rolled back — the dev database is queried, never permanently
    mutated."""

    def test_unwrap_idempotent_and_delta_rescan(self):
        import manage_keys

        manage_keys._load_dotenv()
        dsn = os.getenv("E2E_DSN") or manage_keys._resolve_dsn(None)
        if not dsn:
            pytest.skip("no database DSN resolved")

        sql = MIGRATION_016.read_text(encoding="utf-8")

        class _Rollback(Exception):
            pass

        async def _go():
            import asyncpg

            conn = await asyncpg.connect(dsn=dsn)
            try:
                try:
                    async with conn.transaction():
                        # R5 round-4 isolation: hold an EXCLUSIVE lock for
                        # the whole tx so no concurrent writer can commit
                        # a fresh row past the pinned cursor or a new
                        # repairable string between the neutralise below
                        # and the real sync. Readers are unaffected; the
                        # lock drops with the rollback.
                        await conn.execute("LOCK TABLE canonical_places IN EXCLUSIVE MODE")
                        # R5 round-3 isolation, widened to every row: on a
                        # DB that already holds repairable rows the DO
                        # block below unwraps them all and stamps
                        # updated_at = now() — every one sorts past the
                        # (now(), 0) cursor and breaks scanned == 1. Pin
                        # all pre-existing rows behind the cursor before
                        # the fixture is seeded, then flip the
                        # brace-starting (repairable-candidate) ones to a
                        # real object; unparseable garbage keeps its value
                        # so the untouched-garbage asserts below still
                        # exercise the EXCEPTION branch.
                        await conn.execute(
                            "UPDATE canonical_places SET updated_at = now() - interval '30 days'"
                        )
                        repairable_before = await conn.fetchval(
                            "SELECT count(*) FROM canonical_places"
                            " WHERE jsonb_typeof(opening_hours) = 'string'"
                            "   AND opening_hours #>> '{}' LIKE '{%'"
                        )
                        await conn.execute(
                            "UPDATE canonical_places"
                            " SET opening_hours = '{\"neutralised\": true}'::jsonb"
                            " WHERE jsonb_typeof(opening_hours) = 'string'"
                            "   AND opening_hours #>> '{}' LIKE '{%'"
                        )
                        repairable_after = await conn.fetchval(
                            "SELECT count(*) FROM canonical_places"
                            " WHERE jsonb_typeof(opening_hours) = 'string'"
                            "   AND opening_hours #>> '{}' LIKE '{%'"
                        )
                        assert repairable_after == 0, (
                            f"repairable rows {repairable_before}"
                            f" -> {repairable_after}: neutralise incomplete"
                        )
                        seed = (
                            "INSERT INTO canonical_places"
                            " (canonical_name, normalized_name,"
                            "  normalized_address, status, confidence,"
                            "  source_count, opening_hours, updated_at)"
                            " VALUES ($1, $2, 'p203live', 'open', 0, 1, %s,"
                            "        now() - interval '2 days')"
                            " RETURNING place_id"
                        )
                        # double-encoded: a jsonb string holding the object
                        double_pid = (
                            await conn.fetchrow(
                                seed % "to_jsonb($3::text)",
                                "P203 Double",
                                "p203 double",
                                VN_HOURS_STR,
                            )
                        )["place_id"]
                        # garbage string starting with '{' → inner cast fails
                        garbage_pid = (
                            await conn.fetchrow(
                                seed % "to_jsonb($3::text)",
                                "P203 Garbage",
                                "p203 garbage",
                                "{broken",
                            )
                        )["place_id"]
                        # already an object — must be left alone
                        clean_pid = (
                            await conn.fetchrow(
                                seed % "$3::jsonb",
                                "P203 Clean",
                                "p203 clean",
                                VN_HOURS_STR,
                            )
                        )["place_id"]
                        strings_before = await conn.fetchval(
                            "SELECT count(*) FROM canonical_places"
                            " WHERE jsonb_typeof(opening_hours) = 'string'"
                        )
                        garbage_before = await conn.fetchval(
                            "SELECT updated_at FROM canonical_places WHERE place_id = $1",
                            garbage_pid,
                        )
                        # Pin the durable cursor at this transaction's now():
                        # only a row the DO block bumps to updated_at = now()
                        # sorts past it — every pre-existing row stays behind.
                        cursor_ts = await conn.fetchval("SELECT now()")

                        await conn.execute(sql)

                        val = await conn.fetchval(
                            "SELECT opening_hours FROM canonical_places WHERE place_id = $1",
                            double_pid,
                        )
                        assert json.loads(val) == VN_HOURS
                        garbage_type = await conn.fetchval(
                            "SELECT jsonb_typeof(opening_hours)"
                            " FROM canonical_places"
                            " WHERE place_id = $1",
                            garbage_pid,
                        )
                        assert garbage_type == "string"  # survived, untouched

                        # R5: real PlaceIndexer.sync() — the real
                        # SCAN_DELTA_SQL on real PG — from the pinned
                        # (updated_at, place_id) cursor. The repaired row
                        # must re-enter the delta; untouched rows must not.
                        state = DictIndexState()
                        await state.advance(
                            cursor_updated_at=cursor_ts,
                            cursor_place_id=0,
                            docs_delta=0,
                        )
                        idx = PlaceIndexer(
                            conn,
                            os_index=_MemIndex(),
                            cache=_NoopCache(),
                            state=state,
                        )
                        res = await idx.sync(batch_size=10)
                        assert res["status"] == "done" and res["scanned"] == 1
                        doc = idx._os.docs[str(double_pid)]
                        assert doc.opening_hours == VN_HOURS
                        assert str(garbage_pid) not in idx._os.docs
                        assert str(clean_pid) not in idx._os.docs
                        # untouched rows keep their old updated_at
                        assert (
                            await conn.fetchval(
                                "SELECT updated_at FROM canonical_places WHERE place_id = $1",
                                garbage_pid,
                            )
                            == garbage_before
                        )
                        strings_after = await conn.fetchval(
                            "SELECT count(*) FROM canonical_places"
                            " WHERE jsonb_typeof(opening_hours) = 'string'"
                        )
                        # every parseable string row flips to object — the
                        # garbage string is the only survivor on a clean DB
                        assert strings_before - strings_after >= 1

                        await conn.execute(sql)  # second run: idempotent
                        assert (
                            await conn.fetchval(
                                "SELECT count(*) FROM canonical_places"
                                " WHERE jsonb_typeof(opening_hours) = 'string'"
                            )
                            == strings_after
                        )
                        raise _Rollback
                except _Rollback:
                    pass
            finally:
                await conn.close()

        asyncio.run(_go())
