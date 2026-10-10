"""Generate the secret env vars for .env and Vercel.

    python scripts/setup_secrets.py >> .env

Prompts for your login password (not echoed) and prints APP_PASSWORD_HASH,
SECRET_KEY, TOKEN_ENCRYPTION_KEY and CRON_SECRET as NAME=value lines, so the
output can go straight into .env without passing through your clipboard.
"""
import getpass
import secrets
import sys

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

password = getpass.getpass("Choose a login password (12+ chars): ")
if len(password) < 12:
    sys.exit("Use at least 12 characters.")
if getpass.getpass("Again: ") != password:
    sys.exit("Passwords didn't match.")

print(f"APP_PASSWORD_HASH={generate_password_hash(password, method='scrypt')}")
print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"TOKEN_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
print(f"CRON_SECRET={secrets.token_urlsafe(32)}")
print("Keep TOKEN_ENCRYPTION_KEY safe: if it changes, you must reconnect SimpleFIN.", file=sys.stderr)
