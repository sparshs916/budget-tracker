"""Minimal SimpleFIN client (https://www.simplefin.org/protocol.html).

A setup token is a base64-encoded claim URL. POSTing to it once returns an
access URL with Basic Auth credentials embedded; GET {access_url}/accounts
returns every account with its transactions.
"""
import base64
import binascii
from urllib.parse import urlsplit, urlunsplit

import requests

TIMEOUT = 30


class SimpleFINError(Exception):
    def __init__(self, message, needs_reconnect=False):
        super().__init__(message)
        self.message = message
        self.needs_reconnect = needs_reconnect


def claim_setup_token(setup_token):
    """Exchange a one-time setup token for a long-lived access URL."""
    try:
        claim_url = base64.b64decode(setup_token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token. Copy the whole token and try again.")
    if urlsplit(claim_url).scheme != "https":
        raise SimpleFINError("That doesn't look like a SimpleFIN setup token.")

    response = requests.post(claim_url, timeout=TIMEOUT)
    if response.status_code == 403:
        raise SimpleFINError("SimpleFIN rejected this token. It was already used or has expired. "
                             "Create a new one, and disable the old one in SimpleFIN in case someone else claimed it.")
    if response.status_code >= 400:
        raise SimpleFINError(f"SimpleFIN returned {response.status_code} when claiming the token.")
    access_url = response.text.strip()
    if urlsplit(access_url).scheme != "https":
        raise SimpleFINError("SimpleFIN sent back something other than an access URL.")
    return access_url


class SimpleFINClient:
    def __init__(self, access_url):
        parts = urlsplit(access_url)
        # Keep the credentials out of the URL so they never show up in error messages.
        self.auth = (parts.username or "", parts.password or "")
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        self.base_url = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))

    def accounts(self, start, end):
        """Account set for [start, end) as Unix timestamps, pending included."""
        try:
            response = requests.get(
                self.base_url + "/accounts",
                params={"start-date": int(start), "end-date": int(end), "pending": 1},
                auth=self.auth,
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise SimpleFINError(f"Couldn't reach SimpleFIN: {type(e).__name__}")
        if response.status_code in (401, 403):
            raise SimpleFINError("SimpleFIN no longer accepts this connection. Create a new setup token and connect again.",
                                 needs_reconnect=True)
        if response.status_code == 402:
            raise SimpleFINError("SimpleFIN says payment is required. Check your SimpleFIN subscription.",
                                 needs_reconnect=True)
        if response.status_code >= 400:
            raise SimpleFINError(f"SimpleFIN returned {response.status_code}.")
        return response.json()


def error_messages(account_set):
    """User-facing errors from an account set (`errors` strings or `errlist` objects)."""
    messages = [str(e) for e in account_set.get("errors") or []]
    for item in account_set.get("errlist") or []:
        if isinstance(item, dict):
            messages.append(str(item.get("msg") or item.get("message") or item))
        else:
            messages.append(str(item))
    return [m[:300] for m in messages if m]
