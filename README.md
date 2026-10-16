# Watchtower — Website Change Detector

A runnable full-stack MVP for monitoring public webpages, detecting meaningful text changes, saving version history, and delivering optional email/Telegram alerts. It includes a polished dashboard and a built-in demo page whose content you can change on demand.

## What is implemented

- Add, update, pause/resume, manually check, and delete monitored websites.
- Persistent SQLite storage for monitored sites, check runs, snapshots, change events, and notification attempts.
- Background scheduler that checks enabled sites when due; each site has a lock to prevent overlapping checks.
- HTTP fetching with timeouts, content-type and response-size limits, URL validation, and explicit handling for redirects/access blocks.
- Text extraction and normalization; CSS selector targeting; comma-separated ignored selectors.
- SHA-256 content fingerprints; new snapshots are saved only when normalized content changes.
- Text diffs, event summaries, keyword callouts, sensitivity filtering, and duplicate event/notification protection.
- Status tracking for healthy, changed, error, and blocked pages.
- Dashboard, monitored-sites list, change history, version timeline, and notification delivery log.
- Optional SMTP email and Telegram bot notifications.
- Built-in demo page and change/reset controls.
- Unit tests for normalization, selectors, diffs, and URL checks.

## Current limitations (read before deployment)

- The default browser fetcher uses HTTPX, not a full browser renderer. Some JavaScript-heavy websites will need the optional Playwright browser worker.
- Pixel-level screenshot comparison, visual region picker, RSS feeds, browser extension, AI-generated summaries, authentication, multi-user accounts, and production-grade distributed queues are not included in this MVP. The architecture is structured so these can be added, but they are not falsely presented as finished.
- Low sensitivity filters very small text diffs; medium and high currently accept changed normalized content. Selector targeting is the best way to remove page-specific noise.
- Email and Telegram delivery require your own credentials. No secrets are included.
- SQLite is appropriate for a local hackathon/demo. For a multi-instance deployment, use PostgreSQL and a durable queue such as Redis/Celery or a managed equivalent.
- The app has no authentication in this starter. Do not expose it publicly with sensitive data until authentication, authorization, CSRF protections and production security hardening are added.
- URL validation blocks local/private addresses and redirects, with one narrow exception for the built-in demo page at `http://127.0.0.1:8000/demo/page`. DNS rebinding and complex network environments require additional egress controls in production.

## Requirements

- Python 3.10 or newer
- Internet access for monitoring public sites
- macOS, Linux, or Windows

## Run on macOS (recommended steps)

Open Terminal and move into the unzipped project directory:

```bash
cd website-change-detector
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Then open **http://127.0.0.1:8000** in your browser.

The database is created automatically at `data/monitor.db`. Leave the terminal running while the scheduler is active. To stop the app, press `Control+C`.

> If `python3` on your Mac is a version with dependency issues, use a compatible installed Python such as `python3.12` for the virtual environment without changing your system default:
> `python3.12 -m venv .venv`

## Run tests

In a second terminal, from the project directory with the virtual environment activated:

```bash
pytest -q
```

## Try the complete demo

1. Open the dashboard.
2. Click **Try the live demo**. The app adds its own demo page to the watchlist.
3. The first check saves a baseline. The built-in demo URL is `http://127.0.0.1:8000/demo/page`.
4. In a second browser tab, open **http://127.0.0.1:8000/demo/page** to inspect the page.
5. Change the demo page by running this in Terminal:

   ```bash
   curl -X POST http://127.0.0.1:8000/demo/change
   ```

6. Return to the dashboard and click **Check now** on the Demo Scholarship Portal. The deadline and price change, and the detector creates a version and event.
7. Click the event to inspect the text diff.
8. Reset the demo page when you want to repeat the experiment:

   ```bash
   curl -X POST http://127.0.0.1:8000/demo/reset
   ```

The scheduler's next automatic check is controlled by the site's interval and `CHECK_INTERVAL_SECONDS`; manual check is available for a fast demo. First check = baseline, so make the demo change after the baseline exists.

## Configure notifications

### Email

Fill in `.env` values for `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, and `SMTP_STARTTLS`. Restart the app. Add an email destination to a monitored site. Use a mail provider's app password or approved SMTP credentials; never commit secrets.

### Telegram

Create a bot with Telegram's official BotFather, set `TELEGRAM_BOT_TOKEN` in `.env`, and add your Telegram chat ID to the monitored site. The bot generally must be able to message the destination (for a private chat, start the bot first). Restart the app after editing `.env`.

If no destination or credentials are configured, the app will not attempt delivery. The dashboard's Notifications page shows actual attempts only.

## API quick reference

- `GET /health` — health status
- `GET /api/sites` — list monitored sites
- `POST /api/sites` — create a site
- `PATCH /api/sites/{id}` — update settings / pause or resume
- `DELETE /api/sites/{id}` — delete a site and its history
- `POST /api/sites/{id}/check` — run a check now
- `GET /api/sites/{id}/history` — snapshots and check runs
- `GET /api/sites/{id}/events` — event details and diffs
- `GET /api/events` — recent events
- `GET /api/notifications` — notification attempts
- `GET /demo/page` — built-in test page
- `POST /demo/change` — mutate demo content
- `POST /demo/reset` — reset demo content
- `GET /docs` — interactive OpenAPI docs

Example add-site request:

```bash
curl -X POST http://127.0.0.1:8000/api/sites \
  -H 'Content-Type: application/json' \
  -d '{"name":"Demo Scholarship Portal","url":"http://127.0.0.1:8000/demo/page","interval_minutes":15,"selector":"#announcements","ignore_selectors":"#dynamic","keywords":"deadline, application, fee, price","sensitivity":"high"}'
```

## Configuration

Copy `.env.example` to `.env` and edit it. The app loads `.env` automatically at startup via `python-dotenv`. Restart the server after changing settings.

Important settings:

- `DATABASE_PATH` — SQLite file path
- `CHECK_INTERVAL_SECONDS` — scheduler polling frequency (minimum 5 seconds)
- `REQUEST_TIMEOUT_SECONDS` — HTTP request timeout
- `USER_AGENT` — HTTP user agent
- SMTP and Telegram values — optional notification delivery

## Suggested next steps for production

1. Add authentication and per-user authorization before public deployment.
2. Add Playwright-based rendering and screenshot capture for JavaScript-heavy sites.
3. Add screenshot pixel-diff storage and visual region selection.
4. Replace SQLite and in-process scheduling with PostgreSQL plus a durable distributed queue.
5. Add robust DNS/IP egress enforcement, redirect revalidation, per-host rate limiting, robots-policy handling, and audit logs.
6. Add retention limits, object storage, backups, metrics, structured logging and deployment health checks.
7. Add AI summaries only after the deterministic diff is trustworthy; ground summaries in the exact observed diff.

## Project structure

```text
website-change-detector/
├── app/
│   ├── core.py            # Fetch, normalize, hash, diff, notification helpers
│   ├── db.py              # SQLite schema and connections
│   ├── main.py            # API, scheduler, demo page
│   └── static/
│       ├── index.html     # Dashboard markup
│       ├── style.css      # Responsive styling
│       └── app.js         # Dashboard interactions
├── data/                  # Database is created here at runtime
├── tests/test_core.py
├── .env.example
├── requirements.txt
└── README.md
```
