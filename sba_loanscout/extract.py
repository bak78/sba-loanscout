"""HTML -> clean text. Chrome (nav/footer/script) is stripped before scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup

from .contacts import PageContacts, find_contacts

# Tags whose text is markup or navigation, never business signal.
#
# <footer> is deliberately NOT stripped. Footers concentrate exactly the text
# that decides a lead: "Member FDIC", "501(c)(3)", NMLS numbers, franchise
# disclosures, "since 1998", and location lists. Dropping them let a national
# bank through scoring as a qualified lead — a far worse error than the
# boilerplate noise footers also carry.
_STRIP_TAGS = ("script", "style", "noscript", "nav", "svg", "form")

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Page:
    """One fetched, cleaned page."""

    url: str
    title: str
    description: str
    text: str
    page_type: str = "other"
    contacts: PageContacts = PageContacts()

    @property
    def searchable(self) -> str:
        """Title and description are prepended so signals can match them too."""
        return "\n".join(part for part in (self.title, self.description, self.text) if part)


def clean_text(html: str) -> tuple[str, str, str]:
    """Return (title, meta description, body text) from raw HTML."""
    soup = BeautifulSoup(html, "lxml")

    title = soup.title.get_text(strip=True) if soup.title else ""

    description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        description = meta["content"].strip()

    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    body = soup.body or soup
    text = _WHITESPACE.sub(" ", body.get_text(separator=" ", strip=True)).strip()

    return title, description, text


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute, de-fragmented, same-scheme links found in the page."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        absolute = urldefrag(urljoin(base_url, href)).url
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)

    return links


def build_page(url: str, html: str, page_type: str = "other") -> Page:
    title, description, text = clean_text(html)
    return Page(
        url=url,
        title=title,
        description=description,
        text=text,
        page_type=page_type,
        contacts=find_contacts(html),
    )
