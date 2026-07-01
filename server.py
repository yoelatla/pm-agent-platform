#!/usr/bin/env python3
"""
PM Agent Platform — Flask server
Bridges PLATFORM.html with scraper agents and LinkedIn automation.

Usage: python3 server.py
Then open http://localhost:5002
"""

import json
import os
import subprocess
import sys
import threading
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request, send_from_directory, Response
from flask_cors import CORS

BASE_DIR = Path(__file__).parent
SCAN_DIR = BASE_DIR.parent / "linkedin_scan"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SEEN_JOBS_FILE   = DATA_DIR / "seen_jobs.json"
OUTREACH_FILE    = DATA_DIR / "outreach_data.json"
BLOCKLIST_FILE   = DATA_DIR / "blocklist.json"
COMPANIES_FILE   = DATA_DIR / "companies_pool.json"

sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(SCAN_DIR))

from title_filter import is_pm_title, filter_jobs  # noqa: E402

app = Flask(__name__)
CORS(app, allow_private_network=True)

@app.errorhandler(Exception)
def handle_error(e):
    traceback.print_exc()
    return jsonify({"success": False, "error": str(e)}), 500


# ── Subprocess state helpers ──────────────────────────────────────────────────

def _make_proc_state():
    return {"proc": None, "logs": deque(maxlen=300), "started_at": None, "lock": threading.Lock()}

_li    = _make_proc_state()   # LinkedIn scan
_web   = _make_proc_state()   # Web scan
_scout = _make_proc_state()   # Contact scout
_disc  = _make_proc_state()   # Company discovery
_deep  = _make_proc_state()   # Deep scan
_dedup = _make_proc_state()   # Dedup agent


def _start_proc(state: dict, cmd: list, label: str):
    with state["lock"]:
        if state["proc"] and state["proc"].poll() is None:
            return False, f"{label} already running"
        state["logs"].clear()
        state["logs"].append(f"▶ Starting {label}…")
        state["started_at"] = datetime.now().isoformat()
        try:
            state["proc"] = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(BASE_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except Exception as e:
            return False, str(e)
    threading.Thread(target=_tail, args=(state,), daemon=True).start()
    return True, "started"


def _tail(state: dict):
    proc = state["proc"]
    try:
        for line in proc.stdout:
            state["logs"].append(line.rstrip())
    finally:
        proc.wait()


def _proc_status(state: dict):
    proc = state["proc"]
    return {
        "running": bool(proc and proc.poll() is None),
        "exit_code": proc.poll() if proc else None,
        "started_at": state["started_at"],
        "logs": list(state["logs"]),
    }


def _kill_proc(state: dict, label: str):
    import signal
    proc = state["proc"]
    if not proc or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    state["logs"].append(f"🛑 {label} stopped.")
    return True


# ── Blocklist ─────────────────────────────────────────────────────────────────

def _load_bl():
    if BLOCKLIST_FILE.exists():
        return json.loads(BLOCKLIST_FILE.read_text())
    return []

def _save_bl(bl):
    BLOCKLIST_FILE.write_text(json.dumps(sorted(set(p.lower().strip() for p in bl if p.strip())), indent=2))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "PLATFORM.html")


@app.route("/api/status")
def api_status():
    import urllib.request
    chrome_ok = False
    try:
        urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
        chrome_ok = True
    except Exception:
        pass
    companies = 0
    if OUTREACH_FILE.exists():
        try:
            companies = len(json.loads(OUTREACH_FILE.read_text()).get("companies", {}))
        except Exception:
            pass
    return jsonify({
        "chrome_connected": chrome_ok,
        "data_file_exists": OUTREACH_FILE.exists(),
        "seen_jobs_exists": SEEN_JOBS_FILE.exists(),
        "companies_in_data": companies,
    })


@app.route("/api/jobs")
def api_jobs():
    try:
        hours = float(request.args.get("hours", 48))
    except (ValueError, TypeError):
        hours = 48

    # Try platform data dir first, then fall back to linkedin_scan
    jobs_file = SEEN_JOBS_FILE
    if not jobs_file.exists():
        fallback = SCAN_DIR / "seen_jobs.json"
        if fallback.exists():
            jobs_file = fallback

    if not jobs_file.exists():
        return jsonify({"success": False, "error": "No seen_jobs.json — run a scan first"})

    cutoff = hours * 3600
    now = datetime.now()
    bl = _load_bl()
    jobs = []

    try:
        all_jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
        for job_id, job in all_jobs.items():
            title   = job.get("title", "")
            company = job.get("company", "")

            # Title filter (display layer)
            if not is_pm_title(title):
                continue
            # Blocklist
            if any(p in company.lower() for p in bl):
                continue
            # Time filter
            try:
                source = job.get("source", "linkedin")
                if source == "linkedin":
                    posted = datetime.strptime(job.get("posted_at", ""), "%Y-%m-%d %H:%M")
                    if (now - posted).total_seconds() > cutoff:
                        continue
                else:
                    seen = datetime.fromisoformat(job.get("seen_at", ""))
                    if (now - seen).total_seconds() > cutoff * 24:
                        continue
            except Exception:
                pass

            jobs.append({
                "id":       job_id,
                "title":    title,
                "company":  company,
                "url":      job.get("url") or f"https://www.linkedin.com/jobs/view/{job_id}/",
                "source":   job.get("source", "linkedin"),
                "date":     job.get("posted_at", ""),
                "seen_at":  job.get("seen_at", ""),
            })

        jobs.sort(key=lambda x: x["date"], reverse=True)
        return jsonify({"success": True, "jobs": jobs, "total": len(jobs)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/contacts")
def api_contacts():
    f = OUTREACH_FILE
    if not f.exists():
        f = SCAN_DIR / "outreach_data.json"
    if f.exists():
        return jsonify({"success": True, "data": json.loads(f.read_text(encoding="utf-8"))})
    return jsonify({"success": False, "error": "No outreach_data.json — run scout first"})


@app.route("/api/mark-status", methods=["POST"])
def api_mark_status():
    body = request.get_json(force=True) or {}
    company     = body.get("company", "").strip()
    profile_url = body.get("profile_url", "").strip()
    job_url     = body.get("job_url", "").strip()
    status      = body.get("status", "sent")

    f = OUTREACH_FILE
    if not f.exists():
        f = SCAN_DIR / "outreach_data.json"
    if not f.exists():
        return jsonify({"success": False, "error": "No outreach_data.json"})

    data = json.loads(f.read_text(encoding="utf-8"))
    updated = False
    for c in data.get("companies", {}).get(company, {}).get("contacts", []):
        if c["profile_url"] == profile_url:
            for msg in c.get("messages", []):
                if msg["job_url"] == job_url:
                    msg["status"] = status
                    msg["sent_at"] = datetime.now().isoformat() if status == "sent" else None
                    updated = True
    if updated:
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return jsonify({"success": updated})


# ── Scan actions ──────────────────────────────────────────────────────────────

@app.route("/api/run-linkedin-scan", methods=["POST"])
def api_run_linkedin_scan():
    ok, msg = _start_proc(_li, [sys.executable, str(SCAN_DIR / "scan_pipeline.py")], "LinkedIn scan")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/linkedin-scan-status")
def api_linkedin_scan_status():
    return jsonify(_proc_status(_li))

@app.route("/api/stop-linkedin-scan", methods=["POST"])
def api_stop_linkedin_scan():
    return jsonify({"success": True, "killed": _kill_proc(_li, "LinkedIn scan")})


@app.route("/api/run-web-scan", methods=["POST"])
def api_run_web_scan():
    ok, msg = _start_proc(_web, [sys.executable, str(SCAN_DIR / "web_scan_pipeline.py")], "Web scan")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/web-scan-status")
def api_web_scan_status():
    return jsonify(_proc_status(_web))

@app.route("/api/stop-web-scan", methods=["POST"])
def api_stop_web_scan():
    return jsonify({"success": True, "killed": _kill_proc(_web, "Web scan")})


@app.route("/api/run-scout", methods=["POST"])
def api_run_scout():
    ok, msg = _start_proc(_scout, [sys.executable, str(SCAN_DIR / "linkedin_outreach_agent.py")], "Contact scout")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/scout-status")
def api_scout_status():
    return jsonify(_proc_status(_scout))

@app.route("/api/stop-scout", methods=["POST"])
def api_stop_scout():
    return jsonify({"success": True, "killed": _kill_proc(_scout, "Scout")})


@app.route("/api/run-discovery", methods=["POST"])
def api_run_discovery():
    ok, msg = _start_proc(_disc, [sys.executable, str(SCAN_DIR / "discover_companies.py")], "Company discovery")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/discovery-status")
def api_discovery_status():
    return jsonify(_proc_status(_disc))

@app.route("/api/stop-discovery", methods=["POST"])
def api_stop_discovery():
    return jsonify({"success": True, "killed": _kill_proc(_disc, "Discovery")})


@app.route("/api/run-deep-scan", methods=["POST"])
def api_run_deep_scan():
    ok, msg = _start_proc(_deep, [sys.executable, str(SCAN_DIR / "career_page_scraper.py")], "Deep scan")
    return jsonify({"success": ok, "message": msg})

@app.route("/api/deep-scan-status")
def api_deep_scan_status():
    return jsonify(_proc_status(_deep))

@app.route("/api/stop-deep-scan", methods=["POST"])
def api_stop_deep_scan():
    return jsonify({"success": True, "killed": _kill_proc(_deep, "Deep scan")})


# ── Blocklist CRUD ────────────────────────────────────────────────────────────

@app.route("/api/blocklist", methods=["GET"])
def api_blocklist_get():
    return jsonify({"patterns": _load_bl()})

@app.route("/api/blocklist", methods=["POST"])
def api_blocklist_add():
    pattern = (request.get_json(force=True) or {}).get("pattern", "").strip().lower()
    if not pattern:
        return jsonify({"success": False, "error": "pattern required"})
    bl = _load_bl()
    if pattern not in bl:
        bl.append(pattern)
        _save_bl(bl)
    return jsonify({"success": True, "patterns": sorted(bl)})

@app.route("/api/blocklist", methods=["DELETE"])
def api_blocklist_remove():
    pattern = (request.get_json(force=True) or {}).get("pattern", "").strip().lower()
    _save_bl([p for p in _load_bl() if p != pattern])
    return jsonify({"success": True})


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    results = []

    def check(name, fn):
        try:
            status, detail = fn()
            results.append({"name": name, "status": status, "detail": detail})
        except Exception as e:
            results.append({"name": name, "status": "FAIL", "detail": str(e)})

    def chk_json_file(path, required_keys):
        if not path.exists():
            return "FAIL", f"Missing: {path.name}"
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            return "FAIL", f"Invalid JSON: {e}"
        count = len(data) if isinstance(data, (dict, list)) else 0
        return "OK", f"{count} entries"

    check("seen_jobs.json", lambda: chk_json_file(
        SEEN_JOBS_FILE if SEEN_JOBS_FILE.exists() else SCAN_DIR / "seen_jobs.json",
        ["title", "company"]
    ))
    check("outreach_data.json", lambda: chk_json_file(
        OUTREACH_FILE if OUTREACH_FILE.exists() else SCAN_DIR / "outreach_data.json",
        ["companies"]
    ))
    check("title_filter", lambda: (
        ("OK", "Module loaded") if is_pm_title("Product Manager") and not is_pm_title("VP Product")
        else ("FAIL", "Filter logic broken")
    ))
    check("scraper scripts", lambda: (
        ("OK", f"{len(list(SCAN_DIR.glob('*_scraper.py')))} scrapers found")
        if SCAN_DIR.exists() else ("FAIL", "linkedin_scan dir missing")
    ))

    import urllib.request as _ur
    def chk_chrome():
        try:
            _ur.urlopen("http://127.0.0.1:9222/json/version", timeout=2)
            return "OK", "Chrome debug port open"
        except Exception:
            return "WARN", "Chrome not running (Selenium features unavailable)"
    check("Chrome debug port", chk_chrome)

    # Agent files
    agents_dir = BASE_DIR / ".claude" / "agents"
    agent_files = list(agents_dir.glob("*.md")) if agents_dir.exists() else []
    check("agent definitions", lambda: (
        ("OK", f"{len(agent_files)} agents: {', '.join(f.stem for f in agent_files)}")
        if agent_files else ("FAIL", "No agent .md files found")
    ))

    passed  = sum(1 for r in results if r["status"] == "OK")
    warned  = sum(1 for r in results if r["status"] == "WARN")
    failed  = sum(1 for r in results if r["status"] == "FAIL")
    return jsonify({"results": results, "passed": passed, "warned": warned, "failed": failed})


if __name__ == "__main__":
    print("=" * 50)
    print("  PM Agent Platform — Server")
    print("  http://localhost:5002")
    print("=" * 50)
    app.run(port=5002, debug=False, threaded=True)
