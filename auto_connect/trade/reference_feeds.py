"""
Live reference data for the trade engine, from public official sources:

* ECB euro foreign exchange reference rates (published every TARGET business day ~16:00 CET)
* EU consolidated financial sanctions list (FSF). The Commission's own download now needs an EU Login,
  so the list is taken from the OpenSanctions mirror of the same file (free for non-commercial use).
* US OFAC Specially Designated Nationals list (SDN + aliases + addresses), incl. sanctioned vessels (IMO)

Files are cached on disk and refreshed at most once a day (end-of-day batch, or on demand). If a source
is unreachable the last good copy stays in use, and without any copy the bank's built-in sample lists apply.
The lists are held in an in-memory search index, not in the bank's state file (they are tens of thousands
of names).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from .rules import normalise, similarity

logger = logging.getLogger("auto_connect.trade.reference_feeds")

SOURCES = {
    "ecb": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
    "eu": "https://data.opensanctions.org/datasets/latest/eu_fsf/targets.simple.csv",
    "ofac_sdn": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
    "ofac_alt": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ALT.CSV",
    "ofac_add": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/ADD.CSV",
}
FILES = {"ecb": "ecb_eurofxref.xml", "eu": "eu_fsf.csv", "ofac_sdn": "ofac_sdn.csv", "ofac_alt": "ofac_alt.csv",
         "ofac_add": "ofac_add.csv"}
MAX_AGE_SECONDS = 20 * 3600

# OFAC addresses give country names; the screening compares ISO 3166 codes
COUNTRY_ISO = {
    "russia": "RU", "iran": "IR", "mexico": "MX", "china": "CN", "united arab emirates": "AE", "lebanon": "LB",
    "turkey": "TR", "turkiye": "TR", "colombia": "CO", "syria": "SY", "pakistan": "PK", "ukraine": "UA",
    "belarus": "BY", "venezuela": "VE", "korea, north": "KP", "burma": "MM", "united kingdom": "GB", "iraq": "IQ",
    "india": "IN", "cambodia": "KH", "yemen": "YE", "germany": "DE", "marshall islands": "MH", "afghanistan": "AF",
    "panama": "PA", "cuba": "CU", "singapore": "SG", "cyprus": "CY", "thailand": "TH", "united states": "US",
    "congo, democratic republic of the": "CD", "bulgaria": "BG", "italy": "IT", "switzerland": "CH", "peru": "PE",
    "somalia": "SO", "nicaragua": "NI", "south africa": "ZA", "canada": "CA", "guatemala": "GT", "netherlands": "NL",
    "hong kong": "HK", "virgin islands, british": "VG", "malta": "MT", "spain": "ES", "france": "FR",
    "maldives": "MV", "sudan": "SD", "malaysia": "MY", "liberia": "LR", "serbia": "RS", "belgium": "BE",
    "kuwait": "KW", "taiwan": "TW", "bosnia and herzegovina": "BA", "south sudan": "SS", "honduras": "HN",
    "seychelles": "SC", "saudi arabia": "SA", "oman": "OM", "indonesia": "ID", "central african republic": "CF",
    "brazil": "BR", "egypt": "EG", "libya": "LY", "ecuador": "EC", "nigeria": "NG", "kosovo": "XK", "mali": "ML",
    "greece": "GR", "kyrgyzstan": "KG", "dominican republic": "DO", "zimbabwe": "ZW", "uzbekistan": "UZ",
    "the gambia": "GM", "vietnam": "VN", "japan": "JP", "liechtenstein": "LI", "argentina": "AR",
    "philippines": "PH", "kenya": "KE", "haiti": "HT", "jordan": "JO", "uganda": "UG", "qatar": "QA",
    "israel": "IL", "angola": "AO", "rwanda": "RW", "finland": "FI", "costa rica": "CR", "bangladesh": "BD",
    "algeria": "DZ", "estonia": "EE", "georgia": "GE", "sri lanka": "LK", "latvia": "LV", "benin": "BJ",
    "slovakia": "SK", "paraguay": "PY", "mozambique": "MZ", "austria": "AT", "sweden": "SE", "kazakhstan": "KZ",
    "guyana": "GY", "poland": "PL", "cayman islands": "KY", "moldova": "MD", "north macedonia, the republic of": "MK",
    "slovenia": "SI", "azerbaijan": "AZ", "el salvador": "SV", "czech republic": "CZ", "hungary": "HU",
    "ireland": "IE", "chile": "CL", "luxembourg": "LU", "romania": "RO", "laos": "LA", "palau": "PW",
    "jamaica": "JM", "tunisia": "TN", "albania": "AL", "sierra leone": "SL", "australia": "AU", "jersey": "JE",
    "cote d ivoire": "CI", "belize": "BZ", "armenia": "AM", "tajikistan": "TJ", "turkmenistan": "TM",
    "mongolia": "MN", "bahrain": "BH", "morocco": "MA", "ethiopia": "ET", "eritrea": "ER", "burundi": "BI",
    "denmark": "DK", "norway": "NO", "portugal": "PT", "croatia": "HR", "lithuania": "LT", "montenegro": "ME",
    "korea, south": "KR", "new zealand": "NZ", "bolivia": "BO", "uruguay": "UY", "trinidad and tobago": "TT",
}
_STOP_TOKENS = {"the", "and", "of", "for", "company", "co", "ltd", "llc", "inc", "jsc", "ojsc", "pjsc", "ooo", "oao",
                "zao", "joint", "stock", "limited", "group", "trading", "international", "general", "corporation",
                "corp", "enterprise", "enterprises", "holding", "holdings", "industries", "industrial", "bank", "de",
                "la", "el", "al", "bin", "ibn", "abu", "mohammad", "muhammad", "mohammed", "ali", "fze", "fzco"}


class ListEntry:
    __slots__ = ("entry_id", "list_name", "name", "names", "countries", "kind")

    def __init__(self, entry_id: str, list_name: str, name: str, names: List[str], countries: Set[str], kind: str):
        self.entry_id, self.list_name, self.name, self.names = entry_id, list_name, name, names
        self.countries, self.kind = countries, kind


class SanctionsIndex:
    """Token + 4-letter-prefix index so a name is compared only with plausible candidates."""

    def __init__(self):
        self.entries: List[ListEntry] = []
        self.vessels: Dict[str, str] = {}  # IMO number -> "list: name"
        self._index: Dict[str, Set[int]] = {}

    def add(self, entry: ListEntry):
        names = [n for n in dict.fromkeys(entry.names) if normalise(n)]
        if not names:
            return
        entry.names = names
        idx = len(self.entries)
        self.entries.append(entry)
        for n in names:
            for key in self._keys(n):
                self._index.setdefault(key, set()).add(idx)

    @staticmethod
    def _keys(name: str) -> Set[str]:
        keys = set()
        for tok in normalise(name).split():
            if len(tok) < 3 or tok in _STOP_TOKENS:
                continue
            keys.add(tok)
            if len(tok) >= 5:
                keys.add("~" + tok[:4])  # tolerates spelling variants: Volkova / Volkov
        return keys

    def candidates(self, name: str, limit: int = 400) -> List[ListEntry]:
        hits: Dict[int, int] = {}
        for key in self._keys(name):
            ids = self._index.get(key, ())
            if len(ids) > 3000:  # too common to discriminate
                continue
            for i in ids:
                hits[i] = hits.get(i, 0) + 1
        best = sorted(hits, key=lambda i: -hits[i])[:limit]
        return [self.entries[i] for i in best]

    def search(self, name: str, threshold: float) -> List[Tuple[float, ListEntry]]:
        out = []
        for e in self.candidates(name):
            score = max(similarity(name, n) for n in e.names)
            if score >= threshold:
                out.append((score, e))
        return sorted(out, key=lambda x: -x[0])[:5]


# ---------------------------------------------------------------------------- parsing
def parse_ecb(xml_text: str) -> Tuple[date, Dict[str, Decimal]]:
    """Returns the reference date and EUR value of one unit of each currency (ECB quotes units per EUR)."""
    root = ET.fromstring(xml_text)
    day = next(c for c in root.iter() if c.get("time"))
    rates = {c.get("currency"): Decimal(c.get("rate")) for c in day if c.get("currency")}
    return date.fromisoformat(day.get("time")), {k: (Decimal("1") / v).quantize(Decimal("0.0000001")) for k, v in rates.items()}


def parse_eu(text: str, index: SanctionsIndex):
    for r in csv.DictReader(io.StringIO(text)):
        names = [r["name"]] + [a for a in (r.get("aliases") or "").split(";") if a]
        countries = {c.strip().upper() for c in (r.get("countries") or "").split(";") if c.strip()}
        index.add(ListEntry(r["id"], "EU consolidated list (FSF)", r["name"], names, countries, r.get("schema", "")))
        if r.get("schema") == "Vessel":
            for imo in re.findall(r"IMO\s*(\d{7})", r.get("identifiers") or "") + re.findall(r"\b(\d{7})\b", r.get("identifiers") or ""):
                index.vessels[imo] = f"EU: {r['name']}"


def parse_ofac(sdn: str, alt: str, add: str, index: SanctionsIndex):
    aliases: Dict[str, List[str]] = {}
    for r in csv.reader(io.StringIO(alt)):
        if len(r) > 3:
            aliases.setdefault(r[0].strip(), []).append(r[3].strip())
    countries: Dict[str, Set[str]] = {}
    for r in csv.reader(io.StringIO(add)):
        if len(r) > 4:
            iso = COUNTRY_ISO.get(r[4].strip().lower())
            if iso:
                countries.setdefault(r[0].strip(), set()).add(iso)
    for r in csv.reader(io.StringIO(sdn)):
        if len(r) < 4:
            continue
        ent, name, kind, program = r[0].strip(), r[1].strip(), r[2].strip(), r[3].strip()
        remarks = r[11] if len(r) > 11 else ""
        if kind == "vessel":
            for imo in re.findall(r"IMO (\d{7})", remarks):
                index.vessels[imo] = f"OFAC SDN [{program}]: {name}"
        index.add(ListEntry(f"OFAC-{ent}", f"OFAC SDN [{program}]", name, [name] + aliases.get(ent, []),
                            countries.get(ent, set()), kind if kind != "-0-" else "entity"))


# ---------------------------------------------------------------------------- service
class ReferenceDataService:
    def __init__(self, data_dir: str, enabled: bool = True):
        self.data_dir = data_dir
        self.enabled = enabled
        self.index: Optional[SanctionsIndex] = None
        self.status: Dict[str, Any] = {src: {"state": "NOT_LOADED"} for src in ("ecb", "eu", "ofac")}
        self.fx: Optional[Tuple[date, Dict[str, Decimal]]] = None
        self.attempted = False  # True once the first load has finished (successfully or not)
        self._refresh_lock = threading.Lock()

    def _path(self, key: str) -> str:
        return os.path.join(self.data_dir, FILES[key])

    def _fresh(self, key: str) -> bool:
        p = self._path(key)
        return os.path.exists(p) and time.time() - os.path.getmtime(p) < MAX_AGE_SECONDS

    def _download(self, key: str, timeout: int = 60) -> Optional[str]:
        req = urllib.request.Request(SOURCES[key], headers={"User-Agent": "auto-connect-trade-finance/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        text = raw.decode("utf-8-sig", errors="replace") if key in ("ecb", "eu") else raw.decode("latin-1")
        if len(text) < 500:
            raise ValueError(f"suspiciously small file ({len(text)} bytes)")
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self._path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self._path(key))
        return text

    def _get(self, key: str, force: bool) -> Tuple[Optional[str], str]:
        """Returns (text, origin) - a fresh download when due, else the cached copy."""
        if self.enabled and (force or not self._fresh(key)):
            try:
                return self._download(key), "downloaded"
            except Exception as e:  # source offline / changed: keep the last good copy
                logger.warning(f"Reference feed {key} unavailable: {e}")
                err = f"{type(e).__name__}: {e}"[:200]
                if os.path.exists(self._path(key)):
                    return self._read(key), f"cached copy (download failed: {err})"
                return None, f"unavailable ({err})"
        if os.path.exists(self._path(key)):
            return self._read(key), "cached copy"
        return None, "not downloaded (live reference data disabled)"

    def _read(self, key: str) -> str:
        with open(self._path(key), "r", encoding="utf-8") as f:
            return f.read()

    def refresh(self, force: bool = False) -> Dict[str, Any]:
        """Blocking: download (if due) and rebuild FX + sanctions index. Run it in a worker thread. Concurrent
        callers wait for the refresh in progress and then reuse its fresh files instead of downloading twice."""
        if not self.enabled:
            return self.summary()
        with self._refresh_lock:
            return self._refresh(force)

    def _refresh(self, force: bool) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        text, origin = self._get("ecb", force)
        if text:
            try:
                self.fx = parse_ecb(text)
                self.status["ecb"] = {"state": "LOADED", "origin": origin, "rates_date": self.fx[0].isoformat(),
                                      "currencies": len(self.fx[1]), "checked_at": now}
            except Exception as e:
                self.status["ecb"] = {"state": "ERROR", "origin": origin, "error": str(e)[:200]}
        else:
            self.status["ecb"] = {"state": "UNAVAILABLE", "origin": origin}

        index = SanctionsIndex()
        text, origin = self._get("eu", force)
        n0 = 0
        if text:
            try:
                parse_eu(text, index)
                self.status["eu"] = {"state": "LOADED", "origin": origin, "entries": len(index.entries), "checked_at": now,
                                     "source": "OpenSanctions mirror of the EU FSF list"}
            except Exception as e:
                self.status["eu"] = {"state": "ERROR", "origin": origin, "error": str(e)[:200]}
        else:
            self.status["eu"] = {"state": "UNAVAILABLE", "origin": origin}
        n0 = len(index.entries)
        parts = {k: self._get(k, force) for k in ("ofac_sdn", "ofac_alt", "ofac_add")}
        if all(t for t, _ in parts.values()):
            try:
                parse_ofac(parts["ofac_sdn"][0], parts["ofac_alt"][0], parts["ofac_add"][0], index)
                self.status["ofac"] = {"state": "LOADED", "origin": parts["ofac_sdn"][1],
                                       "entries": len(index.entries) - n0, "checked_at": now}
            except Exception as e:
                self.status["ofac"] = {"state": "ERROR", "origin": parts["ofac_sdn"][1], "error": str(e)[:200]}
        else:
            self.status["ofac"] = {"state": "UNAVAILABLE", "origin": parts["ofac_sdn"][1]}
        if index.entries or self.index is None:
            self.index = index if index.entries else None  # a failed refresh keeps the last good index
        self.attempted = True
        return self.summary()

    def summary(self) -> Dict[str, Any]:
        return {"live_reference_data": self.enabled, "data_dir": self.data_dir, "sources": self.status,
                "sanctions_entries_indexed": len(self.index.entries) if self.index else 0,
                "sanctioned_vessels": len(self.index.vessels) if self.index else 0}
