"""Nine-stage onboarding pipeline. Every stage writes an event so the live view, audit trail and dashboard share one source."""
import hashlib
import json
import time

from . import ai, db, rules

STAGES = ["Intake", "Completeness check", "Document extraction", "Normalisation", "Rule engine",
          "Risk checks", "AI grey-zone review", "Decision", "Communication & audit"]


def create_submission(form, files):
    """files: {doc_type: (filename, bytes)}. Returns (submission_id, run_id)."""
    key = rules.vendor_key(form.get("legal_name"), form.get("country"))
    version = (db.q("SELECT COUNT(*) n FROM submissions WHERE vendor_key=?", (key,), one=True)["n"]) + 1
    sid = db.x("INSERT INTO submissions(vendor_key,version,form_json) VALUES(?,?,?)", (key, version, json.dumps(form)))
    folder = db.UPLOADS / str(sid)
    folder.mkdir(parents=True, exist_ok=True)
    for dtype, (fname, data) in files.items():
        safe = "".join(c for c in fname if c.isalnum() or c in "._-") or f"{dtype}.pdf"
        p = folder / f"{dtype}_{safe}"
        p.write_bytes(data)
        db.x("INSERT INTO documents(submission_id,doc_type,filename,sha256,path) VALUES(?,?,?,?,?)",
             (sid, dtype, fname, hashlib.sha256(data).hexdigest(), str(p)))
    rid = db.x("INSERT INTO runs(submission_id,started_at) VALUES(?,?)", (sid, time.time()))
    db.audit(rid, "submission_created", detail={"submission_id": sid, "version": version, "documents": sorted(files)})
    return sid, rid


def emit(rid, idx, state, message="", detail=None):
    db.x("INSERT INTO run_events(run_id,stage_idx,stage,state,message,detail_json,ts) VALUES(?,?,?,?,?,?,?)",
         (rid, idx, STAGES[idx], state, message, json.dumps(detail or {}), time.time()))
    if state in ("done", "warn", "failed"):
        db.audit(rid, "pipeline_stage_completed", detail={"stage": STAGES[idx], "state": state, "message": message})


def _state(results):
    fails = [r for r in results if r["outcome"] == "fail"]
    if any(r["severity"] == "BLOCK" for r in fails):
        return "failed"
    return "warn" if fails else "done"


def _summ(results):
    f = [r for r in results if r["outcome"] == "fail"]
    return (f"{len(f)} issue(s): " + ", ".join(r["rule"] for r in f)) if f else "All checks passed"


def run(rid, delay=0.0):
    """Run the whole pipeline for run `rid`. Never raises: a crash becomes a Pending/manual-review outcome."""
    try:
        _run(rid, delay)
    except Exception as e:  # last-resort safety net
        emit(rid, 7, "failed", f"Unexpected error: {e}")
        db.x("UPDATE runs SET status='Pending', review_flag=1, finished_at=?, result_json=? WHERE id=?",
             (time.time(), json.dumps({"status": "Pending", "error": str(e)}), rid))
        sub = db.q("SELECT submission_id FROM runs WHERE id=?", (rid,), one=True)
        db.x("UPDATE submissions SET status='Pending' WHERE id=?", (sub["submission_id"],))
        emit(rid, 8, "done", "Run ended with an error; flagged for manual review")


def _pause(d):
    if d:
        time.sleep(d)


def _run(rid, delay):
    run_row = db.q("SELECT * FROM runs WHERE id=?", (rid,), one=True)
    sid = run_row["submission_id"]
    sub = db.q("SELECT * FROM submissions WHERE id=?", (sid,), one=True)
    form = json.loads(sub["form_json"])
    docs = db.q("SELECT * FROM documents WHERE submission_id=?", (sid,))
    all_results = []

    # 0 Intake
    emit(rid, 0, "running"); _pause(delay)
    form = {k: (v.strip() if isinstance(v, str) else v) for k, v in form.items()}
    form["country"] = (form.get("country") or "").upper()
    form["bank_country"] = (form.get("bank_country") or form["country"]).upper()
    emit(rid, 0, "done", f"Submission #{sid} (version {sub['version']}) received with {len(docs)} document(s)",
         {"version": sub["version"], "documents": [{"type": d["doc_type"], "file": d["filename"], "sha256": d["sha256"][:12]} for d in docs]})

    # 1 Completeness
    emit(rid, 1, "running"); _pause(delay)
    res = rules.completeness(form, {d["doc_type"] for d in docs})
    all_results += res
    emit(rid, 1, _state(res), _summ(res), {"rules": res})

    # 2 Extraction
    emit(rid, 2, "running"); _pause(delay)
    ext, notes = {}, []
    for d in docs:
        try:
            r, cached = ai.extract(d["path"], d["doc_type"], d["sha256"])
        except Exception as e:
            r, cached = {"fields": {}, "confidence": {}, "source": "failed", "error": str(e)[:150]}, False
        ext[d["doc_type"]] = r
        db.x("UPDATE documents SET extracted_json=? WHERE id=?", (json.dumps(r), d["id"]))
        notes.append(f"{d['doc_type']}: {r['source']}{' (cached)' if cached else ''}, {len(r['fields'])} fields")
        if r.get("security_note"):
            notes.append(f"{d['doc_type']}: security safeguard applied")
            db.audit(rid, "prompt_injection_ignored", detail={"document": d["doc_type"], "note": r["security_note"]})
    res = rules.extraction_quality(ext)
    all_results += res
    st = _state(res)
    if any("error" in r for r in ext.values()):
        st = "warn"
        notes.append("AI unavailable for some documents; used local parser")
    emit(rid, 2, st, "; ".join(notes) or "No documents to read", {"extractions": ext, "rules": res})

    # 3 Normalisation
    emit(rid, 3, "running"); _pause(delay)
    norm = {"legal_name": rules.norm_name(form.get("legal_name")), "tax_id": rules.norm_id(form.get("tax_id")),
            "account_number": rules.norm_id(form.get("account_number"))}
    emit(rid, 3, "done", f"Name '{form.get('legal_name')}' -> '{norm['legal_name']}'; IDs stripped of spaces and hyphens", norm)

    # 4 Rule engine
    emit(rid, 4, "running"); _pause(delay)
    field_res = rules.field_rules(form, ext)
    all_results += field_res
    grey = [r for r in field_res if r["outcome"] == "grey"]
    st = _state(field_res)
    emit(rid, 4, "warn" if (st == "done" and grey) else st, _summ(field_res) + (f"; {len(grey)} name match(es) sent to AI review" if grey else ""),
         {"rules": field_res})

    # 5 Risk checks
    emit(rid, 5, "running"); _pause(delay)
    key = sub["vendor_key"]
    existing = [dict(v, key=rules.vendor_key(v["legal_name"], v["country"])) for v in rules.load_json("existing_vendors.json")]
    existing += db.q("SELECT id, vendor_key AS key, legal_name, tax_id, account_number FROM vendors")
    risk_res = rules.risk_rules(form, key, existing)
    all_results += risk_res
    emit(rid, 5, _state(risk_res), _summ(risk_res), {"rules": risk_res})

    # 6 AI grey-zone review
    emit(rid, 6, "running"); _pause(delay)
    if grey:
        msgs = []
        for g in grey:
            v = ai.review_names(g["grey"]["a"], g["grey"]["b"], g["title"])
            idx = next(i for i, r in enumerate(all_results) if r is g)
            all_results[idx] = rules.resolve_grey(g, v)
            msgs.append(f"{g['rule']}: {v['verdict']} ({v['reason']})")
        emit(rid, 6, "done", "; ".join(msgs), {"reviewed": len(grey)})
    else:
        emit(rid, 6, "done", "No ambiguous cases; AI review not needed (rules decided)")

    # 7 Decision
    emit(rid, 7, "running"); _pause(delay)
    d = rules.decide(all_results)
    fails = [r for r in all_results if r["outcome"] == "fail"]
    emit(rid, 7, {"Approved": "done", "Pending": "warn", "Rejected": "failed"}[d["status"]],
         f"{d['status']} (risk {d['risk_score']}{', manual review flagged' if d['review_flag'] else ''})", d)

    # 8 Communication and audit
    emit(rid, 8, "running"); _pause(delay)
    vendor_items = [r["vendor_message"] for r in fails if r["vendor_message"]]
    subject, body, src = ai.draft_email(d["status"], form.get("legal_name", ""), form.get("contact_name"), vendor_items)
    db.x("INSERT INTO communications(run_id,kind,subject,body) VALUES(?,?,?,?)", (rid, "vendor_email", subject, body))
    internal = [f"Decision: {d['status']} | risk {d['risk_score']} | manual review: {'YES' if d['review_flag'] else 'no'}"]
    internal += [f"[{r['rule']}/{r['severity']}] {r['message']}" for r in fails]
    if any(r["rule"] == "R14" for r in fails):
        internal.insert(0, "FRAUD ESCALATION: route to Finance Risk before any contact with the vendor.")
    db.x("INSERT INTO communications(run_id,kind,subject,body) VALUES(?,?,?,?)", (rid, "internal_note", "Internal note", "\n".join(internal)))
    for r in all_results:
        if r["outcome"] != "skip":
            db.x("INSERT INTO rule_results(run_id,rule_id,title,outcome,severity,points,message,form_value,evidence_value) VALUES(?,?,?,?,?,?,?,?,?)",
                 (rid, r["rule"], r["title"], r["outcome"], r["severity"], r["points"], r["message"], r["form_value"], r["evidence_value"]))
    if d["status"] == "Approved":
        vid = f"V-{2000 + sid}"
        db.x("INSERT OR REPLACE INTO vendors VALUES(?,?,?,?,?,?,?)",
             (vid, key, form["legal_name"], form["country"], form["tax_id"], form["account_number"], sid))
    db.x("UPDATE submissions SET status=? WHERE id=?", (d["status"], sid))
    emit(rid, 8, "done", f"Vendor email drafted ({src}); internal note and audit trail saved")
    db.x("UPDATE runs SET status=?, risk_score=?, review_flag=?, finished_at=?, result_json=? WHERE id=?",
         (d["status"], d["risk_score"], int(d["review_flag"]), time.time(), json.dumps(d), rid))
    db.audit(rid, "automated_decision", detail={"status": d["status"], "risk_score": d["risk_score"], "review_flag": d["review_flag"]})
