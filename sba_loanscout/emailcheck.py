"""Is an email address worth sending to?

A lead goes out only with an address that passes every free check:

- it is shaped like an address (no spaces, a real top-level domain);
- it isn't a placeholder — none@, na@, test@, noemail.com — or a no-reply box;
- its domain isn't a known misspelling of a big mail provider (gmial.com);
- its domain exists and accepts mail: DNS lists a mail server (MX record) for it.

What this can't tell is whether the mailbox itself still exists — whether
jsmith@ left the company. Mail providers don't answer that for free; a paid
verifier (NeverBounce, ZeroBounce…) checks it before a send.
"""

from __future__ import annotations

import re
import threading

import dns.exception
import dns.resolver

_SHAPE = re.compile(r"[a-z0-9][a-z0-9._%+'-]*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}")

_PLACEHOLDER_MAILBOXES = {
    "none", "na", "n.a", "noemail", "no.email", "no-email", "nomail", "no", "test", "unknown",
    "null", "x", "xx", "xxx", "abc", "asdf", "email", "youremail", "name", "noreply",
    "no-reply", "donotreply", "do-not-reply",
}
_PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "none.com", "noemail.com",
    "nomail.com", "na.com", "email.com", "domain.com", "yourdomain.com", "company.com",
}

# Misspellings of the big providers. Some are registered by squatters and do
# have mail servers, so DNS alone wouldn't catch them.
_TYPO_DOMAINS = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmal.com": "gmail.com",
    "gmail.co": "gmail.com", "gmail.cm": "gmail.com", "gmail.con": "gmail.com",
    "gmaill.com": "gmail.com", "gamil.com": "gmail.com", "gnail.com": "gmail.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com",
    "yahoo.con": "yahoo.com", "yhoo.com": "yahoo.com",
    "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "hotmail.con": "hotmail.com", "outlok.com": "outlook.com", "outlook.co": "outlook.com",
    "aol.co": "aol.com", "aol.con": "aol.com", "comcast.com": "comcast.net",
    "comcat.net": "comcast.net", "verizon.com": "verizon.net", "icloud.co": "icloud.com",
}

# Big providers known to take mail; no need to ask DNS about them.
_KNOWN_GOOD = {
    "gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "ymail.com", "protonmail.com", "mail.com",
}

FREE_MAIL = _KNOWN_GOOD


class EmailCheckUnavailable(ConnectionError):
    """DNS didn't answer, so an address could be neither passed nor failed."""


def domain_of(email: str) -> str:
    return email.rpartition("@")[2].lower()


class EmailChecker:
    """Checks addresses; remembers each domain's answer for the whole run.
    Safe to share between worker threads."""

    def __init__(self, resolver=None, timeout: float = 6.0):
        if resolver is None:
            resolver = dns.resolver.Resolver()
            resolver.lifetime = timeout
        self._resolver = resolver
        self._domains: dict[str, str] = {}
        self._lock = threading.Lock()

    def problem(self, email: str) -> str:
        """Why the address shouldn't be sent to, or '' when it passes.
        Raises EmailCheckUnavailable when DNS can't be reached."""
        answer = self._without_dns(email)
        return answer if answer is not None else self.domain_problem(domain_of(email))

    def ok(self, email: str) -> bool:
        return self.problem(email) == ""

    def needs_dns(self, email: str) -> bool:
        """Whether checking this address means asking DNS about its domain."""
        return self._without_dns(email) is None

    def _without_dns(self, email: str) -> str | None:
        """The verdict from the address alone, or None when DNS must decide."""
        email = (email or "").strip().lower()
        if not email:
            return "no email"
        if not _SHAPE.fullmatch(email) or ".." in email:
            return "not a valid address"
        mailbox, _, domain = email.rpartition("@")
        if mailbox in _PLACEHOLDER_MAILBOXES or domain in _PLACEHOLDER_DOMAINS:
            return "placeholder address"
        if domain in _TYPO_DOMAINS:
            return f"misspelled domain (meant {_TYPO_DOMAINS[domain]}?)"
        # Gmail usernames are 6 to 30 letters and numbers; dots don't count.
        if domain == "gmail.com" and len(mailbox.split("+")[0].replace(".", "")) < 6:
            return "not a possible Gmail address (too short)"
        if domain in _KNOWN_GOOD:
            return ""
        return None

    def domain_problem(self, domain: str) -> str:
        """Whether the domain takes mail; each domain is asked once per run.
        Raises EmailCheckUnavailable when DNS can't be reached."""
        with self._lock:
            if domain in self._domains:
                return self._domains[domain]
        answer = self._ask_dns(domain)
        with self._lock:
            self._domains[domain] = answer
        return answer

    def _ask_dns(self, domain: str) -> str:
        try:
            records = self._resolver.resolve(domain, "MX")
        except dns.resolver.NXDOMAIN:
            return "domain doesn't exist"
        except dns.resolver.NoAnswer:
            return "domain takes no email (no mail server)"
        except (dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
            raise EmailCheckUnavailable(f"couldn't check {domain}: {type(exc).__name__}") from exc
        # A "null MX" (a single record pointing at ".") says: we take no mail.
        hosts = [str(r.exchange).rstrip(".") for r in records]
        if not any(hosts):
            return "domain takes no email (no mail server)"
        return ""
