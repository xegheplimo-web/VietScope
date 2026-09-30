#!/usr/bin/env python3
"""Build the canonical VN administrative seed (P14A).

Emits ``db/seeds/vn_admin_units.json`` — a temporal administrative graph:

* ``new:*``  — current post-2025 two-level geography: 34 province-level
  units + 3,321 commune-level units (xã/phường/đặc khu), keyed by the
  official codes in Công văn 915/CTK-CSCL + 1027/CTK-CSCL (ban hành kèm
  Quyết định 19/2025/QĐ-TTg), commune composition from the 34 NQ-UBTVQH15
  resolutions (1654–1687), provincial merger per NQ 202/2025/QH15 and
  NQ 60-NQ/TW. Effective 2025-07-01.
* ``old:*``  — the pre-2025 three-level geography (province → district →
  commune): the GSO/dvhcvn 2025-03-01 snapshot plus the December-2024
  dissolution round recovered from the 2024-11-01 snapshot (e.g. huyện
  Yên Dũng → TP Bắc Giang under NQ 1191/NQ-UBTVQH15, hiệu lực 1/1/2025).

Edges encode renamed_to / merged_into / split_into / replaced_by /
boundary_changed with effective dates and legal-source provenance.

Inputs:
    --dvhcvn DIR     clone of github.com/daohoangson/dvhcvn at the
                     20250701 layout (mapping/{splits,merges}.json,
                     input/docs/{915_CTK-CSCL.csv,1027_CTK-CSCL.md})
    --snapshots DIR  dir with sorted-20241101.json + sorted-20250301.json;
                     auto-downloaded from the dvhcvn release tags when absent
    --out PATH       output path (default db/seeds/vn_admin_units.json)

Machine-readable mappings are a mirror of the official texts — the
``source`` fields cite the legal documents, not the mirror.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import unicodedata
import urllib.request
from collections import Counter
from pathlib import Path

_TONE_RE = re.compile(r"[̀-̣ͯ]+")
_STRIP = str.maketrans("đĐ", "dD")
_WS_RE = re.compile(r"[^\w]+", re.UNICODE)

_TYPE_PREFIXES = (
    "thành phố trực thuộc",
    "thành phố thuộc",
    "thành phố",
    "thị xã",
    "thị trấn",
    "tỉnh",
    "quận",
    "huyện",
    "phường",
    "xã",
    "đặc khu",
)

_SNAPSHOTS = {
    "sorted-20241101.json": (
        "https://raw.githubusercontent.com/daohoangson/dvhcvn/v20241101/data/sorted.json"
    ),
    "sorted-20250301.json": (
        "https://raw.githubusercontent.com/daohoangson/dvhcvn/v20250301/data/sorted.json"
    ),
}

# NQ-UBTVQH15 commune-level reorganization resolution per *new* province
# (docs/16NN_NQ-UBTVQH15.md in the dvhcvn mirror).
_PROV_NQ = {
    "an giang": 1654,
    "ca mau": 1655,
    "ha noi": 1656,
    "cao bang": 1657,
    "bac ninh": 1658,
    "da nang": 1659,
    "dak lak": 1660,
    "dien bien": 1661,
    "dong nai": 1662,
    "dong thap": 1663,
    "gia lai": 1664,
    "ha tinh": 1665,
    "hung yen": 1666,
    "khanh hoa": 1667,
    "can tho": 1668,
    "hai phong": 1669,
    "lai chau": 1670,
    "lam dong": 1671,
    "lang son": 1672,
    "lao cai": 1673,
    "ninh binh": 1674,
    "hue": 1675,
    "phu tho": 1676,
    "quang ngai": 1677,
    "nghe an": 1678,
    "quang ninh": 1679,
    "quang tri": 1680,
    "son la": 1681,
    "tay ninh": 1682,
    "thai nguyen": 1683,
    "tuyen quang": 1684,
    "ho chi minh": 1685,
    "thanh hoa": 1686,
    "vinh long": 1687,
}

NEW_ERA_START = "2025-07-01"
OLD_ERA_END = "2025-06-30"
WAVE1_END = "2024-12-31"  # effective dates vary 2024-12→2025-02; the Dec-2024
# NQ-UBTVQH15 dissolution round, e.g. NQ 1191 (Yên Dũng) eff. 2025-01-01.

SRC_NEW_PROV = "NQ 202/2025/QH15; NQ 60-NQ/TW; Cong van 915/CTK-CSCL; QD 19/2025/QD-TTg"
SRC_OLD = "GSO DVHC snapshot 2025-03-01 (dvhcvn mirror)"
SRC_WAVE1 = "GSO DVHC snapshot 2024-11-01 + NQ-UBTVQH15 sap xep 2023-2025 (derived)"


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(_STRIP))
    return _TONE_RE.sub("", decomposed).lower().strip()


def flat(text: str) -> str:
    return _WS_RE.sub(" ", fold(text)).strip()


_FOLDED_PREFIXES = sorted({fold(p) for p in _TYPE_PREFIXES}, key=len, reverse=True)


def short_name(name: str) -> str:
    """Strip the leading type prefix: 'Phường Ba Đình' -> 'Ba Đình'.

    Prefix matching runs on the folded form (diacritics-free) so callers
    may pass folded or display names alike.
    """
    n = name.strip()
    low = fold(n)
    for pref_fold in _FOLDED_PREFIXES:
        if low == pref_fold:
            return n
        if low.startswith(pref_fold + " "):
            # recover the unstripped suffix from the raw string by word count
            cut = len(pref_fold.split())
            return " ".join(n.split()[cut:]) or n
    return n


def norm_name(name: str) -> str:
    """Folded, type-stripped lookup key: 'Huyện Yên Dũng' -> 'yen dung'."""
    n = fold(name.strip())
    for pref_fold in _FOLDED_PREFIXES:
        if n == pref_fold:
            break
        if n.startswith(pref_fold + " "):
            n = n[len(pref_fold) :].strip()
            break
    return _WS_RE.sub(" ", n).strip()


_TYPE_NORM = {
    "thanh pho": "thanh_pho",
    "tinh": "tinh",
    "quan": "quan",
    "huyen": "huyen",
    "thi xa": "thi_xa",
    "thi tran": "thi_tran",
    "phuong": "phuong",
    "xa": "xa",
    "dac khu": "dac_khu",
}
_TYPE_DISPLAY = {
    "thanh_pho": "Thành phố",
    "tinh": "Tỉnh",
    "quan": "Quận",
    "huyen": "Huyện",
    "thi_xa": "Thị xã",
    "thi_tran": "Thị trấn",
    "phuong": "Phường",
    "xa": "Xã",
    "dac_khu": "Đặc khu",
}


def unit_type(name: str) -> str:
    """Type token from a full name like 'Phường Ba Đình' -> 'phuong'."""
    n = fold(name.strip())
    for pref in _FOLDED_PREFIXES:
        if n.startswith(pref + " ") or n == pref:
            return _TYPE_NORM[pref]
    return "tinh"  # bare province names carry no type prefix


def display_name(bare: str, type_token: str, level: int) -> str:
    """Compose the readable name: snapshots keep names bare + a type field."""
    if level == 1:
        return bare  # provinces read 'Bắc Giang', not 'Tỉnh Bắc Giang'
    return f"{_TYPE_DISPLAY[type_token]} {bare}"


def parse_snapshot(path: Path) -> dict:
    """dvhcvn sorted.json -> {code: {name,type,level,parent}} (nested arrays).

    Each entry is ``[code, bare_name, type_label, non_accented, children]``;
    display names are composed from the type label ("Huyện Yên Dũng").
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    units: dict[str, dict] = {}

    def add(item, level, parent):
        code, bare, raw_type = str(item[0]), item[1], item[2]
        tok = _TYPE_NORM[fold(raw_type)]
        units[code] = {
            "bare": bare,
            "name": display_name(bare, tok, level),
            "type": tok,
            "level": level,
            "parent": parent,
        }

    for p in raw:
        add(p, 1, None)
        for d in p[4]:
            add(d, 2, str(p[0]))
            for c in d[4]:
                add(c, 3, str(d[0]))
    return units


def parse_1027(path: Path) -> dict[str, str]:
    """Official commune catalog: {code: name}."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        stt, code, name = cells[0], cells[1], cells[2]
        if re.fullmatch(r"\d+", stt or "") and re.fullmatch(r"\d{5}", code):
            out[code] = re.sub(r"<br\s*/?>", " ", name).strip()
    return out


def parse_915(path: Path) -> dict[str, str]:
    """Official province catalog: {code: name}."""
    out = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if len(row) >= 3 and re.fullmatch(r"\d{2}", row[1].strip()):
                out[row[1].strip()] = row[2].strip()
    return out


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, dest)  # noqa: S310 - fixed upstream URLs
    return dest


def build(
    dvhcvn_dir: Path,
    snap_dir: Path,
    geojson_dir: Path | None = None,
    geom_tolerance: float = 0.0005,
) -> dict:
    nov = parse_snapshot(snap_dir / "sorted-20241101.json")
    mar = parse_snapshot(snap_dir / "sorted-20250301.json")
    merges = json.loads((dvhcvn_dir / "mapping" / "merges.json").read_text(encoding="utf-8"))[
        "data"
    ]
    splits = json.loads((dvhcvn_dir / "mapping" / "splits.json").read_text(encoding="utf-8"))[
        "data"
    ]
    cat_communes = parse_1027(dvhcvn_dir / "input" / "docs" / "1027_CTK-CSCL.md")
    cat_provs = parse_915(dvhcvn_dir / "input" / "docs" / "915_CTK-CSCL.csv")
    assert len(cat_provs) == 34, len(cat_provs)
    assert len(cat_communes) == 3321, len(cat_communes)

    units: dict[str, dict] = {}
    relations: list[dict] = []
    aliases: list[dict] = []

    def add_unit(key, code, name, level, parent_key, vf, vt, status, source, type_token=None):
        assert key not in units, key
        units[key] = {
            "key": key,
            "code": code,
            "name": name,
            "normalized_name": norm_name(name),
            "type": type_token or unit_type(name),
            "admin_level": level,
            "parent_key": parent_key,
            "valid_from": vf,
            "valid_to": vt,
            "status": status,
            "source": source,
        }

    # ── old era: union of the Nov-2024 and Mar-2025 snapshots ─────────────
    # Parent links come from the earliest snapshot the unit appears in, so a
    # commune reparented in the Dec-2024 dissolution round keeps its original
    # district ("Cảnh Thụy" stays under huyện Yên Dũng for resolution).
    old_codes = set(nov) | set(mar)
    for code in sorted(old_codes):
        src_snap = nov.get(code) or mar[code]
        in_mar = code in mar
        wave1_dissolved = code in nov and not in_mar
        level = src_snap["level"]
        parent_code = src_snap["parent"]
        add_unit(
            f"old:{code}",
            code,
            src_snap["name"],
            level,
            f"old:{parent_code}" if parent_code else None,
            None,
            WAVE1_END if wave1_dissolved else OLD_ERA_END,
            "historical",
            SRC_WAVE1 if wave1_dissolved else SRC_OLD,
            type_token=src_snap["type"],
        )
        # renamed between snapshots → keep the Nov-2024 name as a historical
        # alias on the Mar-2025 row.
        if in_mar and nov.get(code) and flat(nov[code]["name"]) != flat(mar[code]["name"]):
            aliases.append(
                {
                    "unit_key": f"old:{code}",
                    "alias": nov[code]["name"],
                    "normalized_alias": flat(nov[code]["name"]),
                    "alias_type": "historical",
                    "valid_from": None,
                    "valid_to": WAVE1_END,
                }
            )

    # ── new era: 34 provinces + 3,321 communes ─────────────────────────────
    merge_index = {}  # new code -> list of (old_id, type)
    prov_name_to_code = {}
    for p in merges:
        new_code = str(p["level1_id"])
        official = cat_provs.get(new_code)
        assert official, f"new province {new_code} missing from 915/CTK-CSCL"
        prov_name_to_code[flat(official)] = new_code
        add_unit(
            f"new:{new_code}",
            new_code,
            official,
            1,
            None,
            NEW_ERA_START,
            None,
            "current",
            SRC_NEW_PROV,
        )
        merge_index[new_code] = [(m["old_id"], m["type"]) for m in p["merges"]]
        for c in p["level2s"]:
            c_code = str(c["level2_id"])
            official_c = cat_communes.get(c_code)
            assert official_c, f"new commune {c_code} missing from 1027/CTK-CSCL"
            prov_norm = flat(short_name(official))
            nq = _PROV_NQ.get(prov_norm)
            src = (
                f"NQ {nq}/NQ-UBTVQH15 (2025); Cong van 1027/CTK-CSCL; QD 19/2025/QD-TTg"
                if nq
                else "Cong van 1027/CTK-CSCL; QD 19/2025/QD-TTg"
            )
            add_unit(
                f"new:{c_code}",
                c_code,
                official_c,
                3,
                f"new:{new_code}",
                NEW_ERA_START,
                None,
                "current",
                src,
            )
            merge_index[c_code] = [(m["old_id"], m["type"]) for m in c["merges"]]

    # commune name variants the official catalog keeps verbatim.
    assert len([k for k in units if k.startswith("new:")]) == 34 + 3321

    # ── edges: province level (63 old → 34 new) ────────────────────────────
    for p in splits:
        old_code = str(p["level1_id"])
        for sp in p["splits"]:
            new_code = str(sp["new_id"])
            sole = len(p["splits"]) == 1
            contributors = len(merge_index[new_code])
            same_name = flat(p["name"]) == flat(sp["name"])
            rtype = (
                "renamed_to"
                if (sole and contributors == 1 and same_name)
                else "split_into"
                if not sole
                else "merged_into"
            )
            relations.append(
                {
                    "from_key": f"old:{old_code}",
                    "to_key": f"new:{new_code}",
                    "relation_type": rtype,
                    "effective_date": NEW_ERA_START,
                    "source": SRC_NEW_PROV,
                }
            )

    # ── edges: commune level (10,047 old → 3,321 new) ─────────────────────
    for p in splits:
        prov_new = None
        for c in p["level3s"]:
            old_code = str(c["level3_id"])
            if f"old:{old_code}" not in units:
                continue
            for sp in c["splits"]:
                new_code = str(sp["new_id"])
                prov_new = prov_new or _parent_code_of(units, f"new:{new_code}")
                n_targets = len(c["splits"])
                contrib = dict(merge_index[new_code]).get(old_code, "full")
                same_name = flat(c["name"]) == flat(sp["name"])
                n_contributors = len(merge_index[new_code])
                if n_targets > 1:
                    rtype = "split_into"
                elif contrib == "partial":
                    rtype = "boundary_changed"
                elif n_contributors == 1 and same_name:
                    rtype = "renamed_to"
                else:
                    rtype = "merged_into"
                nq = _PROV_NQ.get(
                    flat(short_name(units[f"new:{prov_new}"]["name"])) if prov_new else ""
                )
                relations.append(
                    {
                        "from_key": f"old:{old_code}",
                        "to_key": f"new:{new_code}",
                        "relation_type": rtype,
                        "effective_date": NEW_ERA_START,
                        "source": (
                            f"NQ {nq}/NQ-UBTVQH15 (2025)"
                            if nq
                            else "NQ-UBTVQH15 sap xep cap xa (2025)"
                        ),
                    }
                )

    # ── edges: district level, derived via constituent communes ───────────
    # A historical district maps to the set of new communes its communes
    # became (each commune edge is already authoritative).
    old2new_communes: dict[str, set[str]] = {}
    for r in relations:
        if r["from_key"].startswith("old:"):
            old2new_communes.setdefault(r["from_key"], set()).add(r["to_key"])
    district_children = {}
    for key, u in units.items():
        if u["parent_key"] and u["admin_level"] == 3:
            district_children.setdefault(u["parent_key"], []).append(key)
    for dkey, kids in district_children.items():
        if units[dkey]["admin_level"] != 2:
            continue  # new communes also parent under level-1 keys — skip
        targets: set[str] = set()
        for k in kids:
            targets |= old2new_communes.get(k, set())
        for t in sorted(targets):
            relations.append(
                {
                    "from_key": dkey,
                    "to_key": t,
                    "relation_type": ("merged_into" if len(targets) == 1 else "split_into"),
                    "effective_date": NEW_ERA_START,
                    "source": "derived: constituent communes + NQ-UBTVQH15 (2025)",
                }
            )

    # ── edges: the Dec-2024 dissolution wave (Nov-2024 → Mar-2025) ─────────
    # Districts absent from the Mar-2025 snapshot map to the districts that
    # received their communes (e.g. huyện Yên Dũng → TP Bắc Giang).
    for code in sorted(set(nov) - set(mar)):
        u = nov[code]
        if u["level"] != 2:
            continue
        kids = [c for c, cu in nov.items() if cu["parent"] == code]
        hosts = Counter(mar[c]["parent"] for c in kids if c in mar and mar[c]["parent"])
        if not hosts:
            continue
        # renamed_to when a brand-new host district is fed solely by this
        # dissolved one (e.g. TP Ninh Bình → TP Hoa Lư, Long Điền → Long Đất).
        feeders_of_host = {
            h: {nov[c]["parent"] for c, cu in mar.items() if cu["parent"] == h and c in nov}
            for h in hosts
        }
        for host in sorted(hosts):
            host_is_new = host not in nov
            if len(hosts) == 1 and host_is_new and feeders_of_host[host] == {code}:
                rtype = "renamed_to"
            elif len(hosts) == 1:
                rtype = "merged_into"
            else:
                rtype = "split_into"
            relations.append(
                {
                    "from_key": f"old:{code}",
                    "to_key": f"old:{host}",
                    "relation_type": rtype,
                    "effective_date": "2025-01-01",
                    "source": SRC_WAVE1,
                }
            )

    # ── curated aliases (provinces + famous former district-level units) ───
    def new_key(code):
        return f"new:{code}"

    CURATED = {
        new_key("79"): [  # TP. Hồ Chí Minh
            ("Sài Gòn", "historical"),
            ("Saigon", "english"),
            ("Ho Chi Minh City", "english"),
            ("TP.HCM", "abbreviation"),
            ("TP HCM", "abbreviation"),
            ("TPHCM", "abbreviation"),
            ("HCM", "abbreviation"),
            ("SG", "abbreviation"),
            ("HCMC", "abbreviation"),
            ("Tp. Hồ Chí Minh", "alternate"),
            ("Hồ Chí Minh", "alternate"),
            ("BRVT", "abbreviation"),
            ("Bà Rịa Vũng Tàu", "historical"),
        ],
        new_key("01"): [  # Hà Nội
            ("Hanoi", "english"),
            ("Ha Noi", "english"),
            ("Ha Noi City", "english"),
            ("HN", "abbreviation"),
            ("Thủ đô", "alternate"),
            ("Thu Do", "alternate"),
            ("TP. Hà Nội", "alternate"),
            ("Tp Hà Nội", "alternate"),
        ],
        new_key("48"): [
            ("Da Nang", "english"),
            ("Danang", "english"),
            ("ĐN", "abbreviation"),
            ("TP. Đà Nẵng", "alternate"),
        ],
        new_key("31"): [
            ("Hai Phong", "english"),
            ("Haiphong", "english"),
            ("HP", "abbreviation"),
            ("TP. Hải Phòng", "alternate"),
        ],
        new_key("92"): [
            ("Can Tho", "english"),
            ("Cantho", "english"),
            ("CT", "abbreviation"),
            ("TP. Cần Thơ", "alternate"),
        ],
        new_key("46"): [  # TP. Huế — pre-2025 it was tỉnh Thừa Thiên Huế
            ("Hue", "english"),
            ("Hue City", "english"),
            ("Thừa Thiên Huế", "historical"),
            ("Thua Thien Hue", "historical"),
            ("TTH", "abbreviation"),
            ("TP Huế", "alternate"),
        ],
        new_key("56"): [("Khanh Hoa", "english")],
        new_key("68"): [("Lam Dong", "english")],
        new_key("66"): [
            ("Dak Lak", "english"),
            ("Đắc Lắc", "historical"),
            ("Dac Lac", "historical"),
        ],
        new_key("52"): [("Gia Lai", "english")],
        new_key("24"): [("Bac Ninh", "english")],
        new_key("22"): [("Quang Ninh", "english"), ("QN", "abbreviation")],
        new_key("80"): [("Tay Ninh", "english")],
        new_key("82"): [("Dong Thap", "english")],
        new_key("91"): [("An Giang", "english")],
        new_key("86"): [("Vinh Long", "english")],
        new_key("96"): [("Ca Mau", "english"), ("Camau", "english")],
        new_key("75"): [("Dong Nai", "english"), ("Bien Hoa", "alternate")],
        new_key("51"): [("Quang Ngai", "english")],
        new_key("38"): [("Thanh Hoa", "english")],
        new_key("40"): [("Nghe An", "english")],
        new_key("42"): [("Ha Tinh", "english")],
        new_key("44"): [("Quang Tri", "english")],
        new_key("37"): [("Ninh Binh", "english")],
        new_key("33"): [("Hung Yen", "english")],
        new_key("25"): [("Phu Tho", "english")],
        new_key("19"): [("Thai Nguyen", "english")],
        new_key("15"): [("Lao Cai", "english")],
        new_key("20"): [("Lang Son", "english")],
        new_key("14"): [("Son La", "english")],
        new_key("12"): [("Lai Chau", "english")],
        new_key("11"): [("Dien Bien", "english")],
        new_key("08"): [("Tuyen Quang", "english")],
        new_key("04"): [("Cao Bang", "english")],
    }
    for ukey, pairs in CURATED.items():
        for alias, atype in pairs:
            aliases.append(
                {
                    "unit_key": ukey,
                    "alias": alias,
                    "normalized_alias": flat(alias),
                    "alias_type": atype,
                    "valid_from": None,
                    "valid_to": None,
                }
            )

    # HCMC quận texting abbreviations ("Q1".."Q12", "Gò Vấp"=gv …) point at
    # the historical district rows; forward edges resolve them to the new
    # phường they became.
    hcmc_old = next(
        k
        for k, u in units.items()
        if u["admin_level"] == 1 and flat(u["name"]) == "ho chi minh" and k.startswith("old:")
    )
    for n in range(1, 13):
        qkey = next(
            (
                k
                for k, u in units.items()
                if u["parent_key"] == hcmc_old
                and u["type"] == "quan"
                and flat(short_name(u["name"])) == str(n)
            ),
            None,
        )
        if qkey:
            aliases.append(
                {
                    "unit_key": qkey,
                    "alias": f"Q{n}",
                    "normalized_alias": f"q{n}",
                    "alias_type": "abbreviation",
                    "valid_from": None,
                    "valid_to": None,
                }
            )
    for alias, dname in {
        "gv": "quận gò vấp",
        "tb": "quận tân bình",
        "bt": "quận bình thạnh",
        "pnh": "quận phú nhuận",
    }.items():
        qkey = next(
            (
                k
                for k, u in units.items()
                if u["parent_key"] == hcmc_old
                and flat(u["name"]) == display_name(dname.split(" ", 1)[1], _TYPE_NORM["quan"], 2)
                or u["parent_key"] == hcmc_old
                and flat(short_name(u["name"])) == dname.split(" ", 1)[1]
            ),
            None,
        )
        if qkey:
            aliases.append(
                {
                    "unit_key": qkey,
                    "alias": alias.upper(),
                    "normalized_alias": alias,
                    "alias_type": "abbreviation",
                    "valid_from": None,
                    "valid_to": None,
                }
            )

    # ── city hints for the address parser: unambiguous name → new province ─
    # Every level-1 unit name (old + new) and every unambiguous old
    # district-level city name maps to the current province it resolves to.
    new_prov_by_old = {}
    for r in relations:
        f, t = r["from_key"], r["to_key"]
        if units[f]["admin_level"] == 1 and units[t]["admin_level"] == 1:
            new_prov_by_old[f] = t
    city_hints: dict[str, str] = {}
    for key, u in units.items():
        if u["admin_level"] != 1:
            continue
        canon = (
            u["name"] if u["status"] == "current" else units[new_prov_by_old.get(key, key)]["name"]
        )
        for form in {flat(short_name(u["name"])), flat(u["name"])}:
            if form:
                city_hints[form] = canon
    for a in aliases:
        if units[a["unit_key"]]["admin_level"] == 1:
            canon = (
                units[a["unit_key"]]["name"]
                if units[a["unit_key"]]["status"] == "current"
                else units[new_prov_by_old[a["unit_key"]]]["name"]
            )
            city_hints[a["normalized_alias"]] = canon
    # district-level city names → their province's successor, only when the
    # name maps to exactly one province ("vinh"→Nghệ An; "châu thành" is
    # ambiguous → skipped).
    name_to_provs: dict[str, set[str]] = {}
    for key, u in units.items():
        if u["admin_level"] == 2 and u["type"] in ("thanh_pho", "thi_xa"):
            old_prov = key if u["admin_level"] == 1 else None
            prov = old_prov or _ancestor_province(units, u["parent_key"])
            canon = (
                units[new_prov_by_old.get(prov, prov)]["name"]
                if prov in new_prov_by_old
                else units[prov]["name"]
                if prov
                else None
            )
            if canon:
                name_to_provs.setdefault(norm_name(u["name"]), set()).add(canon)
    for nm, provs in name_to_provs.items():
        if len(provs) == 1 and nm not in city_hints:
            city_hints[nm] = next(iter(provs))

    geom_source = "pending P14B (boundary data)"
    if geojson_dir is not None:
        geom_source = attach_geometry(units, geojson_dir, geom_tolerance)

    return {
        "version": 1,
        "sources": {
            "new_provinces": SRC_NEW_PROV,
            "new_communes": "34x NQ-UBTVQH15 1654-1687; Cong van 1027/CTK-CSCL; QD 19/2025/QD-TTg",
            "old_snapshot": SRC_OLD,
            "wave1": SRC_WAVE1,
            "geometry": geom_source,
        },
        "units": [units[k] for k in sorted(units)],
        "aliases": aliases,
        "relations": relations,
        "city_hints": city_hints,
    }


# ── geometry (P14B) ──────────────────────────────────────────────────────
# Boundary polygons for the *current* era come from the MIT-licensed
# thanglequoc/vietnamese-provinces-database GeoJSON tree — it is the only
# open boundary set that already covers the post-2025 commune level (OSM
# carries the 34 new provinces but essentially no VN communes). Historical
# units keep geometry NULL: old-era commune boundaries were never
# republished under an open license, and point lookup only serves the
# current era anyway (POINT_SQL filters status='current').
#
# Every source file is a single-feature MultiPolygon, so no polygon union
# is needed — Douglas-Peucker simplification in pure Python keeps this
# script dependency-free. Small urban wards collapse too aggressively at
# the nominal tolerance, so each unit gets an adaptive retry loop with a
# minimum exterior-ring point floor.

_GEOM_MIN_RING_PTS = 30
_GEOM_COORD_DECIMALS = 5
_GEOM_SOURCE = "thanglequoc/vietnamese-provinces-database GeoJSON (MIT); Douglas-Peucker simplified"


def _rdp_ring(points: list, tol: float) -> list:
    """Douglas-Peucker on one lon/lat ring (iterative — rings run to 30k pts).

    Closed rings have coincident endpoints (zero-length chord), so the
    first split uses the point farthest from the shared anchor instead of
    the degenerate endpoint-to-endpoint chord.
    """
    if len(points) < 3 or tol <= 0:
        return list(points)
    keep = bytearray(len(points))
    keep[0] = keep[-1] = 1
    ax, ay = points[0]
    i_far = max(
        range(1, len(points)), key=lambda i: (points[i][0] - ax) ** 2 + (points[i][1] - ay) ** 2
    )
    keep[i_far] = 1
    stack = [(0, i_far), (i_far, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        denom = math.hypot(dx, dy) or 1.0
        i_max = -1
        d_max = tol
        for i in range(a + 1, b):
            px, py = points[i]
            d = abs((px - ax) * dy - (py - ay) * dx) / denom
            if d > d_max:
                d_max, i_max = d, i
        if i_max < 0:
            continue
        keep[i_max] = 1
        stack.append((a, i_max))
        stack.append((i_max, b))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _dedupe_ring(ring: list) -> list:
    out = [ring[0]]
    for p in ring[1:]:
        if p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[-1] == out[0]:
        pass
    return out


def _round_pt(pt: list) -> list:
    return [round(float(v), _GEOM_COORD_DECIMALS) for v in pt[:2]]


def _simplify_multipoly(coords: list, tolerance: float, min_ring: int = _GEOM_MIN_RING_PTS):
    """Adaptive Douglas-Peucker: halve tolerance while any exterior ring
    would fall below ``min_ring`` points (small urban wards need finer
    tolerance than rural xã at the same nominal tolerance)."""
    t = tolerance
    out: list = []
    for _ in range(8):
        out = []
        smallest = 1 << 60
        for poly in coords:
            rings = []
            for j, ring in enumerate(poly):
                r = _rdp_ring(ring, t)
                if j == 0:
                    smallest = min(smallest, len(r))
                if len(r) >= (4 if j == 0 else 0):
                    rings.append(r)
            if rings:
                out.append(rings)
        if smallest >= min_ring or t <= tolerance / 64:
            break
        t /= 2.0
    return [[[_round_pt(p) for p in _dedupe_ring(ring)] for ring in poly] for poly in out]


def _index_geojson(root: Path):
    """code -> path and (province_code, flat_stem) -> path over the
    thanglequoc geojson tree (``NN_province/{NN}_*.geojson`` +
    ``wards/{CCCCC}_*.geojson``)."""
    by_code: dict[str, Path] = {}
    by_name: dict[tuple[str, str], Path] = {}
    for pdir in sorted(root.iterdir()):
        if not pdir.is_dir():
            continue
        pcode = pdir.name.split("_", 1)[0]
        files = list(pdir.glob("*.geojson"))
        wdir = pdir / "wards"
        if wdir.is_dir():
            files += list(wdir.glob("*.geojson"))
        for f in files:
            code, _, stem = f.name.rsplit(".", 1)[0].partition("_")
            by_code[code] = f
            if stem:
                by_name[(pcode, flat(stem.replace("_", " ")))] = f
    return by_code, by_name


def attach_geometry(units: dict[str, dict], geojson_dir: Path, tolerance: float) -> str:
    """Attach simplified GeoJSON MultiPolygons to current-era units in place."""
    by_code, by_name = _index_geojson(geojson_dir)
    missing: list[str] = []
    for u in units.values():
        if not u["key"].startswith("new:"):
            continue
        path = by_code.get(u["code"])
        if path is None:
            pcode = units[u["parent_key"]]["code"] if u.get("parent_key") else u["code"]
            path = by_name.get((pcode, norm_name(u["name"])))
        if path is None:
            missing.append(u["key"])
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
        geom = feats[0].get("geometry") if feats else None
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            missing.append(u["key"])
            continue
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        u["geometry"] = {
            "type": "MultiPolygon",
            "coordinates": _simplify_multipoly(polys, tolerance),
        }
    if missing:
        raise SystemExit(f"no geometry for {len(missing)} current units: {missing[:10]}")
    return f"{_GEOM_SOURCE} (tol={tolerance} deg)"


def _parent_code_of(units, key):
    pk = units[key]["parent_key"]
    return units[pk]["code"] if pk else None


def _ancestor_province(units, key):
    """Walk parent_key up to the level-1 unit; returns its key."""
    cur = key
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        u = units.get(cur)
        if not u:
            return None
        if u["admin_level"] == 1:
            return cur
        cur = u["parent_key"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dvhcvn", type=Path, required=True)
    ap.add_argument("--snapshots", type=Path, default=None)
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "db" / "seeds" / "vn_admin_units.json",
    )
    ap.add_argument(
        "--geojson",
        type=Path,
        default=None,
        help="thanglequoc/vietnamese-provinces-database json/geojson dir "
        "(P14B: attach simplified boundaries to current-era units)",
    )
    ap.add_argument("--geom-tolerance", type=float, default=0.0005)
    args = ap.parse_args()

    snap_dir = args.snapshots or Path.home() / ".cache" / "dvhcvn-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    for fname, url in _SNAPSHOTS.items():
        fetch(url, snap_dir / fname)

    seed = build(args.dvhcvn, snap_dir, args.geojson, args.geom_tolerance)

    # integrity gate — the foundation must not ship a broken graph.
    keys = {u["key"] for u in seed["units"]}
    new = [u for u in seed["units"] if u["status"] == "current"]
    provs = [u for u in new if u["admin_level"] == 1]
    communes = [u for u in new if u["admin_level"] == 3]
    assert len(provs) == 34, len(provs)
    assert len(communes) == 3321, len(communes)
    for u in seed["units"]:
        assert u["parent_key"] is None or u["parent_key"] in keys, u["key"]
    for r in seed["relations"]:
        assert r["from_key"] in keys and r["to_key"] in keys, r
    for a in seed["aliases"]:
        assert a["unit_key"] in keys, a
    by_rel = Counter(r["relation_type"] for r in seed["relations"])
    print(
        f"units={len(seed['units'])} new={len(new)} "
        f"(prov={len(provs)} comm={len(communes)}) "
        f"relations={len(seed['relations'])} {dict(by_rel)} "
        f"aliases={len(seed['aliases'])} city_hints={len(seed['city_hints'])}"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Compact single-line JSON: this is a generated data artifact — keeping
    # it minified keeps PR diffs sane (and the size gate passes) while
    # `load_seed`/`json.loads` reads it identically.
    args.out.write_text(
        json.dumps(seed, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
