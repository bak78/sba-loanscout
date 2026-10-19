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
    return tmp_path


def _rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
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
