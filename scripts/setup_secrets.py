"""Generate the secret env vars for Vercel.

    python scripts/setup_secrets.py

Prompts for your login password (not echoed) and prints APP_PASSWORD_HASH,
SECRET_KEY, TOKEN_ENCRYPTION_KEY and CRON_SECRET. It also prints your Teller
certificate and key as base64 if you pass their paths:

    python scripts/setup_secrets.py teller-certs/certificate.pem teller-certs/private_key.pem
"""
import base64
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

print()
print(f"APP_PASSWORD_HASH={generate_password_hash(password, method='scrypt')}")
print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
print(f"TOKEN_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
print(f"CRON_SECRET={secrets.token_urlsafe(32)}")

if len(sys.argv) == 3:
    for name, path in (("TELLER_CERT", sys.argv[1]), ("TELLER_PRIVATE_KEY", sys.argv[2])):
        with open(path, "rb") as f:
            print(f"{name}={base64.b64encode(f.read()).decode()}")
print("\nKeep TOKEN_ENCRYPTION_KEY safe: if it changes, you must reconnect your banks.")
