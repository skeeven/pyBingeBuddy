import re
import streamlit as st
import sqlitecloud

st.set_page_config(page_title="Secrets Debugger", page_icon="🔐", layout="centered")
st.title("🔐 Streamlit Cloud Secrets Debugger")


def mask(v: str) -> str:
    s = str(v)
    if len(s) <= 10:
        return "********"
    return s[:6] + "..." + s[-4:]


def is_bool_like(v) -> bool:
    if isinstance(v, bool):
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off")
    return False


def to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "on")


def validate_sqlite_url(url: str) -> tuple[bool, str]:
    """
    Expecting: sqlitecloud://HOST[:8860]/DBNAME?apikey=KEY
    """
    if not isinstance(url, str) or not url:
        return False, "URL is empty or not a string"
    if not url.startswith("sqlitecloud://"):
        return False, "URL must start with sqlitecloud://"
    # quick structure check
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


def init_db(conn: sqlitecloud.Connection) -> None:
    schema = """

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            carrier TEXT,
            enable_email INTEGER,
            enable_sms INTEGER,
            password_hash TEXT NOT NULL
        );

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

        CREATE TABLE IF NOT EXISTS user_shows (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            show_id INTEGER,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(show_id) REFERENCES shows(id),
            UNIQUE(user_id, show_id)
        );

        CREATE TABLE IF NOT EXISTS watches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            episode_id      INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
            watched_at      TEXT NOT NULL,
            rating          INTEGER,
            notes           TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(episode_id) REFERENCES episodes(id)
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

        INSERT OR IGNORE INTO alert_config (id, email_to, sms_to, carrier, sms_via_email_enabled,
        email_enabled, sms_enabled)
        VALUES (1, NULL, NULL, NULL, 0, 0, 0);

        CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id);
        CREATE INDEX IF NOT EXISTS idx_watches_episode ON watches(episode_id);
        """

    # Run statements one by one
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt:  # skip empty lines
            conn.execute(stmt)
    conn.commit()


# 1) Show raw keys loaded from st.secrets (masked when sensitive)
st.subheader("Loaded Secrets (masked)")
if len(st.secrets) == 0:
    st.error("No secrets found. On Streamlit Cloud, set them in: App → Settings → Secrets.")
else:
    for k, v in st.secrets.items():
        lower = k.lower()
        sensitive = any(s in lower for s in ["pass", "key", "token", "apikey", "secret"])
        if "url" in lower:  # URLs can hold secrets too
            sensitive = True
        display = mask(v) if sensitive else v
        st.write(f"**{k}** → `{display}`  _(type: {type(v).__name__})_")

st.divider()

# 2) Specific checks
st.subheader("Targeted Checks")

# SQLite Cloud URL
sqlite_ok = False
sqlite_msg = "Missing"
sqlite_url = st.secrets.get("SQLITE_CLOUD_URL")
if sqlite_url:
    sqlite_ok, sqlite_msg = validate_sqlite_url(sqlite_url)

st.write("**SQLITE_CLOUD_URL**")
st.code((sqlite_url[:60] + "…") if isinstance(sqlite_url, str) and len(sqlite_url) > 60 else str(sqlite_url))
st.markdown(("✅ " if sqlite_ok else "❌ ") + sqlite_msg)

try:
    conn = sqlitecloud.connect(sqlite_url)
    init_db(conn)
    conn.execute("SELECT 1;")  # sanity probe
    st.success("Connected to SQLite Cloud.")
except Exception as e:
    st.error(f"Connection failed: {e}")
    st.stop()

# ---------- List tables ----------
st.subheader("📋 Tables")
try:
    # Works across SQLite versions:
    rows = conn.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name;").fetchall()
    table_names = [r[0] for r in rows]
    if not table_names:
        st.info("No tables found in this database.")
    else:
        st.write(f"Found **{len(table_names)}** tables:")
        st.code("\n".join(table_names))

        # Optional: row counts
        with st.expander("Show row counts"):
            counts = []
            for t in table_names:
                try:
                    c = conn.execute(f"SELECT COUNT(*) FROM \"{t}\";").fetchone()[0]
                except Exception:
                    c = "n/a"
                counts.append((t, c))
            st.table({"table": [t for t, _ in counts], "rows": [c for _, c in counts]})

        # Optional: inspect schema for a selected table
        with st.expander("Inspect schema (PRAGMA table_info)"):
            selected = st.selectbox("Pick a table", table_names)
            if selected:
                cols = conn.execute(f'PRAGMA table_info("{selected}");').fetchall()
                # cols: cid, name, type, notnull, dflt_value, pk
                st.table({
                    "cid": [c[0] for c in cols],
                    "name": [c[1] for c in cols],
                    "type": [c[2] for c in cols],
                    "notnull": [c[3] for c in cols],
                    "default": [c[4] for c in cols],
                    "pk": [c[5] for c in cols],
                })
except Exception as e:
    st.error(f"Error listing tables: {e}")

# TMDB API key(s)
tmdb_v3 = st.secrets.get("TMDB_API_KEY") or st.secrets.get("DEFAULT_API_KEY")
st.write("**TMDB API Key (v3)**")
if tmdb_v3:
    st.write("✅ Found:", f"`{mask(tmdb_v3)}`")
else:
    st.write("❌ Not found. Add `TMDB_API_KEY` (or `DEFAULT_API_KEY`).")

# SMTP settings
smtp_host = st.secrets.get("SMTP_HOST")
smtp_port = st.secrets.get("SMTP_PORT")
smtp_tls = st.secrets.get("SMTP_USE_TLS")
smtp_user = st.secrets.get("SMTP_USER")
smtp_pass = st.secrets.get("SMTP_PASS")

st.write("**SMTP Settings (Zoho)**")
st.write("- HOST:", smtp_host or "❌ missing")
st.write("- PORT:", smtp_port if smtp_port is not None else "❌ missing")
st.write("- USE_TLS:", (f"{smtp_tls} (ok)" if is_bool_like(smtp_tls) else f"{smtp_tls} ❌ not boolean-like"))
st.write("- USER:", smtp_user or "❌ missing")
st.write("- PASS:", mask(smtp_pass) if smtp_pass else "❌ missing")

# Convert and show the interpreted TLS
if is_bool_like(smtp_tls):
    st.caption(f"Interpreted SMTP_USE_TLS = {to_bool(smtp_tls)}")

st.divider()
st.caption("Tip: In Streamlit Cloud, repo secrets.toml is ignored. Use the Cloud Secrets UI only.")
