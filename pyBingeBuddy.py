import os
from dotenv import load_dotenv
import sqlitecloud
import requests
import datetime
import smtplib
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any, cast
import bcrypt
import streamlit as st
from urllib.parse import urlparse, urlunparse
import re

# Optional SMS
# try:
#     from twilio.rest import Client as TwilioClient
# except ImportError:
#     TwilioClient = None  # Not installed or not used

# ----------------------------
# Config
# ----------------------------
APP_DB = "shows.db"
DEBUG_ON = False  # True
TMDB_BASE = "https://api.themoviedb.org/3"

# Try .env (local dev) then Streamlit secrets
try:
    load_dotenv()
except ImportError:
    pass


def _get_secret(key: str, default: str = "") -> str:
    # tolerate missing st.secrets during local runs
    try:
        return os.getenv(key) or st.secrets.get(key, default)
    except ImportError:
        return default


# environment debug
ENVIRON = _get_secret("ENVIRONMENT")
# TMDB env
DEFAULT_API_KEY = _get_secret("DEFAULT_API_KEY")
# Email env
SMTP_HOST = _get_secret("SMTP_HOST")
SMTP_PORT = int(_get_secret("SMTP_PORT", "587"))
SMTP_USER = _get_secret("SMTP_USER")
SMTP_PASS = _get_secret("SMTP_PASS")
ALERT_EMAIL_TO_DEFAULT = _get_secret("ALERT_EMAIL_TO")

# SMS env
ALERT_SMS_TO_DEFAULT = _get_secret("ALERT_SMS_TO")
ALERT_SMS_CARRIER_DEFAULT = _get_secret("ALERT_SMS_CARRIER")

CARRIER_GATEWAYS = {
    "att": "@txt.att.net",
    "verizon": "@vtext.com",
    "tmobile": "@tmomail.net",
    "sprint": "@messaging.sprintpcs.com",
    # You can expand with more carriers
}

# sqlitecloud env
sc_url = _get_secret("SQLITE_CLOUD_URL")
sc_dbname = _get_secret("SQLITE_DB")
sql_api_key = _get_secret("SQLITE_API_KEY")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def track_show_for_user(conn, show_id: int):
    user_id = st.session_state["user_id"]
    conn.execute(
        "INSERT OR IGNORE INTO user_shows (user_id, show_id) VALUES (?, ?)",
        (user_id, show_id),
    )
    conn.commit()


def get_user_shows(conn):
    user_id = st.session_state["user_id"]
    cur = conn.execute(
        "SELECT s.id, s.name, s.status, s.next_air_date "
        "FROM shows s "
        "JOIN user_shows us ON s.id = us.show_id "
        "WHERE us.user_id = ?",
        (user_id,)
    )
    return cur.fetchall()


def login_screen(conn):
    st.title("🔐 Login")
    st.write("Environment: ", ENVIRON)
    tab_login, tab_signup = st.tabs(["Login", "Create Account"])

    with tab_login:
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            cur = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,))
            row = cur.fetchone()
            if row and check_password(password, row[1]):
                st.session_state["user_id"] = row[0]
                st.session_state["user_email"] = email
                st.success("Login successful!")
                # Run sync right after login
                updated_count, email_count, sms_count = sync_show_updates(conn, st.session_state["user_id"],
                                                                          sql_api_key, DEFAULT_API_KEY)
                st.info(f"Synced {updated_count} shows • {email_count} emails • {sms_count} SMS")

                st.rerun()
            else:
                st.error("Invalid email or password")

    with tab_signup:
        email = st.text_input("New Email", key="signup_email")
        password = st.text_input("New Password", type="password", key="signup_password")
        phone = st.text_input("Phone (optional)")
        carrier = st.selectbox("Carrier", ["", "AT&T", "Verizon", "T-Mobile", "Sprint"])
        enable_email = st.checkbox("Enable Email Alerts", value=True)
        enable_sms = st.checkbox("Enable SMS Alerts")

        if st.button("Create Account"):
            try:
                conn.execute(
                    "INSERT INTO users (email, phone, carrier, enable_email, enable_sms, password_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (email, phone, carrier, int(enable_email), int(enable_sms), hash_password(password)),
                )
                conn.commit()
                st.success("Account created! Please login.")
            except Exception as e:
                st.error(f"Error creating account: {e}")


def logout():
    """Close DB connection (if any), clear session, and return to login."""
    conn = st.session_state.pop("conn", None)
    try:
        if conn and hasattr(conn, "close"):
            conn.close()
    except Exception:
        # Keep the UI clean; optionally log
        pass

    # Clear everything; if you want to keep some keys (e.g. api_key), remove them from this loop
    st.session_state.clear()
    st.rerun()


def mask(key: str) -> str:
    return key[:4] + "..." + key[-4:]


def validate_sqlite_url(url: str) -> tuple[bool, str]:
    """
    Expecting: sqlitecloud://HOST[:8860]/DBNAME?apikey=KEY
    """
    if not isinstance(url, str) or not url:
        return False, "URL is empty or not a string"
    if not url.startswith("sqlitecloud://"):
        return False, "URL must start with sqlitecloud://"
    m = re.match(r"^sqlitecloud://([^/]+)/([^?]+)\?apikey=([^&]+)$", url)
    if not m:
        return False, "URL format looks off (host/dbname/apikey)"
    host, db, key = m.groups()
    if not host or "." not in host:
        return False, "Host looks invalid"
    if not db:
        return False, "Database name missing"
    if len(key) < 12:
        return False, "API key looks too short"
    return True, f"Host={host}, DB={db}, Key={mask(key)}"


def get_validated_conn(url: str) -> sqlitecloud.Connection | None:
    is_valid, message = validate_sqlite_url(url)
    if not is_valid:
        print(f"Validation failed: {message}")
        return None
    try:
        conn = sqlitecloud.connect(url)
        print(f"Connected successfully: {message}")
        return conn
    except Exception as e:
        print(f"Connection error: {e}")
        return None


# ----------------------------
# TMDB API
# ----------------------------
def tmdb_headers(api_key: str) -> Dict[str, str]:
    # v4 bearer token tends to be > 40 chars
    return {"Authorization": f"Bearer {api_key}"} if len(api_key) > 40 else {}


def tmdb_params(api_key: str) -> Dict[str, str]:
    # Support v3 key if user provides the short one
    return {} if len(api_key) > 40 else {"api_key": api_key}


@st.cache_data(show_spinner=False, ttl=3600)
def tmdb_search_tv(query: str, api_key: str) -> List[Dict[str, Any]]:
    if not query.strip():
        return []
    url = f"{TMDB_BASE}/search/tv"
    r = requests.get(url, headers=tmdb_headers(api_key), params={**tmdb_params(api_key), "query": query})
    r.raise_for_status()
    return r.json().get("results", [])


@st.cache_data(show_spinner=False, ttl=86400)
def tmdb_tv_genres(api_key: str) -> Dict[int, str]:
    url = f"{TMDB_BASE}/genre/tv/list"
    r = requests.get(url, headers=tmdb_headers(api_key), params=tmdb_params(api_key))
    r.raise_for_status()
    genres = r.json().get("genres", [])
    return {g["id"]: g["name"] for g in genres}


@st.cache_data(show_spinner=False, ttl=3600)
def tmdb_discover_tv(api_key: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Thin wrapper over /discover/tv.
    Accepts TMDB params like:
      first_air_date.gte, first_air_date.lte, with_original_language,
      with_genres (comma IDs), sort_by, 'with_watch_providers', 'watch_region', 'vote_average.gte'
    """
    url = f"{TMDB_BASE}/discover/tv"
    r = requests.get(url, headers=tmdb_headers(api_key), params={**tmdb_params(api_key), **params})
    r.raise_for_status()
    return r.json()


@st.cache_data(show_spinner=False, ttl=3600)
def tmdb_tv_details(tmdb_id: int, api_key: str) -> Dict[str, Any]:
    url = f"{TMDB_BASE}/tv/{tmdb_id}"
    try:
        r = requests.get(url, headers=tmdb_headers(api_key), params=tmdb_params(api_key))
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"TMDB API error for show {tmdb_id}: {e}")
        return {}


@st.cache_data(show_spinner=False, ttl=3600)
def tmdb_season_details(tmdb_id: int, season_number: int, api_key: str) -> Dict[str, Any]:
    url = f"{TMDB_BASE}/tv/{tmdb_id}/season/{season_number}"
    r = requests.get(url, headers=tmdb_headers(api_key), params=tmdb_params(api_key))
    r.raise_for_status()
    return r.json()


# ----------------------------
# Sync: TMDB -> SQLite
# ----------------------------
def upsert_show(conn: sqlitecloud.Connection, details: Dict[str, Any]) -> int:
    next_air_date = {}
    if details.get("next_episode_to_air"):
        next_air_date = details["next_episode_to_air"].get("air_date")

    # 👇 Explicitly tell the type checker this is a generic tuple of Any
    us_params: tuple[Any, ...] = (
        details["id"],
        details.get("name") or details.get("original_name") or "Unknown",
        details.get("status"),
        next_air_date,
        details.get("overview"),
        details.get("poster_path"),
        details.get("first_air_date"),
        details.get("last_air_date"),
    )

    us_sql = """
        INSERT INTO shows (tmdb_id, name, status, next_air_date, overview, poster_path, first_air_date, last_air_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tmdb_id) DO UPDATE SET
            name=excluded.name,
            status=excluded.status,
            next_air_date=excluded.next_air_date,
            overview=excluded.overview,
            poster_path=excluded.poster_path,
            first_air_date=excluded.first_air_date,
            last_air_date=excluded.last_air_date
        """

    conn.execute(us_sql, cast(tuple[Any], us_params))
    conn.commit()

    cur = conn.execute("SELECT id FROM shows WHERE tmdb_id = ?", (details["id"],))
    row = cur.fetchone()
    return row[0]


def upsert_season(conn: sqlitecloud.Connection, show_id: int, season_obj: Dict[str, Any]) -> int:
    useason_sql = """
    INSERT INTO seasons (show_id, season_number, name, air_date, episode_count)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(show_id, season_number) DO UPDATE SET
        name=excluded.name,
        air_date=excluded.air_date,
        episode_count=excluded.episode_count
    """
    useason_params: tuple[Any, ...] = (
        show_id,
        season_obj.get("season_number"),
        season_obj.get("name"),
        season_obj.get("air_date"),
        season_obj.get("episode_count"),
    )

    conn.execute(useason_sql, cast(tuple[Any], useason_params))
    conn.commit()
    cur = conn.execute(
        "SELECT id FROM seasons WHERE show_id=? AND season_number=?",
        cast(tuple[Any], (show_id, season_obj.get("season_number")))
             )
    return cur.fetchone()[0]


def upsert_episodes_from_tmdb(
    conn: sqlitecloud.Connection,
    tmdb_show_id: int,
    show_id: int,
    season_number: int,
    api_key: str,
) -> None:
    # Get full season details from TMDB
    sdetails = tmdb_season_details(tmdb_show_id, season_number, api_key) or {}

    episodes = sdetails.get("episodes") or []
    if not episodes:
        return  # nothing to insert

    upsert_sql = """
    INSERT INTO episodes (show_id, season_number, episode_number, name, air_date, overview)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(show_id, season_number, episode_number) DO UPDATE SET
      name=excluded.name,
      air_date=excluded.air_date,
      overview=excluded.overview
    """
    for ep in episodes:
        conn.execute(
            upsert_sql,
            (
                show_id,
                season_number,
                ep.get("episode_number"),
                ep.get("name"),
                ep.get("air_date"),
                ep.get("overview"),
            ),
        )
    conn.commit()


def safe_next_air_date(details: dict):
    nxt = details.get("next_episode_to_air")
    return nxt.get("air_date") if isinstance(nxt, dict) else None


def upsert_episode(conn: sqlitecloud.Connection, show_id: int, ep: Dict[str, Any]) -> int:
    ue_sql = """
    INSERT INTO episodes (show_id, season_number, episode_number, tmdb_episode_id, name, air_date, overview, runtime)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(show_id, season_number, episode_number) DO UPDATE SET
        name=excluded.name,
        air_date=excluded.air_date,
        overview=excluded.overview,
        runtime=excluded.runtime
    """
    ue_params: tuple[Any, ...] = (
        show_id,
        ep.get("season_number"),
        ep.get("episode_number"),
        ep.get("id"),
        ep.get("name"),
        ep.get("air_date"),
        ep.get("overview"),
        ep.get("runtime"),
    )

    conn.execute(ue_sql, cast(tuple[Any], ue_params))
    conn.commit()
    cur = conn.execute(
        "SELECT id FROM episodes WHERE show_id=? AND season_number=? AND episode_number=?",
        cast(tuple[Any], (show_id, ep.get("season_number"), ep.get("episode_number"))),
    )
    return cur.fetchone()[0]


def sync_show_from_tmdb(conn: sqlitecloud.Connection, tmdb_id: int, api_key: str) -> int:
    details = tmdb_tv_details(tmdb_id, api_key)
    show_id = upsert_show(conn, details)

    for s in details.get("seasons", []):
        season_num = s.get("season_number")
        if season_num is None:
            continue
        season_full = tmdb_season_details(tmdb_id, season_num, api_key)
        upsert_season(conn, show_id, season_full)
        for ep in season_full.get("episodes", []):
            ep_copy = dict(ep)
            ep_copy["season_number"] = season_num
            upsert_episode(conn, show_id, ep_copy)
    return show_id


# ----------------------------
# Queries
# ----------------------------
def list_shows(conn: sqlitecloud.Connection) -> List[sqlitecloud.Row]:
    user_id = st.session_state["user_id"]
    conn.row_factory = sqlitecloud.Row

    q = """
    SELECT s.*,
           (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id) AS episode_total,
           (SELECT COUNT(*) FROM episodes e JOIN watches w ON w.episode_id = e.id
             WHERE e.show_id = s.id and w.user_id = ?) AS watched_count
    FROM user_shows us
    join shows s on s.id = us.show_id
    where us.user_id = ?
    ORDER BY COALESCE(s.next_air_date, '9999-12-31') ASC, s.name ASC
    """
    return conn.execute(q, cast(tuple[Any], (user_id, user_id))).fetchall()


def show_episodes(conn: sqlitecloud.Connection, show_id: int, season: Optional[int] = None) -> List[sqlitecloud.Row]:
    conn.row_factory = sqlitecloud.Row
    if season is None:
        q = """
            SELECT e.*,
                   (SELECT MAX(watched_at) FROM watches w WHERE w.episode_id = e.id) AS last_watched_at,
                   (SELECT rating FROM watches w WHERE w.episode_id = e.id
                   ORDER BY watched_at DESC LIMIT 1) AS last_rating,
                   (SELECT notes FROM watches w WHERE w.episode_id = e.id
                   ORDER BY watched_at DESC LIMIT 1) AS last_notes
            FROM episodes e
            WHERE e.show_id = ?
            ORDER BY e.season_number, e.episode_number
        """
        cur = conn.execute(q, (show_id,))
    else:
        q = """
            SELECT e.*,
                   (SELECT MAX(watched_at) FROM watches w WHERE w.episode_id = e.id) AS last_watched_at,
                   (SELECT rating FROM watches w WHERE w.episode_id = e.id
                   ORDER BY watched_at DESC LIMIT 1) AS last_rating,
                   (SELECT notes FROM watches w WHERE w.episode_id = e.id
                   ORDER BY watched_at DESC LIMIT 1) AS last_notes
            FROM episodes e
            WHERE e.show_id = ? AND e.season_number = ?
            ORDER BY e.season_number, e.episode_number
        """
        cur = conn.execute(q, cast(tuple[Any], (show_id, season)))
    return cur.fetchall()


def list_seasons(conn: sqlitecloud.Connection, show_id: int) -> List[sqlitecloud.Row]:
    conn.row_factory = sqlitecloud.Row
    cur = conn.execute(
        "SELECT * FROM seasons WHERE show_id=? ORDER BY season_number",
        (show_id,),
    )
    return cur.fetchall()


def next_unwatched(conn: sqlitecloud.Connection, show_id: int) -> Optional[sqlitecloud.Row]:
    conn.row_factory = sqlitecloud.Row
    q = """
        SELECT e.*
        FROM episodes e
        LEFT JOIN watches w ON w.episode_id = e.id
        WHERE e.show_id = ?
        GROUP BY e.id
        HAVING MAX(w.watched_at) IS NULL
        ORDER BY e.season_number, e.episode_number
        LIMIT 1
    """
    cur = conn.execute(q, (show_id,))
    return cur.fetchone()


def log_watch(conn, episode_id: int, rating: int = None, notes: str = None):
    user_id = st.session_state["user_id"]
    conn.execute(
        "INSERT INTO watches (user_id, episode_id, watched_at, rating, notes) VALUES (?, ?, datetime('now'), ?, ?)",
        (user_id, episode_id, rating, notes),
    )
    conn.commit()


def get_watched_episodes(conn, show_id: int):
    user_id = st.session_state["user_id"]
    cur = conn.execute(
        "SELECT e.episode_number, w.watched_at, w.rating, w.notes "
        "FROM episodes e "
        "JOIN seasons s ON e.season_number = s.id "
        "JOIN shows sh ON s.show_id = sh.id "
        "JOIN watches w ON e.id = w.episode_id "
        "WHERE sh.id = ? AND w.user_id = ?",
        (show_id, user_id)
    )
    return cur.fetchall()


# ----------------------------
# Alerts: Email & SMS
# ----------------------------
def get_alert_config(conn: sqlitecloud.Connection) -> sqlitecloud.Row:
    conn.row_factory = sqlitecloud.Row
    cur = conn.execute("SELECT * FROM alert_config WHERE id = 1")
    return cur.fetchone()


def save_alert_config(conn: sqlitecloud.Connection, email_to: str, sms_to: str, email_enabled: bool, sms_enabled: bool):
    conn.execute(
        "UPDATE alert_config SET email_to=?, sms_to=?, carrier=?,"
        " sms_via_email_enabled=?, email_enabled=?, sms_enabled=? WHERE id=1",
        cast(tuple[Any], (email_to or None, sms_to or None, 1 if email_enabled else 0, 1 if sms_enabled else 0)),
    )
    conn.commit()


def send_email(subject: str, body: str, to_addr: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and to_addr):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [to_addr], msg.as_string())
        return True
    except Exception as e:
        st.warning(f"Email send failed: {e}")
        return False


def sms_via_email_address(phone: str, carrier: str) -> str | None:
    """Return the email-to-SMS address if carrier supported."""
    key = carrier.lower().replace(" ", "")
    domain = CARRIER_GATEWAYS.get(key)
    if not domain:
        return None
    return f"{phone}{domain}"


def send_alert(conn: sqlitecloud.Connection, subject: str, body: str) -> None:
    cur = conn.execute("SELECT email_to, sms_to, email_enabled, "
                       "sms_enabled, carrier, sms_via_email_enabled FROM alert_config WHERE id = 1")
    config = cur.fetchone()
    if not config:
        return

    email_to, sms_to, email_enabled, sms_enabled, carrier, sms_via_email_enabled = config

    if email_enabled and email_to:
        send_email(subject, body, email_to)

#    if sms_enabled and sms_to:
#        send_sms_direct(sms_to, body)  # placeholder for Twilio/etc.

    if sms_via_email_enabled and sms_to and carrier:
        sms_email = sms_via_email_address(sms_to, carrier)
        if sms_email:
            send_email(subject, body, sms_email)


def send_sms_via_email(phone_number: str, carrier: str, message: str):
    gateways = {
        "att": "@txt.att.net",
        "verizon": "@vtext.com",
        "tmobile": "@tmomail.net",
        "sprint": "@messaging.sprintpcs.com",
    }
    if carrier not in gateways:
        raise ValueError("Unsupported carrier")

    to_email = f"{phone_number}{gateways[carrier]}"
    msg = MIMEText(message)
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = "Binge Buddy Alert"

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())


def format_alert(name: str, old_date: Optional[str], new_date: Optional[str]) -> str:
    return (
        f"Show: {name}\n"
        f"Previous next episode date: {old_date or '—'}\n"
        f"New next episode date: {new_date or '—'}\n"
        f"Time: {datetime.datetime.now().isoformat(timespec='seconds')}"
    )


def check_and_alert_updates(conn: sqlitecloud.Connection, api_key: str) -> Dict[str, int]:
    """
    For each show:
      - pull latest details from TMDB
      - if next_air_date changed, update DB and send alerts (email/SMS) based on config
    Returns counts for UI.
    """
    cfg = get_alert_config(conn)
    email_to = cfg["email_to"] or ALERT_EMAIL_TO_DEFAULT
    sms_to = cfg["sms_to"] or ALERT_SMS_TO_DEFAULT
    sms_carrier = cfg["sms_carrier"] or ALERT_SMS_CARRIER_DEFAULT
    do_email = bool(cfg["email_enabled"])
    do_sms = bool(cfg["sms_enabled"])
    do_sms_email = bool(cfg["sms_via_email_enabled"])

    updated = 0
    emailed = 0
    texted = 0
    sms_emailed = 0

    conn.row_factory = sqlitecloud.Row
    shows = conn.execute("SELECT * FROM shows").fetchall()
    for s in shows:
        details = tmdb_tv_details(s["tmdb_id"], api_key) or {}
        nxt = (details.get("next_episode_to_air") or {})
        new_date = nxt.get("air_date")
#    if details.get("next_episode_to_air"):
        #    new_date = details["next_episode_to_air"].get("air_date")

        old_date = s["next_air_date"]
        if new_date != old_date:
            # update DB
            conn.execute(
                "UPDATE shows SET next_air_date=?, status=?, first_air_date=?, last_air_date=? WHERE id=?",
                cast(tuple[Any], (
                    new_date,
                    details.get("status"),
                    details.get("first_air_date"),
                    details.get("last_air_date"),
                    s["id"],
                )),
            )
            conn.commit()
            updated += 1

            # avoid duplicate alerts for same date
            if new_date and new_date != s["alerted_next_air_date"]:
                body = format_alert(s["name"], old_date, new_date)
                if do_email and email_to:
                    if send_email(f"[ShowTracker] New next episode date for {s['name']}", body, email_to):
                        emailed += 1
                if do_sms and sms_to:
                    if send_sms_via_email(sms_to, sms_carrier, f"{s['name']}: next episode date updated -> {new_date}"):
                        texted += 1
                if do_sms_email and sms_to:
                    if send_sms_via_email(sms_to, sms_carrier, f"{s['name']}: next episode date updated -> {new_date}"):
                        sms_emailed += 1
                # record that we've alerted on this new date
                conn.execute(
                    "UPDATE shows SET alerted_next_air_date=? WHERE id=?",
                    cast(tuple[Any], (new_date, s["id"])),
                )
                conn.commit()

    return {"updated": updated, "emailed": emailed, "texted": texted, "sms_emailed": sms_emailed}


# ----------------------------
# UI Helpers
# ----------------------------
def poster_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return f"https://image.tmdb.org/t/p/w342{path}"


def render_show_card(row: sqlitecloud.Row):
    cols = st.columns([1, 3])
    with cols[0]:
        if row["poster_path"]:
            st.image(poster_url(row["poster_path"]), use_container_width=True)
        else:
            st.write("No poster")
    with cols[1]:
        st.markdown(f"### {row['name']}")
        st.write(f"**Status:** {row['status'] or '—'}")
        st.write(f"**Next Air Date:** {row['next_air_date'] or '—'}")
        st.write(f"**Watched:** {row['watched_count'] or 0} / {row['episode_total'] or 0}")
        if row["overview"]:
            with st.expander("Overview"):
                st.write(row["overview"])


# ----------------------------
# Pages
# ----------------------------
def page_profile(conn):
    st.header("👤 My Profile")

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("You must be logged in to view this page.")
        return

    # Fetch current user info
    cur = conn.execute(
        "SELECT email, phone, carrier, enable_email, enable_sms FROM users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    if row:
        current_email, current_phone, current_carrier, enable_email, enable_sms = row
    else:
        st.error("User not found.")
        return

    # store session data

    st.session_state.current_email = current_email
    st.session_state.current_sms = current_phone
    st.session_state.current_email_enabled = enable_email
    st.session_state.current_sms_via_email_enabled = enable_sms
    st.session_state.current_carrier = current_carrier


def page_add_show(conn, api_key: str):
    st.header("➕ Add Show")

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("You must be logged in to add shows.")
        return

    # Search input
    query = st.text_input("Search for a TV Show")
    if query:
        results = tmdb_search_tv(query, api_key)

        if not results:
            st.warning("No results found.")
        else:
            for result in results:
                show_id_tmdb = result["id"]
                name = result["name"]
                first_air_date = result.get("first_air_date", "Unknown")
                overview = result.get("overview", "")

                with st.expander(f"{name} ({first_air_date})"):
                    st.write(overview)

                    if st.button("Add to My Shows", key=f"add_{show_id_tmdb}"):
                        # 1. Insert into global shows table
                        cur = conn.execute("SELECT id FROM shows WHERE tmdb_id = ?", (show_id_tmdb,))
                        row = cur.fetchone()

                        if row:
                            show_id = row[0]
                        else:
                            # Pull full details from TMDB
                            details = tmdb_tv_details(show_id_tmdb, api_key) or {}
                            status = details.get("status", "Unknown")
                            next_air_date = (details.get("next_episode_to_air") or {}).get("air_date")

                            conn.execute(
                                "INSERT INTO shows (tmdb_id, name, status, next_air_date) VALUES (?, ?, ?, ?)",
                                (show_id_tmdb, name, status, next_air_date),
                            )
                            conn.commit()

                            show_id = conn.execute("SELECT id FROM shows WHERE tmdb_id = ?",
                                                   (show_id_tmdb,)).fetchone()[0]

                            # Load seasons/episodes into DB
                            for season in details.get("seasons", []):
                                season_num = season.get("season_number")
                                episode_count = season.get("episode_count", 0)
                                conn.execute(
                                    "INSERT OR IGNORE INTO seasons (show_id, season_number, episode_count) "
                                    "VALUES (?, ?, ?)",
                                    (show_id, season_num, episode_count),
                                )
                                conn.commit()

                                season_id = conn.execute(
                                    "SELECT id FROM seasons WHERE show_id=? AND season_number=?",
                                    (show_id, season_num),
                                ).fetchone()[0]

                                season_details = tmdb_season_details(show_id_tmdb, season_num, api_key)
                                for ep in season_details.get("episodes", []):
                                    conn.execute(
                                        "INSERT OR IGNORE INTO episodes (season_number, episode_number, name, air_date"
                                        ", overview) VALUES (?, ?, ?, ?, ?)",
                                        (
                                            season_num,
                                            ep["episode_number"],
                                            ep["name"],
                                            ep.get("air_date"),
                                            ep.get("overview"),
                                        ),
                                    )
                                conn.commit()

                        # 2. Link show to this user
                        conn.execute(
                            "INSERT OR IGNORE INTO user_shows (user_id, show_id) VALUES (?, ?)",
                            (user_id, show_id),
                        )
                        conn.commit()

                        st.success(f"✅ {name} added to your watchlist!")
                        st.rerun()


def page_watchlist(conn):
    st.header("📺 My Watchlist")

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("You must be logged in to view your watchlist.")
        return

    shows = get_user_shows(conn)
    if not shows:
        st.info("You haven't added any shows yet. Go to **Add Show** to start tracking.")
        return

    for show_id, name, status, next_air_date in shows:
        with st.expander(f"{name} ({status})"):
            st.write(f"**Next Air Date:** {next_air_date or 'N/A'}")

            # Remove option
            if st.button(f"❌ Remove from My Shows", key=f"remove_{show_id}"):
                conn.execute("DELETE FROM user_shows WHERE user_id=? AND show_id=?", (user_id, show_id))
                conn.commit()
                st.success(f"Removed {name} from your watchlist.")
                st.rerun()

            # Seasons for this show
            cur = conn.execute(
                "SELECT id, season_number, episode_count FROM seasons WHERE show_id = ?",
                (show_id,)
            )
            seasons = cur.fetchall()

            for season_id, season_number, episode_count in seasons:
                with st.expander(f"Season {season_number}"):
                    cur = conn.execute(
                        "SELECT id, episode_number, name, air_date FROM episodes WHERE season_number= ?",
                        (season_id,)
                    )
                    episodes = cur.fetchall()

                    # Watched episodes for this user
                    watched = {
                        row[0]: row for row in conn.execute(
                            "SELECT e.id, w.watched_at, w.rating, w.notes "
                            "FROM episodes e "
                            "JOIN watches w ON e.id = w.episode_id "
                            "WHERE e.season_number= ? AND w.user_id = ?",
                            (season_id, user_id),
                        ).fetchall()
                    }

                    for episode_id, ep_number, ep_name, air_date in episodes:
                        watched_entry = watched.get(episode_id)
                        col1, col2, col3 = st.columns([3, 2, 3])

                        with col1:
                            st.write(f"S{season_number}E{ep_number}: {ep_name}")
                            st.caption(f"Air date: {air_date}")

                        with col2:
                            if watched_entry:
                                st.success("Watched")
                            else:
                                if st.button("Mark Watched", key=f"watch_{episode_id}"):
                                    log_watch(conn, episode_id)
                                    st.rerun()

                        with col3:
                            if watched_entry:
                                rating = watched_entry[2] or 0
                                notes = watched_entry[3] or ""
                                st.write(f"⭐ {rating if rating else 'N/A'}")
                                if notes:
                                    st.caption(f"Notes: {notes}")
                                if st.button("Update Rating/Notes", key=f"edit_{episode_id}"):
                                    with st.form(f"edit_form_{episode_id}"):
                                        new_rating = st.slider("Rating", 1, 5, value=rating or 3)
                                        new_notes = st.text_area("Notes", value=notes)
                                        if st.form_submit_button("Save"):
                                            conn.execute(
                                                "UPDATE watches SET rating=?, notes=? WHERE user_id=? AND episode_id=?",
                                                (new_rating, new_notes, user_id, episode_id),
                                            )
                                            conn.commit()
                                            st.rerun()


def page_next_up(conn: sqlitecloud.Connection):
    st.subheader("Next Up & Upcoming")
    rows = list_shows(conn)
    if not rows:
        st.info("Add a show first.")
        return

    for row in rows:
        nx = next_unwatched(conn, row["id"])
        with st.container(border=True):
            st.markdown(f"### {row['name']}")
            st.write(f"**Next Air Date:** {row['next_air_date'] or '—'}  •  **Status:** {row['status'] or '—'}")
            providers = tmdb_watch_providers(row["id"], DEFAULT_API_KEY)
            if providers:
                st.write("Available on:")
                cols = st.columns(len(providers))
                for col, p in zip(cols, providers):
                    if p["logo"]:
                        col.image(p["logo"], width=60)
                    col.caption(p["name"])
            else:
                st.info("No streaming info available for this region.")

            if nx:
                st.write(f"**Next Up:** S{nx['season_number']}E{nx['episode_number']} — {nx['name'] or '—'}")
                if st.button(f"Mark watched: S{nx['season_number']}E{nx['episode_number']}", key=f"nx_{nx['id']}"):
                    log_watch(conn, nx["id"], rating=None, notes=None)
                    st.success("Marked watched.")
                    st.rerun()
            else:
                st.write("All caught up on logged episodes.")


def sync_show_updates(conn, user_id: int, api_key: str, t_api_key: str):
    updated_count = 0
    email_count = 0
    sms_count = 0

    # Get user info
    cur = conn.execute(
        "SELECT email, phone, carrier, enable_email, enable_sms FROM users WHERE id=?",
        (user_id,)
    )
    user = cur.fetchone()
    if not user:
        return updated_count, email_count, sms_count

    user_email, user_phone, user_carrier, enable_email, enable_sms = user

    # Get shows tracked by this user
    cur = conn.execute(
        "SELECT s.id, s.tmdb_id, s.name, s.next_air_date FROM shows s "
        "JOIN user_shows us ON s.id = us.show_id "
        "WHERE us.user_id = ?",
        (user_id,)
    )
    tracked_shows = cur.fetchall()

    for show_id, tmdb_id, name, old_air_date in tracked_shows:
        try:
            details = tmdb_tv_details(tmdb_id, t_api_key) or {}
            status = details.get("status", "Unknown")
            new_air_date = details.get("next_episode_to_air", {}).get("air_date")

            # Only update if changed
            if new_air_date and new_air_date != old_air_date:
                conn.execute(
                    "UPDATE shows SET next_air_date=? WHERE id=?",
                    (new_air_date, show_id),
                )
                conn.commit()
                updated_count += 1

                # Send notifications
                message = f"📺 Update for {name}: next episode now airs on {new_air_date}"

                if enable_email and user_email:
                    send_email("Show Update", message, user_email)
                    email_count += 1

                if enable_sms and user_phone and user_carrier:
                    send_sms_via_email(user_phone, user_carrier, message)
                    sms_count += 1

        except Exception as e:
            st.error(f"Error checking {name}: {e}")

    return updated_count, email_count, sms_count


# https://api.themoviedb.org/3/tv/82856/watch/providers?api_key=56fd720902db9675d91e844b44f58351
def tmdb_watch_providers(tmdb_id: int, api_key: str, region: str = "US") -> list[dict]:
    """Return streaming providers (flatrate) for a given show and region."""
    import requests
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/watch/providers"
    r = requests.get(url, params={"api_key": api_key})
    if r.status_code != 200:
        return []
    data = r.json().get("results", {})
    country = data.get(region, {})
    providers = country.get("flatrate", []) or []
    return [
        {
            "name": p.get("provider_name"),
            "logo": f"https://image.tmdb.org/t/p/w92{p['logo_path']}" if p.get("logo_path") else None,
        }
        for p in providers
    ]


def page_alerts(conn):
    st.header("🔔 Alerts & Notifications")

    user_id = st.session_state.get("user_id")
    if not user_id:
        st.error("You must be logged in to view this page.")
        return

    # Load current user settings
    cur = conn.execute(
        "SELECT email, phone, carrier, enable_email, enable_sms FROM users WHERE id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    if not row:
        st.error("User not found.")
        return

    current_email, current_phone, current_carrier, enable_email, enable_sms = row

    # Form for updating alert preferences
    with st.form("alerts_form"):
        st.text_input("Alert Email", value=current_email or "", disabled=True)
        st.text_input("Phone Number", value=current_phone or "", key="alerts_phone")
        st.selectbox(
            "Carrier",
            ["", "AT&T", "Verizon", "T-Mobile", "Sprint"],
            index=(["", "AT&T", "Verizon", "T-Mobile", "Sprint"].index(current_carrier)
                   if current_carrier in ["AT&T", "Verizon", "T-Mobile", "Sprint"] else 0),
            key="alerts_carrier",
        )
        enable_email_flag = st.checkbox("Enable Email Alerts", value=bool(enable_email))
        enable_sms_flag = st.checkbox("Enable SMS via Email", value=bool(enable_sms))

        submitted = st.form_submit_button("Save Alert Settings")

    if submitted:
        conn.execute(
            "UPDATE users SET phone=?, carrier=?, enable_email=?, enable_sms=? WHERE id=?",
            (
                st.session_state["alerts_phone"],
                st.session_state["alerts_carrier"],
                int(enable_email_flag),
                int(enable_sms_flag),
                user_id,
            ),
        )
        conn.commit()
        st.success("Alert settings updated!")

    st.markdown("---")

    # Manual sync with TMDB
    st.subheader("Check for Show Updates")
    if st.button("Check Now"):
        updated_count, email_count, sms_count = sync_show_updates(conn, st.session_state["user_id"], sql_api_key, DEFAULT_API_KEY)
        st.info(f"Updated shows: {updated_count} • Emails sent: {email_count} • SMS sent: {sms_count}")


# ----------------------------
# Main
# ----------------------------
def main():
    st.set_page_config(page_title="Show Tracker", layout="wide")
    st.title("📺 Personal Show Tracker")
    st.write("Environment: ", ENVIRON)

    with st.sidebar:
        if DEBUG_ON:
            st.markdown("### Settings")
            api_key = st.text_input("TMDB API Key", value=DEFAULT_API_KEY, type="password")
            st.session_state["api_key"] = api_key
            if not api_key:
                st.warning("Enter your TMDB API key to search/import.")

        conn = get_validated_conn(sc_url)
        # init_db(conn)
        st.session_state["conn"] = conn  # <-- store it for logout()
        st.info("SQLite DB ready.")
        # If not logged in, show the login screen and stop rendering the rest
        if not st.session_state.get("user_id"):
            login_screen(st.session_state["conn"])
            st.stop()

        # If logged in, surface a compact identity + logout
        if st.session_state.get("user_id"):
            st.caption(f"Signed in as: {st.session_state.get('user_email', 'Unknown')}")
            # IMPORTANT: inline button (NO on_click), so st.rerun() in logout() is not in a callback
            if st.button("🚪 Logout", use_container_width=True):
                logout()

        st.markdown("---")
        st.caption("Powered by TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.")

    tabs = st.tabs(["Add Show", "Watchlist", "Next Up", "Alerts", "Profile"])
    with tabs[0]:
        page_add_show(conn, DEFAULT_API_KEY)
    with tabs[1]:
        page_watchlist(conn)
    with tabs[2]:
        page_next_up(conn)
    with tabs[3]:
        page_alerts(conn)
    with tabs[4]:
        # elif page == "Profile":
        page_profile(conn)

        user_id = st.session_state.user_id
        current_email = st.session_state.current_email
        current_phone = st.session_state.current_sms
        current_carrier = st.session_state.current_carrier
        enable_email = st.session_state.current_email_enabled
        enable_sms = st.session_state.current_sms_via_email_enabled

        # Profile form
        with st.form("profile_form"):
            st.text_input("Email (cannot change here)", value=current_email, disabled=True)
            phone = st.text_input("Phone", value=current_phone or "")
            carrier = st.selectbox(
                "Carrier",
                ["", "AT&T", "Verizon", "T-Mobile", "Sprint"],
                index=(["", "AT&T", "Verizon", "T-Mobile", "Sprint"].index(current_carrier)
                       if current_carrier in ["AT&T", "Verizon", "T-Mobile", "Sprint"] else 0)
            )
            enable_email_flag = st.checkbox("Enable Email Alerts", value=bool(enable_email))
            enable_sms_flag = st.checkbox("Enable SMS Alerts", value=bool(enable_sms))

            st.markdown("---")
            new_password = st.text_input("Change Password", type="password")
            confirm_password = st.text_input("Confirm Password", type="password")

            submitted = st.form_submit_button("Save Changes")

        if submitted:
            # Update profile
            conn.execute(
                "UPDATE users SET phone=?, carrier=?, enable_email=?, enable_sms=? WHERE id=?",
                cast(tuple[Any], (phone, carrier, int(enable_email_flag), int(enable_sms_flag), user_id)),
            )

            # Update password if provided
            if new_password:
                if new_password != confirm_password:
                    st.error("Passwords do not match.")
                else:
                    conn.execute(
                        "UPDATE users SET password_hash=? WHERE id=?",
                        (hash_password(new_password), user_id),
                    )
                    st.success("Password updated!")

            conn.commit()
            st.success("Profile updated successfully.")


if __name__ == "__main__":
    main()
