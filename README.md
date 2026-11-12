# AI-powered Vendor Onboarding (ASA Case Study 2)

Vendor submits form + 3 PDFs -> 9-stage pipeline -> **Approved / Pending / Rejected** with reasons, a drafted vendor email and an internal note.
Rules decide, AI reads. Every stage is traced and shown live.

## Reviewer controls and evidence

- **Human-in-the-loop decision:** on the result screen, a reviewer records Approved, Pending, or Rejected with their name and a mandatory reason. The time, actor, decision, and reason are stored in an append-only audit trail.
- **Fraud guardrails:** duplicate tax ID (R13), duplicate bank account (R14), and sanctions match (R15) cannot be overridden to Approved. An attempted override is itself logged.
- **Evidence comparison:** the results page compares the submitted form with each extracted PDF value and labels it match, mismatch, or low confidence (below 70%).
- **Editable email drafts:** reviewers can edit, preview, copy, and save the vendor message. Draft changes are timestamped in the audit trail; no email is sent.
- **Operations dashboard:** KPI cards show decision mix, processing time, and estimated manual-review time avoided (12 minutes per automated decision). Vendor runs can be searched by submitted details.

## Controls and audit exports

Open **Rule settings** to tune permitted thresholds (name matching, sanctions screening, review score, fuzzy duplicate matching, and bank-letter age) or switch optional checks on/off. Fraud hard stops R13, R14, and R15 are protected and cannot be disabled.

Open **Audit CSV** to download the append-only activity trail for all runs. It includes timestamps, actors, actions, and structured details, without document contents or API keys.

## Additional demo scenarios

| # | Scenario | Expected result | What it proves |
|---|---|---|---|
| 5 | Prompt injection | Approved | Document text that asks the system to override checks is treated as untrusted data and recorded in the audit trail. |
| 6 | Scanned bank letter | Pending offline | An image-only PDF creates a low-confidence extraction finding; Gemini can help read it when configured. |
| 7 | Stale bank letter | Pending | Bank evidence more than the configured age (90 days by default) requires a replacement. |
| 8 | Fuzzy duplicate | Pending + review | A near-match to an existing vendor is surfaced for human verification without falsely rejecting it. |

## Run it (3 commands)
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload                           # open http://localhost:8000
```
Click **Run** on any demo scenario. No API key needed (local fallback). For real AI: `cp .env.example .env`, set `GEMINI_API_KEY` (free key: https://aistudio.google.com/apikey).

## Verify without the UI
```bash
python run_scenarios.py     # runs all 5 scenarios, prints status + rules fired
pytest -q                   # 16 unit + end-to-end tests
```

## Scenarios (expected result)
| # | Scenario | Result | Key rule |
|---|---|---|---|
| 0 | Happy path (Private Limited vs PVT LTD) | Approved | none |
| 1 | Bank letter account differs from form | Pending + review flag | R11 |
| 2 | UAE vendor with Indian GSTIN | Pending | R03 |
| 3 | Bank account belongs to existing vendor | Rejected + fraud escalation | R14 |
| 4 | Resubmission of #1 with fix (run after #1) | Approved (v2) | R11 cleared |

## Layout
`app/rules.py` rules + decision | `app/ai.py` Gemini extraction/review/email (+fallbacks) | `app/pipeline.py` 9 stages |
`app/main.py` routes + SSE | `app/templates/` UI | `scenarios/generate.py` test PDFs | `data/` seed vendors, sanctions, banks

## Deploy on Render
Use the included `render.yaml` Blueprint (New > Blueprint). It sets the start command, `/health` check, Python 3.12, and a 1 GB persistent disk at `/var/data` for `DB_PATH`, `UPLOAD_DIR` and `RULE_CONFIG_FILE`. Add `GEMINI_API_KEY` in the Render dashboard (optional; `MOCK_LLM=1` forces the offline demo mode). Keep a single instance because the database is SQLite.

## Assumptions
Countries IN/US/GB/AE/DE. Bank, sanctions and registry checks use local mock data (production: bank-verification and screening APIs).
Email is drafted and stored, not sent. Single reviewer role, no auth. Thresholds (name 92/80, sanctions 90/80, review at risk 30) are starting points to tune on real history.
If the Gemini API fails, extraction degrades to a local PDF-text parser and the event log records it.
