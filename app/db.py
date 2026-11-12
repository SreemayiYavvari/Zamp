import json
import os
import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "vendor.db"))
UPLOADS = Path(os.getenv("UPLOAD_DIR", ROOT / "uploads"))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_key TEXT, version INTEGER, form_json TEXT,
  status TEXT DEFAULT 'Processing', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, doc_type TEXT, filename TEXT,
  sha256 TEXT, path TEXT, extracted_json TEXT);
CREATE TABLE IF NOT EXISTS extractions(sha256 TEXT PRIMARY KEY, doc_type TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, submission_id INTEGER, status TEXT DEFAULT 'Running',
  risk_score INTEGER DEFAULT 0, review_flag INTEGER DEFAULT 0, started_at REAL, finished_at REAL, result_json TEXT);
CREATE TABLE IF NOT EXISTS run_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, stage_idx INTEGER, stage TEXT, state TEXT,
  message TEXT, detail_json TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS rule_results(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, rule_id TEXT, title TEXT, outcome TEXT, severity TEXT,
  points INTEGER, message TEXT, form_value TEXT, evidence_value TEXT);
CREATE TABLE IF NOT EXISTS communications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, kind TEXT, subject TEXT, body TEXT,
  updated_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS review_decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, decision TEXT, reason TEXT NOT NULL,
  reviewer TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, action TEXT NOT NULL, actor TEXT NOT NULL,
  detail_json TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS vendors(
  id TEXT PRIMARY KEY, vendor_key TEXT, legal_name TEXT, country TEXT, tax_id TEXT, account_number TEXT,
  submission_id INTEGER);
"""


def connect():
    c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)
        # Safe, idempotent migrations for databases created by earlier builds.
        columns = {r[1] for r in c.execute("PRAGMA table_info(communications)")}
        if "updated_at" not in columns:
            c.execute("ALTER TABLE communications ADD COLUMN updated_at TEXT")


def q(sql, args=(), one=False):
    with _lock, connect() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock, connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def audit(run_id, action, actor="system", detail=None):
    """Append-only activity trail used by pipeline, reviewer and draft actions."""
    return x("INSERT INTO audit_log(run_id,action,actor,detail_json) VALUES(?,?,?,?)",
             (run_id, action, actor, json.dumps(detail or {})))


def reset():
    with _lock, connect() as c:
        for t in ["submissions", "documents", "runs", "run_events", "rule_results", "communications", "review_decisions", "audit_log", "vendors"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM sqlite_sequence")
