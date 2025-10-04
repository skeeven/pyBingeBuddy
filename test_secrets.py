import re
import streamlit as st

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
