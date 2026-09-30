"""P16 entity resolution: raw source records → canonical Place graph.

Pipeline (all deterministic, no ML deps):
    place_source_records (P15 raw)
      → normalize (Vietnamese name/phone/website/category/address)
      → candidate generation (blocking keys)
      → pairwise match scoring
      → merge-or-create canonical_places
      → field-level provenance + confidence resolution

Legacy ``businesses``/``BusinessStore`` stays the query-time compatibility
layer — nothing here writes to it.
"""
