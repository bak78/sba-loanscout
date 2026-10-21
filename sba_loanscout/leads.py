"""Turn one SBA business into a scored lead.

SBA facts (how long it's been in business, whether the email is the contact's
own, certifications) and Google facts found through Serper (a confirmed
listing, its rating and review count) become signal hits of their own, scored
by the same engine as website text. Every point of a score is traceable to
evidence: a row can say "fit 62 — Google: 4.7 stars from 212 ratings".

The SBA already gives the contact's name and email, so no email finder is
needed: Google confirms the business is real and open, and the website is
read only for its signals (hiring, a nonprofit or lender notice...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .churchlaw import church_or_law
from .config import Rules
from .contacts import is_role_address
from .crawl import SiteCrawl, crawl_site, is_crawlable, registered_domain
from .emailcheck import FREE_MAIL, domain_of
from .fetch import PoliteFetcher
from .places import Listing, PlacesClient
from .sba import Business, SbaRules
from .score import Score, score_site
from .signals import SignalHit, match_site

SBA_SOURCE = "SBA Small Business Search"
GOOGLE_SOURCE = "Google listing (Serper)"
PUBLIC_TLDS = ("gov", "edu", "mil")


@dataclass(frozen=True)
class Lead:
    business: Business
    listing: Listing
    crawl: SiteCrawl | None
    score: Score
    email_is_contacts_own: bool = False
    # No Google listing, but its own website loads (on its email's domain, or
    # a franchise brand's site): real, just not on Google Maps — often a
    # telehealth or home-based practice.
    confirmed_by_website: bool = False


def _hit(signal_id: str, weight: float, snippet: str, source: str, category: str = "fit") -> SignalHit:
    return SignalHit(signal_id=signal_id, category=category, weight=float(weight),
                     snippet=snippet, source_url=source, page_type="registry")


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def email_belongs_to(email: str, first: str, last: str) -> bool:
    """True when the address's mailbox carries the contact's name:
    jane.doe@, jdoe@, doej@, jane@ — but never info@ or office@."""
    if not email or is_role_address(email):
        return False
    local = _letters(email.partition("@")[0])
    first, last = _letters(first), _letters(last)
    # A short surname must sit at an end, or "Lee" would match fleet@.
    if len(last) >= 4 and last in local:
        return True
    if len(last) == 3 and (local.startswith(last) or local.endswith(last)):
        return True
    return len(first) >= 3 and (local.startswith(first) or (len(local) >= 3 and first.startswith(local)))


def own_domain(b: Business) -> str:
    """The business's own email domain, or '' for a gmail-style address."""
    domain = domain_of(b.email)
    return "" if domain in FREE_MAIL else domain


def sba_hits(b: Business, rules: SbaRules, today: date | None = None) -> list[SignalHit]:
    """What the SBA record says about this business."""
    w = rules.weights
    hits = [_hit("sba_registered", w.get("sba_registered", 8),
                 f"{b.field} (NAICS {b.naics}); contact {b.contact_full}", SBA_SOURCE)]
    years = b.years_in_business(today)
    if years >= int(w.get("established_years", 3)):
        hits.append(_hit("established", w.get("established", 10),
                         f"In business since {b.year_established} ({years} years)", SBA_SOURCE))
    if email_belongs_to(b.email, b.contact_first, b.contact_last):
        hits.append(_hit("contacts_own_email", w.get("contacts_own_email", 8),
                         f"{b.email} is {b.contact_first} {b.contact_last}'s own", SBA_SOURCE))
    if own_domain(b):
        hits.append(_hit("company_domain", w.get("company_domain", 4),
                         f"Email on the business's own domain ({own_domain(b)})", SBA_SOURCE))
    if b.certifications:
        hits.append(_hit("sba_certified", w.get("sba_certified", 6),
                         f"SBA certified: {', '.join(b.certifications)}", SBA_SOURCE))
    return hits


def google_hits(listing: Listing, rules: SbaRules) -> list[SignalHit]:
    """What the confirmed Google listing says: open, and how busy."""
    if not listing.matched_on:
        return []
    w = rules.weights
    hits = [_hit("google_listing", w.get("google_listing", 10),
                 f"Google listing: {listing.name} — {listing.address} (matched on {listing.matched_on})",
                 GOOGLE_SOURCE)]
    stars = f"{listing.rating:.1f} stars from {listing.reviews} ratings"
    if listing.reviews >= 50:
        hits.append(_hit("google_reviews_many", w.get("reviews_many", 14), f"Google: {stars}", GOOGLE_SOURCE))
    elif listing.reviews >= 10:
        hits.append(_hit("google_reviews_some", w.get("reviews_some", 7), f"Google: {stars}", GOOGLE_SOURCE))
    if listing.reviews >= 10 and listing.rating >= 4.0:
        hits.append(_hit("google_good_rating", w.get("good_rating", 5), f"Google: {stars}", GOOGLE_SOURCE))
    return hits


def disqualify_hits(b: Business, website: str, rules: SbaRules) -> list[SignalHit]:
    """Reasons this isn't a small-business borrower, known without a lookup
    (the SBA record, the email) or from the Google listing's website."""
    hits = []
    if b.nonprofit:
        hits.append(_hit("nonprofit", 0, "Registered with the SBA as a nonprofit organization",
                         SBA_SOURCE, "disqualify"))
    if domain_of(b.email).rpartition(".")[2] in PUBLIC_TLDS:
        hits.append(_hit("public_body", 0, f"{b.email} is a government or school address",
                         SBA_SOURCE, "disqualify"))
    domains = {domain_of(b.email), registered_domain(b.website) if b.website else "",
               registered_domain(website) if website else ""}
    chain = sorted(d for d in domains if d and d in rules.national_chains)
    if chain:
        hits.append(_hit("national_chain", 0, f"{chain[0]} is a national chain (national_chains in sba.yaml)",
                         website or SBA_SOURCE, "disqualify"))
    signal_id, why = church_or_law((b.legal_name, b.dba), domains)
    if signal_id:
        hits.append(_hit(signal_id, 0, f"Churches and law firms are never leads — {why}",
                         SBA_SOURCE, "disqualify"))
    return hits


def website_is_theirs(b: Business, website: str, crawl: SiteCrawl | None, rules: SbaRules) -> str:
    """The website's domain when it's the business's own: it loaded and is on
    the email's domain, or it's a franchise brand's page for this owner (a
    Home Instead office) — brand sites often turn programs away, so those
    needn't load. Else ''."""
    domain = registered_domain(website) if website else ""
    if not domain:
        return ""
    if domain in rules.franchise_domains:
        return domain
    if crawl is not None and crawl.pages and domain == own_domain(b):
        return domain
    return ""


def build_lead(
    b: Business,
    places: PlacesClient | None,
    fetcher: PoliteFetcher | None,
    rules: Rules,
    sba_rules: SbaRules,
    max_pages: int,
    today: date | None = None,
) -> Lead:
    """Look the business up on Google, read its website, and score it.

    Pass places=None for a free preview: SBA facts only, no lookups.
    Raises SerperUnavailable (stop the run) or ConnectionError (retry this
    business next run) from the lookup.
    """
    if places is None:
        listing = Listing(note="preview: not looked up")
    else:
        own = own_domain(b)
        listing = places.find(b.search_name, b.address, b.city, b.state, b.zip,
                              names=(b.legal_name, b.dba, b.contact_full), phones=(b.phone,),
                              domains=(own,) if own else ())
        if not listing.matched_on and "another business" not in listing.note:
            listing = Listing(note=f"{listing.note}; may be home-based, closed, or a mailing address")

    website = listing.website or (b.website if places is not None else "")
    crawl = None
    if fetcher is not None and website and is_crawlable(website, rules):
        crawl = crawl_site(website, fetcher, rules, max_pages)

    site_hits = match_site(crawl.pages, rules) if crawl is not None and crawl.pages else []
    own_site = website_is_theirs(b, website, crawl, sba_rules) if places is not None else ""
    hits = (sba_hits(b, sba_rules, today) + google_hits(listing, sba_rules)
            + disqualify_hits(b, listing.website, sba_rules) + site_hits)
    if own_site:
        hits.append(_hit("own_website", sba_rules.weights.get("own_website", 4),
                         f"Its website {own_site} loads and matches its email", website))
    return Lead(b, listing, crawl, score_site(hits, rules),
                email_is_contacts_own=email_belongs_to(b.email, b.contact_first, b.contact_last),
                confirmed_by_website=bool(own_site) and not listing.matched_on)
