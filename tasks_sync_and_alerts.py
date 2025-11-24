#!/usr/bin/env python3
"""
BingeBuddy: scheduled sync + conditional Sunday alerts.

- Runs any time it's invoked (GitHub Actions every 11 hours).
- Always syncs all shows from TMDB into the DB.
- Only sends alerts if current time in America/Denver is Sunday 13:00–23:59.

Secrets expected (provided via GitHub Actions 'env'):
  ENVIRONMENT
  SQLITE_CLOUD_URL | (SQLITE_USER, SQLITE_PASSWORD, SQLITE_DB, SQLITE_PORT)   # one of these patterns
  DEFAULT_API_KEY
  SMTP_HOST, SMTP_PORT, SMTP_USE_TLS, SMTP_USER, SMTP_PASS
  ALERT_SMS_CARRIER, ALERT_SMS_TO    # optional for email-to-SMS
"""

#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, time, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from typing import List, Optional, Sequence, Tuple

from zoneinfo import ZoneInfo
import sqlitecloud  # type: ignore

# Silence Streamlit cache warnings if app modules import it
logging.getLogger("streamlit.runtime.caching.cache_data_api").setLevel(logging.ERROR)

# Import your existing sync function
from pyBingeBuddy import sync_show_from_tmdb  # type: ignore

DENVER_TZ = ZoneInfo("America/Denver")

# ---------- Logging setup ----------
def setup_logging() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    log_path = os.getenv("LOG_FILE", "logs/bingebuddy_tasks.log")

    logger = logging.getLogger("bingebuddy")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file: 1MB x 5
    fh = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=5)
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


log = setup_logging()


# ---------- Env helpers ----------
def getenv_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def get_env_str(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        if required and default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    return str(val).strip()


def build_sqlitecloud_url() -> str:
    url = get_env_str("SQLITE_CLOUD_URL")
    if url:
        return url

    # Fallback build (adjust if you later add SQLITE_HOST separately)
    user = get_env_str("SQLITE_USER", required=True)
    password = get_env_str("SQLITE_PASSWORD", required=True)
    host_or_db = get_env_str("SQLITE_DB", required=True)
    port = get_env_str("SQLITE_PORT", default="8860")

    if "/" in host_or_db:
        host, db = host_or_db.split("/", 1)
    else:
        host, db = host_or_db, host_or_db

    return f"sqlitecloud://{user}:{password}@{host}:{port}/{db}"


# ---------- DB helpers ----------
def get_conn() -> sqlitecloud.Connection:
    url = build_sqlitecloud_url()
    return sqlitecloud.connect(url)


def all_tmdb_ids_and_names(conn: sqlitecloud.Connection) -> List[Tuple[int, str]]:
    cur = conn.execute("SELECT tmdb_id, COALESCE(name, '') FROM shows WHERE tmdb_id IS NOT NULL")
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def all_users(conn: sqlitecloud.Connection) -> List[Tuple[int, str]]:
    cur = conn.execute("SELECT id, COALESCE(email, '') FROM users")
    return [(int(r[0]), str(r[1])) for r in cur.fetchall()]


def upcoming_for_user(
    conn: sqlitecloud.Connection,
    user_id: int,
    start_date: str,
    end_date: str,
) -> List[Tuple[str, int, int, str, str]]:
    sql = """
    SELECT s.name AS show_name,
           e.season_number,
           e.episode_number,
           COALESCE(e.name, '') AS ep_name,
           COALESCE(e.air_date, '') AS air_date
    FROM episodes e
    JOIN shows s
      ON s.id = e.show_id
    JOIN user_shows us
      ON us.show_id = e.show_id
     AND us.user_id = ?
    LEFT JOIN watches w
      ON w.user_id = ?
     AND w.episode_id = e.id
    WHERE w.episode_id IS NULL
      AND e.air_date IS NOT NULL
      AND date(e.air_date) >= date(?)
      AND date(e.air_date) <= date(?)
    ORDER BY e.air_date, s.name, e.season_number, e.episode_number
    """
    cur = conn.execute(sql, (user_id, user_id, start_date, end_date))
    return [(r[0], int(r[1]), int(r[2]), r[3], r[4]) for r in cur.fetchall()]


# ---------- TMDB sync ----------
def sync_all_shows(conn: sqlitecloud.Connection, api_key: str) -> None:
    shows = all_tmdb_ids_and_names(conn)
    total = len(shows)
    if total == 0:
        log.info("[SYNC] No shows to sync.")
        return

    log.info(f"[SYNC] Starting sync of {total} shows")
    started = datetime.now()

    for i, (tmdb_id, name) in enumerate(shows, start=1):
        t0 = datetime.now()
        try:
            log.info(f"[SYNC] ({i}/{total}) TMDB {tmdb_id} — {name} ...")
            sync_show_from_tmdb(conn, tmdb_id, api_key)
            dt = (datetime.now() - t0).total_seconds()
            log.info(f"[SYNC] ({i}/{total}) ✓ {name} (TMDB {tmdb_id}) in {dt:.2f}s")
        except Exception as exc:  # noqa: BLE001
            dt = (datetime.now() - t0).total_seconds()
            log.warning(f"[SYNC] ({i}/{total}) ✗ {name} (TMDB {tmdb_id}) failed in {dt:.2f}s: {exc}")

    total_dt = (datetime.now() - started).total_seconds()
    log.info(f"[SYNC] Completed sync of {total} shows in {total_dt:.2f}s")


# ---------- Alert window ----------
def is_alert_window_denver(now_utc: Optional[datetime] = None) -> bool:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    denver = now_utc.astimezone(DENVER_TZ)
    if denver.weekday() != 6:  # Sunday
        return False
    start = time(13, 0, 0)      # 1:00 PM
    end   = time(23, 59, 59)    # 11:59 PM
    return start <= denver.time() <= end


def denver_today_and_horizon(days: int = 7) -> Tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    denver = now_utc.astimezone(DENVER_TZ)
    start = denver.date()
    end = (denver + timedelta(days=days)).date()
    return start.isoformat(), end.isoformat()


# ---------- Email / SMS ----------
CARRIER_GATEWAYS = {
    "att": "{number}@txt.att.net",
    "att_mms": "{number}@mms.att.net",
    "verizon": "{number}@vtext.com",
    "verizon_mms": "{number}@vzwpix.com",
    "tmobile": "{number}@tmomail.net",
    "sprint": "{number}@messaging.sprintpcs.com",
    "googlefi": "{number}@msg.fi.google.com",
    "xfinity": "{number}@vtext.com",
}


def _smtp_client() -> smtplib.SMTP:
    host = get_env_str("SMTP_HOST", required=True)
    port = int(get_env_str("SMTP_PORT", default="587"))
    use_tls = getenv_bool("SMTP_USE_TLS", True)
    user = get_env_str("SMTP_USER", required=True)
    password = get_env_str("SMTP_PASS", required=True)

    server = smtplib.SMTP(host, port, timeout=20)
    server.ehlo()
    if use_tls:
        server.starttls()
        server.ehlo()
    server.login(user, password)
    return server


def _send_email(to_addrs: Sequence[str], subject: str, text_body: str, html_body: Optional[str] = None) -> None:
    user = get_env_str("SMTP_USER", required=True)
    msg = MIMEMultipart("alternative")
    msg["From"] = user
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject

    msg.attach(MIMEText(text_body, "plain"))
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    with _smtp_client() as smtp:
        smtp.sendmail(user, list(to_addrs), msg.as_string())


def _sms_email_address() -> Optional[str]:
    number = get_env_str("ALERT_SMS_TO")
    carrier = (get_env_str("ALERT_SMS_CARRIER") or "").lower()
    if not number or not carrier:
        return None
    template = CARRIER_GATEWAYS.get(carrier)
    if not template:
        return None
    digits = "".join(ch for ch in number if ch.isdigit())
    return template.format(number=digits)


def send_alert_bundle(email: str, items: List[Tuple[str, int, int, str, str]]) -> None:
    if not items:
        return

    lines = [f"- {show} S{sn}E{en} “{ep or ''}” · {air}" for (show, sn, en, ep, air) in items]
    text = "Upcoming episodes (next 7 days):\n" + "\n".join(lines)
    html = (
        "<p><b>Upcoming episodes (next 7 days)</b></p><ul>"
        + "".join(
            f"<li>{show} S{sn}E{en} — <i>{(ep or '').replace('<','&lt;')}</i> · {air}</li>"
            for (show, sn, en, ep, air) in items
        )
        + "</ul>"
    )

    recipients: List[str] = []
    if email:
        recipients.append(email)

    sms_addr = _sms_email_address()
    if sms_addr:
        short = "; ".join(f"{show} S{sn}E{en} {air}" for (show, sn, en, _, air) in items)
        try:
            _send_email([sms_addr], "BingeBuddy", short)
            log.info(f"[ALERT] SMS to {sms_addr} ({len(items)} items)")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ALERT] SMS failed to {sms_addr}: {exc}")

    if recipients:
        try:
            _send_email(recipients, "Your BingeBuddy weekly lineup", text, html)
            log.info(f"[ALERT] Email to {recipients[0]} ({len(items)} items)")
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ALERT] Email failed to {recipients}: {exc}")


# ---------- Main ----------
def main() -> None:
    start_run = datetime.now()
    log.info("=== BingeBuddy scheduled task start ===")

    api_key = get_env_str("DEFAULT_API_KEY", required=True)

    with get_conn() as conn:
        # Always sync
        sync_all_shows(conn, api_key)

        # Alert window
        if is_alert_window_denver():
            start, end = denver_today_and_horizon(days=7)
            log.info(f"[ALERT] Window active (Denver local): {start} → {end}")

            for user_id, email in all_users(conn):
                try:
                    items = upcoming_for_user(conn, user_id, start, end)
                    if items:
                        log.info(f"[ALERT] User {user_id} <{email or 'no-email'}> — {len(items)} episode(s)")
                        # Also list the items (concise) so you can see exactly what went out
                        for show, sn, en, ep, air in items:
                            log.info(f"[ALERT]   • {show} S{sn}E{en} — {ep or ''} · {air}")
                        send_alert_bundle(email, items)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"[ALERT] Skipped user {user_id} <{email}> due to error: {exc}")
        else:
            log.info("[ALERT] Outside Sunday 13:00–23:59 Denver window; skipping alerts.")

    total = (datetime.now() - start_run).total_seconds()
    log.info(f"=== BingeBuddy scheduled task end (took {total:.2f}s) ===")


if __name__ == "__main__":
    main()
