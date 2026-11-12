import asyncio
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import ai, db, pipeline, rules
from scenarios import generate

ROOT = Path(__file__).resolve().parent.parent
SCEN = ROOT / "scenarios"
DELAY = float(os.getenv("DEMO_DELAY", "0.7"))
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

SCENARIOS = [
    ("s0_happy_path", "Happy path", "Name differs only by 'Private Limited' vs 'PVT LTD'", "Approved"),
    ("s1_bank_mismatch", "Bank letter mismatch", "Form account 4821 vs bank letter 4281", "Pending"),
    ("s2_wrong_country_tax_id", "Wrong-country tax ID", "UAE vendor supplies an Indian GSTIN", "Pending"),
    ("s3_duplicate_bank_account", "Duplicate bank account", "New vendor uses an existing vendor's account", "Rejected"),
    ("s4_resubmission_fixed", "Resubmission (fix of #1)", "Run after 'Bank letter mismatch'", "Approved"),
    ("s5_prompt_injection", "Prompt injection", "Malicious document text is ignored as untrusted data", "Approved"),
    ("s6_scanned_bank_letter", "Scanned bank letter", "Image-only PDF needs AI or a clearer text copy", "Pending"),
    ("s7_stale_bank_letter", "Stale bank letter", "Valid evidence issued more than 90 days ago", "Pending"),
    ("s8_fuzzy_duplicate", "Fuzzy duplicate", "Similar legal name triggers manual duplicate review", "Pending"),
]

templates.env.globals["llm_on"] = ai.llm_enabled
app = FastAPI(title="Vendor Onboarding Automation")


@app.on_event("startup")
def startup():
    generate.main()
    db.init()


def start(form, files):
    sid, rid = pipeline.create_submission(form, files)
    threading.Thread(target=pipeline.run, args=(rid, DELAY), daemon=True).start()
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"scenarios": SCENARIOS, "countries": rules.COUNTRIES, "llm": ai.llm_enabled()})


@app.post("/submit")
async def submit(request: Request):
    f = await request.form()
    form = {k: v for k, v in f.items() if isinstance(v, str)}
    files = {}
    for t in rules.REQUIRED_DOCS:
        up = f.get(t)
        if up is not None and hasattr(up, "read") and getattr(up, "filename", ""):
            data = await up.read()
            if data:
                files[t] = (up.filename, data[:15_000_000])
    return start(form, files)


@app.post("/demo/{name}")
def demo(name: str):
    folder = SCEN / name
    if not (folder / "form.json").exists():
        return RedirectResponse("/", status_code=303)
    form = json.loads((folder / "form.json").read_text())
    files = {t: (f"{t}.pdf", (folder / f"{t}.pdf").read_bytes()) for t in rules.REQUIRED_DOCS if (folder / f"{t}.pdf").exists()}
    return start(form, files)


@app.post("/reset")
def reset():
    db.reset()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {"ok": True, "ai": "gemini" if ai.llm_enabled() else "local-fallback"}


@app.get("/runs/{rid}", response_class=HTMLResponse)
def run_view(request: Request, rid: int):
    run = db.q("SELECT r.*, s.form_json, s.version FROM runs r JOIN submissions s ON s.id=r.submission_id WHERE r.id=?", (rid,), one=True)
    if not run:
        return RedirectResponse("/dashboard")
    form = json.loads(run["form_json"])
    return templates.TemplateResponse(request, "run.html", {"run": run, "form": form, "stages": pipeline.STAGES})


@app.get("/runs/{rid}/events")
async def events(rid: int):
    async def gen():
        last = 0
        idle = 0
        while True:
            rows = db.q("SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id", (rid, last))
            for e in rows:
                last = e["id"]
                payload = {"idx": e["stage_idx"], "stage": e["stage"], "state": e["state"], "message": e["message"],
                           "detail": json.loads(e["detail_json"] or "{}")}
                yield f"data: {json.dumps(payload)}\n\n"
            status = db.q("SELECT status FROM runs WHERE id=?", (rid,), one=True)
            if not rows and status and status["status"] != "Running":
                yield f"event: end\ndata: {json.dumps({'status': status['status']})}\n\n"
                return
            idle = 0 if rows else idle + 1
            if idle > 600:
                return
            await asyncio.sleep(0.25)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/results/{rid}", response_class=HTMLResponse)
def results(request: Request, rid: int):
    run = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    if not run:
        return RedirectResponse("/dashboard")
    sub = db.q("SELECT * FROM submissions WHERE id=?", (run["submission_id"],), one=True)
    form = json.loads(sub["form_json"])
    docs = db.q("SELECT * FROM documents WHERE submission_id=?", (sub["id"],))
    for d in docs:
        d["ext"] = json.loads(d["extracted_json"] or "{}")
    rr = db.q("SELECT * FROM rule_results WHERE run_id=? ORDER BY rule_id", (rid,))
    comms = {c["kind"]: c for c in db.q("SELECT * FROM communications WHERE run_id=?", (rid,))}
    reviews = db.q("SELECT * FROM review_decisions WHERE run_id=? ORDER BY id DESC", (rid,))
    audit = db.q("SELECT * FROM audit_log WHERE run_id=? ORDER BY id DESC LIMIT 30", (rid,))
    for item in audit:
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
    critical = db.q("SELECT rule_id, title FROM rule_results WHERE run_id=? AND outcome='fail' AND rule_id IN ('R13','R14','R15')", (rid,))
    comparisons = []
    labels = {"legal_name": "Legal name", "registration_number": "Registration number", "tax_id": "Tax ID", "address": "Registered address", "account_holder": "Account holder", "account_number": "Account number", "bank_name": "Bank name"}
    for field, label in labels.items():
        entries = []
        for doc in docs:
            value = doc["ext"].get("fields", {}).get(field)
            if value:
                confidence = doc["ext"].get("confidence", {}).get(field, 0)
                form_val = form.get(field, "")
                matched = rules.norm_name(value) == rules.norm_name(form_val) if field in ("legal_name", "account_holder", "bank_name") else rules.norm_id(value) == rules.norm_id(form_val)
                entries.append({"doc": doc["doc_type"].replace("_", " ").title(), "value": value, "confidence": confidence, "state": "low" if confidence < .7 else ("match" if matched else "mismatch")})
        if entries:
            comparisons.append({"field": label, "form_value": form.get(field, ""), "entries": entries})
    history = db.q("SELECT s.id, s.version, s.status, s.created_at, (SELECT id FROM runs WHERE submission_id=s.id) rid FROM submissions s WHERE vendor_key=? ORDER BY version", (sub["vendor_key"],))
    diff, cleared = [], []
    prev = db.q("SELECT * FROM submissions WHERE vendor_key=? AND version<? ORDER BY version DESC LIMIT 1", (sub["vendor_key"], sub["version"]), one=True)
    if prev:
        pf = json.loads(prev["form_json"])
        diff = [(k, pf.get(k, ""), form.get(k, "")) for k in sorted(set(pf) | set(form)) if pf.get(k, "") != form.get(k, "")]
        prun = db.q("SELECT id FROM runs WHERE submission_id=?", (prev["id"],), one=True)
        if prun:
            old = {r["rule_id"] for r in db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail'", (prun["id"],))}
            now = {r["rule_id"] for r in rr if r["outcome"] == "fail"}
            cleared = sorted(old - now)
    return templates.TemplateResponse(request, "result.html", {"run": run, "sub": sub, "form": form, "docs": docs, "rules": rr, "comms": comms,
                                                               "history": history, "diff": diff, "cleared": cleared, "prev": prev, "comparisons": comparisons,
                                                               "reviews": reviews, "audit": audit, "critical": critical})


@app.post("/runs/{rid}/review")
async def review(rid: int, request: Request):
    """Human decision. Reasons and reviewer identity are mandatory; fraud blocks cannot be approved."""
    form = await request.form()
    decision, reason, reviewer = (form.get("decision") or "").strip(), (form.get("reason") or "").strip(), (form.get("reviewer") or "").strip()
    if decision not in {"Approved", "Pending", "Rejected"} or not reason or not reviewer:
        return RedirectResponse(f"/results/{rid}?review_error=missing", status_code=303)
    critical = db.q("SELECT rule_id FROM rule_results WHERE run_id=? AND outcome='fail' AND rule_id IN ('R13','R14','R15')", (rid,))
    if decision == "Approved" and critical:
        db.audit(rid, "review_override_blocked", reviewer, {"requested": decision, "protected_rules": [r['rule_id'] for r in critical]})
        return RedirectResponse(f"/results/{rid}?review_error=protected", status_code=303)
    db.x("INSERT INTO review_decisions(run_id,decision,reason,reviewer) VALUES(?,?,?,?)", (rid, decision, reason, reviewer))
    db.audit(rid, "review_decision_recorded", reviewer, {"decision": decision, "reason": reason})
    db.x("UPDATE runs SET status=? WHERE id=?", (decision, rid))
    run = db.q("SELECT submission_id FROM runs WHERE id=?", (rid,), one=True)
    db.x("UPDATE submissions SET status=? WHERE id=?", (decision, run["submission_id"]))
    # Keep the payable-vendor projection aligned with the recorded human decision.
    if decision == "Approved":
        sub = db.q("SELECT vendor_key, form_json FROM submissions WHERE id=?", (run["submission_id"],), one=True)
        vendor = json.loads(sub["form_json"])
        db.x("INSERT OR REPLACE INTO vendors VALUES(?,?,?,?,?,?,?)",
             (f"V-{2000 + run['submission_id']}", sub["vendor_key"], vendor.get("legal_name", ""), vendor.get("country", ""),
              vendor.get("tax_id", ""), vendor.get("account_number", ""), run["submission_id"]))
    else:
        db.x("DELETE FROM vendors WHERE submission_id=?", (run["submission_id"],))
    return RedirectResponse(f"/results/{rid}", status_code=303)


@app.post("/runs/{rid}/email")
async def save_email(rid: int, request: Request):
    form = await request.form()
    subject, body, reviewer = (form.get("subject") or "").strip(), (form.get("body") or "").strip(), (form.get("reviewer") or "").strip()
    if not subject or not body or not reviewer:
        return RedirectResponse(f"/results/{rid}?email_error=missing", status_code=303)
    db.x("UPDATE communications SET subject=?, body=?, updated_at=datetime('now') WHERE run_id=? AND kind='vendor_email'", (subject, body, rid))
    db.audit(rid, "email_draft_updated", reviewer, {"subject": subject})
    return RedirectResponse(f"/results/{rid}", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "", q: str = ""):
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM runs GROUP BY status")}
    total = sum(counts.values())
    avg = db.q("SELECT AVG(finished_at-started_at) a FROM runs WHERE finished_at IS NOT NULL", one=True)["a"]
    reasons = db.q("SELECT rule_id, title, COUNT(*) n FROM rule_results WHERE outcome='fail' GROUP BY rule_id, title ORDER BY n DESC LIMIT 8")
    sql = ("SELECT r.id, r.status, r.risk_score, r.review_flag, s.version, s.created_at, s.form_json FROM runs r JOIN submissions s ON s.id=r.submission_id")
    clauses, args = [], []
    if status: clauses.append("r.status=?"); args.append(status)
    if q: clauses.append("LOWER(s.form_json) LIKE ?"); args.append(f"%{q.lower()}%")
    rows = db.q(sql + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY r.id DESC LIMIT 100", tuple(args))
    for r in rows:
        r["name"] = json.loads(r["form_json"]).get("legal_name", "?")
    automated = db.q("SELECT COUNT(*) n FROM runs WHERE finished_at IS NOT NULL AND review_flag=0", one=True)["n"]
    return templates.TemplateResponse(request, "dashboard.html", {"counts": counts, "total": total, "avg": avg, "reasons": reasons, "rows": rows, "status": status, "query": q, "automated": automated, "time_saved": automated * 12})


@app.get("/audit/export.csv")
def export_audit_csv():
    """Portable audit extract for governance review; contains no document contents or API keys."""
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["audit_id", "run_id", "timestamp_utc", "actor", "action", "details"])
    for row in db.q("SELECT id, run_id, created_at, actor, action, detail_json FROM audit_log ORDER BY id"):
        writer.writerow([row["id"], row["run_id"], row["created_at"], row["actor"], row["action"], row["detail_json"] or "{}"])
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=vendor-onboarding-audit.csv"})


@app.get("/rules", response_class=HTMLResponse)
def rule_settings(request: Request):
    return templates.TemplateResponse(request, "rules.html", {"config": rules.config()})


@app.post("/rules")
async def save_rule_settings(request: Request):
    form = await request.form()
    old = rules.config()
    try:
        new = {key: int(form.get(key, old[key])) for key in ("name_pass_threshold", "name_grey_threshold", "sanctions_block_threshold", "sanctions_warn_threshold", "review_risk_threshold", "stale_bank_letter_days", "fuzzy_duplicate_threshold")}
        if not (0 <= new["name_grey_threshold"] < new["name_pass_threshold"] <= 100 and 0 <= new["sanctions_warn_threshold"] < new["sanctions_block_threshold"] <= 100 and 1 <= new["stale_bank_letter_days"] <= 3650):
            raise ValueError
    except (ValueError, TypeError):
        return RedirectResponse("/rules?error=invalid", status_code=303)
    new["enabled_rules"] = {r: bool(form.get(r)) for r in old["enabled_rules"]}
    (rules.CONFIG_FILE).write_text(json.dumps(new, indent=2) + "\n")
    return RedirectResponse("/rules?saved=1", status_code=303)
