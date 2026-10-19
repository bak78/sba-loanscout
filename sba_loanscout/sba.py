"""Small businesses from the SBA's Small Business Search, as leads.

The SBA's directory (search.certifications.sba.gov) lists businesses that
registered to sell to the government. Each chose to show a contact person,
email and phone in public — the name and email the other programs have to
hunt for. The search page's own data feed is asked for one industry at a
time; it can't narrow by state, so the state is picked out here. Each day's
answers are saved under data/, so a second run the same day asks nothing.

Which industries count, and their codes, live in config/sba.yaml.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests
import yaml

from .config import DATA_DIR, DEFAULT_SBA_PATH, ConfigError
from .contacts import _format_phone

log = logging.getLogger(__name__)

SBA_SITE = "https://search.certifications.sba.gov"
SEARCH_URL = f"{SBA_SITE}/_api/v2/search"
NAICS_URL = f"{SBA_SITE}/naicsJson.json"
TIMEOUT = 300  # a whole industry nationwide is one large answer
NAICS_MAX_AGE_DAYS = 30

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_CODES = {name: code for code, name in STATE_NAMES.items()}
ALL_STATES = "ALL"

# The SBA's certification flags, as the spreadsheet names them.
CERTIFICATIONS = {
    "active_8a_boolean": "8(a)",
    "active_wosb_boolean": "Women-owned (WOSB)",
    "active_edwosb_boolean": "Women-owned (EDWOSB)",
    "active_hz_boolean": "HUBZone",
    "active_sdvosb_boolean": "Service-disabled veteran-owned",
    "active_vosb_boolean": "Veteran-owned",
}
# SAM business type code for a nonprofit organization.
NONPROFIT_CODE = "A8"

# Only these fields of each answer are kept, so the saved copy stays small.
KEEP = (
    "uei", "entity_detail_id", "legal_business_name", "dba_name", "contact_person", "email",
    "display_email", "phone", "display_phone", "website", "address_1", "address_2", "city", "state",
    "zipcode", "county", "year_established", "naics_primary", "business_type_codes",
    *CERTIFICATIONS,
)


class SbaUnavailable(RuntimeError):
    """The SBA search couldn't be reached or refused the request."""


class SearchStopped(RuntimeError):
    """Stop was pressed during the SBA search."""


@dataclass(frozen=True)
class Field:
    """One industry checkbox: its name, its group, and its code starts."""

    name: str
    group: str
    naics: tuple[str, ...]

    def covers(self, code: str) -> bool:
        return any(code.startswith(start) for start in self.naics)


@dataclass(frozen=True)
class SbaRules:
    """Everything loaded from sba.yaml."""

    state: str
    zip_prefixes: tuple[str, ...]
    default_limit: int
    groups: tuple[tuple[str, tuple[Field, ...]], ...]
    default_fields: tuple[str, ...] = ()
    weights: dict = field(default_factory=dict)
    chain_min_companies: int = 3
    franchise_domains: tuple[str, ...] = ()
    national_chains: tuple[str, ...] = ()

    @property
    def fields(self) -> tuple[Field, ...]:
        return tuple(f for _, fields in self.groups for f in fields)

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    def field_named(self, name: str) -> Field:
        for f in self.fields:
            if f.name.lower() == name.strip().lower():
                return f
        raise ConfigError(f"unknown industry {name!r}; sba.yaml has: {', '.join(self.field_names)}")


def load_sba_rules(path: Path | str = DEFAULT_SBA_PATH) -> SbaRules:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    groups, seen = [], set()
    for group in raw.get("groups") or []:
        gname = str(group["name"])
        fields = []
        for entry in group.get("fields") or []:
            name = str(entry["name"])
            if name.lower() in seen:
                raise ConfigError(f"sba.yaml lists the industry {name!r} twice")
            seen.add(name.lower())
            codes = tuple(str(c).strip() for c in entry.get("naics") or [])
            bad = [c for c in codes if not c.isdigit() or not 2 <= len(c) <= 6]
            if not codes or bad:
                raise ConfigError(f"sba.yaml industry {name!r}: codes must be 2 to 6 digits, got {bad or 'none'}")
            fields.append(Field(name, gname, codes))
        groups.append((gname, tuple(fields)))
    if not groups:
        raise ConfigError("sba.yaml has no industries (groups)")

    # default_fields may name whole groups as well as single industries.
    by_group = {g.lower(): [f.name for f in fs] for g, fs in groups}
    defaults = []
    for name in raw.get("default_fields") or []:
        defaults += by_group.get(str(name).strip().lower(), [str(name).strip()])

    rules = SbaRules(
        state=str(raw.get("state", "MD")).upper(),
        zip_prefixes=tuple(str(z) for z in raw.get("zip_prefixes") or []),
        default_limit=int(raw.get("default_limit", 500)),
        groups=tuple(groups),
        default_fields=tuple(dict.fromkeys(defaults)),
        weights=dict(raw.get("weights") or {}),
        chain_min_companies=int(raw.get("chain_min_companies", 3)),
        franchise_domains=tuple(str(d).strip().lower() for d in raw.get("franchise_domains") or []),
        national_chains=tuple(str(d).strip().lower() for d in raw.get("national_chains") or []),
    )
    for name in rules.default_fields:
        rules.field_named(name)  # a typo in default_fields stops here, with the list
    return rules


def check_state(state: str) -> str:
    state = (state or "").strip().upper()
    if state != ALL_STATES and state not in STATE_NAMES:
        raise ConfigError(f"unknown state {state!r}; use a two-letter code like MD, or ALL")
    return state


def check_zip_prefixes(prefixes) -> None:
    for z in prefixes:
        if not z.isdigit() or not 2 <= len(z) <= 5:
            raise ConfigError(f"ZIP code starts must be 2 to 5 digits, got {z!r}")


# --- the business record ------------------------------------------------------------------

_NAME_TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof"}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "do", "phd", "dds", "dmd", "rn", "np", "pa",
                  "lcsw", "lpc", "esq", "cpa", "pe"}
_UPPER_WORDS = {"llc", "pllc", "llp", "lp", "pc", "pa", "dds", "dmd", "md", "dba", "usa", "us", "hvac",
                "ii", "iii", "iv", "ltd", "inc"}


def tidy_name(name: str) -> str:
    """'BODYSENSE PT, LLC' -> 'Bodysense PT, LLC'; already mixed case is kept."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or name != name.upper():
        return name

    def word(m: re.Match) -> str:
        w = m.group(0)
        if w.lower() in _UPPER_WORDS or (len(w) <= 2 and w.isalpha() and w.lower() not in {"of", "at", "in", "on", "to", "by"}):
            return w.upper() if w.lower() != "inc" else "Inc"
        return w[0].upper() + w[1:].lower()

    return re.sub(r"[A-Za-z]+", word, name)


def split_contact(full: str) -> tuple[str, str]:
    """'PAMELA STOKES EGGLESTON' -> ('Pamela', 'Eggleston'); titles and
    suffixes (Dr, Jr, MD) are dropped; ('', '') when there's no full name."""
    words = [w.strip(".,") for w in re.split(r"\s+", (full or "").strip()) if w.strip(".,")]
    while words and words[0].lower() in _NAME_TITLES:
        words.pop(0)
    while words and words[-1].lower() in _NAME_SUFFIXES:
        words.pop()
    if len(words) < 2 or not words[0][0].isalpha():
        return "", ""

    def cap(w: str) -> str:
        if w != w.upper() and w != w.lower():
            return w  # McDonald, DeAngelo: already written as the person writes it
        return "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-"))

    first, last = cap(words[0]), cap(words[-1])
    last = re.sub(r"^(O')([a-z])", lambda m: m.group(1) + m.group(2).upper(), last)
    return first, last


@dataclass(frozen=True)
class Business:
    key: str
    legal_name: str
    dba: str
    contact_full: str
    contact_first: str
    contact_last: str
    email: str
    phone: str
    website: str
    address: str
    city: str
    state: str
    zip: str
    county: str
    year_established: str
    naics: str
    field: str
    group: str
    certifications: tuple[str, ...] = ()
    nonprofit: bool = False

    @property
    def company_name(self) -> str:
        return self.dba or self.legal_name

    @property
    def search_name(self) -> str:
        return re.sub(r"\b(LLC|L\.L\.C\.|INC\.?|CORP\.?|PLLC|LLP|P\.?C\.?)\b", "", self.company_name,
                      flags=re.IGNORECASE).strip(" ,.")

    def years_in_business(self, today: date | None = None) -> int:
        if not self.year_established.isdigit():
            return 0
        years = (today or date.today()).year - int(self.year_established)
        return years if 0 <= years <= 150 else 0


def parse_business(rec: dict, fld: Field) -> tuple[Business | None, str]:
    """(the business, '') or (None, why it isn't a lead)."""
    email = (rec.get("email") or "").strip().lower()
    if not email or not rec.get("display_email") or "@" not in email:
        return None, "no public email"
    first, last = split_contact(rec.get("contact_person") or "")
    if not (first and last):
        return None, "no contact person's full name"
    website = (rec.get("website") or "").strip()
    if website and not re.match(r"https?://", website, re.IGNORECASE):
        website = "https://" + website
    phone = _format_phone(re.sub(r"\D", "", str(rec.get("phone") or ""))[-10:]) if rec.get("display_phone") else ""
    year = str(rec.get("year_established") or "")[:4]
    codes = set((rec.get("business_type_codes") or "").split("~"))
    contact = " ".join(w for w in re.split(r"\s+", (rec.get("contact_person") or "").strip()) if w)
    return Business(
        key=str(rec.get("uei") or rec.get("entity_detail_id") or email),
        legal_name=tidy_name(rec.get("legal_business_name") or ""),
        dba=tidy_name(rec.get("dba_name") or ""),
        contact_full=tidy_name(contact),
        contact_first=first,
        contact_last=last,
        email=email,
        phone=phone or "",
        website=website,
        address=" ".join(p for p in (rec.get("address_1"), rec.get("address_2")) if p).strip(),
        city=tidy_name(rec.get("city") or ""),
        state=STATE_CODES.get(rec.get("state") or "", rec.get("state") or ""),
        zip=str(rec.get("zipcode") or "")[:5],
        county=rec.get("county") or "",
        year_established=year if year.isdigit() else "",
        naics=str(rec.get("naics_primary") or ""),
        field=fld.name,
        group=fld.group,
        certifications=tuple(name for flag, name in CERTIFICATIONS.items() if rec.get(flag)),
        nonprofit=NONPROFIT_CODE in codes,
    ), ""


# --- talking to the SBA -----------------------------------------------------------------------

def _search_body(codes: list[str]) -> dict:
    """The search page's own request, with only the industry codes filled in."""
    return {
        "searchProfiles": {"searchTerm": ""},
        "location": {"states": [], "zipCodes": [], "counties": [], "districts": [], "msas": []},
        "sbaCertifications": {"activeCerts": [], "isPreviousCert": False, "operatorType": "Or"},
        "naics": {"codes": [{"value": c, "label": c} for c in codes], "isPrimary": False, "operatorType": "Or"},
        "selfCertifications": {"certifications": [], "operatorType": "Or"},
        "keywords": {"list": [], "operatorType": "Or"},
        "lastUpdated": {"date": {"label": "Anytime", "value": "anytime"}},
        "samStatus": {"isActiveSAM": False},
        "qualityAssuranceStandards": {"qas": []},
        "bondingLevels": {"constructionIndividual": "", "constructionAggregate": "",
                          "serviceIndividual": "", "serviceAggregate": ""},
        "businessSize": {"relationOperator": "at-least", "numberOfEmployees": ""},
        "annualRevenue": {"relationOperator": "at-least", "annualGrossRevenue": ""},
        "entityDetailId": "",
    }


class SbaClient:
    """The SBA search's data feed, politely: one request per industry, a pause
    between them, and each day's answers saved for reuse."""

    def __init__(self, user_agent: str = "SBALoanScoutBot/0.1", cache_dir: Path | str | None = DATA_DIR / "sba",
                 stop: threading.Event | None = None, pause: float = 2.0, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Content-Type": "application/json"})
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.stop = stop or threading.Event()
        self.pause = pause
        self._asked = False
        self._naics: list[str] | None = None

    def close(self) -> None:
        self.session.close()

    def _wait_turn(self) -> None:
        if self.stop.is_set():
            raise SearchStopped()
        if self._asked and self.pause:
            end = time.monotonic() + self.pause
            while time.monotonic() < end:
                if self.stop.is_set():
                    raise SearchStopped()
                time.sleep(0.1)
        self._asked = True

    def naics_codes(self) -> list[str]:
        """Every 6-digit industry code the SBA knows, from its own list."""
        if self._naics is not None:
            return self._naics
        path = self.cache_dir / "naics.json" if self.cache_dir else None
        data = None
        if path and path.exists() and time.time() - path.stat().st_mtime < NAICS_MAX_AGE_DAYS * 86400:
            data = json.loads(path.read_text(encoding="utf-8"))
        if data is None:
            self._wait_turn()
            try:
                response = self.session.get(NAICS_URL, timeout=60)
            except requests.RequestException as exc:
                raise SbaUnavailable(f"couldn't reach the SBA search ({exc})") from exc
            if response.status_code != 200:
                raise SbaUnavailable(f"the SBA search's industry list answered {response.status_code}")
            data = response.json()
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding="utf-8")
        self._naics = sorted({str(d.get("naics_code")) for d in data if len(str(d.get("naics_code"))) == 6})
        return self._naics

    def expand(self, starts: tuple[str, ...]) -> list[str]:
        """('6211', '446110') -> every 6-digit code starting so."""
        return [c for c in self.naics_codes() if any(c.startswith(s) for s in starts)]

    def search(self, codes: list[str], today: date | None = None) -> list[dict]:
        """Every business registered as small in any of these codes, nationwide."""
        if not codes:
            return []
        digest = hashlib.sha1(",".join(sorted(codes)).encode()).hexdigest()[:16]
        path = (self.cache_dir / (today or date.today()).isoformat() / f"{digest}.json") if self.cache_dir else None
        if path and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        self._wait_turn()
        try:
            response = self.session.post(SEARCH_URL, json=_search_body(codes), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SbaUnavailable(f"couldn't reach the SBA search ({exc})") from exc
        if response.status_code != 200:
            raise SbaUnavailable(f"the SBA search answered {response.status_code}; try again later")
        try:
            results = response.json().get("results") or []
        except ValueError as exc:
            raise SbaUnavailable("the SBA search sent an answer that isn't data; it may have changed") from exc
        results = [{k: r.get(k) for k in KEEP} for r in results]
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(results), encoding="utf-8")
        return results


def find_businesses(client: SbaClient, rules: SbaRules, state: str, zip_prefixes, field_names) -> tuple[list[Business], dict[str, int]]:
    """The leads for an area and the ticked industries, and how many were
    skipped and why. A business counts only in the industry of its MAIN code."""
    state = check_state(state)
    selected = [rules.field_named(n) for n in field_names]
    if not selected:
        raise ConfigError("tick at least one industry")
    state_name = None if state == ALL_STATES else STATE_NAMES[state]

    found: dict[str, Business] = {}
    skipped: dict[str, int] = {}
    for fld in selected:
        codes = client.expand(fld.naics)
        log.info("  %s: asking the SBA (%d industry codes)...", fld.name, len(codes))
        for rec in client.search(codes):
            if state_name and rec.get("state") != state_name:
                continue
            if zip_prefixes and not str(rec.get("zipcode") or "").startswith(tuple(zip_prefixes)):
                continue
            main = str(rec.get("naics_primary") or "")
            owner = next((f for f in selected if f.covers(main)), None)
            if owner is not fld:
                continue  # its main industry is unticked, or it's counted under another line
            business, why = parse_business(rec, fld)
            if business is None:
                skipped[why] = skipped.get(why, 0) + 1
            elif business.key not in found:
                found[business.key] = business
    return list(found.values()), skipped
