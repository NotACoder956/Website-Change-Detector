from __future__ import annotations
import hashlib, html, json, os, threading, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

from . import db
from .core import (fetch_page, normalize_page, text_diff, summarize_diff, validate_public_url,
                   utcnow, send_email, send_telegram)

ROOT = Path(__file__).resolve().parent
CHECK_INTERVAL_SECONDS = max(5, int(os.getenv("CHECK_INTERVAL_SECONDS", "15")))
_scheduler_stop = threading.Event()
_scheduler_thread = None
_site_locks: dict[int, threading.Lock] = {}
_site_locks_guard = threading.Lock()


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048)
    interval_minutes: int = Field(default=60, ge=15, le=1440)
    selector: Optional[str] = Field(default=None, max_length=300)
    ignore_selectors: Optional[str] = Field(default=None, max_length=1000)
    keywords: Optional[str] = Field(default=None, max_length=500)
    sensitivity: str = Field(default="medium", pattern="^(low|medium|high)$")
    email: Optional[str] = Field(default=None, max_length=254)
    telegram_chat_id: Optional[str] = Field(default=None, max_length=100)


class SiteUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    interval_minutes: Optional[int] = Field(default=None, ge=15, le=1440)
    selector: Optional[str] = Field(default=None, max_length=300)
    ignore_selectors: Optional[str] = Field(default=None, max_length=1000)
    keywords: Optional[str] = Field(default=None, max_length=500)
    sensitivity: Optional[str] = Field(default=None, pattern="^(low|medium|high)$")
    email: Optional[str] = Field(default=None, max_length=254)
    telegram_chat_id: Optional[str] = Field(default=None, max_length=100)
    enabled: Optional[bool] = None


def get_site(site_id: int):
    with db.connect() as c:
        row = c.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Monitored website not found")
    return dict(row)


def _lock_for(site_id: int):
    with _site_locks_guard:
        return _site_locks.setdefault(site_id, threading.Lock())


def _deliver_notifications(site: dict, event_id: int, summary: str, url: str):
    message = f"Website changed: {site['name']}\n{summary}\nURL: {url}\nEvent ID: {event_id}"
    destinations = []
    if site.get("email"):
        ok, detail = send_email(site["email"], f"Website changed: {site['name']}", message)
        destinations.append(("email", site["email"], ok, detail))
    if site.get("telegram_chat_id"):
        ok, detail = send_telegram(message, site["telegram_chat_id"])
        destinations.append(("telegram", site["telegram_chat_id"], ok, detail))
    with db.connect() as c:
        for channel, destination, ok, detail in destinations:
            c.execute("""INSERT INTO notifications(event_id,channel,destination,status,detail,created_at)
              VALUES(?,?,?,?,?,?) ON CONFLICT(event_id,channel,destination) DO UPDATE SET
              status=excluded.status,detail=excluded.detail,attempt_count=notifications.attempt_count+1,created_at=excluded.created_at""",
              (event_id, channel, destination, "sent" if ok else "failed", detail, utcnow()))


def run_check(site_id: int) -> dict:
    """Run one check. Safe to invoke from scheduler or API; overlapping checks are skipped."""
    lock = _lock_for(site_id)
    if not lock.acquire(blocking=False):
        return {"result": "skipped", "message": "A check is already running for this site."}
    started = time.monotonic()
    started_at = utcnow()
    run_id = None
    try:
        site = get_site(site_id)
        with db.connect() as c:
            cur = c.execute("INSERT INTO check_runs(site_id,started_at,result) VALUES(?,?,?)", (site_id, started_at, "running"))
            run_id = cur.lastrowid
        try:
            page = fetch_page(site["url"])
            normalized = normalize_page(page["html"], site.get("selector"), site.get("ignore_selectors"))
        except Exception as exc:
            message = str(exc)[:1000]
            with db.connect() as c:
                c.execute("UPDATE check_runs SET finished_at=?,result=?,error_message=?,duration_ms=? WHERE id=?",
                          (utcnow(), "error", message, int((time.monotonic()-started)*1000), run_id))
                c.execute("UPDATE sites SET status=?,last_checked_at=?,last_error=?,next_check_at=? WHERE id=?",
                          ("blocked" if any(k in message.lower() for k in ("blocked", "captcha", "403", "429", "redirect")) else "error",
                           utcnow(), message, (datetime.now(timezone.utc)+timedelta(minutes=site["interval_minutes"])).isoformat(timespec="seconds"), site_id))
            return {"result": "error", "message": message}

        with db.connect() as c:
            previous = c.execute("SELECT * FROM snapshots WHERE site_id=? ORDER BY id DESC LIMIT 1", (site_id,)).fetchone()
            now = utcnow()
            if previous is None:
                cur = c.execute("INSERT INTO snapshots(site_id,content_hash,title,text_content,html_content,created_at) VALUES(?,?,?,?,?,?)",
                                (site_id, normalized["hash"], normalized["title"], normalized["text"], normalized["html"], now))
                c.execute("UPDATE sites SET status='ok',last_checked_at=?,last_error=NULL,next_check_at=? WHERE id=?",
                          (now, (datetime.now(timezone.utc)+timedelta(minutes=site["interval_minutes"])).isoformat(timespec="seconds"), site_id))
                c.execute("UPDATE check_runs SET finished_at=?,result='baseline',http_status=?,duration_ms=? WHERE id=?",
                          (now, page["status_code"], int((time.monotonic()-started)*1000), run_id))
                return {"result": "baseline", "message": "Initial snapshot saved. Future checks will compare against it.", "snapshot_id": cur.lastrowid}
            if previous["content_hash"] == normalized["hash"]:
                c.execute("UPDATE sites SET status='ok',last_checked_at=?,last_error=NULL,next_check_at=? WHERE id=?",
                          (now, (datetime.now(timezone.utc)+timedelta(minutes=site["interval_minutes"])).isoformat(timespec="seconds"), site_id))
                c.execute("UPDATE check_runs SET finished_at=?,result='unchanged',http_status=?,duration_ms=? WHERE id=?",
                          (now, page["status_code"], int((time.monotonic()-started)*1000), run_id))
                return {"result": "unchanged", "message": "No content change detected. No duplicate snapshot created."}
            diff = text_diff(previous["text_content"], normalized["text"])
            # Low sensitivity suppresses tiny text changes; high sensitivity accepts all hash changes.
            if site["sensitivity"] == "low" and diff["changed_lines"] <= 2 and diff["similarity"] >= 0.98:
                c.execute("UPDATE sites SET status='ok',last_checked_at=?,last_error=NULL,next_check_at=? WHERE id=?",
                          (now, (datetime.now(timezone.utc)+timedelta(minutes=site["interval_minutes"])).isoformat(timespec="seconds"), site_id))
                c.execute("UPDATE check_runs SET finished_at=?,result='filtered',http_status=?,duration_ms=? WHERE id=?",
                          (now, page["status_code"], int((time.monotonic()-started)*1000), run_id))
                return {"result": "filtered", "message": "A very small change was suppressed by low sensitivity."}
            cur = c.execute("INSERT INTO snapshots(site_id,content_hash,title,text_content,html_content,created_at) VALUES(?,?,?,?,?,?)",
                            (site_id, normalized["hash"], normalized["title"], normalized["text"], normalized["html"], now))
            new_snapshot_id = cur.lastrowid
            summary = summarize_diff(diff, site.get("keywords"))
            event_key = hashlib.sha256(f"{site_id}:{previous['id']}:{new_snapshot_id}:{normalized['hash']}".encode()).hexdigest()
            cur = c.execute("""INSERT OR IGNORE INTO change_events(site_id,old_snapshot_id,new_snapshot_id,summary,diff_json,event_key,created_at)
                              VALUES(?,?,?,?,?,?,?)""",
                            (site_id, previous["id"], new_snapshot_id, summary, json.dumps(diff, ensure_ascii=False), event_key, now))
            event_row = c.execute("SELECT id FROM change_events WHERE event_key=?", (event_key,)).fetchone()
            event_id = event_row["id"]
            c.execute("UPDATE sites SET status='changed',last_checked_at=?,last_error=NULL,next_check_at=? WHERE id=?",
                      (now, (datetime.now(timezone.utc)+timedelta(minutes=site["interval_minutes"])).isoformat(timespec="seconds"), site_id))
            c.execute("UPDATE check_runs SET finished_at=?,result='changed',http_status=?,duration_ms=? WHERE id=?",
                      (now, page["status_code"], int((time.monotonic()-started)*1000), run_id))
        _deliver_notifications(site, event_id, summary, site["url"])
        return {"result": "changed", "message": summary, "event_id": event_id, "snapshot_id": new_snapshot_id, "diff": diff}
    finally:
        lock.release()


def scheduler_loop():
    while not _scheduler_stop.is_set():
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with db.connect() as c:
                rows = c.execute("SELECT id FROM sites WHERE enabled=1 AND (next_check_at IS NULL OR next_check_at<=?) ORDER BY COALESCE(next_check_at,created_at) LIMIT 20", (now,)).fetchall()
            for row in rows:
                if _scheduler_stop.is_set():
                    break
                try:
                    run_check(row["id"])
                except Exception:
                    # One site failure must not kill the scheduler.
                    continue
        except Exception:
            pass
        _scheduler_stop.wait(CHECK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_thread
    db.init_db()
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=scheduler_loop, name="site-scheduler", daemon=True)
    _scheduler_thread.start()
    yield
    _scheduler_stop.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=2)

app = FastAPI(title="Website Change Detector", version="1.0.0", description="Monitor public pages and inspect meaningful changes.", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

@app.get("/health")
def health():
    return {"status": "ok", "scheduler_interval_seconds": CHECK_INTERVAL_SECONDS}

@app.get("/api/sites")
def list_sites():
    with db.connect() as c:
        rows = c.execute("""SELECT s.*, (SELECT COUNT(*) FROM snapshots p WHERE p.site_id=s.id) AS snapshot_count,
          (SELECT COUNT(*) FROM change_events e WHERE e.site_id=s.id) AS change_count,
          (SELECT MAX(created_at) FROM change_events e WHERE e.site_id=s.id) AS last_change_at
          FROM sites s ORDER BY s.created_at DESC""").fetchall()
    return [dict(r) for r in rows]

@app.post("/api/sites", status_code=201)
def create_site(payload: SiteCreate):
    try:
        url = validate_public_url(payload.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db.connect() as c:
        cur = c.execute("""INSERT INTO sites(name,url,interval_minutes,selector,ignore_selectors,keywords,sensitivity,email,telegram_chat_id,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""", (payload.name.strip(), url, payload.interval_minutes, payload.selector or None,
          payload.ignore_selectors or None, payload.keywords or None, payload.sensitivity, payload.email or None,
          payload.telegram_chat_id or None, utcnow()))
        site_id = cur.lastrowid
    return {"id": site_id, "message": "Website added. The first check will create a baseline snapshot."}

@app.patch("/api/sites/{site_id}")
def update_site(site_id: int, payload: SiteUpdate):
    get_site(site_id)
    values = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if not values:
        return {"message": "No changes supplied"}
    if "enabled" in values:
        values["enabled"] = int(values["enabled"])
        if values["enabled"]:
            values["next_check_at"] = None
    cols = ",".join(f"{k}=?" for k in values)
    with db.connect() as c:
        c.execute(f"UPDATE sites SET {cols} WHERE id=?", (*values.values(), site_id))
    return {"message": "Website updated"}

@app.delete("/api/sites/{site_id}")
def delete_site(site_id: int):
    get_site(site_id)
    with db.connect() as c:
        c.execute("DELETE FROM sites WHERE id=?", (site_id,))
    return {"message": "Website and its history deleted"}

@app.post("/api/sites/{site_id}/check")
def check_now(site_id: int):
    get_site(site_id)
    return run_check(site_id)

@app.get("/api/sites/{site_id}/history")
def history(site_id: int):
    get_site(site_id)
    with db.connect() as c:
        snaps = c.execute("SELECT id,content_hash,title,created_at FROM snapshots WHERE site_id=? ORDER BY id DESC LIMIT 100", (site_id,)).fetchall()
        runs = c.execute("SELECT id,started_at,finished_at,result,http_status,error_message,duration_ms FROM check_runs WHERE site_id=? ORDER BY id DESC LIMIT 100", (site_id,)).fetchall()
        events = c.execute("SELECT id,old_snapshot_id,new_snapshot_id,summary,created_at FROM change_events WHERE site_id=? ORDER BY id DESC LIMIT 100", (site_id,)).fetchall()
    return {"snapshots": [dict(x) for x in snaps], "checks": [dict(x) for x in runs], "events": [dict(x) for x in events]}

@app.get("/api/sites/{site_id}/events")
def site_events(site_id: int):
    get_site(site_id)
    with db.connect() as c:
        rows = c.execute("""SELECT e.*, o.text_content AS old_text, n.text_content AS new_text,
          o.created_at AS old_created_at,n.created_at AS new_created_at
          FROM change_events e LEFT JOIN snapshots o ON o.id=e.old_snapshot_id
          JOIN snapshots n ON n.id=e.new_snapshot_id WHERE e.site_id=? ORDER BY e.id DESC LIMIT 100""", (site_id,)).fetchall()
    output=[]
    for row in rows:
        item=dict(row); item["diff"]=json.loads(item.pop("diff_json")); output.append(item)
    return output

@app.get("/api/events")
def all_events():
    with db.connect() as c:
        rows = c.execute("SELECT e.id,e.site_id,s.name AS site_name,s.url,e.summary,e.created_at FROM change_events e JOIN sites s ON s.id=e.site_id ORDER BY e.id DESC LIMIT 50").fetchall()
    return [dict(r) for r in rows]

@app.get("/api/notifications")
def all_notifications():
    with db.connect() as c:
        rows = c.execute("""SELECT n.*,e.site_id,s.name AS site_name FROM notifications n JOIN change_events e ON e.id=n.event_id
          JOIN sites s ON s.id=e.site_id ORDER BY n.id DESC LIMIT 100""").fetchall()
    return [dict(r) for r in rows]

# Deterministic demo endpoints. They are deliberately local to this application.
_demo_state = {"deadline": "10 November", "price": "₹999", "visitors": 105, "banner": "Welcome to the demo portal", "revision": 1}
_demo_lock = threading.Lock()

@app.get("/demo/page", response_class=HTMLResponse)
def demo_page():
    with _demo_lock:
        state = dict(_demo_state)
    return HTMLResponse(f"""<!doctype html><html><head><title>Demo Scholarship Portal</title><style>body{{font-family:Arial;margin:3rem;max-width:760px}}.banner{{padding:1rem;background:#edf2ff}}.card{{padding:1rem;border:1px solid #ddd;margin-top:1rem}}</style></head><body>
    <h1>Demo Scholarship Portal</h1><div class='banner'>{html.escape(state['banner'])}</div>
    <section id='announcements' class='card'><h2>Important announcement</h2><p>Application deadline: <strong>{html.escape(state['deadline'])}</strong></p><p>Application fee: <strong>{html.escape(state['price'])}</strong></p></section>
    <section id='dynamic' class='card'><p>Visitors today: {state['visitors']}</p><p>Revision: {state['revision']}</p></section>
    <p><button>Apply now</button></p></body></html>""")

@app.post("/demo/change")
def change_demo():
    with _demo_lock:
        _demo_state["deadline"] = "15 November" if _demo_state["deadline"] == "10 November" else "10 November"
        _demo_state["price"] = "₹1,099" if _demo_state["price"] == "₹999" else "₹999"
        _demo_state["visitors"] += 1
        _demo_state["banner"] = "Updated demo announcement" if _demo_state["revision"] % 2 else "Welcome to the demo portal"
        _demo_state["revision"] += 1
        return dict(_demo_state)

@app.post("/demo/reset")
def reset_demo():
    with _demo_lock:
        _demo_state.update({"deadline": "10 November", "price": "₹999", "visitors": 105, "banner": "Welcome to the demo portal", "revision": 1})
        return {"message": "Demo page reset", **_demo_state}

@app.get("/api/sites/{site_id}/snapshots/{snapshot_id}")
def snapshot_detail(site_id: int, snapshot_id: int):
    get_site(site_id)
    with db.connect() as c:
        row = c.execute("SELECT * FROM snapshots WHERE id=? AND site_id=?", (snapshot_id, site_id)).fetchone()
    if not row:
        raise HTTPException(404, "Snapshot not found")
    return dict(row)
