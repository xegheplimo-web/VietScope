"""Concrete PlaceSourceAdapter implementations (P15 V1)."""

from ingestion.adapters.gmaps import GoogleMapsAdapter
from ingestion.adapters.osm_osmium import OsmiumPbfAdapter
from ingestion.adapters.osm_pbf import OsmPbfAdapter
from ingestion.adapters.web_corpus import WebCorpusAdapter

ADAPTERS: dict[str, type] = {
    GoogleMapsAdapter.name: GoogleMapsAdapter,
    OsmPbfAdapter.name: OsmPbfAdapter,
    WebCorpusAdapter.name: WebCorpusAdapter,
}

__all__ = [
    "ADAPTERS",
    "GoogleMapsAdapter",
    "OsmPbfAdapter",
    "OsmiumPbfAdapter",
    "WebCorpusAdapter",
]
