"""Churches and law firms are never leads; look-alikes are kept."""

import pytest

from sba_loanscout.churchlaw import church_or_law
from sba_loanscout.config import load_rules
from sba_loanscout.extract import build_page
from sba_loanscout.signals import match_site


@pytest.mark.parametrize("name, expected", [
    ("NEW HOPE MINISTRIES IHS LLC", "church"),
    ("GRACE BAPTIST CHURCH", "church"),
    ("Grace Baptist Church, Inc.", "church"),
    ("CHURCH OF GOD IN CHRIST", "church"),
    ("ST. JOHN'S PARISH", "church"),
    ("SMITH LAW FIRM, PLLC", "law_firm"),
    ("Law Offices Of Jacquelyn V. Meiser, P.C.", "law_firm"),
    ("JONES & BROWN ATTORNEYS AT LAW", "law_firm"),
    ("KAREN DOE, ESQ.", "law_firm"),
    ("MILLER LAW LLC", "law_firm"),
    ("Miller Law", "law_firm"),
])
def test_churches_and_law_firms_by_name(name, expected):
    assert church_or_law([name])[0] == expected


@pytest.mark.parametrize("name", [
    "TEMPLE GRADING AND CONSTRUCTION COMPANY, INC.", "DELAWARE METALS INC", "M-F ATHLETIC CO INC",
    "LAWRENCE LAWN CARE LLC", "CHURCHILL HOME CARE LLC", "R.A. DAFFER CHURCH ORGANS INC",
    "CATHEDRAL ART METAL CO., INC.", "CHURCH CHILDCARE CENTER, INC.", "CHURCH BROTHERS ROOFING LLC",
    "Church Interiors Audio And Video, Inc.", "Outlaw Excavating", "Lawson Paving",
])
def test_look_alike_names_are_kept(name):
    assert church_or_law([name]) == ("", "")


def test_a_law_domain_is_a_law_firm_but_delaware_is_not():
    assert church_or_law([], ["millerlaw.com"])[0] == "law_firm"
    assert church_or_law([], ["smithlawfirm.net"])[0] == "law_firm"
    assert church_or_law([], ["delaware.com", "dentaldelaware.com", "gmail.com", ""]) == ("", "")


def _site_ids(text):
    page = build_page("https://example.com/about", f"<html><body><p>{text}</p></body></html>")
    return {h.signal_id for h in match_site([page], load_rules())}


def test_a_churchs_or_law_firms_website_disqualifies():
    for text, signal in [("Join us for worship every Sunday at 10am.", "church"),
                         ("Welcome to our church family!", "church"),
                         ("Meet our senior pastor, Rev. Dan Lee.", "church"),
                         ("Watch past sermons online.", "church"),
                         ("Smith & Jones, attorneys at law.", "law_firm"),
                         ("Our law firm has served Baltimore for 30 years.", "law_firm"),
                         ("Talk to our personal injury lawyers today.", "law_firm")]:
        assert signal in _site_ids(text), text


def test_care_homes_and_contractors_that_mention_church_or_lawyers_are_kept():
    for text in ["Residents enjoy weekly worship services and Sunday service in our chapel.",
                 "We drive clients to church, the pharmacy and appointments.",
                 "Please consult your attorney about long-term care planning.",
                 "We built the new parish hall and church parking lot.",
                 "Lawn care and snow removal for commercial properties."]:
        assert not _site_ids(text) & {"church", "law_firm"}, text
