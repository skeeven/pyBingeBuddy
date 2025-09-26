import os
from dotenv import load_dotenv
import sqlitecloud
import requests
import datetime
import smtplib
from email.mime.text import MIMEText
from typing import Optional, List, Dict, Any, cast

import streamlit as st

# Optional SMS
# try:
#     from twilio.rest import Client as TwilioClient
# except ImportError:
#     TwilioClient = None  # Not installed or not used

# ----------------------------
# Config
# ----------------------------
APP_DB = "shows.db"
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
    except Exception:
        return default

# TMDB env
DEFAULT_API_KEY = _get_secret("TMDB_API_KEY")
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


# ----------------------------
# DB Utilities
# ----------------------------
def get_conn() -> sqlitecloud.Connection:
    if not sc_url or not sc_dbname:
        st.error("Missing SQLITE_CLOUD_URL or SQLITE_DB. Set them in .env or secrets.toml.")
        raise RuntimeError("Database configuration missing")
    conn = sqlitecloud.connect(sc_url)
    conn.execute(f"USE DATABASE {sc_dbname}")
    return conn


def init_db(conn: sqlitecloud.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shows (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            tmdb_id         INTEGER UNIQUE NOT NULL,
            name            TEXT NOT NULL,
            status          TEXT,
            next_air_date   TEXT,
            overview        TEXT,
            poster_path     TEXT,
            first_air_date  TEXT,
            last_air_date   TEXT,
            -- new: what date we already alerted on (to avoid duplicates)
            alerted_next_air_date TEXT
        );

        CREATE TABLE IF NOT EXISTS seasons (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id         INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            season_number   INTEGER NOT NULL,
            name            TEXT,
            air_date        TEXT,
            episode_count   INTEGER,
            UNIQUE(show_id, season_number)
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            show_id         INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
            season_number   INTEGER NOT NULL,
            episode_number  INTEGER NOT NULL,
            tmdb_episode_id INTEGER UNIQUE,
            name            TEXT,
            air_date        TEXT,
            overview        TEXT,
            runtime         INTEGER,
            UNIQUE(show_id, season_number, episode_number)
        );

        CREATE TABLE IF NOT EXISTS watches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            episode_id      INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            watched_at      TEXT NOT NULL,
            rating          INTEGER,
            notes           TEXT
        );

        -- Persist alert recipients & preferences
        CREATE TABLE IF NOT EXISTS alert_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            email_to TEXT,
            sms_to TEXT,
            carrier TEXT, -- NEW: carrier for email-to-sms
            sms_via_email_enabled INTEGER DEFAULT 0, -- NEW: opt-in
            email_enabled INTEGER DEFAULT 0,
            sms_enabled INTEGER DEFAULT 0
        );

        INSERT OR IGNORE INTO alert_config (id, email_to, sms_to, email_enabled, sms_enabled)
        VALUES (1, NULL, NULL, 0, 0);

        CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id);
        CREATE INDEX IF NOT EXISTS idx_watches_episode ON watches(episode_id);
        """
    )
    conn.commit()

    # Backfill alerted_next_air_date for existing rows
    try:
        conn.execute("SELECT alerted_next_air_date FROM shows LIMIT 1")
    except sqlitecloud.OperationalError:
        conn.execute("ALTER TABLE shows ADD COLUMN alerted_next_air_date TEXT")
        conn.commit()


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
    r = requests.get(url, headers=tmdb_headers(api_key), params=tmdb_params(api_key))
    r.raise_for_status()
    return r.json()


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
    next_air_date = None
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
    conn.row_factory = sqlitecloud.Row
    cur = conn.execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM episodes e WHERE e.show_id = s.id) AS episode_total,
               (SELECT COUNT(*) FROM episodes e JOIN watches w ON w.episode_id = e.id
                 WHERE e.show_id = s.id) AS watched_count
        FROM shows s
        ORDER BY COALESCE(s.next_air_date, '9999-12-31') ASC, s.name ASC
        """
    )
    return cur.fetchall()


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


def log_watch(conn: sqlitecloud.Connection, episode_id: int, rating: Optional[int], notes: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO watches (episode_id, watched_at, rating, notes) VALUES (?, ?, ?, ?)",
        cast(tuple[Any], (episode_id, datetime.datetime.now().isoformat(timespec="seconds"), rating, notes)),
    )
    conn.commit()


# ----------------------------
# Alerts: Email & SMS
# ----------------------------
def get_alert_config(conn: sqlitecloud.Connection) -> sqlitecloud.Row:
    conn.row_factory = sqlitecloud.Row
    cur = conn.execute("SELECT * FROM alert_config WHERE id = 1")
    return cur.fetchone()


def save_alert_config(conn: sqlitecloud.Connection, email_to: str, sms_to: str, email_enabled: bool, sms_enabled: bool):
    conn.execute(
        "UPDATE alert_config SET email_to=?, sms_to=?, email_enabled=?, sms_enabled=? WHERE id=1",
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
    cur = conn.execute("SELECT email_to, sms_to, email_enabled, sms_enabled, carrier, sms_via_email_enabled FROM alert_config WHERE id = 1")
    config = cur.fetchone()
    if not config:
        return

    email_to, sms_to, email_enabled, sms_enabled, carrier, sms_via_email_enabled = config

    if email_enabled and email_to:
        send_email(email_to, subject, body)

#    if sms_enabled and sms_to:
#        send_sms_direct(sms_to, body)  # placeholder for Twilio/etc.

    if sms_via_email_enabled and sms_to and carrier:
        sms_email = sms_via_email_address(sms_to, carrier)
        if sms_email:
            send_email(sms_email, subject, body)


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
    msg["From"] = "your_email@example.com"
    msg["To"] = to_email
    msg["Subject"] = "Alert"

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login("your_email@example.com", "your_app_password")
        server.sendmail("your_email@example.com", to_email, msg.as_string())


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

    updated = 0
    emailed = 0
    texted = 0

    conn.row_factory = sqlitecloud.Row
    shows = conn.execute("SELECT * FROM shows").fetchall()
    for s in shows:
        details = tmdb_tv_details(s["tmdb_id"], api_key)
        new_date = None
        if details.get("next_episode_to_air"):
            new_date = details["next_episode_to_air"].get("air_date")

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
                # record that we've alerted on this new date
                conn.execute(
                    "UPDATE shows SET alerted_next_air_date=? WHERE id=?",
                    cast(tuple[Any], (new_date, s["id"])),
                )
                conn.commit()

    return {"updated": updated, "emailed": emailed, "texted": texted}


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
def page_add_show(conn: sqlitecloud.Connection, api_key: str):
    st.subheader("Add / Import a Show")

    # --- Search mode selection ---
    mode = st.radio("Search mode", ["Simple", "Discover (filters)"], horizontal=True)

    if mode == "Simple":
        query = st.text_input("Search by title", placeholder="e.g., Countdown")
        if st.button("Search", use_container_width=True) and query.strip():
            results = tmdb_search_tv(query, api_key)
            if not results:
                st.info("No results.")
            for r in results:
                with st.container(border=True):
                    title = r.get("name") or r.get("original_name")
                    date = r.get("first_air_date") or "—"
                    st.markdown(f"**{title}**  \nFirst aired: {date}")
                    if r.get("poster_path"):
                        st.image(poster_url(r["poster_path"]), width=160)
                    st.caption(r.get("overview") or "")
                    cols = st.columns([1, 1, 3])
                    with cols[0]:
                        if st.button(f"Import #{r['id']}", key=f"import_{r['id']}"):
                            show_id = sync_show_from_tmdb(conn, r["id"], api_key)
                            st.success(f"Imported '{title}' (show_id={show_id}).")
                    with cols[1]:
                        st.write(f"TMDB ID: {r['id']}")

    else:
        # --- Discover with filters ---
        with st.expander("Filters", expanded=True):
            genres_map = tmdb_tv_genres(api_key)
            genre_names = list(genres_map.values())
            sel_genres = st.multiselect("Genres", options=genre_names)

            col1, col2, col3 = st.columns(3)
            with col1:
                year_from = st.number_input("First air year ≥", min_value=1950, max_value=2100, value=2000)
            with col2:
                year_to = st.number_input("First air year ≤", min_value=1950, max_value=2100,
                                          value=datetime.date.today().year)
            with col3:
                min_vote = st.slider("Min TMDB rating", 0.0, 10.0, 6.5, 0.5)

            language = st.text_input("Original language (ISO-639-1, e.g., en, ja, ko)", value="en")

            colp = st.columns(3)
            with colp[0]:
                providers = st.text_input("Watch provider IDs (comma)",
                                          help="""Advanced: TMDB provider IDs. Example: 8 for Netflix, 
                                          9 for Prime (varies by region).""")
            with colp[1]:
                region = st.text_input("Watch region (ISO-3166-1, e.g., US)", value="US")
            with colp[2]:
                sort_by = st.selectbox("Sort by", ["popularity.desc", "first_air_date.desc",
                                                   "vote_average.desc", "name.asc"])

            statuses = st.multiselect("Status (post-filter)", ["Returning Series", "Ended", "Planned", "Canceled"])

        if st.button("Discover", use_container_width=True):
            params = {
                "first_air_date.gte": f"{year_from}-01-01",
                "first_air_date.lte": f"{year_to}-12-31",
                "with_original_language": language,
                "vote_average.gte": min_vote,
                "sort_by": sort_by,
                "page": 1,
            }
            if sel_genres:
                # map names -> ids
                ids = [gid for gid, name in genres_map.items() if name in sel_genres]
                if ids:
                    params["with_genres"] = ",".join(map(str, ids))
            if providers.strip():
                params["with_watch_providers"] = providers
                params["watch_region"] = region

            data = tmdb_discover_tv(api_key, params)
            results = data.get("results", [])
            if not results:
                st.info("No results.")
            for r in results:
                # Post-filter by status if requested: need a details call to get status
                if statuses:
                    det = tmdb_tv_details(r["id"], api_key)
                    if det.get("status") not in statuses:
                        continue

                with st.container(border=True):
                    title = r.get("name") or r.get("original_name")
                    date = r.get("first_air_date") or "—"
                    st.markdown(f"**{title}**  \nFirst aired: {date}")
                    if r.get("poster_path"):
                        st.image(poster_url(r["poster_path"]), width=160)
                    st.caption(r.get("overview") or "")
                    cols = st.columns([1, 2])
                    with cols[0]:
                        if st.button(f"Import #{r['id']}", key=f"import_disc_{r['id']}"):
                            show_id = sync_show_from_tmdb(conn, r["id"], api_key)
                            st.success(f"Imported '{title}' (show_id={show_id}).")

                    with cols[1]:
                        det = tmdb_tv_details(r["id"], api_key)
                        st.caption(f"Status: {det.get('status') or '—'}; "
                                   f"Next air: {(det.get('next_episode_to_air') or {}).get('air_date') or '—'}")


def page_watchlist(conn: sqlitecloud.Connection):
    st.subheader("Watchlist")
    rows = list_shows(conn)
    if not rows:
        st.info("No shows yet. Add one on the **Add Show** page.")
        return

    for row in rows:
        with st.container(border=True):
            render_show_card(row)
            show_id = row["id"]
            nx = next_unwatched(conn, show_id)
            c1, c2, c3 = st.columns([1, 1, 2])
            with c1:
                if nx and st.button(f"Mark Next Up: S{nx['season_number']}E{nx['episode_number']} watched",
                                    key=f"mark_{show_id}_{nx['id']}"):
                    log_watch(conn, nx["id"], rating=None, notes=None)
                    st.success("Marked watched.")
                    st.rerun()
            with c2:
                if st.button("Open Episodes", key=f"open_{show_id}"):
                    st.session_state["open_show"] = show_id
                    st.rerun()

            if st.session_state.get("open_show") == show_id:
                st.markdown("---")
                seasons = list_seasons(conn, show_id)
                season_map = {f"S{r['season_number']} ({r['episode_count']} eps)": r["season_number"] for r in seasons}
                sel = st.selectbox("Season", options=list(season_map.keys()))
                season_num = season_map[sel]
                eps = show_episodes(conn, show_id, season=season_num)
                for ep in eps:
                    with st.container(border=True):
                        header = f"S{ep['season_number']}E{ep['episode_number']}: {ep['name'] or '—'}"
                        st.markdown(f"**{header}**")
                        meta = f"Air: {ep['air_date'] or '—'}"
                        if ep["last_watched_at"]:
                            meta += f" • Last watched: {ep['last_watched_at']}"
                        st.caption(meta)
                        if ep["overview"]:
                            with st.expander("Episode overview"):
                                st.write(ep["overview"])

                        with st.form(f"watch_{ep['id']}"):
                            r = st.slider("Rating", 1, 5, value=ep["last_rating"] if ep["last_rating"] else 3)
                            n = st.text_area("Notes", value=ep["last_notes"] or "", height=80)
                            submitted = st.form_submit_button("Save watch")
                            if submitted:
                                log_watch(conn, ep["id"], rating=r, notes=n)
                                st.success("Saved.")
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
            if nx:
                st.write(f"**Next Up:** S{nx['season_number']}E{nx['episode_number']} — {nx['name'] or '—'}")
                if st.button(f"Mark watched: S{nx['season_number']}E{nx['episode_number']}", key=f"nx_{nx['id']}"):
                    log_watch(conn, nx["id"], rating=None, notes=None)
                    st.success("Marked watched.")
                    st.rerun()
            else:
                st.write("All caught up on logged episodes.")


def page_alerts(conn: sqlitecloud.Connection, sql_api_key: str):
    st.subheader("Alerts & Updates")

    cfg = get_alert_config(conn)
    email_enabled = st.toggle("Enable email alerts", value=bool(cfg["email_enabled"]))
    email_to = st.text_input("Send alerts to this email", value=cfg["email_to"] or ALERT_EMAIL_TO_DEFAULT)

    sms_enabled = st.toggle("Enable SMS alerts (Twilio)", value=bool(cfg["sms_enabled"]))
    sms_to = st.text_input("Send alerts to this phone (E.164, e.g., +15551234567)",
                           value=cfg["sms_to"] or ALERT_SMS_TO_DEFAULT)

    if st.button("Save alert settings"):
        save_alert_config(conn, email_to, sms_to, email_enabled, sms_enabled)
        st.success("Saved alert preferences.")

    st.markdown("---")
    st.caption("Manually check TMDB for updated 'next episode' dates and send alerts for changes.")
    if st.button("Check now"):
        if not DEFAULT_API_KEY and not st.session_state.get("api_key"):
            st.warning("Enter your TMDB API key in the sidebar first.")
        else:
            api_used = st.session_state.get("api_key") or DEFAULT_API_KEY
            result = check_and_alert_updates(conn, api_used)
            st.success(f"Updated shows: {result['updated']} • "
                       f"Emails sent: {result['emailed']} • "
                       f"SMS sent: {result['texted']}")


# ----------------------------
# Main
# ----------------------------
def main():
    st.set_page_config(page_title="Show Tracker", layout="wide")
    st.title("📺 Personal Show Tracker")

    with st.sidebar:
        st.markdown("### Settings")
        api_key = st.text_input("TMDB API Key", value=DEFAULT_API_KEY, type="password")
        st.session_state["api_key"] = api_key
        if not api_key:
            st.warning("Enter your TMDB API key to search/import.")

        conn = get_conn()
        init_db(conn)
        st.info("SQLite DB ready.")

        st.markdown("---")
        st.caption("Powered by TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.")

    tabs = st.tabs(["Add Show", "Watchlist", "Next Up", "Alerts", "Profile"])
    with tabs[0]:
        page_add_show(conn, api_key)
    with tabs[1]:
        page_watchlist(conn)
    with tabs[2]:
        page_next_up(conn)
    with tabs[3]:
        page_alerts(conn, api_key)
    with tabs[4]:
        with st.form("alerts"):
            email_to = st.text_input("Alert Email", value=current_email)
            sms_to = st.text_input("Phone Number", value=current_sms)
            carrier = st.selectbox("Carrier", ["", "AT&T", "Verizon", "TMobile", "Sprint"], index=0)
            email_enabled = st.checkbox("Enable Email Alerts", value=bool(current_email_enabled))
            sms_via_email_enabled = st.checkbox("Enable SMS via Email",
                                                value=bool(current_sms_via_email_enabled))

            if st.form_submit_button("Save"):
                conn.execute("""
                    UPDATE alert_config
                    SET email_to=?, sms_to=?, carrier=?, email_enabled=?, sms_via_email_enabled=?
                    WHERE id=1
                """, cast(tuple[Any], (email_to, sms_to, carrier, int(email_enabled), int(sms_via_email_enabled))))
                conn.commit()
                st.success("Alert preferences updated")


if __name__ == "__main__":
    main()
