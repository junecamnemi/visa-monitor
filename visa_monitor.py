#!/usr/bin/env python3
"""
Visa Portal Monitor — Korea visa application status watcher.

Reads the input list from a Google Sheet, scrapes each target applicant's
current visa status from www.visa.go.kr via plain HTTP (no browser), detects
status changes against a stored state file, downloads the VISA GRANT NOTICE
(사증발급확인서) PDF for newly-approved applicants, and pushes updates to
Telegram via the bot API.

Status flow on the portal (재외공관 / overseas-embassy query by passport):
    접수 (Accepted/Received) -> 심사 중 (Under Review) -> 허가 (Approved) | 거절/불허 (Denied)

Usage:
    python visa_monitor.py                     # run one full pass
    python visa_monitor.py --chat 123 --token  # override Telegram target

Configuration (top of file or env):
    SHEET_ID           Google Spreadsheet id (public export)
    TELEGRAM_BOT_TOKEN Bot token
    TELEGRAM_CHAT_ID   Telegram chat id
    STATE_FILE         JSON state of last-known statuses
    CERT_DIR           Folder where approved certificates are saved
"""
import csv
import io
import json
import os
import re
import sys
import time
import datetime
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Configuration
SHEET_ID = os.environ.get("SHEET_ID", "1vfzCRuHi-VviCj3z8BXTg76kw7wK1xJlSw-yCVSavec")
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"

BASE = "https://www.visa.go.kr/openPage.do?MENU_ID=10301"
PRINT_URL = "https://www.visa.go.kr/biz/ap/ev/selectElectronicVisaPrint3.do"

PROJECT_DIR = os.environ.get("MONITOR_DIR", r"C:\Users\wisew\OneDrive\바탕 화면\비자포털모니터링")
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(PROJECT_DIR, "monitor_state.json"))
CERT_DIR = os.environ.get("CERT_DIR", os.path.join(PROJECT_DIR, "certificates"))
os.makedirs(CERT_DIR, exist_ok=True)

# Telegram target: env override, else defaults in .env
HERMES_ENV = os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", ".env")


def load_env_secret(key, default=""):
    if key in os.environ and os.environ[key]:
        return os.environ[key]
    try:
        with open(HERMES_ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return default


TELEGRAM_BOT_TOKEN = load_env_secret("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = load_env_secret("TELEGRAM_HOME_CHANNEL")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# ---------------------------------------------------------------------------
# Adjustable config (edit visa_monitor_config.json to change via chat)
# ---------------------------------------------------------------------------
CONFIG_FILE = os.path.join(PROJECT_DIR, "visa_monitor_config.json")

_DEFAULT_CONFIG = {
    "enabled": True,
    "target_status_prefixes": ["Under Review"],
    "target_status_equal": ["Accepted"],
    "target_result_date_today": True,
    "notify_run_start": False,
    "interval_seconds": 1,
    "notify_on_approval": True,
    "notify_on_denial": True,
    "send_certificate_to_telegram": True,
    "send_certificate_to_slack": True,
    "check_frequency_override": None,
}


def load_config():
    cfg = dict(_DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        pass
    return cfg


CONFIG = load_config()
TARGET_STATUS_PREFIXES = tuple(CONFIG.get("target_status_prefixes") or ["Under Review"])
TARGET_STATUS_EQUAL = tuple(CONFIG.get("target_status_equal") or ["Accepted"])
EXCLUDED_PASSPORTS = set(CONFIG.get("excluded_passports") or [])
TARGET_PASSPORTS = set(CONFIG.get("target_passports") or [])


def get_sheet_rows():
    req = urllib.request.Request(SHEET_CSV_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(raw))
    rows = []
    for line in reader:
        if len(line) < 4:
            continue
        passno = (line[1] or "").strip()
        name = (line[2] or "").strip()
        birth = (line[3] or "").strip()
        status = (line[9] if len(line) > 9 else "").strip()
        result_date = (line[6] if len(line) > 6 else "").strip()
        if not passno or not name or not birth:
            continue
        if not re.fullmatch(r"[A-Za-z0-9]+", passno):
            continue
        rows.append({"passno": passno, "name": name, "birth": birth,
                     "sheet_status": status, "result_date": result_date,
                     "row_no": line[0]})
    return rows


def is_target(row):
    status = (row.get("sheet_status") or "").strip()
    if row.get("passno") in EXCLUDED_PASSPORTS:
        return False
    # Explicitly-named applicants are always targeted (cloud mode).
    if TARGET_PASSPORTS:
        return row.get("passno") in TARGET_PASSPORTS
    if status.startswith(TARGET_STATUS_PREFIXES):
        return True
    if status in TARGET_STATUS_EQUAL:
        return True
    # Also target rows whose Result Date (col6) is today, regardless of status.
    if CONFIG.get("target_result_date_today"):
        rd = (row.get("result_date") or "").strip().replace("-", "")
        if rd and rd == datetime.date.today().strftime("%Y%m%d"):
            return True
    return False


# ---------------------------------------------------------------------------
# Portal scraping
# ---------------------------------------------------------------------------
def _clean(s):
    s = re.sub(r"<!--.*?-->", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lstrip(">").strip()


def fetch_status(session, passno, name, birth):
    data = {
        "CMM_TEST_VAL": "test", "TRAN_TYPE": "ComSubmit", "SE_FLAG_YN": "",
        "LANG_TYPE": "KO", "pRADIOSEARCH": "gb03", "sBUSI_GB": "PASS_NO",
        "sBUSI_GBNO": passno, "ssBUSI_GBNO": passno,
        "sEK_NM": name, "sFROMDATE": birth.replace("-", ""),
        "sMainPopUpGB": "main",
    }
    r = session.post(BASE, data=data, headers={"Referer": BASE, "Origin": "https://www.visa.go.kr"}, timeout=20)
    t = r.text
    m = re.search(r'\$\(\"#APPL_DTM\"\)\.html\(\"([^\"]*)\"\)', t) or \
        re.search(r'id="APPL_DTM">([^<]*)</div>', t)
    apply_date = m.group(1).strip() if m else ""
    m = re.search(r'id="ENTRY_PURPOSE">([^<]*)</div>', t)
    purpose = m.group(1).strip() if m else ""
    m = re.search(r'id="PROC_STS_CDNM_1"([\s\S]*?)</div>', t)
    status = _clean(m.group(1)) if m else ""
    # certificate link (only present when approved)
    m = re.search(r"fn_reportByCsvMap4\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'\)", t)
    cert = None
    if m:
        cert = {"ev_seq": m.group(1), "invitee": m.group(2),
                "appl_no": m.group(3), "eng_nm": m.group(4),
                "birth": m.group(5)}
    # rejection reason (denied)
    reason = ""
    m = re.search(r'id="NONPERM_RSN_CDNM"[^>]*>\s*([\s\S]*?)</div>', t) or \
        re.search(r'NONPERM_RSN_CDNM\)\.html\("([^"]*)"\)', t)
    if m:
        reason = _clean(m.group(1))
    return {"passno": passno, "name": name, "birth": birth,
            "status": status, "apply": apply_date, "purpose": purpose,
            "cert": cert, "reason": reason}


def download_cert(session, cert, passno, name):
    data = {
        "CMM_TEST_VAL": "test", "TRAN_TYPE": "ComSubmit", "SE_FLAG_YN": "",
        "LANG_TYPE": "KO", "sBUSI_GB": "PASS_NO", "sBUSI_GBNO": passno,
        "EV_SEQ": cert["ev_seq"], "INVITEE_SEQ": cert["invitee"],
        "APPL_NO": cert["appl_no"], "ENG_NM": cert["eng_nm"],
        "BIRTH_YMD": cert["birth"],
    }
    r = session.post(PRINT_URL, data=data, headers={"Referer": BASE, "Origin": "https://www.visa.go.kr"})
    if r.content[:4] == b"%PDF":
        safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
        fname = os.path.join(CERT_DIR, f"비자발급확인서_{passno}_{safe}.pdf")
        with open(fname, "wb") as f:
            f.write(r.content)
        return fname
    return None


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------
def classify(status):
    s = status or ""
    if "허가" in s or "APPROVED" in s.upper():
        return "approved"
    if "심사 중" in s or "UNDER REVIEW" in s.upper():
        return "under_review"
    if "접수" in s or "ACCEPTED" in s.upper() or "RECEIVED" in s.upper():
        return "accepted"
    if "불허" in s or "거절" in s or "DENIED" in s.upper():
        return "denied"
    if not s.strip():
        return "no_record"
    return "other"


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; skipped. Message:\n" + text)
        return False
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print("Telegram send failed:", e)
        return False


def send_telegram_document(file_path, caption=""):
    """Send a file (e.g. the approved 비자발급확인서 PDF) to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured; skipped file:", file_path)
        return False
    import requests
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": (os.path.basename(file_path), f, "application/pdf")}
            data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            r = requests.post(url, data=data, files=files, timeout=90)
            return r.status_code == 200
    except Exception as e:
        print("Telegram document send failed:", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if "--chat" in sys.argv:
        global TELEGRAM_CHAT_ID
        TELEGRAM_CHAT_ID = sys.argv[sys.argv.index("--chat") + 1]
    if "--token" in sys.argv:
        global TELEGRAM_BOT_TOKEN
        TELEGRAM_BOT_TOKEN = sys.argv[sys.argv.index("--token") + 1]

    if not CONFIG.get("enabled", True):
        print("visa_monitor disabled (visa_monitor_config.json enabled=false). Skipping.")
        return

    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}

    rows = get_sheet_rows()
    # Terminal states (Approved/Denied) are removed from monitoring permanently.
    final_passports = {k for k, v in state.items() if v.get("final")}
    targets = [r for r in rows
               if is_target(r) and r["passno"] not in final_passports]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    session = requests_session()
    changed = []
    errors = []
    results = []

    for t in targets:
        try:
            res = fetch_status(session, t["passno"], t["name"], t["birth"])
        except Exception as e:
            errors.append(f"{t['passno']} {t['name']}: ERROR {e}")
            continue
        key = t["passno"]
        prev = state.get(key, {}).get("status")
        new = res["status"]
        state.setdefault(key, {})["status"] = new
        state[key]["last_check"] = now
        state[key]["name"] = t["name"]
        cls = classify(new)
        # Terminal stage reached (Approved / Denied): download the certificate on
        # approval, then permanently remove this applicant from future monitoring.
        if cls in ("approved", "denied"):
            state[key]["final"] = True
            if cls == "approved" and res["cert"] and not state[key].get("cert_downloaded"):
                path = download_cert(session, res["cert"], t["passno"], t["name"])
                if path:
                    res["cert_file"] = path
                    state[key]["cert_downloaded"] = True
        # Report status transitions (including into a terminal state).
        if prev is not None and prev != new:
            res["prev"] = prev
            res["checked_at"] = now
            changed.append(res)
        results.append(res)
        # Pace requests to avoid the portal throttling/blocking this IP.
        time.sleep(int(CONFIG.get("interval_seconds", 1)))

    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # Report only changes (plus errors) — stay silent when nothing changed.
    lines = []
    if changed:
        lines.append(f"\n\U0001f514 VISA STATUS UPDATE ({now}) — {len(changed)} change(s)")
        for c in changed:
            lines.append(f"\n• {c['passno']} {c['name']}")
            lines.append(f"  Previous: {c['prev'] or '—'}")
            lines.append(f"  Now:      {c['status']}")
            if c.get("cert_file"):
                lines.append(f"  \U0001f4c4 비자발급확인서 downloaded: {os.path.basename(c['cert_file'])}")
            if classify(c["status"]) == "denied" and c.get("reason"):
                lines.append(f"  Reason: {c['reason']}")
    if errors:
        lines.append("\n\U000026a0 ERRORS:")
        lines += [f"  {e}" for e in errors]

    summary = f"Checked {len(targets)} targets ({now}). Changes: {len(changed)}. Errors: {len(errors)}."
    if lines:
        msg = "\n".join(lines) + f"\n\n{summary}"
        send_telegram(msg)
    # Dedicated, prominent alert for per-target failures.
    if errors:
        alert = "\U0001f6a8 VISA CHECK ERROR — {0} target(s) failed ({now})".format(len(errors))
        alert += "\n\n" + "\n".join(f"• {e}" for e in errors)
        alert += "\n\nWill retry next run. If errors persist, the portal may be throttling/blocking this IP."
        send_telegram(alert)
        print(alert)
    # Send any newly downloaded approval certificates to Telegram as PDFs.
    for c in changed:
        if c.get("cert_file"):
            send_telegram_document(
                c["cert_file"],
                caption=f"비자발급확인서 — {c['name']} ({c['passno']})")
    if lines:
        print(msg)
    elif "--verbose" in sys.argv:
        print(summary)


def requests_session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    s.get(BASE, timeout=20)
    return s


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"VISA MONITOR ERROR: {e}")
        sys.exit(1)
