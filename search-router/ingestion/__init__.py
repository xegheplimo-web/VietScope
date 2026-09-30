"""P15 source ingestion: providers → raw staging → (P16) canonical places.

Google Maps / OSM / web / government output is *discovery* data, never
source of truth: every record lands in ``place_source_records`` with its
verbatim payload, source-native identity, provenance and P14 admin
anchor, so P16 can re-derive canonical entities without re-crawling.
"""
