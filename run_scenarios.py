"""Headless end-to-end run of every demo scenario (no web server).
Usage: python run_scenarios.py            -> runs s0..s4 in order against a fresh database
       python run_scenarios.py s1 s4      -> only scenarios whose folder name starts with these"""
import json
import os
import sys
from pathlib import Path

from app import db, pipeline
from scenarios import generate

EXPECTED = {"s0": "Approved", "s1": "Pending", "s2": "Pending", "s3": "Rejected", "s4": "Approved", "s5": "Approved", "s6": "Pending", "s7": "Pending", "s8": "Pending"}
ROOT = Path(__file__).parent / "scenarios"


def load(folder):
    form = json.loads((folder / "form.json").read_text())
    files = {t: (f"{t}.pdf", (folder / f"{t}.pdf").read_bytes()) for t in ("incorporation", "tax", "bank_letter") if (folder / f"{t}.pdf").exists()}
    return form, files


def run_one(folder):
    form, files = load(folder)
    sid, rid = pipeline.create_submission(form, files)
    pipeline.run(rid)
    return rid


def main(argv):
    generate.main()
    db.init()
    db.reset()
    names = sorted(p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith("__"))
    if argv:
        names = [p for p in names if any(p.name.startswith(a) for a in argv)]
    ok = True
    for folder in names:
        rid = run_one(folder)
        run = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
        fails = db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail'", (rid,))
        exp = EXPECTED.get(folder.name[:2])
        mark = "OK " if run["status"] == exp else "BAD"
        ok &= run["status"] == exp
        print(f"[{mark}] {folder.name:30} -> {run['status']:9} risk={run['risk_score']:<3} review={bool(run['review_flag'])!s:5} fired={[f['rule_id'] for f in fails]}")
    print("ALL SCENARIOS PASS" if ok else "SOME SCENARIOS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
