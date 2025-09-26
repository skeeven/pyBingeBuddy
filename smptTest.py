import smtplib
from email.mime.text import MIMEText
import os
from dotenv import load_dotenv

# Try .env (local dev) then Streamlit secrets
try:
    load_dotenv()
except ImportError:
    pass

# Email env
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ALERT_EMAIL_TO_DEFAULT = os.getenv("ALERT_EMAIL_TO")

msg = MIMEText("Test email from BingeBuddy")
msg["Subject"] = "Test Alert OS Variables"
msg["From"] = "binge.buddy@mightymiraclemax.com"
msg["To"] = "murraysc@gmail.com"

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(SMTP_USER, SMTP_PASS)
    server.sendmail(msg["From"], [msg["To"]], msg.as_string())
