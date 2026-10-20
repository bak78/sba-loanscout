"""Business contact details: emails and phone numbers the site itself publishes.

Only what appears on the crawled pages is collected — nothing is guessed,
inferred from name patterns, or looked up elsewhere. A detail that isn't found
is left blank; a wrong address is worse than none.

Phone parsing is North American (NANP) only, matching the US lending focus of
the rest of the rules. Other formats are ignored rather than half-parsed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# "info [at] shop [dot] com" — the common hand-obfuscation, and nothing fancier.
_OBFUSCATED_AT = re.compile(r"\s*[\[(]\s*at\s*[\])]\s*", re.IGNORECASE)
_OBFUSCATED_DOT = re.compile(r"\s*[\[(]\s*dot\s*[\])]\s*", re.IGNORECASE)

# Matches of the email pattern that are not mailboxes: retina image names
# (logo@2x.png), and addresses belonging to site builders and error trackers
# that end up in page source.
_NOT_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_NOT_EMAIL_DOMAINS = (
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "yourdomain.com",
    "sentry.io",
    "sentry.wixpress.com",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "wix.com",
    "squarespace.com",
    "godaddy.com",
)

# In text, a phone needs its separators — (215) 555-0134, 215-555-0134,
# 215.555.0134 — so bare 10-digit order numbers and IDs don't match.
_PHONE_TEXT = re.compile(
    r"(?<![\d-])(?:\+?1[\s.-]?)?"
    r"(?:\(([2-9]\d{2})\)\s?|([2-9]\d{2})[\s.-])"
    r"([2-9]\d{2})[\s.-](\d{4})(?![\d-])"
)
_FAX_NEARBY = re.compile(r"fax|facsimile", re.IGNORECASE)


@dataclass(frozen=True)
class PageContacts:
    """Contacts found on one page, in order of appearance."""

    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()


def _clean_email(raw: str) -> str:
    email = raw.strip().strip(".").lower()
    if email.endswith(_NOT_EMAIL_SUFFIXES):
        return ""
    domain = email.rsplit("@", 1)[-1]
    if any(domain == d or domain.endswith("." + d) for d in _NOT_EMAIL_DOMAINS):
        return ""
    return email


def _format_phone(digits: str) -> str:
    """'2155550134' -> '(215) 555-0134'; '' when not a valid NANP number."""
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _add(found: list[str], value: str) -> None:
    if value and value not in found:
        found.append(value)


def find_contacts(html: str) -> PageContacts:
    """Emails and phone numbers published on one page."""
    soup = BeautifulSoup(html, "lxml")
    emails: list[str] = []
    phones: list[str] = []

    # Links first: a mailto: or tel: link is the site deliberately saying
    # "contact us here", which is the strongest evidence available.
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        lowered = href.lower()
        if lowered.startswith("mailto:"):
            address = href[len("mailto:") :].split("?")[0]
            for part in address.split(","):
                if _EMAIL.fullmatch(part.strip()):
                    _add(emails, _clean_email(part))
        elif lowered.startswith("tel:"):
            _add(phones, _format_phone(re.sub(r"\D", "", href)))

    # Structured data (schema.org JSON-LD) often carries the canonical
    # "email" and "telephone" even when the visible page hides them.
    structured = " ".join(
        tag.get_text(" ")
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"})
    )

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ")
    text = _OBFUSCATED_DOT.sub(".", _OBFUSCATED_AT.sub("@", text))

    for source in (structured, text):
        for match in _EMAIL.finditer(source):
            _add(emails, _clean_email(match.group()))

        for match in _PHONE_TEXT.finditer(source):
            if _FAX_NEARBY.search(source[max(0, match.start() - 15) : match.start()]):
                continue
            area = match.group(1) or match.group(2)
            _add(phones, _format_phone(area + match.group(3) + match.group(4)))

    return PageContacts(emails=tuple(emails), phones=tuple(phones))


@dataclass(frozen=True)
class SiteContacts:
    """The best email and phone for a site, and where each was found."""

    email: str = ""
    email_page: str = ""
    other_emails: tuple[str, ...] = ()
    phone: str = ""
    phone_page: str = ""
    other_phones: tuple[str, ...] = ()


_FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com"}

# Inboxes that belong to a function, not a person. Matched on the first word
# of the address, so sales-team@ and info.philly@ count too.
_ROLE_WORDS = {
    "info", "contact", "contactus", "support", "sales", "hello", "hi", "office",
    "admin", "mail", "email", "enquiries", "inquiries", "inquiry", "service",
    "services", "customerservice", "help", "team", "orders", "order", "booking",
    "bookings", "reservations", "events", "catering", "careers", "jobs", "hr",
    "billing", "accounts", "accounting", "marketing", "media", "press",
    "webmaster", "frontdesk", "general",
}
_NO_REPLY = {"noreply", "no-reply", "donotreply", "do-not-reply"}


def is_role_address(email: str) -> bool:
    """True for info@, sales-team@ and the like; False for maria@ or j.smith@."""
    local = email.rpartition("@")[0].lower()
    first_word = re.split(r"[._+-]", local, maxsplit=1)[0]
    return local in _NO_REPLY or first_word in _ROLE_WORDS


# Inboxes for one department, which the owner doesn't read: job applications,
# billing, records requests, the press. A loan offer sent there is lost,
# where info@ or office@ at a small business still reaches someone who passes it on.
_WRONG_DEPARTMENT = re.compile(
    r"(?:hr|humanresources|jobs?|careers?|recruit\w*|resumes?|hiring|employment|payroll|credentialing|"
    r"\w*billing|medicalrecords\w*|records|privacy|compliance|abuse|techsupport|webmaster|"
    r"media|press|partnerships|publicaffairs|publicrelations|patientrelations|store\d+)"
)


def is_wrong_department(email: str) -> bool:
    """True for hr@, careers@, billing@, credentialing@ and the like."""
    local = email.rpartition("@")[0].lower()
    words = re.split(r"[._+-]", local)
    return any(_WRONG_DEPARTMENT.fullmatch(w) for w in (words[0], "".join(words)))


def _rank(found: dict[str, list[str]], prefer) -> list[tuple[str, str]]:
    """Order values best-first; returns (value, first page it appeared on).

    Ranked by prefer(value), then by how many pages repeat it — the main
    business line sits in every header and footer; a staff member's direct
    line appears once — then by first appearance.
    """
    order = list(found)
    ranked = sorted(
        order, key=lambda v: (prefer(v), -len(found[v]), order.index(v))
    )
    return [(value, found[value][0]) for value in ranked]


def summarize(pages, site_domain: str, extra: int = 3) -> SiteContacts:
    """Pick the best contacts across a site's pages.

    pages is (url, PageContacts) pairs in crawl order. The contact page is
    looked at first, then the homepage, then everything else, so ties in the
    ranking go to where a business puts the details it wants used.
    """
    ordered = sorted(
        enumerate(pages),
        key=lambda item: (0 if "contact" in item[1][0].lower() else 1, item[0]),
    )

    email_pages: dict[str, list[str]] = {}
    phone_pages: dict[str, list[str]] = {}
    for _, (url, contacts) in ordered:
        for email in contacts.emails:
            email_pages.setdefault(email, []).append(url)
        for phone in contacts.phones:
            phone_pages.setdefault(phone, []).append(url)

    def preference(email: str) -> tuple[int, int]:
        # Domain first: info@theirshop.com beats a gmail address, which beats
        # a web designer's address that happens to sit in the footer credit.
        # Within a domain, a person (maria@) beats a role inbox (info@), since
        # a named person is who a caller wants to reach.
        local, _, domain = email.rpartition("@")
        if local in _NO_REPLY:
            return (3, 1)
        if site_domain and (domain == site_domain or domain.endswith("." + site_domain)):
            where = 0
        elif domain in _FREE_MAIL:
            where = 1
        else:
            where = 2
        return (where, 1 if is_role_address(email) else 0)

    emails = _rank(email_pages, preference)
    phones = _rank(phone_pages, lambda _: 0)

    return SiteContacts(
        email=emails[0][0] if emails else "",
        email_page=emails[0][1] if emails else "",
        other_emails=tuple(value for value, _ in emails[1 : 1 + extra]),
        phone=phones[0][0] if phones else "",
        phone_page=phones[0][1] if phones else "",
        other_phones=tuple(value for value, _ in phones[1 : 1 + extra]),
    )
