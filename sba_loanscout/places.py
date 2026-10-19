"""Find a business's Google Maps listing via Serper's Places search.

A listing carries the phone number and website the business chose to publish,
plus its Google rating and review count — the public signs of how busy it is.
Search results are only trusted when they are provably the same business: the
listing's street address must match the SBA business address, or failing that,
its name must match and it must sit in the same ZIP code, or failing that,
its website must be on the business's own email domain. Anything less is
left blank, because calling the wrong business is worse than calling none.

An address alone isn't proof: an office building or a shopping strip holds
many businesses, and Google may return a neighbour. So an address match also
needs the listing's name to share a word with the business's (or its
contact's) name, or its phone to be the one the SBA lists for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

SERPER_PLACES_URL = "https://google.serper.dev/places"
TIMEOUT = 15

# Words that carry no identity in a street or business name.
_STREET_WORDS = {
    "n", "s", "e", "w", "north", "south", "east", "west",
    "st", "street", "ave", "avenue", "rd", "road", "blvd", "boulevard", "dr",
    "drive", "ln", "lane", "pl", "place", "ct", "court", "ter", "terrace",
    "pike", "hwy", "highway", "pkwy", "parkway", "way", "sq", "square",
}
# Generic industry words are left out too: "Unity Home Health Care LLC" and
# "Unity Homecare" share "unity", which is what identifies them.
_BUSINESS_WORDS = {
    "llc", "inc", "corp", "co", "company", "ltd", "lp", "pc", "pllc", "the", "and", "of",
    "home", "health", "healthcare", "care", "homecare", "services", "service", "medical",
    "group", "center", "centre", "agency", "associates", "clinic", "practice",
    "construction", "contracting", "contractor", "contractors", "builders", "building",
    "sons", "brothers", "bros", "enterprises", "excavating", "excavation", "paving", "roofing",
    "concrete", "masonry", "trucking", "hauling", "landscaping", "plumbing", "heating",
    "electric", "electrical", "general", "solutions", "consulting", "llp", "incorporated",
}


class SerperUnavailable(RuntimeError):
    """Serper can't be used at all (bad key, out of credits); stop the run."""


@dataclass(frozen=True)
class Listing:
    """The matched Google listing, or an empty one when nothing matched."""

    name: str = ""
    address: str = ""
    phone: str = ""
    website: str = ""
    matched_on: str = ""  # "address", "address + phone", "name + zip", "email domain", or "" for no match
    note: str = ""
    rating: float = 0.0  # Google star rating, 0 when none
    reviews: int = 0  # number of Google ratings
    category: str = ""  # Google's category, e.g. "Home health care service"


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and "))


def _street_numbers(address: str) -> set[str]:
    """'1528-30 SANSOM ST' -> {'1528', '1529', '1530'}. City addresses write
    a building spanning several lots as a range with a shortened end."""
    match = re.match(r"\s*(\d+)(?:-(\d+))?", address or "")
    if not match:
        return set()
    start, end = match.group(1), match.group(2)
    if not end:
        return {start}
    end = start[: len(start) - len(end)] + end
    low, high = int(start), int(end)
    if high < low or high - low > 60:
        return {start}
    return {str(n) for n in range(low, high + 1)}


# Where the street ends and the unit begins: '1234 MARKET ST STE 200'.
_UNIT = re.compile(
    r"(?:,?\s(?:ste|suite|unit|apt|apartment|fl|floor|rm|room|bldg|building|dept)\b|\s*#).*$",
    re.IGNORECASE,
)


def _street_core(address: str) -> set[str]:
    """The identifying words of a street: '903 N 2ND ST' -> {'2nd'}.

    Unit designators are cut off first. SBA addresses carry them
    ('1234 MARKET ST STE 200') and Google often omits them, so leaving
    'ste' and '200' in would make an exact listing fail to match."""
    street = _UNIT.sub("", address or "")
    words = _words(re.sub(r"^\s*[\d-]+\s*", "", street))
    return {w for w in words if w not in _STREET_WORDS}


def _name_overlap(a: str, b: str) -> float:
    """Share of a's meaningful words that also appear in b."""
    left = {w for w in _words(a) if w not in _BUSINESS_WORDS}
    right = {w for w in _words(b) if w not in _BUSINESS_WORDS}
    if not left:
        return 0.0
    return len(left & right) / len(left)


# Words that only say what kind of company it is, left out when names are
# compared run together.
_LEGAL_WORDS = {
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd",
    "lp", "pc", "pllc", "pa", "the", "and", "of",
}


def _compact(name: str) -> str:
    """'E J Paving Co Inc' -> 'ejpaving'; "PJ's Roofing" -> 'pjsroofing'."""
    return "".join(w for w in _words(name) if w not in _LEGAL_WORDS)


def _same_name(a: str, b: str) -> bool:
    """Whether two names are the same business's, however spaced or shortened:
    'North Star Paving' and 'Northstar Inc' are, and so are 'E J Paving Co'
    and 'EJ Paving'; 'Unity Home Care' and 'Bright Smiles Dental' aren't."""
    left = {w for w in _words(a) if w not in _BUSINESS_WORDS}
    right = {w for w in _words(b) if w not in _BUSINESS_WORDS}
    if left & right:
        return True
    joined_a, joined_b = "".join(_words(a)), "".join(_words(b))
    if (any(len(w) >= 4 and w in joined_b for w in left)
            or any(len(w) >= 4 and w in joined_a for w in right)):
        return True
    short, long_ = sorted((_compact(a), _compact(b)), key=len)
    return (len(short) >= 2 and short == long_) or (len(short) >= 4 and short in long_)


def _domain(url: str) -> str:
    """'https://www.chscompanies.com/about' -> 'chscompanies.com'."""
    host = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0].split(":")[0]
    return host[4:] if host.startswith("www.") else host


# Toll-free numbers are often a shared call center serving many businesses,
# so they prove nothing about which one a listing is.
_TOLL_FREE = {"800", "833", "844", "855", "866", "877", "888"}


def _phone_key(phone: str) -> str:
    """The 10 digits of a US number, or '' — '+1 (215) 555-0134' -> '2155550134'."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 and digits[:3] not in _TOLL_FREE else ""


def match_listing(places: list[dict], search_name: str, address: str, zip5: str,
                  names: tuple[str, ...] = (), phones: tuple[str, ...] = (),
                  domains: tuple[str, ...] = ()) -> Listing:
    """Pick the listing that is provably this business, or none.

    names: other names it goes by (legal name, the official's name); domains:
    its own email domain; phones: numbers the registry lists for it. Any of
    them can confirm an address match."""
    numbers = _street_numbers(address)
    core = _street_core(address)
    all_names = [n for n in (search_name,) + tuple(names) if n]
    known_phones = {_phone_key(p) for p in phones} - {""}
    own_domains = {d.lower() for d in domains if d}
    neighbour, by_phone = "", None

    for place in places:
        place_address = place.get("address") or ""
        place_words = set(_words(place_address))
        if numbers and core and numbers & place_words and core <= place_words:
            title = place.get("title") or ""
            if any(_same_name(n, title) for n in all_names) or _domain(place.get("website")) in own_domains:
                return _listing(place, "address")
            # The same phone under another name: most often the business was
            # renamed or sold, so the registry's contact may be out of date.
            if by_phone is None and _phone_key(place.get("phoneNumber") or "") in known_phones:
                by_phone = place
            neighbour = neighbour or title

    for place in places:
        place_address = place.get("address") or ""
        if zip5 and zip5 in place_address and _name_overlap(search_name, place.get("title", "")) >= 0.6:
            return _listing(place, "name + zip")

    # SBA addresses are often a home or mailing address, so the listing may
    # sit elsewhere; its website on the business's own email domain proves it.
    for place in places:
        if place.get("website") and _domain(place["website"]) in own_domains:
            return _listing(place, "email domain")

    if by_phone is not None:
        return _listing(by_phone, "address + phone")
    if neighbour:
        return Listing(note=f"the only listing at this address is another business ({neighbour})")
    return Listing(note=f"no listing matched ({len(places)} checked)" if places else "no listings found")


def _listing(place: dict, matched_on: str) -> Listing:
    return Listing(
        name=place.get("title") or "",
        address=place.get("address") or "",
        phone=place.get("phoneNumber") or "",
        website=place.get("website") or "",
        matched_on=matched_on,
        rating=_number(place.get("rating"), float),
        reviews=_number(place.get("ratingCount"), int),
        category=place.get("category") or "",
    )


def _number(value, kind):
    """Serper sends numbers, occasionally as strings; anything else is 0."""
    try:
        return kind(value or 0)
    except (TypeError, ValueError):
        return kind(0)


class PlacesClient:
    """Serper Places search. One lookup costs one Serper credit."""

    def __init__(self, api_key: str):
        self._session = requests.Session()
        self._session.headers.update(
            {"X-API-KEY": api_key, "Content-Type": "application/json"}
        )
        self.lookups = 0
        # Once Serper refuses the key or runs out of credits, every other
        # worker's next lookup would be refused too, so they fail fast.
        self._refused: SerperUnavailable | None = None

    def search(self, query: str) -> list[dict]:
        if self._refused is not None:
            raise self._refused
        try:
            response = self._session.post(
                SERPER_PLACES_URL, json={"q": query, "gl": "us"}, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            # A network blip on one business is not a reason to stop the run.
            raise ConnectionError(f"Serper request failed: {exc}") from exc
        self.lookups += 1

        if response.status_code in (401, 403):
            self._refused = SerperUnavailable(
                "Serper rejected the API key. Check SERPER_API_KEY in .env"
            )
            raise self._refused
        if response.status_code == 429:
            self._refused = SerperUnavailable(
                "Serper is out of credits or rate-limited (429)"
            )
            raise self._refused
        if response.status_code != 200:
            raise ConnectionError(f"Serper returned {response.status_code}")
        return response.json().get("places") or []

    def find(self, search_name: str, address: str, city: str, state: str, zip5: str,
             names: tuple[str, ...] = (), phones: tuple[str, ...] = (),
             domains: tuple[str, ...] = ()) -> Listing:
        query = f"{search_name}, {address}, {city}, {state} {zip5}".strip()
        return match_listing(self.search(query), search_name, address, zip5, names, phones, domains)

    def close(self) -> None:
        self._session.close()
