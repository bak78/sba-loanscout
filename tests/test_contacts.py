"""Contact extraction: what a site publishes, and nothing it doesn't."""

import pytest

from sba_loanscout.contacts import PageContacts, find_contacts, is_role_address, summarize
from sba_loanscout.crawl import crawl_site
from sba_loanscout.fetch import FetchResult

# --- one page ---------------------------------------------------------------


def test_mailto_and_tel_links():
    html = """
      <a href="mailto:Office@RidgelineHVAC.com?subject=Quote">Email us</a>
      <a href="tel:+1-215-555-0134">Call</a>
    """
    found = find_contacts(html)
    assert found.emails == ("office@ridgelinehvac.com",)
    assert found.phones == ("(215) 555-0134",)


def test_phone_formats_in_text_are_normalized():
    html = "<p>Call (215) 555-0134, 610.555.0199 or +1 484-555-0100.</p>"
    assert find_contacts(html).phones == (
        "(215) 555-0134",
        "(610) 555-0199",
        "(484) 555-0100",
    )


def test_bare_digit_runs_are_not_phones():
    html = "<p>Order #2155550134 shipped. License 4845550100.</p>"
    assert find_contacts(html).phones == ()


def test_fax_numbers_are_skipped():
    html = "<p>Phone: 215-555-0134 &middot; Fax: 215-555-0135</p>"
    assert find_contacts(html).phones == ("(215) 555-0134",)


def test_obfuscated_email_is_recovered():
    html = "<p>Write to info [at] ridgelinehvac [dot] com</p>"
    assert find_contacts(html).emails == ("info@ridgelinehvac.com",)


def test_email_lookalikes_are_rejected():
    html = """
      <img src="logo@2x.png"><p>logo@2x.png</p>
      <p>errors go to abc123@sentry.wixpress.com</p>
      <p>e.g. you@example.com</p>
    """
    assert find_contacts(html).emails == ()


def test_structured_data_is_read():
    html = """
      <script type="application/ld+json">
        {"@type": "LocalBusiness", "telephone": "+1-215-555-0134",
         "email": "hello@ridgelinehvac.com"}
      </script>
      <p>Welcome!</p>
    """
    found = find_contacts(html)
    assert found.emails == ("hello@ridgelinehvac.com",)
    assert found.phones == ("(215) 555-0134",)


def test_other_scripts_are_not_scanned():
    html = '<script>var support = "dev@cdn-vendor.net"; tel = "215-555-0134";</script>'
    assert find_contacts(html) == PageContacts()


# --- across a site ----------------------------------------------------------


def test_own_domain_email_beats_free_mail_and_web_designer():
    pages = [
        ("https://shop.com/", PageContacts(emails=("site@webdesignco.com",))),
        ("https://shop.com/about", PageContacts(emails=("owner@gmail.com",))),
        ("https://shop.com/news", PageContacts(emails=("info@shop.com",))),
    ]
    best = summarize(pages, "shop.com")

    assert best.email == "info@shop.com"
    assert best.email_page == "https://shop.com/news"
    assert best.other_emails == ("owner@gmail.com", "site@webdesignco.com")


def test_person_beats_role_inbox_on_the_same_domain():
    pages = [
        ("https://shop.com/", PageContacts(emails=("info@shop.com", "sales@shop.com"))),
        ("https://shop.com/about", PageContacts(emails=("maria.lopez@shop.com",))),
    ]
    best = summarize(pages, "shop.com")

    assert best.email == "maria.lopez@shop.com"
    assert best.other_emails == ("info@shop.com", "sales@shop.com"), "kept as backups"


def test_business_role_inbox_beats_a_stranger_with_a_name():
    """Regression guard: the old prototype preferred any non-generic address,
    so a web designer's jane@agency.com beat the business's own info@."""
    pages = [("https://shop.com/", PageContacts(emails=("jane@webagency.com", "info@shop.com")))]
    assert summarize(pages, "shop.com").email == "info@shop.com"


def test_no_reply_addresses_come_last():
    pages = [("https://shop.com/", PageContacts(emails=("noreply@shop.com", "owner@gmail.com")))]
    assert summarize(pages, "shop.com").email == "owner@gmail.com"


@pytest.mark.parametrize(
    "email,role",
    [
        ("info@shop.com", True),
        ("sales-team@shop.com", True),
        ("info.philly@shop.com", True),
        ("maria@shop.com", False),
        ("j.smith@shop.com", False),
        ("information.desk@shop.com", False),  # whole word only
    ],
)
def test_role_address_detection(email, role):
    assert is_role_address(email) is role


def test_phone_repeated_across_pages_is_the_main_line():
    main, direct = "(215) 555-0134", "(215) 555-0177"
    pages = [
        ("https://shop.com/", PageContacts(phones=(direct, main))),
        ("https://shop.com/about", PageContacts(phones=(main,))),
        ("https://shop.com/careers", PageContacts(phones=(main,))),
    ]
    best = summarize(pages, "shop.com")

    assert best.phone == main
    assert best.other_phones == (direct,)


def test_contact_page_is_named_as_the_source_on_ties():
    phone = "(215) 555-0134"
    pages = [
        ("https://shop.com/", PageContacts(phones=(phone,))),
        ("https://shop.com/contact-us", PageContacts(phones=(phone,))),
    ]
    assert summarize(pages, "shop.com").phone_page == "https://shop.com/contact-us"


def test_nothing_found_leaves_blanks():
    best = summarize([("https://shop.com/", PageContacts())], "shop.com")
    assert (best.email, best.phone, best.email_page) == ("", "", "")


# --- the contact page always gets crawled -----------------------------------


def test_contact_page_gets_a_slot_on_a_crowded_site(rules):
    links = "".join(f'<a href="/careers/{i}">j</a>' for i in range(10))
    links += '<a href="/contact">c</a>'
    calls = []

    class StubFetcher:
        def get(self, url):
            calls.append(url)
            return FetchResult(url, 200, f"<html><body>{links}</body></html>")

    crawl_site("https://shop.com/", StubFetcher(), rules, max_pages=4)

    assert len(calls) == 4, "the budget is unchanged"
    assert calls[1] == "https://shop.com/contact", "contact is fetched first"
