"""SQLite persistence layer. Uses short-lived connections for thread safety."""
import os, sqlite3
from pathlib import Path

DB_PATH = os.getenv("DATABASE_PATH", "./data/monitor.db")


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sites (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          url TEXT NOT NULL,
          interval_minutes INTEGER NOT NULL DEFAULT 60,
          selector TEXT,
          ignore_selectors TEXT,
          keywords TEXT,
          sensitivity TEXT NOT NULL DEFAULT 'medium',
          email TEXT,
          telegram_chat_id TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'pending',
          last_checked_at TEXT,
          next_check_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS check_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          result TEXT NOT NULL,
          http_status INTEGER,
          error_message TEXT,
          duration_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
          content_hash TEXT NOT NULL,
          title TEXT,
          text_content TEXT NOT NULL,
          html_content TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(site_id, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_site_created ON snapshots(site_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS change_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
          old_snapshot_id INTEGER REFERENCES snapshots(id),
          new_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
          summary TEXT NOT NULL,
          diff_json TEXT NOT NULL,
          event_key TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id INTEGER NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
          channel TEXT NOT NULL,
          destination TEXT,
          status TEXT NOT NULL,
          detail TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          UNIQUE(event_id, channel, destination)
        );
        """)
