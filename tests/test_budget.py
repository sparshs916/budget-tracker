"""Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest"""
import base64
import datetime
import os
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

os.environ.update({
    "DATABASE_URL": DB_URL or "",
    "APP_PASSWORD_HASH": generate_password_hash("correct horse battery"),
    "SECRET_KEY": "test-secret",
    "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "CRON_SECRET": "cron-secret",
})

from budget import app as app_module, db, queries, simplefin, sync  # noqa: E402
from budget.app import app  # noqa: E402
from budget.sync import save_connection, sync_enrollment  # noqa: E402

TODAY = datetime.date(2026, 10, 9)
TZ = ZoneInfo("America/Los_Angeles")
ACCESS_URL = "https://user:secretpass@bridge.example/simplefin"


def K(account, txn=None):
    """Stored ids: accounts are keyed by org + id, transactions by account + id."""
    return f"chase.com:{account}" + (f":{txn}" if txn else "")


def ts(date):
    return int(datetime.datetime.combine(datetime.date.fromisoformat(date), datetime.time(12), TZ).timestamp())


# SimpleFIN convention: positive = money into the account, on every account
# type. A card purchase is negative; a card payment is positive.
def txn(id, date, amount, description, payee=None, pending=False):
    t = {"id": id, "amount": amount, "description": description, "transacted_at": ts(date),
         "posted": 0 if pending else ts(date), "pending": pending}
    if payee:
        t["payee"] = payee
    return t


OCT = {
    "chk": [
        txn("c1", "2026-10-01", "2701.00", "ACME CORP PAYROLL"),
        txn("c2", "2026-10-02", "-1650.00", "ESSEX PROPERTY RENT"),
        txn("c3", "2026-10-03", "-500.00", "Online Transfer to SAV ...2222"),
        txn("c4", "2026-10-05", "-300.00", "CHASE CREDIT CRD AUTOPAY"),
        txn("c5", "2026-10-06", "-92.00", "PGANDE WEB ONLINE PG&E"),
        txn("c6", "2026-09-28", "-999.00", "Old September thing"),
    ],
    "sav": [
        txn("s1", "2026-10-03", "500.00", "Online Transfer from CHK ...1111"),
        txn("s2", "2026-10-04", "2.10", "Interest"),
    ],
    "cc": [
        txn("k1", "2026-10-02", "-45.10", "TRADER JOE'S #123", payee="Trader Joe's"),
        txn("k2", "2026-10-03", "-15.49", "NETFLIX.COM", payee="Netflix"),
        txn("k3", "2026-10-05", "300.00", "AUTOMATIC PAYMENT - THANK YOU"),
        txn("k4", "2026-10-07", "-30.00", "CHEWY.COM", payee="Chewy"),
        txn("k5", "2026-10-08", "10.00", "CHEWY.COM refund", payee="Chewy"),
        txn("k6", "2026-10-09", "-12.00", "TST* BLUE BOTTLE", pending=True),
    ],
}
ACCOUNTS = [
    ("chk", "Everyday Checking", "3412.55"),
    ("sav", "Way2Save Savings", "12500.00"),
    ("cc", "Sapphire Preferred", "-842.17"),
]


class FakeSimpleFIN:
    def __init__(self, txns, errors=()):
        self.txns = txns
        self.errors = list(errors)
        self.calls = []

    def accounts(self, start, end):
        self.calls.append((start, end))
        return {
            "errors": self.errors,
            "accounts": [
                {"org": {"domain": "chase.com", "name": "Chase", "sfin-url": "https://bridge.example/simplefin"},
                 "id": id, "name": name, "currency": "USD", "balance": balance, "balance-date": ts("2026-10-09"),
                 "transactions": [t for t in self.txns.get(id, [])
                                  if start <= (t["posted"] or t["transacted_at"]) < end]}
                for id, name, balance in ACCOUNTS
            ],
        }


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, login_attempts CASCADE")
        save_connection(c, ACCESS_URL)
        c.commit()
        enrollment = c.execute("SELECT * FROM enrollments").fetchone()
        result = sync_enrollment(c, enrollment, today=TODAY, client=FakeSimpleFIN(OCT))
        assert "error" not in result
        yield c


def month_filters(**kw):
    return {"start": datetime.date(2026, 10, 1), "end": TODAY, "account": "budget", **kw}


def amounts(rows):
    return {r["category"]: r["amount"] for r in rows}


def resync(conn, client):
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    return sync_enrollment(conn, enrollment, today=TODAY, client=client)


def test_access_url_encrypted_at_rest(conn):
    stored = conn.execute("SELECT access_token FROM enrollments").fetchone()["access_token"]
    assert "secretpass" not in stored


def test_account_types_guessed_from_names(conn):
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM accounts").fetchall()}
    assert (rows[K("chk")]["subtype"], rows[K("chk")]["include_in_budget"]) == ("checking", True)
    assert (rows[K("sav")]["subtype"], rows[K("sav")]["include_in_budget"]) == ("savings", False)
    assert (rows[K("cc")]["type"], rows[K("cc")]["include_in_budget"]) == ("credit", True)
    assert rows[K("cc")]["ledger_balance"] == Decimal("-842.17")


def test_categories_and_math(conn):
    s = queries.summary(conn, month_filters())
    assert amounts(s["income"]) == {"Income": Decimal("2701.00")}
    exp = amounts(s["expenses"])
    assert exp["Rent"] == Decimal("1650.00")
    assert exp["Electricity"] == Decimal("92.00")
    assert exp["Groceries"] == Decimal("45.10")
    assert exp["Subscriptions"] == Decimal("15.49")
    assert exp["Pet Food & Supplies"] == Decimal("20.00")  # $30 purchase - $10 refund
    assert exp["Dining Out"] == Decimal("12.00")            # pending counts
    assert exp["Savings"] == Decimal("500.00")
    # Card payment shows up on both sides, but is excluded from totals.
    assert amounts(s["excluded"]) == {"Credit Card Payment": Decimal("0.00")}
    total = Decimal("1650") + Decimal("92") + Decimal("45.10") + Decimal("15.49") + Decimal("20") + Decimal("12") + Decimal("500")
    assert s["total_expenses"] == total
    assert s["remaining"] == Decimal("2701.00") - total
    assert s["total_spending"] == total - 500


def test_savings_account_excluded_by_default_but_viewable(conn):
    rows = queries.transactions(conn, month_filters(), 1, 50)["rows"]
    assert not any(r["id"].startswith(K("sav")) for r in rows)
    sav = queries.summary(conn, month_filters(account=K("sav")))
    assert amounts(sav["excluded"]) == {"Transfer": Decimal("-500.00")}


def test_credit_purchase_is_money_out(conn):
    r = conn.execute("SELECT flow, raw_amount, status FROM transactions WHERE id = %s", (K("cc", "k1"),)).fetchone()
    assert r["raw_amount"] == Decimal("-45.10") and r["flow"] == Decimal("-45.10")
    pending = conn.execute("SELECT status, date FROM transactions WHERE id = %s", (K("cc", "k6"),)).fetchone()
    assert pending["status"] == "pending" and pending["date"] == TODAY


def test_first_sync_fetches_history_in_chunks():
    client = FakeSimpleFIN(OCT)
    with db.connect() as c:
        c.execute("TRUNCATE enrollments CASCADE")
        save_connection(c, ACCESS_URL)
        resync(c, client)
    assert len(client.calls) == 3  # 90 days of history in 40-day chunks
    assert all(a[1] == b[0] for a, b in zip(client.calls, client.calls[1:]))
    assert all(end - start <= 45 * 86400 for start, end in client.calls)


def test_resync_drops_vanished_pending(conn):
    posted = dict(OCT, cc=[t for t in OCT["cc"] if t["id"] != "k6"]
                  + [txn("k6b", "2026-10-09", "-12.50", "TST* BLUE BOTTLE")])
    client = FakeSimpleFIN(posted)
    resync(conn, client)
    assert len(client.calls) == 1  # later syncs only re-fetch the recent window
    ids = {r["id"] for r in conn.execute("SELECT id FROM transactions WHERE account_id = %s", (K("cc"),)).fetchall()}
    assert K("cc", "k6") not in ids and K("cc", "k6b") in ids


def test_bank_added_later_gets_full_history(conn):
    ACCOUNTS.append(("cc2", "Freedom Unlimited", "-50.00"))
    try:
        later = dict(OCT, cc2=[txn("f1", "2026-08-15", "-20.00", "SHELL OIL 57444"),
                               txn("f2", "2026-10-08", "-5.00", "STARBUCKS")])
        client = FakeSimpleFIN(later)
        resync(conn, client)
    finally:
        ACCOUNTS.pop()
    assert len(client.calls) == 4  # recent window, then 90 days of history for the new card
    ids = {r["id"] for r in conn.execute("SELECT id FROM transactions WHERE account_id = %s", (K("cc2"),)).fetchall()}
    assert ids == {K("cc2", "f1"), K("cc2", "f2")}
    assert conn.execute("SELECT history_synced FROM accounts WHERE id = %s", (K("cc2"),)).fetchone()["history_synced"]


def test_bank_errors_are_reported_and_keep_existing_rows(conn):
    result = resync(conn, FakeSimpleFIN({}, errors=["Chase: You must reauthenticate."]))
    assert result["error"] == "Chase: You must reauthenticate."
    e = conn.execute("SELECT status, last_error FROM enrollments").fetchone()
    assert (e["status"], e["last_error"]) == ("attention", "Chase: You must reauthenticate.")
    assert conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 14


def test_changing_account_type_sticks_and_recategorizes(conn):
    client = app.test_client()
    csrf = login(client)
    payment = K("cc", "k3")
    category = lambda: queries.transactions(conn, month_filters(account="all", q="AUTOMATIC"), 1, 5)["rows"][0]["category"]  # noqa: E731
    assert category() == "Credit Card Payment"

    resp = client.post("/api/accounts/update", json={"id": K("cc"), "subtype": "checking"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    conn.rollback()
    assert category() == "Other Income"

    assert client.post("/api/accounts/update", json={"id": "missing", "subtype": "checking"},
                       headers={"X-CSRF-Token": csrf}).status_code == 404

    resync(conn, FakeSimpleFIN(OCT))
    row = conn.execute("SELECT subtype, type_locked FROM accounts WHERE id = %s", (K("cc"),)).fetchone()
    assert (row["subtype"], row["type_locked"]) == ("checking", True)

    client.post("/api/accounts/update", json={"id": K("cc"), "subtype": "credit_card"}, headers={"X-CSRF-Token": csrf})
    conn.rollback()
    assert category() == "Credit Card Payment"
    assert conn.execute("SELECT 1 FROM transactions WHERE id = %s", (payment,)).fetchone()


def test_pagination_and_filters(conn):
    page1 = queries.transactions(conn, month_filters(account="all"), 1, 5)
    page2 = queries.transactions(conn, month_filters(account="all"), 2, 5)
    assert page1["total"] == 13
    assert len(page1["rows"]) == 5 and len(page2["rows"]) == 5
    assert not {r["id"] for r in page1["rows"]} & {r["id"] for r in page2["rows"]}
    cc = queries.transactions(conn, month_filters(account="type:credit"), 1, 50)
    assert cc["total"] == 6
    assert queries.transactions(conn, month_filters(q="chewy"), 1, 50)["total"] == 2


def login(client):
    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    assert client.post("/login", data={"password": "correct horse battery", "csrf": csrf}).status_code == 302
    with client.session_transaction() as s:
        return s["csrf"]


def test_web_flow(conn):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/summary").status_code == 401

    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    assert client.post("/login", data={"password": "nope", "csrf": csrf}).status_code == 200
    csrf = login(client)

    assert client.get("/").status_code == 200
    s = client.get("/api/summary?start=2026-10-01&end=2026-10-09").get_json()
    assert s["total_income"] == "2701.00"

    # Mutations need the CSRF header.
    chewy = K("cc", "k4")
    assert client.post("/api/transactions/category", json={"id": chewy, "category": "Shopping"}).status_code == 403
    resp = client.post("/api/transactions/category", json={"id": chewy, "category": "Shopping", "remember": True},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    s = client.get("/api/summary?start=2026-10-01&end=2026-10-09").get_json()
    exp = {r["category"]: r["amount"] for r in s["expenses"]}
    assert exp["Shopping"] == "20.00" and "Pet Food & Supplies" not in exp

    assert client.get("/api/cron/sync").status_code == 401


def test_connect_with_setup_token(conn, monkeypatch):
    claimed = []
    monkeypatch.setattr(app_module, "claim_setup_token", lambda token: claimed.append(token) or ACCESS_URL)
    monkeypatch.setattr(sync, "SimpleFINClient", lambda url: FakeSimpleFIN(OCT))
    client = app.test_client()
    csrf = login(client)

    resp = client.post("/api/connections", json={"setupToken": "  abc  "}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200 and resp.get_json()["accounts"] == 3
    assert claimed == ["abc"]
    # The accounts moved to the new connection, so the old, now-empty one is cleaned up.
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) AS n FROM enrollments").fetchone()["n"] == 1

    resp = client.post("/api/connections", json={"setupToken": ""}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_login_lockout(conn):
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    for _ in range(5):
        client.post("/login", data={"password": "bad", "csrf": csrf})
    assert client.post("/login", data={"password": "correct horse battery", "csrf": csrf}).status_code == 429


# --- SimpleFIN client (no database needed, but kept here with the other sync tests) ---

class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_claim_setup_token(monkeypatch):
    token = base64.b64encode(b"https://bridge.example/simplefin/claim/XYZ").decode()
    posts = []
    monkeypatch.setattr(simplefin.requests, "post",
                        lambda url, timeout: posts.append(url) or FakeResponse(200, ACCESS_URL + "\n"))
    assert simplefin.claim_setup_token(token) == ACCESS_URL
    assert posts == ["https://bridge.example/simplefin/claim/XYZ"]

    monkeypatch.setattr(simplefin.requests, "post", lambda url, timeout: FakeResponse(403))
    with pytest.raises(simplefin.SimpleFINError, match="already used"):
        simplefin.claim_setup_token(token)
    with pytest.raises(simplefin.SimpleFINError, match="doesn't look like"):
        simplefin.claim_setup_token("not a token!")
    with pytest.raises(simplefin.SimpleFINError, match="doesn't look like"):
        simplefin.claim_setup_token(base64.b64encode(b"http://insecure.example/claim").decode())


def test_client_keeps_credentials_out_of_url():
    c = simplefin.SimpleFINClient(ACCESS_URL)
    assert c.base_url == "https://bridge.example/simplefin"
    assert c.auth == ("user", "secretpass")


def test_error_messages_accepts_both_formats():
    assert simplefin.error_messages({"errors": ["a"], "errlist": [{"msg": "b"}, "c"]}) == ["a", "b", "c"]


def test_compare_account_finds_gaps(conn):
    # Pretend an earlier sync lost one Chase transaction.
    conn.execute("DELETE FROM transactions WHERE id = %s", (K("cc", "k2"),))
    conn.commit()
    r = sync.compare_account(conn, K("cc"), datetime.date(2026, 10, 1), TODAY, client=FakeSimpleFIN(OCT))
    assert r["found"] and r["simplefin_count"] == 6 and r["app_count"] == 5
    assert [m["description"] for m in r["missing"]] == ["NETFLIX.COM"]
    assert r["not_returned"] == []
    assert sync.compare_account(conn, "nope", datetime.date(2026, 10, 1), TODAY) is None


def test_full_resync_refills_history(conn, monkeypatch):
    conn.execute("DELETE FROM transactions WHERE id = %s", (K("cc", "k2"),))
    conn.commit()
    client = FakeSimpleFIN(OCT)
    monkeypatch.setattr(sync, "SimpleFINClient", lambda url: client)
    monkeypatch.setattr(app_module, "today", lambda: TODAY)
    web = app.test_client()
    csrf = login(web)
    resp = web.get("/api/accounts/compare", query_string={"check_account": K("cc"), "start": "2026-10-01",
                                                          "end": "2026-10-09"})
    assert resp.status_code == 200 and len(resp.get_json()["missing"]) == 1

    assert web.post("/api/sync?full=1", headers={"X-CSRF-Token": csrf}).status_code == 200
    conn.rollback()
    assert conn.execute("SELECT 1 FROM transactions WHERE id = %s", (K("cc", "k2"),)).fetchone()
