"""Google listing lookup: only a provably matching listing is used."""

import pytest

from sba_loanscout.places import (
    PlacesClient,
    SerperUnavailable,
    _street_numbers,
    match_listing,
)


def place(**overrides):
    base = {
        "title": "Rustica Pizza",
        "address": "903 N 2nd St, Philadelphia, PA 19123",
        "phoneNumber": "(215) 555-0134",
        "website": "https://rusticapizza.com/",
    }
    return base | overrides


def test_match_on_street_address():
    found = match_listing([place()], "Rustica Pizza", "903 N 2ND ST", "19123")
    assert found.matched_on == "address"
    assert found.phone == "(215) 555-0134"
    assert found.website == "https://rusticapizza.com/"


def test_wrong_street_number_is_not_a_match():
    """Same street, different building: a neighbour, not this business."""
    other = place(title="Other Pizza", address="911 N 2nd St, Philadelphia, PA 19123")
    assert match_listing([other], "Rustica Pizza", "903 N 2ND ST", "19123").matched_on == ""


def test_same_number_on_a_different_street_is_not_a_match():
    other = place(title="Other Shop", address="903 S 4th St, Philadelphia, PA 19147")
    assert match_listing([other], "Rustica Pizza", "903 N 2ND ST", "19123").matched_on == ""


def test_address_ranges_cover_every_number():
    assert _street_numbers("1528-30 SANSOM ST") == {"1528", "1529", "1530"}
    assert "318" in _street_numbers("300-18 S BROAD ST")
    found = match_listing(
        [place(title="Ladder 15", address="1530 Sansom St, Philadelphia, PA 19102")],
        "Ladder 15", "1528-30 SANSOM ST", "19102",
    )
    assert found.matched_on == "address"


def test_name_and_zip_match_when_google_lists_another_door():
    corner = place(address="100 W Girard Ave, Philadelphia, PA 19123")
    found = match_listing([corner], "Rustica Pizza", "903 N 2ND ST", "19123")
    assert found.matched_on == "name + zip"


def test_same_name_in_another_zip_is_not_a_match():
    """Different branch of a same-named business across town."""
    elsewhere = place(address="12 Main St, Philadelphia, PA 19128")
    assert match_listing([elsewhere], "Rustica Pizza", "903 N 2ND ST", "19123").matched_on == ""


def test_address_match_beats_an_earlier_name_match():
    name_only = place(address="100 W Girard Ave, Philadelphia, PA 19123", phoneNumber="(215) 555-9999")
    exact = place(phoneNumber="(215) 555-0134")
    found = match_listing([name_only, exact], "Rustica Pizza", "903 N 2ND ST", "19123")
    assert found.phone == "(215) 555-0134"


def test_no_results_leaves_everything_blank():
    found = match_listing([], "Rustica Pizza", "903 N 2ND ST", "19123")
    assert (found.phone, found.website, found.matched_on) == ("", "", "")
    assert found.note == "no listings found"


# --- the Serper client -----------------------------------------------------------


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _client(status, payload=None):
    client = PlacesClient("test-key")
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        return _Response(status, payload)

    client._session.post = post
    return client, sent


def test_lookup_sends_name_and_address_and_counts_credits():
    client, sent = _client(200, {"places": [place()]})
    found = client.find("Rustica Pizza", "903 N 2ND ST", "Philadelphia", "PA", "19123")

    assert found.matched_on == "address"
    assert sent[0]["q"] == "Rustica Pizza, 903 N 2ND ST, Philadelphia, PA 19123"
    assert client.lookups == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_bad_key_or_no_credits_stops_the_run(status):
    client, _ = _client(status)
    with pytest.raises(SerperUnavailable):
        client.find("Rustica Pizza", "903 N 2ND ST", "Philadelphia", "PA", "19123")


def test_after_a_refusal_no_more_requests_are_sent():
    client, sent = _client(429)
    for _ in range(3):
        with pytest.raises(SerperUnavailable):
            client.find("Rustica Pizza", "903 N 2ND ST", "Philadelphia", "PA", "19123")
    assert len(sent) == 1


def test_server_hiccup_is_retryable_not_fatal():
    client, _ = _client(502)
    with pytest.raises(ConnectionError):
        client.find("Rustica Pizza", "903 N 2ND ST", "Philadelphia", "PA", "19123")


def test_unit_numbers_do_not_block_an_address_match():
    """NPI addresses carry suites that Google listings usually leave out."""
    listing = place(title="Rustica", address="1234 Market St, Philadelphia, PA 19107")
    for address in ("1234 MARKET ST STE 200", "1234 Market Street, Suite 5", "1234 MARKET ST # 3", "1234 MARKET ST FL 2"):
        assert match_listing([listing], "Rustica", address, "19107").matched_on == "address", address


def test_lookup_uses_the_borrowers_own_city():
    client, sent = _client(200, {"places": []})
    client.find("Rustica Pizza", "12 MAIN ST", "Pittsburgh", "PA", "15222")
    assert sent[0]["q"] == "Rustica Pizza, 12 MAIN ST, Pittsburgh, PA 15222"
