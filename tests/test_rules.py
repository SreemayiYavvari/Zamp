import datetime as dt

import pytest

from app import rules
from scenarios.generate import gstin


def test_name_normalisation_treats_suffix_variants_as_equal():
    assert rules.norm_name("Acme Solutions Private Limited") == rules.norm_name("ACME SOLUTIONS PVT. LTD.")
    assert rules.name_score("Acme Solutions Private Limited", "ACME SOLUTIONS PVT LTD") == 100


@pytest.mark.parametrize("a,b,expected", [
    ("Acme Solutions", "Acme Solutions", "pass"),
    ("Acme Solutions Pvt Ltd", "Totally Different Traders", "fail"),
])
def test_name_rule_pass_and_fail(a, b, expected):
    assert rules.name_rule("R07", "t", a, b, 0, "v")["outcome"] == expected


def test_name_rule_grey_zone():
    r = rules.name_rule("R07", "t", "Acme Solutions", "Acme Solution Traders", 0, "v")
    assert r["outcome"] in ("grey", "pass", "fail")
    g = rules.name_rule("R07", "t", "Bharat Textile Mills", "Bharat Textiles Mill Co", 0, "v")
    assert g["outcome"] in ("grey", "pass")


def test_gstin_valid_and_bad_checksum():
    good = gstin("27", "AABCB5678K")
    assert rules.check_tax_id("IN", good)[:2] == (True, True)
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    fmt, chk, _ = rules.check_tax_id("IN", bad)
    assert fmt and not chk


def test_gstin_rejected_for_uae():
    assert rules.check_tax_id("AE", gstin("07", "AAHCG9012P"))[0] is False
    assert rules.check_tax_id("AE", "100123456789012")[0] is True


def test_us_and_eu_ids():
    assert rules.check_tax_id("US", "12-3456789")[0]
    assert not rules.check_tax_id("US", "12-345")[0]
    assert rules.check_tax_id("DE", "DE136695976")[0]


def test_bank_checks():
    banks = rules.load_json("banks.json")
    assert rules.check_bank("IN", "50100234567890", "HDFC0001234", "HDFC Bank", banks)[0]
    assert not rules.check_bank("IN", "50100234567890", "HDFC1001234", "HDFC Bank", banks)[0]
    assert not rules.check_bank("IN", "50100234567890", "HDFC0001234", "Axis Bank", banks)[0]
    assert rules.check_bank("DE", "DE89370400440532013000", "", "", banks)[0]
    assert not rules.check_bank("DE", "DE89370400440532013001", "", "", banks)[0]
    assert rules.aba_valid("021000021") and not rules.aba_valid("021000022")


def test_decision_logic():
    fail = lambda sev, pts=0, flag=False: {"outcome": "fail", "severity": sev, "points": pts, "flag": flag}
    assert rules.decide([])["status"] == "Approved"
    assert rules.decide([fail("WARN", 10)])["status"] == "Approved"
    assert rules.decide([fail("WARN", 30)])["status"] == "Pending"
    assert rules.decide([fail("FIX")])["status"] == "Pending"
    d = rules.decide([fail("FIX"), fail("BLOCK")])
    assert d["status"] == "Rejected"
    assert rules.decide([fail("FIX", 40, True)])["review_flag"] is True


def test_stale_bank_letter_flagged():
    form = {"country": "IN", "bank_country": "IN", "tax_id": gstin("27", "AABCB5678K"), "pan": "AABCB5678K", "legal_name": "X Ltd",
            "account_holder": "X Ltd", "account_number": "50100234567890", "bank_code": "HDFC0001234", "bank_name": "HDFC Bank"}
    old = str(dt.date.today() - dt.timedelta(days=200))
    ext = {"bank_letter": {"fields": {"issue_date": old, "account_number": "50100234567890", "account_holder": "X Ltd"}, "confidence": {}}}
    res = {r["rule"]: r for r in rules.field_rules(form, ext)}
    assert res["R12"]["outcome"] == "fail"


def test_free_mail_and_po_box_warn():
    form = {"legal_name": "Foo", "country": "IN", "tax_id": "x", "account_number": "1", "contact_email": "foo@gmail.com",
            "website": "foo.in", "address": "PO Box 12, Delhi"}
    res = {r["rule"]: r for r in rules.risk_rules(form, "foo|IN", [])}
    assert res["R17"]["outcome"] == "fail" and res["R18"]["outcome"] == "fail"


def test_sanctions_block_and_near_match():
    base = {"country": "IN", "tax_id": "x", "account_number": "1", "contact_email": "a@b.in", "website": "b.in", "address": "x India"}
    hit = {r["rule"]: r for r in rules.risk_rules(dict(base, legal_name="Red Dragon Trading Ltd"), "k", [])}
    assert hit["R15"]["outcome"] == "fail" and hit["R15"]["severity"] == "BLOCK"
    clean = {r["rule"]: r for r in rules.risk_rules(dict(base, legal_name="Sunrise Bakery Pvt Ltd"), "k", [])}
    assert clean["R15"]["outcome"] == "pass"
