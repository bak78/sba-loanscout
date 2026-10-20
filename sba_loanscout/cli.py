"""Command-line entry point and run orchestration.

    run  search the SBA Small Business Search for businesses in an area and the
         chosen industries (free), check their emails (free), rank them, look
         the best up on Google via Serper (1 credit each), read their
         websites, score, and append to the CSV

A run takes an optional threading.Event so the window's Stop button can end
it the same way Ctrl+C does: finished work is kept, the rest resumes on the
next run.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from .cleanup import LeftOutWriter, leave_out_chains, left_out_path, never_a_chain
from .config import ConfigError, load_settings
from .contacts import is_wrong_department
from .crawl import DEFAULT_MAX_PAGES
from .emailcheck import EmailChecker, EmailCheckUnavailable, domain_of
from .export import COLUMNS, CsvWriter, build_row, remembered_emails, sort_csv_by_score
from .fetch import PoliteFetcher
from .leads import build_lead
from .places import PlacesClient, SerperUnavailable
from .sba import (ALL_STATES, SbaClient, SbaUnavailable, SearchStopped, check_state, check_zip_prefixes,
                  find_businesses, load_sba_rules)

log = logging.getLogger("sba_loanscout")

EXIT_OK = 0
EXIT_NO_MATCHES = 1  # the SBA list has no leads for this area and these industries
EXIT_CONFIG_ERROR = 2  # missing key, bad settings, SBA search unreachable
EXIT_DEPLETED = 3  # every matching business already processed
EXIT_LOOKUPS_FAILED = 4  # businesses were attempted and none could be looked up
EXIT_SERPER_UNAVAILABLE = 5  # Serper refused the key or is out of credits; the run stopped early
EXIT_INTERRUPTED = 130

DEFAULT_OUT = "out/sba_leads.csv"
EMAIL_CHECK_WORKERS = 32
UNREACHABLE = "couldn't check the domain (try again later)"


@dataclass
class RunOptions:
    """What a run should do. None means 'use the sba.yaml setting'."""

    state: str | None = None
    zip_prefixes: tuple[str, ...] | None = None
    fields: tuple[str, ...] | None = None  # industry checkboxes; None = sba.yaml's default_fields
    limit: int | None = None
    dry_run: bool = False
    out: str = DEFAULT_OUT
    max_pages: int = DEFAULT_MAX_PAGES
    workers: int = 8
    delay: float = 1.0
    no_resume: bool = False


@dataclass
class RunSummary:
    """Where every business ended up. Printed at the end of a run."""

    matching: int = 0
    no_contact: int = 0
    left_before_lookup: Counter = field(default_factory=Counter)
    retry_email_check: int = 0
    already_done: int = 0
    attempted: int = 0
    written: int = 0
    disqualified: int = 0
    no_listing: int = 0
    website_only: int = 0
    retry_later: int = 0
    chains: int = 0
    with_phone: int = 0
    with_website: int = 0
    contacts_own: int = 0
    credits_used: int = 0
    serper_unavailable: bool = False

    def outcome(self) -> int:
        if self.serper_unavailable:
            return EXIT_SERPER_UNAVAILABLE
        if self.matching == 0:
            return EXIT_NO_MATCHES
        if self.attempted == 0:
            return EXIT_DEPLETED
        if self.written == 0 and self.retry_later:
            return EXIT_LOOKUPS_FAILED
        return EXIT_OK

    def report(self, out_path: str, preview: bool = False) -> None:
        line = "-" * 58
        log.info(line)
        log.info("%-34s %d", "businesses with a public contact", self.matching)
        for reason, n in self.left_before_lookup.most_common():
            log.info("%-34s %d", f"  left out: {reason}"[:34], n)
        if self.retry_email_check:
            log.info("%-34s %d", "  email not checked (next run)", self.retry_email_check)
        if self.already_done:
            log.info("%-34s %d", "skipped (already done)", self.already_done)
        log.info("%-34s %d", "ranked in the preview" if preview else "looked up this run", self.attempted)
        if self.retry_later:
            log.info("%-34s %d", "  failed (will retry)", self.retry_later)
        if self.disqualified:
            log.info("%-34s %d", "  left out: disqualified", self.disqualified)
        if self.no_listing:
            log.info("%-34s %d", "  left out: no Google listing", self.no_listing)
        if self.website_only:
            log.info("%-34s %d", "  kept: own website, not on Google", self.website_only)
        log.info("%-34s %d", "  with the contact's own email", self.contacts_own)
        log.info("%-34s %d", "  with a phone number", self.with_phone)
        log.info("%-34s %d", "  with a website", self.with_website)
        log.info("%-34s %d", "Serper credits used", self.credits_used)
        log.info("%-34s %d  ->  %s", "rows written", self.written, out_path)
        if self.chains:
            log.info("%-34s %d", "moved out: chains or groups", self.chains)
        log.info("%-34s %s", "left-out rows (with reasons)", left_out_path(out_path))
        log.info(line)


def preview_path(out: str) -> str:
    return out[:-4] + "_preview.csv" if out.endswith(".csv") else out + "_preview.csv"


def run_leads(options: RunOptions, stop: threading.Event | None = None,
              sba_client: SbaClient | None = None, places=None) -> int:
    """sba_client and places can be passed in (tests); otherwise real ones are made."""
    stop = stop or threading.Event()
    try:
        settings = load_settings()
        srules = load_sba_rules()
        state = check_state(options.state or srules.state)
        prefixes = srules.zip_prefixes if options.zip_prefixes is None else options.zip_prefixes
        check_zip_prefixes(prefixes)
        if state == ALL_STATES and prefixes:
            raise ConfigError("ZIP codes need a state; pick one, or clear the ZIP codes for all states")
        fields = srules.default_fields if options.fields is None else options.fields
        if not fields:
            raise ConfigError("Tick at least one industry.")
        for name in fields:
            srules.field_named(name)  # an unknown name stops here, with the list
        api_key = None if options.dry_run or places is not None else settings.require_serper_key()
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR

    rules = settings.rules
    limit = srules.default_limit if options.limit is None else options.limit
    area = "all states" if state == ALL_STATES else (
        f"{state} ZIP {', '.join(p + '*' for p in prefixes)}" if prefixes else f"all of {state}")
    log.info("searching the SBA list: %s, %d industries (free)...", area, len(fields))

    client = sba_client or SbaClient(settings.user_agent, stop=stop)
    try:
        businesses, skipped = find_businesses(client, srules, state, prefixes, fields)
    except SearchStopped:
        log.warning("stopped during the SBA search — nothing was written")
        return EXIT_INTERRUPTED
    except (SbaUnavailable, ConfigError) as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR
    finally:
        if sba_client is None:
            client.close()

    summary = RunSummary(matching=len(businesses), no_contact=sum(skipped.values()))
    log.info("%d businesses with a public contact and email%s", len(businesses),
             f" ({summary.no_contact} more without one were skipped)" if skipped else "")
    if not businesses:
        return EXIT_NO_MATCHES

    # Rank on SBA facts alone (free), so credits go to the strongest first.
    ranked = sorted(
        businesses,
        key=lambda b: (build_lead(b, None, None, rules, srules, options.max_pages).score.total,
                       b.years_in_business(), b.company_name),
        reverse=True,
    )

    checker = EmailChecker()
    used = {} if options.dry_run or options.no_resume else emails_in(options.out)
    try:
        kept, left = screen(ranked, rules, srules, options.max_pages, checker, summary, stop, used)
    except SearchStopped:
        log.warning("stopped while checking emails — nothing was written")
        return EXIT_INTERRUPTED

    if options.dry_run:
        return _preview(kept, left, options, limit, rules, srules, summary)

    places = places or PlacesClient(api_key)
    fetcher = PoliteFetcher(settings.user_agent, default_delay=options.delay)
    left_out = LeftOutWriter(options.out, COLUMNS)
    try:
        with CsvWriter(options.out, resume=not options.no_resume) as writer:
            # Businesses left out before any lookup are recorded once, with
            # their reason, and not checked again.
            for b, reason in left:
                if not writer.already_done(b.key):
                    lead = build_lead(b, None, None, rules, srules, options.max_pages)
                    left_out.write(build_row(lead, rules), reason)
                    writer.mark_done(b.key)

            pending = [b for b in kept if not writer.already_done(b.key)]
            summary.already_done = len(kept) - len(pending)
            if limit > 0:
                pending = pending[:limit]
            if not pending:
                log.info("nothing new — every matching business is already done")
                summary.report(options.out)
                return EXIT_DEPLETED

            log.info("looking up the top %d on Google (about %d Serper credits)", len(pending), len(pending))
            with ThreadPoolExecutor(max_workers=options.workers) as pool:
                futures = {pool.submit(build_lead, b, places, fetcher, rules, srules, options.max_pages): b
                           for b in pending}
                not_done = set(futures)
                try:
                    while not_done:
                        if stop.is_set():
                            raise KeyboardInterrupt
                        # Polled once a second so Stop and Ctrl+C are noticed promptly.
                        done, not_done = wait(not_done, timeout=1.0, return_when=FIRST_COMPLETED)
                        for future in done:
                            b = futures[future]
                            summary.attempted += 1
                            try:
                                lead = future.result()
                            except SerperUnavailable as exc:
                                log.error("%s — stopping; finished rows are saved", exc)
                                summary.serper_unavailable = True
                                raise
                            except Exception as exc:  # one bad business must not kill the run
                                summary.retry_later += 1
                                log.warning("%s: failed, will retry next run (%s)", b.company_name, exc)
                                continue

                            row = build_row(lead, rules)
                            if lead.score.disqualified:
                                why = f"disqualified: {lead.score.disqualify_reason}"
                                summary.disqualified += 1
                            elif not lead.listing.matched_on and not lead.confirmed_by_website:
                                why = f"no matching Google listing ({lead.listing.note})"
                                summary.no_listing += 1
                            else:
                                why = ""
                            if why:
                                # Done either way: the lookup was paid for.
                                writer.mark_done(b.key)
                                left_out.write(row, why)
                                log.info("%-36.36s left out: %s", b.company_name, why)
                                continue

                            writer.write(row)
                            _tally(summary, row, lead)
                            log.info("%-36.36s %-22.22s score=%-5s %s", b.company_name,
                                     f"{b.contact_first} {b.contact_last}", row["score"], b.email)
                except (KeyboardInterrupt, SerperUnavailable):
                    fetcher.stop()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    except KeyboardInterrupt:
        log.warning("stopped — %d rows already written to %s (run again to continue)",
                    summary.written, options.out)
        return EXIT_INTERRUPTED
    except SerperUnavailable:
        pass
    finally:
        summary.credits_used = getattr(places, "lookups", 0)
        fetcher.close()
        places.close()

    summary.chains = _leave_out_chains(options.out, srules, rules)
    sort_csv_by_score(options.out)
    summary.report(options.out)
    return summary.outcome()


def screen(ranked, rules, srules, max_pages, checker: EmailChecker, summary: RunSummary,
           stop: threading.Event, used: dict[str, str]):
    """Free checks before any lookup, best-ranked first. Returns (kept,
    [(business, reason)]); a business whose email domain couldn't be checked
    is in neither, and is tried again next run."""
    problems = check_emails([b.email for b in ranked], checker, stop)
    chains = shared_domains(ranked, rules, srules)
    kept, left = [], []
    emails: dict[str, str] = {}
    people: dict[tuple, str] = {}
    for b in ranked:
        lead = build_lead(b, None, None, rules, srules, max_pages)
        person = (b.contact_first.lower(), b.contact_last.lower(), b.state)
        if problems.get(b.email) == UNREACHABLE:
            summary.retry_email_check += 1
            continue
        if lead.score.disqualified:
            label, reason = lead.score.disqualify_reason, f"disqualified: {lead.score.disqualify_reason}"
        elif domain_of(b.email) in chains:
            label = "chain or group"
            reason = f"chain or group: {domain_of(b.email)} is shared by {chains[domain_of(b.email)]} businesses"
        elif problems.get(b.email):
            label, reason = "email fails the check", f"email fails the check: {problems[b.email]}"
        elif is_wrong_department(b.email):
            label, reason = "department inbox", f"department inbox: {b.email} won't reach the owner"
        elif used.get(b.email, b.key) != b.key:
            label, reason = "email already used", f"{b.email} already used for another lead"
        elif b.email in emails:
            label, reason = "same email twice", f"same email as {emails[b.email]} (kept that one)"
        elif person in people:
            label, reason = "same contact twice", f"same contact as {people[person]} (kept that one)"
        else:
            emails[b.email] = b.company_name
            people[person] = b.company_name
            kept.append(b)
            continue
        left.append((b, reason))
        summary.left_before_lookup[label] += 1
    log.info("%d pass every free check; %d left out before any lookup", len(kept), len(left))
    return kept, left


def shared_domains(ranked, rules, srules) -> dict[str, int]:
    """{email domain: businesses using it} for domains shared by
    chain_min_companies or more different businesses — one company's many
    nursing homes or branches (credentialing@chs-corp.com on 17 of them).
    Email providers, franchise brands and the like never count."""
    if srules.chain_min_companies < 2:
        return {}
    never = never_a_chain(srules.franchise_domains, rules.domain_blocklist)
    keys = defaultdict(set)
    for b in ranked:
        domain = domain_of(b.email)
        if domain and domain not in never:
            keys[domain].add(b.key)
    return {d: len(k) for d, k in keys.items() if len(k) >= srules.chain_min_companies}


def check_emails(emails: list[str], checker: EmailChecker, stop: threading.Event) -> dict[str, str]:
    """{email: problem or ''}. Each email domain is asked once, many at a time;
    Stop is checked twice a second. Raises SearchStopped on Stop."""
    unique = sorted(set(emails))
    domains = sorted({domain_of(e) for e in unique if checker.needs_dns(e)})
    unreachable: set[str] = set()

    def ask(domain: str) -> None:
        try:
            checker.domain_problem(domain)
        except EmailCheckUnavailable:
            unreachable.add(domain)

    if domains:
        log.info("checking %d email domains...", len(domains))
    # Not a with-block: leaving one waits for every running check, which would
    # make Stop slow. On Stop, checks not started are cancelled.
    pool = ThreadPoolExecutor(max_workers=EMAIL_CHECK_WORKERS)
    try:
        pending = {pool.submit(ask, d) for d in domains}
        last_report = time.monotonic()
        while pending:
            if stop.is_set():
                raise SearchStopped()
            _, pending = wait(pending, timeout=0.5)
            if pending and time.monotonic() - last_report >= 10:
                log.info("  checked %d of %d email domains", len(domains) - len(pending), len(domains))
                last_report = time.monotonic()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {e: UNREACHABLE if domain_of(e) in unreachable else checker.problem(e) for e in unique}


def emails_in(path: str) -> dict[str, str]:
    """Emails already used, and whose they are — in the results, or in an
    earlier batch whose results file has since been deleted."""
    used = remembered_emails(path)
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("email"):
                    used.setdefault(row["email"].strip().lower(), row.get("key", ""))
    except (OSError, KeyError):
        pass
    return used


def _leave_out_chains(out: str, srules, rules) -> int:
    """Move chains' and groups' rows out of the results; how many moved."""
    moved = leave_out_chains(out, COLUMNS, srules.chain_min_companies,
                             never_a_chain(srules.franchise_domains, rules.domain_blocklist))
    for domain, count in sorted(moved.items(), key=lambda x: -x[1]):
        log.info("left out %d rows sharing %s (a chain or group; add it to franchise_domains "
                 "in sba.yaml to keep them)", count, domain)
    return sum(moved.values())


def _tally(summary: RunSummary, row: dict, lead) -> None:
    summary.written += 1
    if row["phone"]:
        summary.with_phone += 1
    if row["website"]:
        summary.with_website += 1
    if lead.email_is_contacts_own:
        summary.contacts_own += 1
    if lead.confirmed_by_website:
        summary.website_only += 1


def _preview(kept, left, options, limit, rules, srules, summary) -> int:
    """Score from SBA facts alone, into a separate file. Nothing is marked
    done, so a real run afterwards still processes every business."""
    out = preview_path(options.out)
    left_out_path(out).unlink(missing_ok=True)  # the preview starts over each time
    left_out = LeftOutWriter(out, COLUMNS)
    with CsvWriter(out, resume=False, track_seen=False) as writer:
        for b in kept:
            lead = build_lead(b, None, None, rules, srules, options.max_pages)
            row = build_row(lead, rules)
            writer.write(row)
            summary.attempted += 1
            _tally(summary, row, lead)
        for b, reason in left:
            left_out.write(build_row(build_lead(b, None, None, rules, srules, options.max_pages), rules), reason)

    summary.chains = _leave_out_chains(out, srules, rules)
    sort_csv_by_score(out)
    next_run = min(limit, len(kept)) if limit > 0 else len(kept)
    log.info("preview only — no Google lookups made. The next real run would look up the "
             "top %d (about %d credits).", next_run, next_run)
    summary.report(out, preview=True)
    return EXIT_OK


# --- command line ----------------------------------------------------------------------


def _zip_list(text: str) -> tuple[str, ...]:
    """'191,190' -> ('191', '190'); 'all' -> () meaning the whole state."""
    if text.strip().lower() in ("", "all"):
        return ()
    return tuple(z.strip() for z in text.split(",") if z.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sba_loanscout",
        description="Find small businesses from the SBA Small Business Search, with the contact's "
        "name, email and phone and an evidence-backed score.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="find, look up and score leads")
    run.add_argument("--state", help="two-letter state code, or ALL (default: sba.yaml)")
    run.add_argument("--zip", type=_zip_list,
                     help="ZIP code starts, comma-separated, e.g. 191 or 191,190; 'all' for the whole state")
    run.add_argument("--industry", action="append", metavar="NAME",
                     help="an industry as named in sba.yaml (repeat for several), e.g. --industry 'Home care'")
    run.add_argument("--group", action="append", metavar="NAME",
                     help="every industry in a group, e.g. --group Healthcare")
    run.add_argument("--limit", type=int, help="businesses looked up this run (default: sba.yaml; 0 = no cap)")
    run.add_argument("--dry-run", action="store_true",
                     help="free preview from the SBA list only; no lookups, no credits")
    run.add_argument("--out", default=DEFAULT_OUT, help="output CSV path")
    run.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES, help="website pages per business")
    run.add_argument("--workers", type=int, default=8, help="businesses processed at once")
    run.add_argument("--delay", type=float, default=1.0, help="seconds between requests to one website")
    run.add_argument("--no-resume", action="store_true",
                     help="start the output over instead of skipping businesses already done")
    run.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def fields_from(industries, groups) -> tuple[str, ...] | None:
    """--industry and --group as one list of industry names; None = the default."""
    if not industries and not groups:
        return None
    names = list(industries or [])
    if groups:
        srules = load_sba_rules()
        wanted = {g.lower() for g in groups}
        known = {g.lower() for g, _ in srules.groups}
        unknown = wanted - known
        if unknown:
            raise ConfigError(f"unknown group {sorted(unknown)}; sba.yaml has: "
                              f"{', '.join(g for g, _ in srules.groups)}")
        names += [f.name for g, fs in srules.groups if g.lower() in wanted for f in fs]
    return tuple(dict.fromkeys(names))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    try:
        fields = fields_from(args.industry, args.group)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG_ERROR
    return run_leads(
        RunOptions(
            state=args.state,
            zip_prefixes=args.zip,
            fields=fields,
            limit=args.limit,
            dry_run=args.dry_run,
            out=args.out,
            max_pages=args.max_pages,
            workers=args.workers,
            delay=args.delay,
            no_resume=args.no_resume,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
