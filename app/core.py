"""Core monitoring logic for Website Change Detector."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from datetime import datetime, timezone
from difflib import HtmlDiff, SequenceMatcher, unified_diff
from email.message import EmailMessage
import smtplib
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = os.getenv("USER_AGENT", "WebsiteChangeDetector/1.0 (respectful public-page monitoring)")
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_public_url(url: str) -> str:
    """Basic SSRF guard: allow public HTTP(S) URLs only; reject private/local IPs."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    # Explicit, narrow exception for the built-in local demo page. No other loopback
    # path is permitted, which keeps the demo testable without opening a general SSRF hole.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    is_loopback_demo = bool(ip and ip.is_loopback and parsed.path == "/demo/page" and parsed.port in (None, 8000))
    if is_loopback_demo:
        return url
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".internal"):
        raise ValueError("Local and internal hostnames are not allowed (except the built-in demo page)")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            addresses = {ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos}
        except socket.gaierror:
            # DNS may be temporarily unavailable; actual fetch will report the error.
            addresses = set()
    for address in addresses:
        if not address.is_global:
            raise ValueError("Private, loopback, link-local and reserved IP addresses are not allowed")
    return url


def normalize_page(html: str, selector: str | None = None, ignore_selectors: str | None = None) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if soup.head:
        soup.head.decompose()
    for node in soup.select("script, style, noscript, template, svg, canvas"):
        node.decompose()
    if ignore_selectors:
        for selector_item in [s.strip() for s in ignore_selectors.split(",") if s.strip()]:
            try:
                for node in soup.select(selector_item):
                    node.decompose()
            except Exception:
                # One malformed optional ignore selector must not break monitoring.
                continue
    selected = soup
    if selector:
        try:
            match = soup.select_one(selector)
        except Exception as exc:
            raise ValueError(f"Invalid CSS selector: {exc}") from exc
        if match is None:
            raise ValueError("The configured CSS selector did not match any element")
        selected = match
    text = "\n".join(line.strip() for line in selected.get_text("\n").splitlines() if line.strip())
    # Collapse repeated blank lines/whitespace without deleting meaningful numbers or dates.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    canonical = json.dumps({"text": text}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"text": text, "hash": digest, "title": title, "html": str(selected)}


def text_diff(old: str, new: str) -> dict:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    unified = "\n".join(unified_diff(old_lines, new_lines, fromfile="Previous version", tofile="Latest version", lineterm=""))
    matcher = SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({"type": tag, "old": "\n".join(old_lines[i1:i2]), "new": "\n".join(new_lines[j1:j2])})
    ratio = matcher.ratio()
    return {"unified": unified, "changes": changes, "similarity": round(ratio, 4), "changed_lines": sum(len(c["old"].splitlines()) + len(c["new"].splitlines()) for c in changes)}


def summarize_diff(diff: dict, keywords: str | None = None) -> str:
    changes = diff.get("changes", [])
    if not changes:
        return "No text differences detected."
    added = sum(len(c["new"].splitlines()) for c in changes if c["type"] in ("insert", "replace"))
    removed = sum(len(c["old"].splitlines()) for c in changes if c["type"] in ("delete", "replace"))
    fragments = []
    for change in changes[:3]:
        candidate = (change.get("new") or change.get("old") or "").replace("\n", " ").strip()
        if candidate:
            fragments.append(candidate[:120])
    summary = f"Detected {added} added/updated and {removed} removed/updated text line(s)."
    if fragments:
        summary += " Key differences: " + " | ".join(fragments)
    terms = [x.strip().lower() for x in (keywords or "").split(",") if x.strip()]
    if terms:
        joined = " ".join([c.get("old", "") + " " + c.get("new", "") for c in changes]).lower()
        matches = [term for term in terms if term in joined]
        summary += " Keyword match: " + (", ".join(matches) if matches else "none of your configured keywords") + "."
    return summary


def fetch_page(url: str) -> dict:
    """Fetch a public page. Redirects are disabled to avoid SSRF through redirects."""
    validate_public_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers=headers) as client:
        response = client.get(url)
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        raise RuntimeError(f"Redirect detected (HTTP {response.status_code}). Add the final public URL directly. Redirect target: {location[:180]}")
    if response.status_code in (401, 403, 407, 429, 503):
        raise RuntimeError(f"Access blocked or service unavailable (HTTP {response.status_code}); no snapshot was saved.")
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if content_type and not any(t in content_type.lower() for t in ("text/html", "text/plain", "application/xhtml+xml")):
        raise RuntimeError(f"Unsupported content type: {content_type}")
    if len(response.content) > 5_000_000:
        raise RuntimeError("Page response is larger than the 5 MB safety limit")
    return {"html": response.text, "status_code": response.status_code, "final_url": str(response.url)}


def send_telegram(message: str, chat_id: str) -> tuple[bool, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id:
        return False, "Telegram bot token or chat ID is not configured"
    try:
        response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": message[:4000]}, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            return False, "Telegram API did not confirm delivery"
        return True, "sent"
    except Exception as exc:
        return False, str(exc)[:300]


def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    host = os.getenv("SMTP_HOST", "").strip()
    if not host or not to_email:
        return False, "SMTP settings or recipient email are not configured"
    try:
        message = EmailMessage()
        message["Subject"], message["From"], message["To"] = subject, os.getenv("SMTP_FROM") or os.getenv("SMTP_USERNAME", ""), to_email
        message.set_content(body)
        port = int(os.getenv("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=10) as server:
            if os.getenv("SMTP_STARTTLS", "true").lower() == "true":
                server.starttls()
            username, password = os.getenv("SMTP_USERNAME", ""), os.getenv("SMTP_PASSWORD", "")
            if username:
                server.login(username, password)
            server.send_message(message)
        return True, "sent"
    except Exception as exc:
        return False, str(exc)[:300]
