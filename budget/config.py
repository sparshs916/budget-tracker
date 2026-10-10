import os

from dotenv import load_dotenv

load_dotenv()


def _env(name, default=None):
    value = os.getenv(name, default)
    if not isinstance(value, str):
        return value
    value = value.strip()
    # Values copied from .env into the Vercel dashboard often keep their quotes.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


# Postgres connection string. Vercel's Neon integration sets DATABASE_URL.
DATABASE_URL = _env("DATABASE_URL") or _env("POSTGRES_URL")

# Login: the hash produced by scripts/setup_secrets.py
APP_PASSWORD_HASH = _env("APP_PASSWORD_HASH")
# Signs the session cookie. Any long random string.
SECRET_KEY = _env("SECRET_KEY")
# Fernet key used to encrypt SimpleFIN access URLs at rest.
TOKEN_ENCRYPTION_KEY = _env("TOKEN_ENCRYPTION_KEY")
# Vercel sends "Authorization: Bearer $CRON_SECRET" on cron requests.
CRON_SECRET = _env("CRON_SECRET")

# SimpleFIN amounts already use one convention for every account: positive
# means money came into the account, so a card purchase is negative. Set to
# "true" only if your card purchases show up as income.
INVERT_CREDIT_AMOUNTS = _env("INVERT_CREDIT_AMOUNTS", "false").lower() == "true"

# How far back the first sync of a new connection goes, and how many recent
# days every later sync re-fetches (pending transactions settle and change).
# SimpleFIN rate-limits requests, and history is fetched in 60-day chunks.
INITIAL_HISTORY_DAYS = int(_env("INITIAL_HISTORY_DAYS", "90"))
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
    }
    return [k for k, v in required.items() if not v]
