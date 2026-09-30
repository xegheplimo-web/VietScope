"""P17.1 API probe — /v1/places/search?min_rating=4.5&open_now=true&limit=5.

In-process FastAPI roundtrip: real param binding + PlaceService fusion
over a FakeOS lane (no Docker needed). Asserts the filter semantics the
contract requires, then prints the wire response.

Run: PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/probe_p17_1.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api.v1 as api_v1  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from serving.places.cache import PlaceCache  # noqa: E402
from serving.places.document import PlaceDocumentV1  # noqa: E402
from serving.places.os_index import build_search_body  # noqa: E402
from serving.places.service import PlaceService  # noqa: E402

NOW = datetime(2026, 9, 30, 10, 0)  # Wednesday 10:00 UTC+7 wall time
OPEN_DAY = {
    d: ["06:00–22:00"]
    for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
}
NIGHT_DAY = {
    d: ["18:00–23:00"]
    for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
}


def _doc(i, **kw):
    base = {
        "place_id": str(i),
        "business_id": None,
        "name": f"Probe Place {i}",
        "normalized_name": f"probe place {i}",
        "category_ids": ["food"],
        "status": "open",
        "confidence": 0.9,
        "freshness_score": 0.9,
        "source_count": 2,
    }
    base.update(kw)
    return PlaceDocumentV1(**base)


class FakeOS:
    def __init__(self, docs):
        self.docs = list(docs)

    async def search(self, spec, top_k):
        body = build_search_body(spec, top_k=top_k)
        print("[probe] OS body filters:", body["query"]["bool"]["filter"])
        out = []
        for d in self.docs:
            if spec.statuses and d.status not in spec.statuses:
                continue
            if spec.min_rating is not None and (d.rating is None or d.rating < spec.min_rating):
                continue
            if spec.price_level and d.price_level != spec.price_level:
                continue
            out.append((d, 1.0))
        return out[:top_k]


async def _nopool():
    return None


docs = [
    _doc(1, rating=4.8, review_count=120, opening_hours=OPEN_DAY),  # PASS
    _doc(2, rating=4.9, review_count=30, opening_hours=NIGHT_DAY),  # closed at 10:00
    _doc(3, rating=3.0, review_count=500, opening_hours=OPEN_DAY),  # below min_rating
    _doc(4, rating=4.6, review_count=12, opening_hours=OPEN_DAY),  # PASS
    _doc(5, rating=None, opening_hours=OPEN_DAY),  # unrated
]
svc = PlaceService(
    os_index=FakeOS(docs),
    cache=PlaceCache(redis_enabled=False),
    pool_getter=_nopool,
    clock=lambda: NOW,
)
api_v1._get_places_service = lambda: svc  # noqa: SLF001

app = FastAPI()
app.include_router(api_v1.router)
client = TestClient(app)

r = client.get("/v1/places/search?min_rating=4.5&open_now=true&limit=5")
print("[probe] HTTP", r.status_code)
print("[probe] X-Places-Lanes:", r.headers.get("X-Places-Lanes"))
rows = r.json()
print("[probe] rows:", [(x["place_id"], x["rating"], x["open_now"]) for x in rows])

assert r.status_code == 200, r.text
ids = [x["place_id"] for x in rows]
assert ids == [1, 4], f"expected [1, 4], got {ids}"
assert all(x["open_now"] is True for x in rows)
assert all(x["rating"] >= 4.5 for x in rows)

r2 = client.get("/v1/places/search?open_now=false&limit=5")
ids2 = [x["place_id"] for x in r2.json()]
assert ids2 == [2], f"expected [2], got {ids2}"

r3 = client.get("/v1/places/search?sort=rating&limit=5")
ids3 = [x["place_id"] for x in r3.json()]
# Bayesian: 4.8×120 (≈0.94) outranks 4.9×30 (≈0.91); unrated sinks low.
assert ids3 == [1, 2, 4, 5, 3], f"unexpected rating order {ids3}"

r4 = client.get("/v1/places/search?price_level=%E2%82%AB%E2%82%AB&limit=5")
assert r4.status_code == 200

print("[probe] ALL ASSERTIONS PASSED")
