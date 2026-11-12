"""Deterministic rule engine. Rules decide; AI only reads documents and judges the grey zone."""
import datetime as dt
import json
import os
import re
from pathlib import Path

from rapidfuzz import fuzz
from stdnum import iban as _iban
from stdnum.de import vat as _de_vat
from stdnum.gb import vat as _gb_vat
from stdnum.in_ import gstin as _gstin

DATA = Path(__file__).resolve().parent.parent / "data"
CONFIG_FILE = Path(os.getenv("RULE_CONFIG_FILE", DATA / "rule_config.json"))

COUNTRIES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "AE": "United Arab Emirates", "DE": "Germany"}
COUNTRY_WORDS = {
    "IN": ["india"], "US": ["united states", "usa"], "GB": ["united kingdom", "england"],
    "AE": ["uae", "united arab emirates", "dubai", "abu dhabi"], "DE": ["germany", "deutschland"],
}
IBAN_COUNTRIES = {"GB", "DE", "AE"}
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me", "aol.com"}
LEGAL_SUFFIXES = {"pvt", "private", "ltd", "limited", "llc", "inc", "incorporated", "gmbh", "co", "corp",
                  "corporation", "llp", "fzco", "fze", "plc", "company", "ug", "ag"}

REQUIRED_FIELDS = ["legal_name", "country", "address", "registration_number", "tax_id", "contact_name",
                   "contact_email", "website", "bank_name", "account_holder", "account_number", "bank_code",
                   "bank_country"]
REQUIRED_DOCS = {"incorporation": "Certificate of incorporation", "tax": "Tax registration certificate",
                 "bank_letter": "Bank letter or cancelled cheque"}
KEY_DOC_FIELDS = {"incorporation": ["legal_name"], "tax": ["tax_id"], "bank_letter": ["account_number", "account_holder"]}

DEFAULT_CONFIG = {"name_pass_threshold": 92, "name_grey_threshold": 80, "sanctions_block_threshold": 90,
                  "sanctions_warn_threshold": 80, "review_risk_threshold": 30, "stale_bank_letter_days": 90,
                  "fuzzy_duplicate_threshold": 86, "enabled_rules": {"R12": True, "R13b": True, "R17": True, "R18": True}}


def load_json(name):
    return json.loads((DATA / name).read_text())


def config():
    """Read validated, operator-editable thresholds. Critical hard-stop rules are never configurable."""
    try:
        saved = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        saved = {}
    out = dict(DEFAULT_CONFIG)
    out.update({k: saved[k] for k in DEFAULT_CONFIG if k in saved and k != "enabled_rules"})
    out["enabled_rules"] = dict(DEFAULT_CONFIG["enabled_rules"], **saved.get("enabled_rules", {}))
    return out


def rule_enabled(rule_id):
    return bool(config()["enabled_rules"].get(rule_id, True))


# ---------- normalisation ----------
def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in LEGAL_SUFFIXES)


def norm_id(s):
    return re.sub(r"[\s\-]", "", (s or "")).upper()


def name_score(a, b):
    return fuzz.token_sort_ratio(norm_name(a), norm_name(b))


def vendor_key(name, country):
    return f"{norm_name(name)}|{(country or '').upper()}"


def domain_of(s):
    s = (s or "").lower().strip()
    s = s.split("@")[-1]
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return re.sub(r"^www\.", "", s)


# ---------- country specific validators ----------
def check_tax_id(country, tax_id):
    """Returns (format_ok, checksum_ok, expected_description)."""
    t = norm_id(tax_id)
    if country == "IN":
        fmt = bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", t))
        return fmt, (fmt and _gstin.is_valid(t)), "a 15-character GSTIN such as 27AABCA1234F1Z5"
    if country == "US":
        fmt = bool(re.fullmatch(r"\d{9}", t))
        return fmt, True, "a 9-digit EIN formatted NN-NNNNNNN"
    if country == "GB":
        ok = _gb_vat.is_valid(t.replace("GB", "", 1)) if t.startswith("GB") else _gb_vat.is_valid(t)
        return ok, True, "a UK VAT number such as GB123456789"
    if country == "DE":
        ok = _de_vat.is_valid(t)
        return ok, True, "a German VAT ID (DE plus 9 digits)"
    if country == "AE":
        fmt = bool(re.fullmatch(r"\d{15}", t))
        return fmt, True, "a 15-digit UAE Tax Registration Number (TRN)"
    return False, False, "a valid tax ID for the declared country"


def aba_valid(r):
    if not re.fullmatch(r"\d{9}", r):
        return False
    d = [int(c) for c in r]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def check_bank(country, account, code, bank_name, banks):
    """Returns (ok, message)."""
    acct, code = norm_id(account), norm_id(code)
    if country in IBAN_COUNTRIES:
        if not _iban.is_valid(acct):
            return False, "IBAN is invalid (format or checksum)"
        if not acct.startswith(country):
            return False, f"IBAN country prefix {acct[:2]} does not match bank country {country}"
        return True, "IBAN valid"
    if country == "IN":
        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", code):
            return False, "IFSC code format is invalid (expected 4 letters, a zero, 6 characters)"
        if not re.fullmatch(r"\d{9,18}", acct):
            return False, "Account number must be 9 to 18 digits"
        expected = banks.get(code[:4])
        if expected and bank_name and fuzz.partial_ratio(expected.lower(), bank_name.lower()) < 80:
            return False, f"IFSC prefix {code[:4]} belongs to {expected}, not {bank_name}"
        return True, "IFSC and account number valid"
    if country == "US":
        if not aba_valid(code):
            return False, "Routing number is not a valid 9-digit ABA number"
        if not re.fullmatch(r"\d{4,17}", acct):
            return False, "Account number must be 4 to 17 digits"
        return True, "Routing and account number valid"
    return False, "Unsupported bank country"


def parse_date(s):
    try:
        return dt.date.fromisoformat((s or "").strip())
    except ValueError:
        return None


# ---------- result helper ----------
def R(rule, title, outcome, severity="FIX", points=0, message="", vendor_message="", form_value="",
      evidence_value="", flag=False, grey=None):
    return {"rule": rule, "title": title, "outcome": outcome, "severity": severity, "points": points if outcome == "fail" else 0,
            "message": message, "vendor_message": vendor_message, "form_value": form_value,
            "evidence_value": evidence_value, "flag": flag, "grey": grey}


# ---------- stage 2: completeness ----------
def completeness(form, docs_present):
    out = []
    missing = [f.replace("_", " ") for f in REQUIRED_FIELDS if not (form.get(f) or "").strip()]
    if form.get("country") == "IN" and not (form.get("pan") or "").strip():
        missing.append("pan")
    if missing:
        out.append(R("R01", "Required fields present", "fail", "FIX", 0, "Missing fields: " + ", ".join(missing),
                     "Please provide the following missing details: " + ", ".join(missing) + "."))
    else:
        out.append(R("R01", "Required fields present", "pass"))
    miss_docs = [lbl for k, lbl in REQUIRED_DOCS.items() if k not in docs_present]
    if miss_docs:
        out.append(R("R02", "Required documents present", "fail", "FIX", 0, "Missing documents: " + ", ".join(miss_docs),
                     "Please upload the following documents: " + ", ".join(miss_docs) + "."))
    else:
        out.append(R("R02", "Required documents present", "pass"))
    return out


# ---------- stage 3 add-on: extraction quality (R19) ----------
def extraction_quality(ext):
    out = []
    bad = []
    for dtype, keys in KEY_DOC_FIELDS.items():
        if dtype not in ext:
            continue
        f, c = ext[dtype].get("fields", {}), ext[dtype].get("confidence", {})
        for k in keys:
            if not f.get(k) or float(c.get(k, 0)) < 0.7:
                bad.append(f"{REQUIRED_DOCS[dtype]} ({k.replace('_', ' ')})")
    if bad:
        out.append(R("R19", "Documents machine-readable", "fail", "FIX", 0, "Low-confidence or missing extraction: " + "; ".join(bad),
                     "We could not read key details from: " + "; ".join(bad) + ". Please upload a clearer, text-based copy."))
    else:
        out.append(R("R19", "Documents machine-readable", "pass"))
    return out


# ---------- stage 5: field and cross-document rules ----------
def field_rules(form, ext, today=None):
    today = today or dt.date.today()
    country = (form.get("country") or "").upper()
    bank_country = (form.get("bank_country") or country).upper()
    banks = load_json("banks.json")
    out = []

    # R03 / R04 tax id
    fmt_ok, chk_ok, expected = check_tax_id(country, form.get("tax_id"))
    if not fmt_ok:
        out.append(R("R03", "Tax ID format valid for country", "fail", "FIX", 0,
                     f"Tax ID '{form.get('tax_id')}' is not a valid {COUNTRIES.get(country, country)} format",
                     f"The tax ID provided does not match the format for {COUNTRIES.get(country, country)}. Expected {expected}. "
                     "Please confirm your country of registration and resubmit the correct ID.",
                     form.get("tax_id"), expected))
    else:
        out.append(R("R03", "Tax ID format valid for country", "pass", form_value=form.get("tax_id")))
    if fmt_ok and not chk_ok:
        out.append(R("R04", "Tax ID checksum valid", "fail", "FIX", 0, "Tax ID checksum failed",
                     "The tax ID fails its built-in checksum, which usually means a typo. Please recheck it against your certificate.",
                     form.get("tax_id")))
    else:
        out.append(R("R04", "Tax ID checksum valid", "pass" if fmt_ok else "skip"))

    # R05 PAN inside GSTIN (India)
    if country == "IN" and fmt_ok and form.get("pan"):
        embedded = norm_id(form["tax_id"])[2:12]
        if embedded != norm_id(form["pan"]):
            out.append(R("R05", "PAN matches GSTIN", "fail", "FIX", 0, f"PAN {form['pan']} differs from PAN inside GSTIN ({embedded})",
                         "The PAN you entered does not match the PAN embedded in your GSTIN. Please correct one of them.",
                         form["pan"], embedded))
        else:
            out.append(R("R05", "PAN matches GSTIN", "pass", form_value=form["pan"], evidence_value=embedded))
    else:
        out.append(R("R05", "PAN matches GSTIN", "skip"))

    # R06 tax id vs tax certificate
    if "tax" in ext and ext["tax"]["fields"].get("tax_id"):
        ev = ext["tax"]["fields"]["tax_id"]
        if norm_id(ev) != norm_id(form.get("tax_id")):
            out.append(R("R06", "Tax ID matches tax certificate", "fail", "FIX", 0,
                         f"Form tax ID {form.get('tax_id')} differs from certificate {ev}",
                         "The tax ID on the form differs from the one on your tax certificate. Please confirm which is correct.",
                         form.get("tax_id"), ev))
        else:
            out.append(R("R06", "Tax ID matches tax certificate", "pass", form_value=form.get("tax_id"), evidence_value=ev))
    else:
        out.append(R("R06", "Tax ID matches tax certificate", "skip"))

    # R07 legal name vs incorporation certificate
    if "incorporation" in ext and ext["incorporation"]["fields"].get("legal_name"):
        ev = ext["incorporation"]["fields"]["legal_name"]
        out.append(name_rule("R07", "Legal name matches incorporation certificate", form.get("legal_name"), ev, 0,
                             "The legal name on the form does not match your certificate of incorporation. Please correct the form or upload the right certificate."))
    else:
        out.append(R("R07", "Legal name matches incorporation certificate", "skip"))

    # R08 account holder vs legal name
    holder_form = form.get("account_holder")
    holder_doc = (ext.get("bank_letter", {}).get("fields", {}) or {}).get("account_holder")
    if holder_form:
        out.append(name_rule("R08", "Bank account holder matches legal name", form.get("legal_name"), holder_form, 30,
                             "The bank account holder name does not match your company's legal name. Payments can only go to an account held in the company's name.",
                             flag=True))
        if holder_doc and name_score(holder_doc, holder_form) < int(config()["name_grey_threshold"]):
            out.append(R("R08b", "Bank letter holder matches form", "fail", "FIX", 30,
                         f"Bank letter holder '{holder_doc}' differs from form holder '{holder_form}'",
                         "The account holder on the bank letter does not match the form. Please upload a matching bank letter.",
                         holder_form, holder_doc, flag=True))
    else:
        out.append(R("R08", "Bank account holder matches legal name", "skip"))

    # R09 bank country
    if bank_country != country:
        out.append(R("R09", "Bank country matches vendor country", "fail", "WARN", 25,
                     f"Bank country {bank_country} differs from vendor country {country}", "", bank_country, country))
    else:
        out.append(R("R09", "Bank country matches vendor country", "pass"))

    # R10 bank formats
    ok, msg = check_bank(bank_country, form.get("account_number"), form.get("bank_code"), form.get("bank_name"), banks)
    out.append(R("R10", "Bank account and code valid", "pass" if ok else "fail", "FIX", 0, msg,
                 "We could not validate the bank details: " + msg + ". Please check and resubmit.", form.get("account_number"), form.get("bank_code")))

    # R11 account number vs bank letter
    acct_doc = (ext.get("bank_letter", {}).get("fields", {}) or {}).get("account_number")
    if acct_doc:
        if norm_id(acct_doc) != norm_id(form.get("account_number")):
            out.append(R("R11", "Account number matches bank letter", "fail", "FIX", 40,
                         f"Form account {form.get('account_number')} differs from bank letter {acct_doc}. Possible payment-diversion pattern.",
                         "The account number on the form does not match your bank letter. Please send an updated bank letter or correct the form.",
                         form.get("account_number"), acct_doc, flag=True))
        else:
            out.append(R("R11", "Account number matches bank letter", "pass", form_value=form.get("account_number"), evidence_value=acct_doc))
    else:
        out.append(R("R11", "Account number matches bank letter", "skip"))

    # R12 document currency
    stale = []
    bl = ext.get("bank_letter", {}).get("fields", {})
    issued = parse_date(bl.get("issue_date"))
    stale_days = int(config()["stale_bank_letter_days"])
    if issued and (today - issued).days > stale_days:
        stale.append(f"bank letter issued {issued} (older than {stale_days} days)")
    for dtype in ("incorporation", "tax"):
        exp = parse_date(ext.get(dtype, {}).get("fields", {}).get("expiry_date"))
        if exp and exp < today:
            stale.append(f"{REQUIRED_DOCS[dtype].lower()} expired {exp}")
    if stale and rule_enabled("R12"):
        out.append(R("R12", "Documents current", "fail", "FIX", 0, "; ".join(stale),
                     "Some documents are out of date: " + "; ".join(stale) + ". Please upload current copies."))
    else:
        out.append(R("R12", "Documents current", "pass"))
    return out


def name_rule(rid, title, a, b, points, vmsg, flag=False):
    score = name_score(a, b)
    settings = config()
    if score >= int(settings["name_pass_threshold"]):
        return R(rid, title, "pass", form_value=a, evidence_value=b, message=f"similarity {score:.0f}")
    if score >= int(settings["name_grey_threshold"]):
        return R(rid, title, "grey", "FIX", points, f"similarity {score:.0f} is in the grey zone", vmsg, a, b, flag,
                 grey={"a": a, "b": b, "score": score})
    return R(rid, title, "fail", "FIX", points, f"names differ (similarity {score:.0f})", vmsg, a, b, flag)


def resolve_grey(result, verdict):
    """Apply an AI verdict to a grey-zone result. AI may keep or raise severity but cannot hide a mismatch it is unsure about."""
    r = dict(result)
    reason = verdict.get("reason", "")
    if verdict.get("verdict") == "same":
        r["outcome"], r["points"] = "pass", 0
        r["message"] += f"; AI judged same entity: {reason}"
    else:
        r["outcome"] = "fail"
        r["message"] += f"; AI verdict {verdict.get('verdict')}: {reason}"
    return r


# ---------- stage 6: risk checks ----------
def risk_rules(form, key, existing):
    """existing: list of dicts with key, legal_name, tax_id, account_number (other vendors, already onboarded)."""
    country = (form.get("country") or "").upper()
    out = []
    tax = norm_id(form.get("tax_id"))
    acct = norm_id(form.get("account_number"))
    others = [v for v in existing if v.get("key") != key]

    dup_tax = [v for v in others if tax and norm_id(v.get("tax_id")) == tax]
    if dup_tax:
        out.append(R("R13", "No duplicate vendor (tax ID)", "fail", "BLOCK", 0,
                     f"Tax ID already registered to {dup_tax[0]['legal_name']} ({dup_tax[0].get('id', '')})",
                     "", form.get("tax_id"), dup_tax[0]["legal_name"]))
    else:
        out.append(R("R13", "No duplicate vendor (tax ID)", "pass"))

    dup_bank = [v for v in others if acct and norm_id(v.get("account_number")) == acct]
    if dup_bank:
        out.append(R("R14", "Bank account not shared with another entity", "fail", "BLOCK", 0,
                     f"FRAUD ESCALATION: account already linked to {dup_bank[0]['legal_name']} ({dup_bank[0].get('id', '')})",
                     "", form.get("account_number"), dup_bank[0]["legal_name"], flag=True))
    else:
        out.append(R("R14", "Bank account not shared with another entity", "pass"))

    # A similar legal name with different identifiers is not a hard stop, but it is a valuable
    # duplicate-vendor signal for a reviewer to resolve before a second vendor is created.
    fuzzy = max(((name_score(form.get("legal_name"), v.get("legal_name")), v) for v in others), default=(0, None))
    fuzzy_threshold = int(config()["fuzzy_duplicate_threshold"])
    if rule_enabled("R13b") and fuzzy[1] and fuzzy[0] >= fuzzy_threshold:
        out.append(R("R13b", "Fuzzy duplicate-vendor screening", "fail", "WARN", 30,
                     f"Possible duplicate ({fuzzy[0]:.0f}% name similarity) with {fuzzy[1]['legal_name']} ({fuzzy[1].get('id', '')})",
                     "", form.get("legal_name"), fuzzy[1]["legal_name"], flag=True))
    else:
        out.append(R("R13b", "Fuzzy duplicate-vendor screening", "pass" if rule_enabled("R13b") else "skip"))

    sanctions = load_json("sanctions.json")
    best = max(((name_score(form.get("legal_name"), s), s) for s in sanctions), default=(0, ""))
    settings = config()
    if best[0] >= int(settings["sanctions_block_threshold"]):
        out.append(R("R15", "Sanctions screening", "fail", "BLOCK", 0, f"Sanctions match {best[0]:.0f}: {best[1]}", "",
                     form.get("legal_name"), best[1], flag=True))
        out.append(R("R16", "Sanctions near-match", "skip"))
    elif best[0] >= int(settings["sanctions_warn_threshold"]):
        out.append(R("R15", "Sanctions screening", "pass"))
        out.append(R("R16", "Sanctions near-match", "fail", "WARN", 30, f"Near-match {best[0]:.0f}: {best[1]}", "",
                     form.get("legal_name"), best[1], flag=True))
    else:
        out.append(R("R15", "Sanctions screening", "pass"))
        out.append(R("R16", "Sanctions near-match", "pass"))

    email_dom, web_dom = domain_of(form.get("contact_email")), domain_of(form.get("website"))
    if not rule_enabled("R17"):
        out.append(R("R17", "Corporate email domain", "skip"))
    elif email_dom in FREE_MAIL:
        out.append(R("R17", "Corporate email domain", "fail", "WARN", 10, f"Free-mail address {email_dom}", "", email_dom, web_dom))
    elif web_dom and email_dom != web_dom:
        out.append(R("R17", "Corporate email domain", "fail", "WARN", 10, f"Email domain {email_dom} differs from website {web_dom}", "", email_dom, web_dom))
    else:
        out.append(R("R17", "Corporate email domain", "pass"))

    addr = (form.get("address") or "").lower()
    issues = []
    if re.search(r"\bp\.?\s?o\.?\s?box\b", addr):
        issues.append("PO box address")
    own = COUNTRY_WORDS.get(country, [])
    foreign = [c for c, words in COUNTRY_WORDS.items() if c != country and any(w in addr for w in words)]
    if foreign and not any(w in addr for w in own):
        issues.append(f"address mentions {foreign[0]} but vendor country is {country}")
    if not rule_enabled("R18"):
        out.append(R("R18", "Address plausibility", "skip"))
    elif issues:
        out.append(R("R18", "Address plausibility", "fail", "WARN", 15, "; ".join(issues), "", form.get("address")))
    else:
        out.append(R("R18", "Address plausibility", "pass"))
    return out


# ---------- stage 8: decision ----------
def decide(results):
    fails = [r for r in results if r["outcome"] == "fail"]
    risk = sum(r["points"] for r in fails)
    review_threshold = int(config()["review_risk_threshold"])
    review = risk >= review_threshold or any(r["flag"] for r in fails)
    if any(r["severity"] == "BLOCK" for r in fails):
        status = "Rejected"
    elif any(r["severity"] == "FIX" for r in fails):
        status = "Pending"
    elif risk >= review_threshold:
        status = "Pending"
    else:
        status = "Approved"
    return {"status": status, "risk_score": risk, "review_flag": review and status != "Approved"}
