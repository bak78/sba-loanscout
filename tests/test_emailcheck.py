"""The email checks — DNS stubbed, so nothing leaves the machine."""

import dns.exception
import dns.resolver
import pytest

from sba_loanscout.emailcheck import EmailChecker, EmailCheckUnavailable


class FakeMx:
    def __init__(self, host):
        self.exchange = host


class FakeResolver:
    """Answers MX questions from a table: a list of hosts, or an exception."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def resolve(self, domain, kind):
        self.asked.append(domain)
        answer = self.table.get(domain, dns.resolver.NXDOMAIN())
        if isinstance(answer, Exception):
            raise answer
        return [FakeMx(h) for h in answer]


def checker(**table):
    return EmailChecker(resolver=FakeResolver({k.replace("_", "."): v for k, v in table.items()}))


@pytest.mark.parametrize("email", ["janedoe@gmail.com", "Mike.Spano@Comcast.net", "office@yahoo.com"])
def test_big_providers_pass_without_asking_dns(email):
    c = checker()
    assert c.problem(email) == ""
    assert c._resolver.asked == []


@pytest.mark.parametrize(
    "email,problem",
    [
        ("", "no email"),
        ("jane smith@gmail.com", "not a valid address"),
        ("jane@gmail", "not a valid address"),
        ("jane..doe@gmail.com", "not a valid address"),
        ("none@none.com", "placeholder address"),
        ("na@gmail.com", "placeholder address"),
        ("bob@example.com", "placeholder address"),
        ("noreply@spano.com", "placeholder address"),
        ("bob@gmial.com", "misspelled domain (meant gmail.com?)"),
        ("bob@comcast.com", "misspelled domain (meant comcast.net?)"),
    ],
)
def test_bad_addresses_are_named(email, problem):
    assert checker().problem(email) == problem


def test_a_domain_with_a_mail_server_passes():
    assert checker(spanoandsons_com=["mx1.spanoandsons.com."]).ok("aspano@spanoandsons.com")


def test_a_domain_that_does_not_exist_fails():
    assert checker().problem("bob@bluewaveexp.cpm") == "domain doesn't exist"


def test_a_domain_without_a_mail_server_fails():
    c = checker(parked_com=dns.resolver.NoAnswer())
    assert c.problem("bob@parked.com") == "domain takes no email (no mail server)"


def test_a_null_mx_fails():
    assert not checker(nomail_org=["."]).ok("bob@nomail.org")


def test_unreachable_dns_is_not_a_verdict():
    with pytest.raises(EmailCheckUnavailable):
        checker(slow_com=dns.exception.Timeout()).problem("bob@slow.com")


def test_each_domain_is_asked_once():
    c = checker(spanoandsons_com=["mx1.spanoandsons.com."])
    c.ok("a@spanoandsons.com")
    c.ok("b@spanoandsons.com")
    assert c._resolver.asked == ["spanoandsons.com"]


def test_a_gmail_address_too_short_to_exist_fails():
    from sba_loanscout.emailcheck import EmailChecker
    checker = EmailChecker()
    assert checker.problem("0@gmail.com") == "not a possible Gmail address (too short)"
    assert checker.problem("j.o.e@gmail.com") != ""
    assert checker.problem("joesmith@gmail.com") == ""
    assert checker.problem("j.smith+leads@gmail.com") == ""
