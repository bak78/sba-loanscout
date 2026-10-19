"""Keep the results to real small-business leads.

Rows that aren't leads move out of the results into a sibling file,
<results>_left_out.csv, with the reason in its first column, so they can
still be reviewed (and copied back if a rule was wrong about one):

- disqualified companies (nonprofits, lenders, public companies, directory
  sites), as each one is found;
- chains and corporate groups, at the end of each run: a website or email
  domain shared by several different companies in the results means they
  belong to one larger organization (a health system's practices, a
  building-products group's subsidiaries). Each files separately — with the
  SBA, the NPI registry or US DOT — so nothing else reveals it.

Domains that many unrelated businesses share without being one company never
count: email providers (gmail, comcast, cox…), website builders (wixsite…),
directories, and the franchise brands listed in the settings, whose offices
are independently owned small businesses.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .crawl import registered_domain
from .emailcheck import FREE_MAIL

# Internet providers and mail services whose addresses many unrelated small
# businesses use, beyond the big providers the email check already knows.
SHARED_MAIL = {
    "cox.net", "comporium.net", "bellsouth.net", "charter.net", "earthlink.net", "frontier.com",
    "frontiernet.net", "windstream.net", "centurylink.net", "embarqmail.com", "optonline.net",
    "optimum.net", "rcn.com", "ptd.net", "juno.com", "netzero.net", "roadrunner.com", "rr.com",
    "twc.com", "spectrum.net", "mindspring.com", "q.com", "suddenlink.net", "mchsi.com",
    "atlanticbb.net", "gmx.com", "zoho.com", "zohomail.com", "mail.com", "protonmail.com",
    "proton.me", "pm.me", "yandex.com", "fastmail.com", "hughes.net", "hughesnet.com",
    "tds.net", "metrocast.net", "breezeline.net", "atmc.net", "ec.rr.com", "nc.rr.com",
    "triad.rr.com", "carolina.rr.com", "sc.rr.com", "md.metrocast.net", "netscape.net",
    "aim.com", "mac.com", "live.com", "msn.com", "ymail.com", "rocketmail.com",
}
# Hosts where many businesses keep a free website under one domain.
SITE_HOSTS = {
    "wixsite.com", "wix.com", "squarespace.com", "weebly.com", "wordpress.com", "godaddysites.com",
    "business.site", "google.com", "square.site", "webflow.io", "yolasite.com", "site123.me",
    "jimdosite.com", "webs.com", "carrd.co", "blogspot.com", "myshopify.com", "hubspotpagebuilder.com",
}


PUBLIC_TLDS = {"gov", "edu", "mil"}


def left_out_path(results: Path | str) -> Path:
    results = Path(results)
    return results.with_name(f"{results.stem}_left_out.csv")


class LeftOutWriter:
    """Appends rows that aren't leads, with the reason, to <results>_left_out.csv."""

    def __init__(self, results: Path | str, columns: list[str]):
        self.path = left_out_path(results)
        self.columns = ["left_out_reason"] + list(columns)

    def write(self, row: dict, reason: str) -> None:
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.columns, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({"left_out_reason": reason, **row})


def _domain_of(email: str) -> str:
    return (email or "").strip().lower().rpartition("@")[2] if "@" in (email or "") else ""


def row_domains(row: dict, never: set[str]) -> set[str]:
    """The company's own domains in a results row: its email's, its filed
    email's (contractors) and its website's — minus shared ones."""
    found = {_domain_of(row.get("email") or ""), _domain_of(row.get("filed_email") or "")}
    if row.get("website"):
        found.add(registered_domain(row["website"]))
    # Government and school sites (a Google listing linking to a US DOT page)
    # are shared by countless unrelated businesses.
    return {d for d in found if d and d not in never and d.rpartition(".")[2] not in PUBLIC_TLDS}


def never_a_chain(allow_domains, domain_blocklist) -> set[str]:
    return set(FREE_MAIL) | SHARED_MAIL | SITE_HOSTS | set(domain_blocklist) | {d.lower() for d in allow_domains}


def leave_out_chains(results: Path | str, columns: list[str], min_companies: int,
                     never: set[str]) -> dict[str, int]:
    """Move every results row whose domain is shared by min_companies or more
    different companies to the left-out file. Returns {domain: rows moved}."""
    results = Path(results)
    if min_companies < 2 or not results.exists():
        return {}
    with results.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    companies: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        for domain in row_domains(row, never):
            companies[domain].add(row.get("key") or row.get("company_name") or "")
    chains = {d: len(keys) for d, keys in companies.items() if len(keys) >= min_companies}
    if not chains:
        return {}

    kept, moved = [], defaultdict(int)
    left_out = LeftOutWriter(results, columns)
    for row in rows:
        hit = sorted(d for d in row_domains(row, never) if d in chains)
        if hit:
            left_out.write(row, f"chain or group: {hit[0]} is shared by {chains[hit[0]]} companies")
            moved[hit[0]] += 1
        else:
            kept.append(row)

    with results.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)
    return dict(moved)
