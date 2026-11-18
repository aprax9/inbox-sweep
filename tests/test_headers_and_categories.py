import pytest

from inbox_sweep.categories import categorize
from inbox_sweep.headers import (
    decode,
    domain_matches,
    is_bulk,
    is_machine,
    one_click_url,
    parse_address_list,
    parse_from,
    parse_list_unsubscribe,
)
from inbox_sweep.model import CATEGORY_PROMOTIONS, CATEGORY_SOCIAL

from conftest import make


def test_parse_from_decodes_and_lowercases():
    name, email = parse_from("=?utf-8?q?Ren=C3=A9e?= <Renee@Example.ORG>")
    assert name == "Renée"
    assert email == "renee@example.org"


def test_address_list():
    assert parse_address_list("A <a@x.org>, b@y.org", "C <c@z.org>") == ["a@x.org", "b@y.org", "c@z.org"]


def test_decode_tolerates_garbage():
    assert decode("=?bogus?x?abc?=") != ""


def test_list_unsubscribe_and_one_click():
    m = make(lu="<mailto:u@x.org>, <https://x.org/u?id=1>", lup="List-Unsubscribe=One-Click")
    assert parse_list_unsubscribe(m.list_unsubscribe) == ["mailto:u@x.org", "https://x.org/u?id=1"]
    assert one_click_url(m) == "https://x.org/u?id=1"
    assert one_click_url(make(lu="<https://x.org/u>", lup="")) == ""


def test_bulk_and_machine_signals():
    assert is_bulk(make(bulk=True))
    assert is_bulk(make(precedence="bulk"))
    assert not is_bulk(make(auto_submitted="no"))
    assert is_bulk(make(auto_submitted="auto-generated"))
    assert is_machine(make(sender="no-reply@shop.example"))
    assert is_machine(make(labels=[CATEGORY_PROMOTIONS]))
    assert not is_machine(make(sender="jane.doe@gmail.com"))


def test_domain_matches_subdomains():
    assert domain_matches("a@mail.uni.edu", ["uni.edu"])
    assert not domain_matches("a@notuni.edu", ["uni.edu"])


@pytest.mark.parametrize("subject,expected", [
    ("Your 2021 T4 is ready", "documents"),
    ("Notice of Assessment available", "documents"),
    ("e-Visa approved", "documents"),
    ("Offer letter - signed", "documents"),
    ("Interac e-Transfer: Sam sent you $40", "person_payment"),
    ("Payment reminder: card due", "reminder"),
    ("Your verification code is 123456", "security"),
    ("Interview confirmation: PM role", "job_process"),
    ("Thank you for applying to Initech", "job_automated"),
    ("Your e-ticket receipt and itinerary", "booking"),
    ("Your order #12 has shipped", "receipt"),
    ("Your e-statement is ready", "account_notice"),
])
def test_categories(subject, expected):
    assert categorize(make(sender="alerts@service.example", subject=subject)) == expected


def test_promotions_never_count_as_documents():
    m = make(sender="deals@bank.example", subject="Open an RRSP today - 50% off fees", labels=[CATEGORY_PROMOTIONS], bulk=True)
    assert categorize(m) == "promotion"


def test_social_newsletter_and_human():
    assert categorize(make(sender="notification@facebookmail.com", subject="New comment", labels=[CATEGORY_SOCIAL])) == "social"
    assert categorize(make(sender="digest@news.example", subject="Weekly roundup", bulk=True, list_id="<n.example>")) == "newsletter"
    assert categorize(make(sender="pat@vendor.example", subject="Lunch next week?")) == "human_unknown"
