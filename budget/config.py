import os

from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


# Postgres connection string. Vercel's Neon integration sets DATABASE_URL.
DATABASE_URL = _env("DATABASE_URL") or _env("POSTGRES_URL")

# Login: the hash produced by scripts/hash_password.py
APP_PASSWORD_HASH = _env("APP_PASSWORD_HASH")
# Signs the session cookie. Any long random string.
SECRET_KEY = _env("SECRET_KEY")
# Fernet key used to encrypt Teller access tokens at rest.
TOKEN_ENCRYPTION_KEY = _env("TOKEN_ENCRYPTION_KEY")
# Vercel sends "Authorization: Bearer $CRON_SECRET" on cron requests.
CRON_SECRET = _env("CRON_SECRET")

# Teller
TELLER_APP_ID = _env("TELLER_APP_ID")
TELLER_ENV = _env("TELLER_ENV", "development")  # sandbox | development | production
# PEM contents of certificate.pem / private_key.pem (raw or base64-encoded).
TELLER_CERT = _env("TELLER_CERT")
TELLER_PRIVATE_KEY = _env("TELLER_PRIVATE_KEY")
TELLER_BASE_URL = "https://api.teller.io"

# Teller reports credit card purchases as positive amounts (they increase what
# you owe) and payments/refunds as negative. Depository accounts are the
# opposite: money out is negative. We flip credit amounts so every stored
# "flow" means the same thing: negative = money left you, positive = came in.
# Set to "false" if your card purchases show up as income.
CREDIT_PURCHASES_ARE_POSITIVE = _env("CREDIT_PURCHASES_ARE_POSITIVE", "true").lower() != "false"

# How far back the first sync of a new account goes, and how many recent days
# every later sync re-fetches (pending transactions settle and change).
INITIAL_HISTORY_DAYS = int(_env("INITIAL_HISTORY_DAYS", "365"))
RESYNC_WINDOW_DAYS = int(_env("RESYNC_WINDOW_DAYS", "21"))

# Dates are bucketed into months in this zone.
TIMEZONE = _env("TIMEZONE", "America/Los_Angeles")

IS_PRODUCTION = bool(_env("VERCEL")) or _env("FLASK_ENV") == "production"


def missing_settings():
    required = {
        "DATABASE_URL": DATABASE_URL,
        "APP_PASSWORD_HASH": APP_PASSWORD_HASH,
        "SECRET_KEY": SECRET_KEY,
        "TOKEN_ENCRYPTION_KEY": TOKEN_ENCRYPTION_KEY,
        "TELLER_APP_ID": TELLER_APP_ID,
        "TELLER_CERT": TELLER_CERT,
        "TELLER_PRIVATE_KEY": TELLER_PRIVATE_KEY,
    }
    return [k for k, v in required.items() if not v]
