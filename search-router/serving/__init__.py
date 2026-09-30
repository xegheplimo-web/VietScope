"""P17 serving layer — read-side projections, indexes, caches, retrieval.

Boundary rule: serving code consumes the frozen ``PlaceDocumentV1``
contract (``serving.places.document``) projected from the canonical graph.
It must never import ``resolution/`` internals or read resolver-private
structures — canonical PostgreSQL/PostGIS is crossed only through the
projection.
"""
