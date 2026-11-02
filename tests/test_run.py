"""Scoring a business, and whole runs — with the SBA, Serper, email checks and
websites stubbed, so nothing is spent."""

import csv
import threading
from datetime import date

import pytest

from sba_loanscout import cli, leads
from sba_loanscout.export import COLUMNS, build_row
from sba_loanscout.places import Listing, SerperUnavailable, match_listing
from sba_loanscout.sba import load_sba_rules, parse_business
from test_sba import FakeClient, rec

TODAY = date(2026, 10, 19)


@pytest.fixture(scope="module")
def srules():
    return load_sba_rules()


def business(srules, **overrides):
    b, why = parse_business(rec(**overrides), srules.field_named("Therapy (PT, OT, speech)"))
    assert b is not None, why
    return b


def listing(**overrides):
    values = dict(name="BodySense PT", address="6 Tiger Way, Boonsboro, MD 21713", phone="+1 301-432-0011",
                  website="", matched_on="address", rating=4.8, reviews=72, category="Physical therapist")
    values.update(overrides)
    return Listing(**values)


class StubPlaces:
    def __init__(self, result=None, error=None):
        self.result = result or listing()
        self.error = error
        self.lookups = 0

    def find(self, *args, **kwargs):
        self.lookups += 1
        if self.error:
            raise self.error
        return self.result

    def close(self):
        pass


class StubChecker:
    """No DNS: emails at these domains fail, all others pass."""
    bad = {"deadmail.com": "domain doesn't accept mail"}

    def needs_dns(self, email):
        return False

    def domain_problem(self, domain):
        return self.bad.get(domain, "")

    def problem(self, email):
        return self.bad.get(email.rpartition("@")[2], "")


# --- scoring ------------------------------------------------------------------------------------


def test_sba_facts_score_without_any_lookup(rules, srules):
    lead = leads.build_lead(business(srules), None, None, rules, srules, 8, TODAY)
    assert {"sba_registered", "established", "contacts_own_email", "company_domain", "sba_certified"} <= set(
        lead.score.signal_ids)
    assert lead.email_is_contacts_own and not lead.score.disqualified


def test_google_facts_raise_the_weight(rules, srules):
    alone = leads.build_lead(business(srules), None, None, rules, srules, 8, TODAY).score.total
    looked_up = leads.build_lead(business(srules), StubPlaces(), None, rules, srules, 8, TODAY).score.total
    assert looked_up > alone


@pytest.mark.parametrize("overrides, reason", [
    (dict(business_type_codes="A8"), "nonprofit"),
    (dict(email="director@county.gov"), "public_body"),
    (dict(email="store9@walgreens.com"), "national_chain"),
])
def test_never_leads(rules, srules, overrides, reason):
    lead = leads.build_lead(business(srules, **overrides), None, None, rules, srules, 8, TODAY)
    assert lead.score.disqualified and reason in lead.score.disqualify_reason


def test_an_org_email_alone_is_not_a_nonprofit(rules, srules):
    # Most .org addresses in the SBA list belong to for-profit companies.
    lead = leads.build_lead(business(srules, email="jane@carepartners.org"), None, None, rules, srules, 8, TODAY)
    assert not lead.score.disqualified


def test_the_row_has_every_column(rules, srules):
    row = build_row(leads.build_lead(business(srules), StubPlaces(), None, rules, srules, 8, TODAY), rules)
    assert set(row) == set(COLUMNS)
    assert (row["first_name"], row["last_name"], row["email"]) == ("Shannon", "Murphy", "smurphy@bodysensept.com")
    assert (row["industry"], row["industry_group"]) == ("Therapy (PT, OT, speech)", "Healthcare")
    assert row["phone_source"] == "SBA" and row["email_is_contacts_own"] == "yes"


def test_a_listing_elsewhere_with_the_businesss_own_website_is_a_match():
    # The SBA address is a home; the Google listing is at the office, on the same domain.
    office = {"title": "BodySense Physical Therapy", "address": "100 Main St, Hagerstown, MD 21740",
              "website": "https://www.bodysensept.com/contact"}
    found = match_listing([office], "Bodysense PT", "6 TIGER WAY", "21713", domains=("bodysensept.com",))
    assert found.matched_on == "email domain"
    assert match_listing([office], "Bodysense PT", "6 TIGER WAY", "21713").matched_on == ""


# --- whole runs ---------------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setattr(cli, "EmailChecker", StubChecker)
    monkeypatch.setattr(cli, "sent_emails", lambda path: set())  # never the real sent list
    return tmp_path


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _records(n):
    return [rec(uei=f"U{i}", email=f"owner{i}@gmail.com", contact_person=f"PAT SMITH{chr(65 + i)}",
                website=f"www.practice{i}.com")
            for i in range(n)]


OPTIONS = dict(state="MD", zip_prefixes=(), fields=("Therapy (PT, OT, speech)",), out="out/leads.csv", workers=1)


def test_preview_is_free_and_writes_every_business(workdir):
    places = StubPlaces()
    options = cli.RunOptions(dry_run=True, **OPTIONS)
    assert cli.run_leads(options, sba_client=FakeClient(_records(4)), places=places) == cli.EXIT_OK
    assert places.lookups == 0
    assert len(_rows(workdir / "out/leads_preview.csv")) == 4


def test_run_looks_up_the_limit_and_resumes_where_it_stopped(workdir):
    places = StubPlaces()
    options = cli.RunOptions(limit=2, **OPTIONS)
    assert cli.run_leads(options, sba_client=FakeClient(_records(3)), places=places) == cli.EXIT_OK
    assert places.lookups == 2
    assert cli.run_leads(options, sba_client=FakeClient(_records(3)), places=places) == cli.EXIT_OK
    assert places.lookups == 3, "the second run only looked up the one left"
    assert len(_rows(workdir / "out/leads.csv")) == 3
    assert cli.run_leads(options, sba_client=FakeClient(_records(3)), places=places) == cli.EXIT_DEPLETED


def test_free_checks_leave_businesses_out_before_any_lookup_with_reasons(workdir):
    records = [
        rec(uei="GOOD", email="owner@gmail.com", contact_person="ANA LOPEZ"),
        rec(uei="DEPT", email="hr@bodysensept.com", contact_person="BEN KIM"),
        rec(uei="DEAD", email="x@deadmail.com", contact_person="CY DOE"),
        rec(uei="NPO", email="npo@gmail.com", contact_person="DEE FOX", business_type_codes="A8"),
        rec(uei="TWIN", email="owner@gmail.com", contact_person="EVE RAY"),
        rec(uei="SAME", email="other@gmail.com", contact_person="ANA LOPEZ"),
    ]
    places = StubPlaces()
    assert cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(records), places=places) == cli.EXIT_OK
    assert places.lookups == 1
    assert [r["key"] for r in _rows(workdir / "out/leads.csv")] == ["GOOD"]
    reasons = {r["key"]: r["left_out_reason"] for r in _rows(workdir / "out/leads_left_out.csv")}
    assert reasons["DEPT"].startswith("department inbox")
    assert reasons["DEAD"].startswith("email fails the check")
    assert reasons["NPO"].startswith("disqualified: nonprofit")
    assert reasons["TWIN"].startswith("same email as")
    assert reasons["SAME"].startswith("same contact as")
    # Recorded once: a second run neither looks them up nor writes them again.
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(records), places=places)
    assert len(_rows(workdir / "out/leads_left_out.csv")) == 5


def test_an_email_from_an_earlier_batch_is_not_listed_again(workdir):
    places = StubPlaces()
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient([rec(uei="A", email="pat@gmail.com")]), places=places)
    (workdir / "out/leads.csv").unlink()  # sent to the boss; a fresh batch starts
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient([rec(uei="B", email="pat@gmail.com")]), places=places)
    assert places.lookups == 1
    reasons = [r["left_out_reason"] for r in _rows(workdir / "out/leads_left_out.csv")]
    assert reasons == ["pat@gmail.com already used for another lead"]


def test_no_google_listing_is_left_out_with_the_reason(workdir):
    places = StubPlaces(Listing(note="no listings found"))
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(_records(1)), places=places)
    assert not (workdir / "out/leads.csv").exists() or _rows(workdir / "out/leads.csv") == []
    [left] = _rows(workdir / "out/leads_left_out.csv")
    assert left["left_out_reason"].startswith("no matching Google listing")


def test_out_of_credits_stops_the_run(workdir):
    places = StubPlaces(error=SerperUnavailable("out of credits"))
    assert cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(_records(2)),
                         places=places) == cli.EXIT_SERPER_UNAVAILABLE


def test_stop_before_lookups_writes_nothing(workdir):
    stop = threading.Event()
    stop.set()
    assert cli.run_leads(cli.RunOptions(**OPTIONS), stop, sba_client=FakeClient(_records(2)),
                         places=StubPlaces()) == cli.EXIT_INTERRUPTED


def test_zip_codes_need_a_state(workdir):
    options = cli.RunOptions(**(OPTIONS | dict(state="ALL", zip_prefixes=("212",))))
    assert cli.run_leads(options, sba_client=FakeClient([]), places=StubPlaces()) == cli.EXIT_CONFIG_ERROR


def test_groups_on_the_command_line_mean_all_their_industries():
    names = cli.fields_from(None, ["construction"])
    assert "Roofing" in names and len(names) == 8


# --- chains, credentialing inboxes, and businesses not on Google ------------------------------


def test_one_companys_many_businesses_are_a_chain_before_any_lookup(workdir):
    homes = [rec(uei=f"N{i}", email=f"admin{i}@chs-corp.com", contact_person=f"PAT LEE{chr(65 + i)}",
                 website=f"www.home{i}.com") for i in range(3)]
    places = StubPlaces()
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(homes + _records(1)), places=places)
    assert places.lookups == 1, "no credits spent on the chain"
    reasons = [r["left_out_reason"] for r in _rows(workdir / "out/leads_left_out.csv")]
    assert reasons == ["chain or group: chs-corp.com is shared by 3 businesses"] * 3


def test_a_credentialing_inbox_is_a_department_inbox():
    from sba_loanscout.contacts import is_wrong_department

    assert is_wrong_department("credentialing@chs-corp.com")


def _crawl_of(url):
    from sba_loanscout.crawl import SiteCrawl, registered_domain
    from sba_loanscout.extract import build_page

    crawl = SiteCrawl(domain=registered_domain(url), start_url=url)
    crawl.pages = [build_page(url, "<p>Welcome to our practice.</p>")]
    return crawl


@pytest.mark.parametrize("email, website, kept", [
    ("arielle@mindsetqualityllc.com", "https://mindsetqualityllc.com", True),   # own site, own domain
    ("tyler@thomaspointcap.com", "https://www.homeinstead.com/131", True),      # a franchise owner's page
    ("jane@gmail.com", "https://janesclinic.com", False),                       # site doesn't match the email
    ("jane@janesclinic.com", "", False),                                        # no website at all
])
def test_not_on_google_but_its_own_website_loads(workdir, monkeypatch, email, website, kept):
    monkeypatch.setattr(leads, "crawl_site", lambda url, *a, **k: _crawl_of(url))
    record = rec(uei="W1", email=email, website=website)
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient([record]),
                  places=StubPlaces(Listing(note="no listings found")))
    results = _rows(workdir / "out/leads.csv") if (workdir / "out/leads.csv").exists() else []
    if kept:
        [row] = results
        assert row["google_match"] == "not on Google; own website"
        assert "own_website" in row["top_signals"]
    else:
        assert results == []
        [left] = _rows(workdir / "out/leads_left_out.csv")
        assert left["left_out_reason"].startswith("no matching Google listing")


def test_a_franchise_brands_page_counts_even_when_the_brand_turns_programs_away(rules, srules, monkeypatch):
    from sba_loanscout.crawl import SiteCrawl

    monkeypatch.setattr(leads, "crawl_site", lambda url, *a, **k: SiteCrawl(domain="", start_url=url))
    b = business(srules, email="tyler@thomaspointcap.com", website="https://www.homeinstead.com/131")
    lead = leads.build_lead(b, StubPlaces(Listing(note="no listings found")), object(), rules, srules, 8, TODAY)
    assert lead.confirmed_by_website


def test_a_website_that_wont_load_confirms_nothing(rules, srules, monkeypatch):
    from sba_loanscout.crawl import SiteCrawl

    gone = SiteCrawl(domain="renewedbh.org", start_url="https://renewedbh.org", permanently_failed=True)
    monkeypatch.setattr(leads, "crawl_site", lambda url, *a, **k: gone)
    b = business(srules, email="kim@renewedbh.org", website="renewedbh.org")
    lead = leads.build_lead(b, StubPlaces(Listing(note="no listings found")), object(), rules, srules, 8, TODAY)
    assert not lead.confirmed_by_website


def _silent_site(url, *args, **kwargs):
    from sba_loanscout.crawl import SiteCrawl

    return SiteCrawl(domain="renewedbh.org", start_url=url, errors=[f"{url}: ConnectionError"])


def test_a_website_that_didnt_answer_isnt_a_verdict_yet(rules, srules, monkeypatch):
    monkeypatch.setattr(leads, "crawl_site", _silent_site)
    b = business(srules, email="kim@renewedbh.org", website="renewedbh.org")
    with pytest.raises(leads.SiteUnreachable) as caught:
        leads.build_lead(b, StubPlaces(Listing(note="no listings found")), object(), rules, srules, 8, TODAY)
    assert not caught.value.lead.confirmed_by_website


def test_a_silent_website_doesnt_matter_when_google_confirms_the_business(rules, srules, monkeypatch):
    monkeypatch.setattr(leads, "crawl_site", _silent_site)
    b = business(srules, email="kim@renewedbh.org", website="renewedbh.org")
    lead = leads.build_lead(b, StubPlaces(), object(), rules, srules, 8, TODAY)
    assert lead.listing.matched_on


def test_a_silent_website_gets_one_more_lookup_then_is_left_out(workdir, monkeypatch):
    monkeypatch.setattr(leads, "crawl_site", _silent_site)
    places = StubPlaces(Listing(note="no listings found"))
    record = rec(uei="W1", email="kim@renewedbh.org", website="renewedbh.org")
    run = lambda: cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient([record]), places=places)

    run()
    assert places.lookups == 1 and not (workdir / "out/leads_left_out.csv").exists(), "not a verdict yet"
    run()
    assert places.lookups == 2
    [left] = _rows(workdir / "out/leads_left_out.csv")
    assert left["left_out_reason"].startswith("no matching Google listing")
    assert run() == cli.EXIT_DEPLETED and places.lookups == 2, "a dead website can't cost a credit every run"


def test_a_redirect_to_another_domain_isnt_its_own_website(rules, srules, monkeypatch):
    from sba_loanscout.crawl import SiteCrawl
    from sba_loanscout.extract import build_page

    def elsewhere(url, *args, **kwargs):
        crawl = SiteCrawl(domain="someoneelse.com", start_url=url)
        crawl.pages = [build_page("https://someoneelse.com/", "<p>Welcome.</p>")]
        return crawl

    monkeypatch.setattr(leads, "crawl_site", elsewhere)
    b = business(srules, email="kim@renewedbh.org", website="renewedbh.org")
    lead = leads.build_lead(b, StubPlaces(Listing(note="no listings found")), object(), rules, srules, 8, TODAY)
    assert not lead.confirmed_by_website


# --- the sent list, Preview's memory, and tidying -------------------------------------------------


def test_the_sent_list_is_left_out_before_any_lookup(workdir, monkeypatch):
    monkeypatch.setattr(cli, "sent_emails", lambda path: {"owner0@gmail.com"})
    places = StubPlaces()
    cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(_records(2)), places=places)
    assert places.lookups == 1
    [left] = _rows(workdir / "out/leads_left_out.csv")
    assert left["email"] == "owner0@gmail.com" and "sent list" in left["left_out_reason"]


def test_the_sent_list_file_is_read_and_a_missing_one_is_said_out_loud(tmp_path, caplog):
    sent = tmp_path / "sent.txt"
    sent.write_bytes("\ufeffJane@Acme.com\tsba-10-20\r\nbob@acme.com\r\n\r\nnot an email\r\n".encode("utf-8"))
    assert cli.sent_emails(str(sent)) == {"jane@acme.com", "bob@acme.com"}
    assert cli.sent_emails("") == set()
    assert cli.sent_emails(str(tmp_path / "gone.txt")) == set()
    assert "sent list not found" in caplog.text


def test_preview_shows_only_what_a_real_run_would_still_do(workdir):
    places = StubPlaces()
    assert cli.run_leads(cli.RunOptions(limit=2, **OPTIONS), sba_client=FakeClient(_records(3)), places=places) == cli.EXIT_OK
    preview = cli.RunOptions(dry_run=True, **OPTIONS)
    assert cli.run_leads(preview, sba_client=FakeClient(_records(3)), places=places) == cli.EXIT_OK
    assert len(_rows(workdir / "out/leads_preview.csv")) == 1
    assert len(_rows(workdir / "out/leads.csv")) == 2, "a preview changes nothing"


def test_stop_still_sorts_the_results(workdir, monkeypatch):
    stop = threading.Event()
    tidied = []
    monkeypatch.setattr(cli, "_tidy", lambda out, *a: tidied.append(out))

    class StopsAfterOne(StubPlaces):
        def find(self, *args, **kwargs):
            stop.set()
            return super().find(*args, **kwargs)

    code = cli.run_leads(cli.RunOptions(**OPTIONS), stop, sba_client=FakeClient(_records(3)), places=StopsAfterOne())
    assert code == cli.EXIT_INTERRUPTED and tidied == ["out/leads.csv"]


def test_results_open_in_excel_at_the_end_of_a_run_lose_nothing(workdir, monkeypatch, caplog):
    def locked(path):
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(cli, "sort_csv_by_score", locked)
    code = cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(_records(2)), places=StubPlaces())
    assert code == cli.EXIT_OK and len(_rows(workdir / "out/leads.csv")) == 2
    assert "open in another program" in caplog.text


def test_results_open_in_excel_before_a_run_spend_nothing(workdir, monkeypatch, caplog):
    def locked(*args, **kwargs):
        raise PermissionError(13, "in use", "out/leads.csv")

    monkeypatch.setattr(cli, "CsvWriter", locked)
    places = StubPlaces()
    code = cli.run_leads(cli.RunOptions(**OPTIONS), sba_client=FakeClient(_records(2)), places=places)
    assert code == cli.EXIT_CONFIG_ERROR and places.lookups == 0
    assert "open in another program" in caplog.text


def test_a_preview_open_in_excel_says_so(workdir, monkeypatch, caplog):
    def locked(*args, **kwargs):
        raise PermissionError(13, "in use", "out/leads_preview.csv")

    monkeypatch.setattr(cli, "CsvWriter", locked)
    code = cli.run_leads(cli.RunOptions(dry_run=True, **OPTIONS), sba_client=FakeClient(_records(2)), places=StubPlaces())
    assert code == cli.EXIT_CONFIG_ERROR and "open in another program" in caplog.text


def test_a_left_out_file_open_in_excel_moves_no_chain_rows(tmp_path, monkeypatch):
    from sba_loanscout.cleanup import LeftOutWriter, leave_out_chains

    results = tmp_path / "leads.csv"
    with results.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["key", "email"])
        writer.writeheader()
        writer.writerows({"key": str(i), "email": f"a{i}@chain.com"} for i in range(3))

    def locked(self):
        raise PermissionError(13, "in use", str(self.path))

    monkeypatch.setattr(LeftOutWriter, "check_writable", locked)
    with pytest.raises(PermissionError):
        leave_out_chains(results, ["key", "email"], 3, set())
    assert len(_rows(results)) == 3 and not (tmp_path / "leads_left_out.csv").exists()


def test_excel_is_told_the_file_is_utf8_exactly_once(tmp_path):
    from sba_loanscout.cleanup import LeftOutWriter
    from sba_loanscout.export import CsvWriter, sort_csv_by_score

    path = tmp_path / "leads.csv"
    row = dict.fromkeys(COLUMNS, "") | {"key": "K1", "email": "a@b.com", "company_name": "José’s Café", "score": "1"}
    for _ in range(2):  # the second time appends to the file
        with CsvWriter(path) as writer:
            writer.write(row)
    assert path.read_bytes().count(b"\xef\xbb\xbf") == 1 and path.read_bytes().startswith(b"\xef\xbb\xbf")
    sort_csv_by_score(path)
    assert path.read_bytes().count(b"\xef\xbb\xbf") == 1
    assert _rows(path)[0]["company_name"] == "José’s Café" and len(_rows(path)) == 2

    left = LeftOutWriter(path, ["email"])
    left.write({"email": "a@b.com"}, "why")
    left.write({"email": "c@d.com"}, "why")
    assert left.path.read_bytes().count(b"\xef\xbb\xbf") == 1
    assert list(_rows(left.path)[0]) == ["left_out_reason", "email"]


def test_a_front_office_inbox_stays_but_the_notes_say_so(rules, srules):
    b = business(srules, email="intake@bodysensept.com")
    row = build_row(leads.build_lead(b, None, None, rules, srules, 8, TODAY), rules)
    assert "inbox may not reach the owner (intake@)" in row["notes"]
    b = business(srules, email="jane@bodysensept.com")
    row = build_row(leads.build_lead(b, None, None, rules, srules, 8, TODAY), rules)
    assert "inbox" not in row["notes"]


def test_a_hospital_systems_address_is_a_national_chain(rules, srules):
    b = business(srules, email="awright@novanthealth.org")
    lead = leads.build_lead(b, None, None, rules, srules, 8, TODAY)
    assert "national_chain" in lead.score.disqualify_reason
