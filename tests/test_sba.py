"""Reading the SBA list: settings, names, the main-industry rule, the state
filter, and the SBA client's saved answers. The SBA is never really asked."""

import json

import pytest

from sba_loanscout.config import ConfigError
from sba_loanscout.sba import (ALL_STATES, SbaClient, find_businesses, load_sba_rules, parse_business,
                               split_contact, tidy_name)


@pytest.fixture(scope="module")
def srules():
    return load_sba_rules()


def rec(**overrides):
    base = {
        "uei": "UEI1", "entity_detail_id": 1, "legal_business_name": "BODYSENSE PT, LLC", "dba_name": "",
        "contact_person": "SHANNON MURPHY", "email": "SMurphy@BodysensePT.com", "display_email": True,
        "phone": "3014320011", "display_phone": True, "website": "www.bodysensept.com",
        "address_1": "6 TIGER WAY", "address_2": "", "city": "BOONSBORO", "state": "Maryland",
        "zipcode": "21713", "county": "Washington County", "year_established": "2012",
        "naics_primary": "621340", "business_type_codes": "2X~LJ", "active_wosb_boolean": True,
    }
    return base | overrides


class FakeClient:
    CODES = ["621340", "621610", "624120", "621111", "238160", "541611"]

    def __init__(self, records):
        self.records = records
        self.asked = []

    def expand(self, starts):
        return [c for c in self.CODES if c.startswith(tuple(starts))]

    def search(self, codes):
        self.asked.append(tuple(codes))
        return [r for r in self.records if r["naics_primary"] in codes or r.get("also") in codes]


# --- settings ---------------------------------------------------------------------------------


def test_the_three_columns_and_their_industries(srules):
    assert [g for g, _ in srules.groups] == ["Healthcare", "Construction", "Other fields"]
    assert all(len(fields) == 8 for _, fields in srules.groups)
    assert srules.field_named("home care").covers("621610")
    assert srules.field_named("Home care").covers("624120")  # non-medical home care too


def test_a_group_name_in_default_fields_ticks_the_whole_group(srules):
    assert srules.default_fields == tuple(f.name for f in dict(srules.groups)["Healthcare"])


def test_an_unknown_industry_is_named_with_the_list(srules):
    with pytest.raises(ConfigError, match="Home care"):
        srules.field_named("Dentistry")


# --- names ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("full, expected", [
    ("SHANNON MURPHY", ("Shannon", "Murphy")),
    ("PAMELA STOKES EGGLESTON", ("Pamela", "Eggleston")),
    ("DR. JANE O'BRIEN MD", ("Jane", "O'Brien")),
    ("ROBERT BRABHAM JR", ("Robert", "Brabham")),
    ("Juliane Schoenherr", ("Juliane", "Schoenherr")),
    ("MARY-ANN SMITH-JONES", ("Mary-Ann", "Smith-Jones")),
    ("MADONNA", ("", "")),
    ("", ("", "")),
])
def test_the_contact_is_split_into_first_and_last(full, expected):
    assert split_contact(full) == expected


def test_shouting_business_names_are_tidied():
    assert tidy_name("BODYSENSE PT, LLC") == "Bodysense PT, LLC"
    assert tidy_name("SPRING LAKE DENTAL GROUP INC") == "Spring Lake Dental Group Inc"
    assert tidy_name("Already Mixed Case, Inc.") == "Already Mixed Case, Inc."


def test_a_record_becomes_a_business(srules):
    b, why = parse_business(rec(), srules.field_named("Therapy (PT, OT, speech)"))
    assert why == ""
    assert (b.contact_first, b.contact_last, b.email) == ("Shannon", "Murphy", "smurphy@bodysensept.com")
    assert (b.state, b.zip, b.website) == ("MD", "21713", "https://www.bodysensept.com")
    assert b.phone == "(301) 432-0011"
    assert b.certifications == ("Women-owned (WOSB)",)
    assert not b.nonprofit and b.key == "UEI1"


def test_a_hidden_email_or_phone_is_not_used(srules):
    fld = srules.field_named("Home care")
    assert parse_business(rec(display_email=False), fld) == (None, "no public email")
    b, _ = parse_business(rec(display_phone=False), fld)
    assert b.phone == ""


def test_the_nonprofit_code_is_read(srules):
    b, _ = parse_business(rec(business_type_codes="A8"), srules.field_named("Home care"))
    assert b.nonprofit


# --- finding businesses --------------------------------------------------------------------


def test_only_the_chosen_state_and_zip(srules):
    client = FakeClient([rec(uei="MD1"), rec(uei="VA1", state="Virginia"), rec(uei="MD2", zipcode="20715")])
    found, _ = find_businesses(client, srules, "MD", ("217",), ["Therapy (PT, OT, speech)"])
    assert [b.key for b in found] == ["MD1"]
    everywhere, _ = find_businesses(client, srules, ALL_STATES, (), ["Therapy (PT, OT, speech)"])
    assert {b.key for b in everywhere} == {"MD1", "VA1", "MD2"}


def test_a_business_counts_only_in_its_main_industry(srules):
    # A consultant registered as small in home care too, but its main code is consulting.
    consultant = rec(uei="C1", naics_primary="541611", also="621610")
    agency = rec(uei="H1", naics_primary="621610")
    found, _ = find_businesses(FakeClient([consultant, agency]), srules, "MD", (), ["Home care"])
    assert [b.key for b in found] == ["H1"]
    assert [b.field for b in found] == ["Home care"]


def test_one_business_once_even_when_two_searches_find_it(srules):
    client = FakeClient([rec(uei="H1", naics_primary="621610", also="621340")])
    found, _ = find_businesses(client, srules, "MD", (), ["Home care", "Therapy (PT, OT, speech)"])
    assert len(found) == 1 and found[0].field == "Home care"


def test_skipped_records_are_counted_with_the_reason(srules):
    client = FakeClient([rec(uei="A", display_email=False), rec(uei="B", contact_person="N/A")])
    found, skipped = find_businesses(client, srules, "MD", (), ["Therapy (PT, OT, speech)"])
    assert found == [] and skipped == {"no public email": 1, "no contact person's full name": 1}


def test_nothing_ticked_is_an_error(srules):
    with pytest.raises(ConfigError):
        find_businesses(FakeClient([]), srules, "MD", (), [])


# --- the client --------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers, self.posts, self.gets = {}, [], []

    def get(self, url, timeout):
        self.gets.append(url)
        return FakeResponse([{"naics_code": "62"}, {"naics_code": "621610"}, {"naics_code": "621340"}])

    def post(self, url, json, timeout):
        self.posts.append(json)
        return FakeResponse({"results": [rec() | {"extra_field": "x" * 50}]})

    def close(self):
        pass


def test_the_client_expands_codes_and_saves_each_days_answers(tmp_path):
    session = FakeSession()
    client = SbaClient(cache_dir=tmp_path, pause=0, session=session)
    assert client.expand(("6216",)) == ["621610"]
    first = client.search(["621610"])
    again = client.search(["621610"])
    assert first == again and len(session.posts) == 1, "the second search the same day is free"
    assert "extra_field" not in first[0], "only the needed fields are kept"
    assert session.posts[0]["naics"]["codes"] == [{"value": "621610", "label": "621610"}]
    assert json.loads((tmp_path / "naics.json").read_text())


def test_a_refusal_is_reported_plainly(tmp_path):
    from sba_loanscout.sba import SbaUnavailable

    session = FakeSession()
    session.post = lambda url, json, timeout: FakeResponse({}, 500)
    with pytest.raises(SbaUnavailable, match="500"):
        SbaClient(cache_dir=None, pause=0, session=session).search(["621610"])
