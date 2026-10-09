"""Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest"""
import datetime
import os
from decimal import Decimal

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
    "TELLER_APP_ID": "app_test",
    "TELLER_CERT": "x",
    "TELLER_PRIVATE_KEY": "x",
})

from budget import db, queries  # noqa: E402
from budget.app import app  # noqa: E402
from budget.sync import save_enrollment, sync_enrollment  # noqa: E402

TODAY = datetime.date(2026, 10, 9)


def txn(id, date, amount, description, type="card_payment", category=None, merchant=None, status="posted"):
    return {"id": id, "date": date, "amount": amount, "description": description, "type": type,
            "status": status, "details": {"category": category, "counterparty": {"name": merchant}}}


class FakeTeller:
    def __init__(self, txns):
        self.txns = txns

    def accounts(self):
        return [
            {"id": "chk", "enrollment_id": "enr", "name": "Checking", "type": "depository", "subtype": "checking",
             "last_four": "1111", "status": "open", "institution": {"name": "Bank"},
             "links": {"transactions": "x", "balances": "x"}},
            {"id": "sav", "enrollment_id": "enr", "name": "Savings", "type": "depository", "subtype": "savings",
             "last_four": "2222", "status": "open", "institution": {"name": "Bank"},
             "links": {"transactions": "x", "balances": "x"}},
            {"id": "cc", "enrollment_id": "enr", "name": "Sapphire", "type": "credit", "subtype": "credit_card",
             "last_four": "3333", "status": "open", "institution": {"name": "Bank"},
             "links": {"transactions": "x", "balances": "x"}},
        ]

    def balances(self, account_id):
        return {"ledger": "100.00", "available": "90.00"}

    def transactions(self, account_id, start_date=None):
        return list(self.txns.get(account_id, []))


OCT = {
    "chk": [
        txn("c1", "2026-10-01", "2701.00", "ACME CORP PAYROLL", type="ach", category="income"),
        txn("c2", "2026-10-02", "-1650.00", "ESSEX PROPERTY RENT", type="ach"),
        txn("c3", "2026-10-03", "-500.00", "Online Transfer to SAV ...2222", type="transfer"),
        txn("c4", "2026-10-05", "-300.00", "CHASE CREDIT CRD AUTOPAY", type="ach"),
        txn("c5", "2026-10-06", "-92.00", "PGANDE WEB ONLINE PG&E", type="ach"),
        txn("c6", "2026-09-28", "-999.00", "Old September thing", type="ach"),
    ],
    "sav": [
        txn("s1", "2026-10-03", "500.00", "Online Transfer from CHK ...1111", type="transfer"),
        txn("s2", "2026-10-04", "2.10", "Interest", type="interest"),
    ],
    "cc": [
        # Teller: credit purchases positive, payments/refunds negative.
        txn("k1", "2026-10-02", "45.10", "TRADER JOE'S #123", category="groceries", merchant="Trader Joe's"),
        txn("k2", "2026-10-03", "15.49", "NETFLIX.COM", category="entertainment", merchant="Netflix"),
        txn("k3", "2026-10-05", "-300.00", "AUTOMATIC PAYMENT - THANK YOU", type="payment"),
        txn("k4", "2026-10-07", "30.00", "CHEWY.COM", category="shopping", merchant="Chewy"),
        txn("k5", "2026-10-08", "-10.00", "CHEWY.COM refund", category="shopping", merchant="Chewy"),
        txn("k6", "2026-10-09", "12.00", "TST* BLUE BOTTLE", category="dining", status="pending"),
    ],
}


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, login_attempts CASCADE")
        save_enrollment(c, "enr", "Bank", "token_abc")
        c.commit()
        enrollment = c.execute("SELECT * FROM enrollments").fetchone()
        result = sync_enrollment(c, enrollment, today=TODAY, client=FakeTeller(OCT))
        assert "error" not in result
        yield c


def month_filters(**kw):
    return {"start": datetime.date(2026, 10, 1), "end": TODAY, "account": "budget", **kw}


def amounts(rows):
    return {r["category"]: r["amount"] for r in rows}


def test_token_encrypted_at_rest(conn):
    stored = conn.execute("SELECT access_token FROM enrollments").fetchone()["access_token"]
    assert "token_abc" not in stored


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
    assert not any(r["id"].startswith("s") for r in rows)
    sav = queries.summary(conn, month_filters(account="sav"))
    assert amounts(sav["excluded"]) == {"Transfer": Decimal("-500.00")}


def test_credit_purchase_is_money_out(conn):
    r = conn.execute("SELECT flow, raw_amount FROM transactions WHERE id = 'k1'").fetchone()
    assert r["raw_amount"] == Decimal("45.10") and r["flow"] == Decimal("-45.10")


def test_resync_drops_vanished_pending(conn):
    enrollment = conn.execute("SELECT * FROM enrollments").fetchone()
    posted = dict(OCT, cc=[t for t in OCT["cc"] if t["id"] != "k6"]
                  + [txn("k6b", "2026-10-09", "12.50", "TST* BLUE BOTTLE", category="dining")])
    sync_enrollment(conn, enrollment, today=TODAY, client=FakeTeller(posted))
    ids = {r["id"] for r in conn.execute("SELECT id FROM transactions WHERE account_id='cc'").fetchall()}
    assert "k6" not in ids and "k6b" in ids


def test_pagination_and_filters(conn):
    page1 = queries.transactions(conn, month_filters(account="all"), 1, 5)
    page2 = queries.transactions(conn, month_filters(account="all"), 2, 5)
    assert page1["total"] == 13
    assert len(page1["rows"]) == 5 and len(page2["rows"]) == 5
    assert not {r["id"] for r in page1["rows"]} & {r["id"] for r in page2["rows"]}
    cc = queries.transactions(conn, month_filters(account="type:credit"), 1, 50)
    assert cc["total"] == 6
    assert queries.transactions(conn, month_filters(q="chewy"), 1, 50)["total"] == 2


def test_web_flow(conn):
    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/summary").status_code == 401

    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    assert client.post("/login", data={"password": "nope", "csrf": csrf}).status_code == 200
    resp = client.post("/login", data={"password": "correct horse battery", "csrf": csrf})
    assert resp.status_code == 302
    with client.session_transaction() as s:
        csrf = s["csrf"]

    assert client.get("/").status_code == 200
    s = client.get("/api/summary?start=2026-10-01&end=2026-10-09").get_json()
    assert s["total_income"] == "2701.00"

    # Mutations need the CSRF header.
    assert client.post("/api/transactions/k4/category", json={"category": "Shopping"}).status_code == 403
    resp = client.post("/api/transactions/k4/category", json={"category": "Shopping", "remember": True},
                       headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    s = client.get("/api/summary?start=2026-10-01&end=2026-10-09").get_json()
    exp = {r["category"]: r["amount"] for r in s["expenses"]}
    assert exp["Shopping"] == "20.00" and "Pet Food & Supplies" not in exp

    assert client.get("/api/cron/sync").status_code == 401


def test_login_lockout(conn):
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    for _ in range(5):
        client.post("/login", data={"password": "bad", "csrf": csrf})
    assert client.post("/login", data={"password": "correct horse battery", "csrf": csrf}).status_code == 429
