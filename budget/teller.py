import base64
import os
import tempfile

import requests

from . import config

_cert_paths = None


def _pem(value):
    """Accept PEM text (with real or escaped newlines) or base64 of the PEM."""
    value = value.strip()
    if "-----BEGIN" not in value:
        value = base64.b64decode(value).decode()
    return value.replace("\\n", "\n") + "\n"


def _client_cert():
    # Teller requires mutual TLS. requests wants file paths, and Vercel only lets
    # us write to /tmp, so materialise the env vars there once per instance.
    global _cert_paths
    if _cert_paths is None:
        directory = tempfile.mkdtemp(prefix="teller-")
        cert_path = os.path.join(directory, "certificate.pem")
        key_path = os.path.join(directory, "private_key.pem")
        for path, value in ((cert_path, config.TELLER_CERT), (key_path, config.TELLER_PRIVATE_KEY)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(_pem(value))
        _cert_paths = (cert_path, key_path)
    return _cert_paths


class TellerError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def needs_reconnect(self):
        return self.status in (401, 403) or (self.code or "").startswith("enrollment.")


class TellerClient:
    def __init__(self, access_token):
        self.session = requests.Session()
        self.session.auth = (access_token, "")
        self.session.cert = _client_cert()

    def _get(self, path, params=None):
        response = self.session.get(config.TELLER_BASE_URL + path, params=params, timeout=25)
        if response.status_code >= 400:
            code, message = None, response.text[:300]
            try:
                err = response.json().get("error", {})
                code, message = err.get("code"), err.get("message", message)
            except ValueError:
                pass
            raise TellerError(response.status_code, code, message)
        return response.json()

    def accounts(self):
        return self._get("/accounts")

    def balances(self, account_id):
        return self._get(f"/accounts/{account_id}/balances")

    def transactions(self, account_id, start_date=None, page_size=500):
        """All transactions on or after start_date, following from_id pagination."""
        results, from_id = [], None
        while True:
            params = {"count": page_size}
            if start_date:
                params["start_date"] = start_date.isoformat()
            if from_id:
                params["from_id"] = from_id
            page = self._get(f"/accounts/{account_id}/transactions", params)
            results.extend(page)
            if len(page) < page_size:
                return results
            from_id = page[-1]["id"]
