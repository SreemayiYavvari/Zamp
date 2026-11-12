"""End-to-end regression: every demo scenario must produce its expected status and fire the expected rules."""
import json
from pathlib import Path

import pytest

from app import db, pipeline
from scenarios import generate

ROOT = Path(__file__).resolve().parent.parent / "scenarios"


def run_folder(name):
    d = ROOT / name
    form = json.loads((d / "form.json").read_text())
    files = {t: (f"{t}.pdf", (d / f"{t}.pdf").read_bytes()) for t in ("incorporation", "tax", "bank_letter") if (d / f"{t}.pdf").exists()}
    sid, rid = pipeline.create_submission(form, files)
    pipeline.run(rid)
    run = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    fired = {r["rule_id"] for r in db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail'", (rid,))}
    return run, fired, rid


@pytest.fixture(scope="module", autouse=True)
def fresh():
    generate.main()
    db.init()
    db.reset()


def test_scenarios_in_order():
    run, fired, _ = run_folder("s0_happy_path")
    assert run["status"] == "Approved" and not fired and run["risk_score"] == 0

    run, fired, _ = run_folder("s1_bank_mismatch")
    assert run["status"] == "Pending" and fired == {"R11"} and run["review_flag"] == 1

    run, fired, _ = run_folder("s2_wrong_country_tax_id")
    assert run["status"] == "Pending" and "R03" in fired

    run, fired, rid = run_folder("s3_duplicate_bank_account")
    assert run["status"] == "Rejected" and "R14" in fired
    note = db.q("SELECT body FROM communications WHERE run_id=? AND kind='internal_note'", (rid,), one=True)["body"]
    mail = db.q("SELECT body FROM communications WHERE run_id=? AND kind='vendor_email'", (rid,), one=True)["body"]
    assert "FRAUD ESCALATION" in note
    assert "Orion" not in mail and "account" not in mail.lower()  # vendor email must reveal nothing

    run, fired, rid = run_folder("s4_resubmission_fixed")
    assert run["status"] == "Approved" and not fired
    assert db.q("SELECT version FROM submissions WHERE id=(SELECT submission_id FROM runs WHERE id=?)", (rid,), one=True)["version"] == 2


def test_missing_everything_is_pending_not_crash():
    sid, rid = pipeline.create_submission({"legal_name": "Ghost Co", "country": "IN"}, {})
    pipeline.run(rid)
    run = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    fired = {r["rule_id"] for r in db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail'", (rid,))}
    assert run["status"] == "Pending" and {"R01", "R02"} <= fired


def test_corrupt_pdf_does_not_crash():
    form = json.loads((ROOT / "s0_happy_path" / "form.json").read_text())
    form.update(legal_name="Broken Docs Pvt Ltd", tax_id="29AACCB1234F1ZP", account_number="50100999888777")
    junk = ("x.pdf", b"not a pdf")
    sid, rid = pipeline.create_submission(form, {"incorporation": junk, "tax": junk, "bank_letter": junk})
    pipeline.run(rid)
    run = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    assert run["status"] in ("Pending", "Rejected") and run["finished_at"]
    fired = {r["rule_id"] for r in db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail'", (rid,))}
    assert "R19" in fired


def test_idempotent_extraction_cache():
    d = ROOT / "s0_happy_path"
    sid, rid = pipeline.create_submission(json.loads((d / "form.json").read_text()), {"bank_letter": ("b.pdf", (d / "bank_letter.pdf").read_bytes())})
    pipeline.run(rid)
    ev = db.q("SELECT message FROM run_events WHERE run_id=? AND stage_idx=2 AND state!='running'", (rid,), one=True)["message"]
    assert "cached" in ev
