"""Churches and law firms are never leads (the boss's rule, October 2026).

Caught by name or by an email or website domain, before any lookup; the
website wording ("join us for worship", "attorneys at law") is caught by the
church and law_firm signals in signals.yaml.

Whole words only, so "Temple Grading", "LaChapelle" and "Delaware" pass.
"Church" and the like count only when they name the organization — at the end
of the name, or before "of" (Grace Baptist Church Inc, Church of God) — not a
business named after one (Church Organs Inc, Cathedral Art Metal Co, or a
contractor whose owner is called Church).
"""

from __future__ import annotations

import re

CHURCH_NAME = re.compile(
    r"\bministr(?:y|ies)\b|\b(?:arch)?diocese\b|"
    r"\b(?:church(?:es)?|parish|synagogue|mosque|masjid|cathedral|tabernacle|congregation)\b"
    r"(?=\s+of\b|[\s,.]*(?:(?:inc|incorporated|corp|corporation|llc)\b\.?)?[\s,.]*$)", re.I)
LAW_NAME = re.compile(
    r"\blaw (?:firm|offices?|group|center|partners)\b|\b(?:attorneys?|lawyers?)\b|\besq\b\.?|"
    r"\blaw(?:,)? (?:llc|pllc|llp|pc|p\.c\.|pa|p\.a\.)\b|\b\w+ law\s*$", re.I)
# smithlaw.com, smithlawfirm.com, jonesattorneys.com — but not delaware.com.
LAW_DOMAIN = re.compile(r"(?:law|lawfirm|lawoffices?|lawgroup|lawyers?|attorneys?|esq)\.[a-z]+$", re.I)


def church_or_law(names=(), domains=()) -> tuple[str, str]:
    """('church' or 'law_firm', the evidence), or ('', '') for anyone else."""
    names = [n.strip() for n in names if n and n.strip()]
    for signal_id, by_name in (("church", CHURCH_NAME), ("law_firm", LAW_NAME)):
        for name in names:
            found = by_name.search(name)
            if found:
                return signal_id, f"name: {name} ({found.group(0).strip()})"
    for domain in sorted(d for d in domains if d):
        if LAW_DOMAIN.search(domain):
            return "law_firm", f"domain: {domain}"
    return "", ""
