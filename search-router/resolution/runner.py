"""P16 resolution runner — raw staging → canonical places.

Keyset-scans ``place_source_records`` (resumable cursor), blocks
candidates, scores pairs, merge-or-creates canonical places, then
recomputes field-level provenance and confidence. Same committed-
checkpoint discipline as the P15.1 ingest runner: a failed run persists
only the cursor of the last fully-processed batch.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from decimal import Decimal
from typing import Any

from resolution.match import MERGE_THRESHOLD, RESOLVER_VERSION, NormSource, score_pair
from resolution.normalize import (
    CategoryResolver,
    canonical_category,
    name_signature,
    name_tokens,
    norm_address,
    norm_name,
    norm_phone,
    norm_status,
    norm_website,
    website_domain,
)
from resolution.provenance import confidence_from, map_canonical, resolve_fields
from resolution.store import CanonicalStore, DictCanonicalStore, PgCanonicalStore

logger = logging.getLogger(__name__)

_PAGE_SQL = """
SELECT id, provider, external_id, raw_name, raw_address, raw_phone,
       raw_website, raw_category, raw_hours, raw_status, lat, lon,
       admin_unit_id, observed_at, raw_payload
FROM place_source_records
WHERE id > $1 AND record_status = 'valid'
ORDER BY id
LIMIT $2
"""

# raw_payload keys carrying image data (provider-verbatim — Google-style
# payloads seen so far use these names; anything URL-shaped is taken).
_IMAGE_PAYLOAD_KEYS = ("photos", "image", "thumbnail", "main_photo")

# canonical_places.review_count is Postgres int4 — out-of-range values are
# dropped at promotion rather than aborting the run on persistence overflow.
_REVIEW_COUNT_MAX = 2_147_483_647

# String review counts promote only as bare digits or strict
# thousands-grouping ("1,234", "1,234,567") — loose comma placements
# ("1,2", "1e,3", "1,,234") are malformed and drop the field.
_REVIEW_COUNT_STR_RE = re.compile(r"\d+|\d{1,3}(?:,\d{3})+")


def _image_urls(payload: dict[str, Any]) -> list[str]:
    """Ordered, deduped http(s) image URLs under the image-ish keys."""
    out: list[str] = []
    seen: set[str] = set()

    def _take(v: Any) -> None:
        if isinstance(v, str):
            u = v.strip()
            if u.lower().startswith(("http://", "https://")) and u not in seen:
                seen.add(u)
                out.append(u)
        elif isinstance(v, dict):
            for k in ("url", "src", "image_url"):
                _take(v.get(k))
        elif isinstance(v, (list, tuple)):
            for item in v:
                _take(item)

    for key in _IMAGE_PAYLOAD_KEYS:
        _take(payload.get(key))
    return out


def _jsonb_dict(v: Any) -> Any:
    """Staged jsonb arrives as str under asyncpg (no codec) — unwrap a
    JSON-object string back to its dict so the pipeline carries the real
    value and store writes never re-serialize text (P2.0.3). Anything else
    passes through verbatim."""
    if isinstance(v, str):
        try:
            out = json.loads(v)
        except ValueError:
            return v
        return out if isinstance(out, dict) else v
    return v


def _rich_fields(payload: Any) -> dict[str, Any]:
    """P2.0 promotion: extract rich-card fields from a stored raw_payload.

    Reads ONLY the verbatim staged payload — never the network. Anything
    missing or malformed is simply absent (→ NULL canonical), never a
    crash, so partial/foreign payloads resolve cleanly.
    """
    if isinstance(payload, str):  # asyncpg returns jsonb as str w/o codec
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    try:
        # Decimal(str()) is exact — float() overflows on ints like 10**400
        # and silently rounds out-of-range decimals into 0..5.
        rating = Decimal(str(payload["review_rating"]))
        if rating.is_finite() and 0 <= rating <= 5:
            out["rating"] = float(rating)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    try:
        count = payload["review_count"]
        if isinstance(count, str):
            s = count.strip()
            count = s.replace(",", "") if _REVIEW_COUNT_STR_RE.fullmatch(s) else None
        # exact integrality: "1.00000000000000001" is non-integral even
        # though float() collapses it to 1.0 — it must drop, not promote.
        if count is not None and not isinstance(count, bool):
            dcount = Decimal(str(count))
            if (
                dcount.is_finite()
                and dcount == dcount.to_integral_value()
                and 0 <= dcount <= _REVIEW_COUNT_MAX
            ):
                out["review_count"] = int(dcount)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    price = payload.get("price_range")
    if isinstance(price, str) and price.strip():
        out["price_level"] = price.strip()
    images = _image_urls(payload)
    if images:
        out["images"] = images
    return out


def _contrib(
    row: dict[str, Any],
    cats: CategoryResolver | None = None,
    *,
    log_unknown: bool = True,
) -> NormSource:
    """Normalized contributor view of a staged source row."""
    cat = (
        cats.resolve(
            row.get("raw_category"),
            provider=row.get("provider"),
            log_unknown=log_unknown,
        )
        if cats is not None
        else canonical_category(row.get("raw_category"))
    )
    phone = norm_phone(row.get("raw_phone"))
    website = norm_website(row.get("raw_website"))
    nn = norm_name(row.get("raw_name"))
    fields: dict[str, Any] = {}
    if row.get("raw_name"):
        fields["name"] = row["raw_name"].strip()
    if row.get("raw_address"):
        fields["address"] = row["raw_address"].strip()
    if phone:
        fields["phone"] = phone
    if website:
        fields["website"] = website
    if cat:
        fields["category"] = cat
    if row.get("raw_hours"):
        fields["hours"] = _jsonb_dict(row["raw_hours"])
    st = norm_status(row.get("raw_status"))
    if st:
        fields["status"] = st
    if row.get("lat") is not None and row.get("lon") is not None:
        fields["location"] = {"lat": row["lat"], "lon": row["lon"]}
    fields.update(_rich_fields(row.get("raw_payload")))
    return NormSource(
        record_id=row["id"],
        provider=row["provider"],
        external_id=row.get("external_id"),
        norm_name=nn,
        name_sig=name_signature(nn),
        tokens=name_tokens(nn),
        norm_address=norm_address(row.get("raw_address")),
        phone=phone,
        domain=website_domain(row.get("raw_website")),
        category=cat,
        lat=row.get("lat"),
        lon=row.get("lon"),
        admin_unit_id=row.get("admin_unit_id"),
        observed_at=row.get("observed_at"),
        fields=fields,
    )


async def _rescan(
    pool: Any, cursor: int, batch_size: int, provider: str | None
) -> list[dict[str, Any]]:
    sql = _PAGE_SQL
    args: list[Any] = [cursor, batch_size]
    if provider:
        sql = _PAGE_SQL.replace(
            "record_status = 'valid'", "record_status = 'valid' AND provider = $3"
        )
        args.append(provider)
    rows = await pool.fetch(sql, *args)
    return [dict(r) for r in rows]


async def _apply(
    store: CanonicalStore,
    policies: dict[str, dict],
    place_id: int,
    run_id: int,
    src: NormSource,
    extra_rows: list[dict[str, Any]] | None = None,
    cats: CategoryResolver | None = None,
) -> int:
    """Recompute provenance + canonical fields for a place. Returns
    fields-written count."""
    staged = await store.sources_for(place_id)
    # staged re-reads were already counted when scanned — don't double-log
    contribs = [_contrib(r, cats, log_unknown=False) for r in staged]
    if extra_rows:
        contribs.extend(_contrib(r, cats, log_unknown=False) for r in extra_rows)
    if not any(c.record_id == src.record_id for c in contribs):
        contribs.append(src)
    fields, prov = resolve_fields(place_id, contribs, policies)
    canonical = map_canonical(fields)
    providers = {c.provider for c in contribs}
    updates = {
        **canonical,
        "normalized_name": norm_name(canonical.get("canonical_name", "")),
        "normalized_address": norm_address(canonical.get("address", "")),
        "confidence": confidence_from(prov),
        "source_count": len(providers),
        "resolution_run_id": run_id,
    }
    if "website" in canonical:
        updates["website_domain"] = website_domain(canonical["website"])
    await store.update_place(place_id, updates)
    await store.write_provenance(prov)
    return len(prov)


async def run_resolution(
    pool: Any,
    *,
    store: CanonicalStore | None = None,
    provider: str | None = None,
    since_id: int = 0,
    batch_size: int = 200,
    threshold: float = MERGE_THRESHOLD,
    resume_of: int | None = None,
    sources_feed: Any = None,
    relink_stale: bool = False,
) -> dict[str, Any]:
    """Resolve staged source records into canonical places.

    ``sources_feed`` overrides the staging scan (tests / dict mode):
    an async-iterable of staged row dicts — cursor then means record id.
    """
    store = store or (PgCanonicalStore(pool) if pool else DictCanonicalStore())
    counters: Counter[str] = Counter()
    cursor = since_id
    error_summary: dict[str, Any] = {}
    saved_cursor = cursor
    status = "done"

    params: dict[str, Any] = {"provider": provider, "since_id": since_id}
    if relink_stale:
        params["relink_stale"] = True
    run_id = 0
    lock_conn: Any = None
    if pool is not None:
        if resume_of:
            prior = await pool.fetchrow(
                "SELECT parameters, cursor, checkpoint FROM resolution_runs"
                " WHERE run_id = $1 AND status IN ('done','failed','aborted')",
                resume_of,
            )
            if prior is None:
                raise ValueError(f"resume run {resume_of} not found or still running")
            prior_params = prior["parameters"]
            if isinstance(prior_params, str):
                prior_params = json.loads(prior_params)
            # only explicitly-supplied params override the stored ones
            explicit = {
                k: v
                for k, v in params.items()
                if v is not None and not (k == "since_id" and v == 0)
            }
            params = {**prior_params, **explicit}
            provider = params.get("provider")
            relink_stale = bool(params.get("relink_stale"))
            ck = prior["checkpoint"]
            if isinstance(ck, str):
                ck = json.loads(ck)
            if not since_id:
                if prior["cursor"] is not None:
                    cursor = int(prior["cursor"])
                elif ck.get("cursor") is not None:
                    cursor = int(ck["cursor"])
            saved_cursor = cursor
            params["since_id"] = cursor
        # single-writer: a second concurrent resolution run fails fast.
        # Session-level advisory lock → the conn must stay checked out
        # for the run's lifetime, released after finalizing.
        lock_conn = await pool.acquire()
        lr = await lock_conn.fetchrow("SELECT pg_try_advisory_lock(1600016) AS ok")
        if not (lr and lr["ok"]):
            await pool.release(lock_conn)
            raise RuntimeError("another resolution run holds the advisory lock")
        r = await pool.fetchrow(
            """INSERT INTO resolution_runs
               (status, parameters, resolver_version, resume_of)
               VALUES ('running', $1::jsonb, $2, $3) RETURNING run_id""",
            json.dumps(params, ensure_ascii=False),
            RESOLVER_VERSION,
            resume_of,
        )
        run_id = int(r["run_id"])

    # relink-stale rebuild: places whose links were written by a different
    # matcher version (NULL = pre-telemetry) are dropped wholesale — their
    # canonical fields may be blends of wrongly-merged sources, so the
    # records re-resolve fresh below rather than re-scoring against a
    # poisoned place. One statement; FK order: provenance, links, places.
    if params.get("relink_stale") and pool is not None:
        async with pool.acquire() as conn, conn.transaction():
            # materialize first — the stale set is computed from
            # place_sources, which the deletes below mutate
            stale_ids = [
                int(r["place_id"])
                for r in await conn.fetch(
                    "SELECT DISTINCT place_id FROM place_sources"
                    " WHERE matcher_version IS DISTINCT FROM $1",
                    RESOLVER_VERSION,
                )
            ]
            del_prov = del_links = del_places = "DELETE 0"
            if stale_ids:
                del_prov = await conn.execute(
                    "DELETE FROM place_field_provenance WHERE place_id = ANY($1::bigint[])",
                    stale_ids,
                )
                # drop ALL links of tainted places, not just stale ones — a
                # place's fields blend every linked source, so any stale link
                # contaminates the whole place
                del_links = await conn.execute(
                    "DELETE FROM place_sources WHERE place_id = ANY($1::bigint[])",
                    stale_ids,
                )
                del_places = await conn.execute(
                    "DELETE FROM canonical_places WHERE place_id = ANY($1::bigint[])",
                    stale_ids,
                )
        counters["rebuilt_places"] = int(del_places.split()[-1])
        logger.info(
            "relink-stale: dropped %s prov rows / %s links / %s places",
            del_prov.split()[-1],
            del_links.split()[-1],
            del_places.split()[-1],
        )

    # source_policies authority map → field weights
    policies: dict[str, dict] = {}
    if pool is not None:
        try:
            for pr in await pool.fetch("SELECT provider, authority FROM source_policies"):
                row = dict(pr)
                if isinstance(row.get("authority"), str):
                    row["authority"] = json.loads(row["authority"])
                policies[row["provider"]] = row
        except Exception as exc:
            logger.warning("source_policies read failed, defaults apply: %r", exc)

    # DB category overlay (migration 014) over the built-in map — a new
    # provider label becomes a row, not a deploy. Missing table → built-ins.
    cats = CategoryResolver()
    if pool is not None:
        try:
            for mr in await pool.fetch(
                "SELECT provider, category_key, canonical_category FROM source_category_mappings"
            ):
                cats.add(mr["provider"], mr["category_key"], mr["canonical_category"])
        except Exception as exc:
            logger.warning("source_category_mappings read failed, built-ins apply: %r", exc)

    async def _checkpoint(c: int) -> None:
        if pool is None:
            return
        await pool.execute(
            "UPDATE resolution_runs SET cursor = $2, checkpoint = $3::jsonb,"
            " records_scanned = $4, pairs_scored = $5, places_created = $6,"
            " places_merged = $7, fields_written = $8 WHERE run_id = $1",
            run_id,
            c,
            json.dumps({"cursor": c}),
            counters["scanned"],
            counters["scored"],
            counters["created"],
            counters["merged"],
            counters["fields"],
        )

    async def _process(row: dict[str, Any]) -> None:
        counters["scanned"] += 1
        src = _contrib(row, cats)
        link_score: float | None = None
        link_reason = "existing_link"
        # a record is bound to one place — reuse that link even when the
        # record has no external_id (re-resolution stays idempotent)
        place_id = await store.find_place_by_record(src.record_id)
        if place_id is None and src.external_id:
            place_id = await store.find_place_by_source(src.provider, src.external_id)
            if place_id is not None:
                link_reason = "external_id"
        if place_id is None:
            best, best_score, best_reason = None, 0.0, None
            for cand in await store.candidates(src):
                score, force = score_pair(src, cand.as_norm())
                counters["scored"] += 1
                if force or score > best_score:
                    best, best_score = cand, (1.0 if force else score)
                    best_reason = f"force:{force}" if force else "weighted"
            if best is not None and (best_score >= threshold or best_score == 1.0):
                place_id = best.place_id
                link_score, link_reason = best_score, best_reason
                counters["merged"] += 1
        if place_id is None:
            link_reason = "create"
            # brand-level merge: branches of the same normalized name share
            # one canonical_businesses row rather than duplicating per place
            biz = await store.find_business_by_name(
                src.norm_name or "",
                category=src.category,
                phone=src.phone,
                website_domain=src.domain,
                admin_unit_id=src.admin_unit_id,
            )
            if biz is None:
                biz = await store.create_business(
                    (row.get("raw_name") or "").strip() or "unknown",
                    src.norm_name or "unknown",
                )
            place_id = await store.create_place(
                {
                    "business_id": biz,
                    "canonical_name": (row.get("raw_name") or "").strip() or "unknown",
                    "normalized_name": src.norm_name or "unknown",
                    "canonical_category": src.category,
                    "address": (row.get("raw_address") or "").strip() or None,
                    "normalized_address": src.norm_address or None,
                    "phone": src.phone,
                    "website": norm_website(row.get("raw_website")),
                    "opening_hours": src.fields.get("hours"),
                    "lat": src.lat,
                    "lon": src.lon,
                    "admin_unit_id": src.admin_unit_id,
                    "status": src.fields.get("status", "open"),
                    "confidence": 0.0,
                    "source_count": 1,
                    "resolution_run_id": run_id,
                }
            )
            counters["created"] += 1
        linked = await store.link_source(
            place_id,
            src.record_id,
            src.provider,
            src.external_id,
            run_id,
            score=link_score,
            reason=link_reason,
        )
        if not linked:
            # lost the link race — another place already owns this record;
            # fold this row into the winner instead of the orphan we made
            winner = await store.find_place_by_record(src.record_id)
            if winner is not None:
                place_id = winner
        if isinstance(store, DictCanonicalStore):
            store.seed_source(src.record_id, row)
        counters["fields"] += await _apply(store, policies, place_id, run_id, src, cats=cats)

    try:
        if sources_feed is not None:
            batch: list[dict[str, Any]] = []
            async for row in sources_feed:
                if row["id"] <= cursor:
                    continue
                batch.append(row)
                if len(batch) >= batch_size:
                    for r_ in batch:
                        await _process(r_)
                    saved_cursor = batch[-1]["id"]
                    await _checkpoint(saved_cursor)
                    batch.clear()
            if batch:
                for r_ in batch:
                    await _process(r_)
                saved_cursor = batch[-1]["id"]
        else:
            while True:
                batch = await _rescan(pool, saved_cursor, batch_size, provider)
                if not batch:
                    break
                for r_ in batch:
                    await _process(r_)
                saved_cursor = batch[-1]["id"]
                await _checkpoint(saved_cursor)
    except Exception as exc:
        status = "failed"
        logger.exception("resolution run %d failed", run_id)
        error_summary = {"fatal": repr(exc)[:500]}

    # post-run merge audit — flags implausible merges on the places this
    # run touched so they are reviewable instead of silently trusted
    audit: dict[str, Any] = {}
    if status == "done":
        try:
            audit = await store.audit_merges(run_id)
        except Exception as exc:
            logger.warning("merge audit failed (run unaffected): %r", exc)

    # persist the labels nothing mapped — even a failed run's partial
    # unknowns are worth recording since they arrived before the failure
    unknown_categories = len(cats.unknowns)
    if pool is not None and cats.unknowns:
        try:
            for provider_k, key, sample, n in cats.unknown_rows():
                await pool.execute(
                    """INSERT INTO unknown_source_categories
                       (provider, category_key, raw_sample, seen_count)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (provider, category_key) DO UPDATE SET
                           seen_count = unknown_source_categories.seen_count
                                        + EXCLUDED.seen_count,
                           last_seen_at = now()""",
                    provider_k,
                    key,
                    sample,
                    n,
                )
        except Exception as exc:
            logger.warning("unknown category log failed (run unaffected): %r", exc)

    if pool is not None:
        await pool.execute(
            """UPDATE resolution_runs SET status = $2, completed_at = now(),
               cursor = $3, checkpoint = $4::jsonb, records_scanned = $5,
               pairs_scored = $6, places_created = $7, places_merged = $8,
               fields_written = $9, error_summary = $10::jsonb,
               audit_summary = $11::jsonb
               WHERE run_id = $1""",
            run_id,
            status,
            saved_cursor,
            json.dumps({"cursor": saved_cursor}),
            counters["scanned"],
            counters["scored"],
            counters["created"],
            counters["merged"],
            counters["fields"],
            json.dumps(error_summary),
            json.dumps(audit),
        )
        if lock_conn is not None:
            try:
                await lock_conn.execute("SELECT pg_advisory_unlock(1600016)")
            finally:
                await pool.release(lock_conn)

    return {
        "run_id": run_id,
        "status": status,
        "resume_of": resume_of,
        "resolver_version": RESOLVER_VERSION,
        "cursor": saved_cursor,
        "scanned": counters["scanned"],
        "scored": counters["scored"],
        "created": counters["created"],
        "merged": counters["merged"],
        "fields_written": counters["fields"],
        "rebuilt_places": counters["rebuilt_places"],
        "suspicious": audit.get("suspicious", 0),
        "unknown_categories": unknown_categories,
        "errors": error_summary,
    }
