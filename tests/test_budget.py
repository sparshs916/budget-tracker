"""Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest"""
import base64
import io
import json
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
        c.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, login_attempts, ai_categories CASCADE")
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
    # Savings is shown on its own and left out of expenses and Remaining.
    total = Decimal("1650") + Decimal("92") + Decimal("45.10") + Decimal("15.49") + Decimal("20") + Decimal("12")
    assert s["total_expenses"] == s["total_spending"] == total
    assert s["remaining"] == Decimal("2701.00") - total
    assert s["total_savings"] == Decimal("500.00")


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
    # No longer a card, but still matched with the -$300 from checking: a transfer.
    assert category() == "Transfer"

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


# --- CSV uploads ---

from budget import csv_import  # noqa: E402

GOLDEN1 = '''"SAVINGS","*******7022-0"
"Date","Account","Account Type","Description","Check #","Category","Credit","Debit","Daily Balance"
"10/06/2026","SAVINGS","Savings","Withdrawal Overdraft transfer to ****022-S9","","Financial Services/Transfers","","-200.00","2515.26"
"10/01/2026","SAVINGS","Savings","Dividend Through date: 30SEP2026 (Dividend)","","Income/Interest","0.40","","2715.26"
"09/30/2026","SAVINGS","Savings","Withdrawal-ACH-A-1608460 WEBHealthy Paws (debitpmt)","","Shopping/Pets","","-49.29","2714.86"
"09/28/2026","SAVINGS","Savings","Deposit-ACH-1296959 HP ENTERPRISE CO (PAYROLL)","","Income/Payroll","2915.90","","3564.15"
"09/28/2026","SAVINGS","Savings","Withdrawal at G1 Online Transfer 10.00 to share 9","","Financial Services/Transfers","","-10.00",""
"09/28/2026","SAVINGS","Savings","Withdrawal at G1 Online Transfer 10.00 to share 9","","Financial Services/Transfers","","-10.00",""
"","","","","Totals:","6 items","2916.30","-269.29",""
'''
OLDER = '''"SAVINGS","*******7022-0"
"Date","Account","Account Type","Description","Check #","Category","Credit","Debit","Daily Balance"
"09/30/2026","SAVINGS","Savings","Withdrawal-ACH-A-1608460 WEBHealthy Paws (debitpmt)","","Shopping/Pets","","-49.29","2714.86"
"09/23/2026","SAVINGS","Savings","Withdrawal-ACH-A-987134 WEBALLY (ALLY PAYMT)","","Uncategorized Expense","","-351.75","648.25"
'''


def account_row(conn, key):
    return conn.execute("SELECT id, name, type, subtype FROM accounts WHERE id = %s", (key,)).fetchone()


def upload(conn, key, text, flip=None):
    account = account_row(conn, key)
    parsed = csv_import.parse(text.encode())
    flip = csv_import.suggest_flip(parsed, account["type"]) if flip is None else flip
    new, dups, _ = csv_import.plan(conn, account, parsed, flip)
    if new:
        csv_import.save(conn, account, new, "test.csv")
    conn.commit()
    return new, dups


def test_parse_golden1_export():
    p = csv_import.parse(GOLDEN1.encode())
    assert p["two_columns"] and p["skipped"] == 1  # the Totals line
    assert len(p["rows"]) == 7 - 1
    assert sum(r["amount"] for r in p["rows"]) == Decimal("2916.30") - Decimal("269.29")
    assert p["rows"][0]["date"] == datetime.date(2026, 10, 6) and p["rows"][0]["hint"] == "Financial Services/Transfers"


def test_parse_single_amount_column_and_flip_detection():
    chase = "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n" \
            "10/02/2026,10/03/2026,TRADER JOE'S,Groceries,Sale,-45.10,\n" \
            "10/05/2026,10/05/2026,Payment Thank You-Mobile,,Payment,300.00,\n" \
            "10/07/2026,10/08/2026,CHEWY.COM,Shopping,Sale,-30.00,\n"
    apple = "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By\n" \
            "10/02/2026,10/03/2026,UBER *TRIP,Uber,Transportation,Purchase,23.18,Me\n" \
            "10/04/2026,10/04/2026,ACH DEPOSIT INTERNET TRANSFER,Apple Card,Payment,Payment,-100.00,Me\n" \
            "10/06/2026,10/07/2026,TRADER JOES,Trader Joe's,Grocery,Purchase,\"$1,045.10\",Me\n"
    p = csv_import.parse(chase)
    assert [r["amount"] for r in p["rows"]] == [Decimal("-45.10"), Decimal("300.00"), Decimal("-30.00")]
    assert not csv_import.suggest_flip(p, "credit")
    p = csv_import.parse(apple)
    assert p["rows"][0]["merchant"] == "Uber" and p["rows"][2]["amount"] == Decimal("1045.10")
    assert csv_import.suggest_flip(p, "credit") and not csv_import.suggest_flip(p, "depository")
    with pytest.raises(csv_import.CSVImportError):
        csv_import.parse("just,some\nwords,here\n")


def test_reuploading_the_same_file_adds_nothing(conn):
    new, dups = upload(conn, K("sav"), GOLDEN1)
    assert len(new) == 6 and not dups  # the two identical $10 transfers are both kept
    new, dups = upload(conn, K("sav"), GOLDEN1)
    assert not new and len(dups) == 6 and all(d["reason"] == "Already uploaded" for d in dups)
    new, dups = upload(conn, K("sav"), GOLDEN1, flip=True)  # toggling the sign still counts as a repeat
    assert not new
    assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source = 'csv'").fetchone()["n"] == 6


def test_overlapping_exports_only_add_the_new_part(conn):
    upload(conn, K("sav"), GOLDEN1)
    new, dups = upload(conn, K("sav"), OLDER)
    assert [r["description"] for r in new] == ["Withdrawal-ACH-A-987134 WEBALLY (ALLY PAYMT)"]
    assert len(dups) == 1


def test_upload_skips_rows_simplefin_already_has(conn):
    # Chase card: SimpleFIN has k1..k6. The export words things differently and dates
    # one purchase by posting date; Netflix (k2) is the only one SimpleFIN lacks.
    conn.execute("DELETE FROM transactions WHERE id = %s", (K("cc", "k2"),))
    conn.commit()
    export = "Transaction Date,Post Date,Description,Category,Type,Amount\n" \
             "10/03/2026,10/04/2026,TRADER JOE S #123 SACRAMENTO,Groceries,Sale,-45.10\n" \
             "10/03/2026,10/03/2026,Netflix.com,Entertainment,Sale,-15.49\n" \
             "10/05/2026,10/05/2026,Payment Thank You - Web,,Payment,300.00\n" \
             "10/07/2026,10/07/2026,CHEWY.COM,Shopping,Sale,-30.00\n" \
             "10/08/2026,10/08/2026,CHEWY.COM,Shopping,Return,10.00\n" \
             "10/09/2026,10/09/2026,CHEWY.COM,Shopping,Sale,-30.00\n"
    new, dups = upload(conn, K("cc"), export)
    assert sorted(r["description"] for r in new) == ["CHEWY.COM", "Netflix.com"]  # a second, real $30 Chewy order
    assert len(dups) == 4
    s = queries.summary(conn, month_filters())
    assert amounts(s["expenses"])["Subscriptions"] == Decimal("15.49")


def test_simplefin_takes_over_uploaded_rows_instead_of_duplicating(conn):
    conn.execute("DELETE FROM transactions WHERE account_id = %s", (K("cc"),))
    conn.commit()
    upload(conn, K("cc"), "Transaction Date,Description,Amount\n10/02/2026,TRADER JOES 123,-45.10\n"
                          "10/03/2026,NETFLIX,-15.49\n")
    netflix = conn.execute("SELECT id FROM transactions WHERE description = 'NETFLIX'").fetchone()["id"]
    conn.execute("UPDATE transactions SET override_category = 'Entertainment' WHERE id = %s", (netflix,))
    conn.commit()

    resync(conn, FakeSimpleFIN(OCT))
    rows = conn.execute("SELECT id, source, override_category FROM transactions WHERE account_id = %s",
                        (K("cc"),)).fetchall()
    assert len(rows) == 6 and all(r["source"] == "simplefin" for r in rows)
    assert {r["id"]: r["override_category"] for r in rows}[K("cc", "k2")] == "Entertainment"


def test_sync_never_deletes_uploaded_rows(conn):
    upload(conn, K("sav"), GOLDEN1)
    resync(conn, FakeSimpleFIN(OCT))
    resync(conn, FakeSimpleFIN({}))  # SimpleFIN sends nothing for any account
    assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE account_id = %s AND source = 'csv'",
                        (K("sav"),)).fetchone()["n"] == 6


def test_upload_endpoint_preview_commit_undo_and_new_account(conn):
    web = app.test_client()
    csrf = login(web)
    headers = {"X-CSRF-Token": csrf}

    def post(**form):
        data = {"file": (io.BytesIO(GOLDEN1.encode()), "golden1.csv"), **form}
        return web.post("/api/import", data=data, headers=headers, content_type="multipart/form-data")

    preview = post(account_id=K("sav"), commit="0").get_json()
    assert preview["new_count"] == 6 and preview["duplicate_count"] == 0 and preview["ignored_lines"] == 1
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source = 'csv'").fetchone()["n"] == 0

    done = post(account_id=K("sav"), commit="1").get_json()
    assert done["saved"] and done["import_id"]
    again = post(account_id=K("sav"), commit="0").get_json()
    assert again["new_count"] == 0 and again["duplicate_count"] == 6

    assert web.delete(f"/api/imports/{done['import_id']}", headers=headers).status_code == 200
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE source = 'csv'").fetchone()["n"] == 0

    made = post(account_id="new", new_name="Apple Card", new_subtype="credit_card", commit="1").get_json()
    assert made["new_count"] == 6
    accounts = web.get("/api/accounts").get_json()["accounts"]
    apple = next(a for a in accounts if a["name"] == "Apple Card")
    assert apple["enrollment_status"] == "manual" and apple["transaction_count"] == 6
    assert resync(conn, FakeSimpleFIN(OCT)).get("error") is None  # manual accounts don't disturb syncing

    assert post(account_id="", commit="0").status_code == 400
    assert web.post("/api/import", data={"file": (io.BytesIO(GOLDEN1.encode()), "x.csv"), "account_id": K("sav")},
                    content_type="multipart/form-data").status_code == 403  # CSRF


APPLE = '''Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By
10/04/2026,10/06/2026,"KP RX CPP 256 300 PULLMAN ST LIVERMORE 94551 CA USA","Kp Rx Cpp 256","Other","Purchase","30.00","Me"
09/29/2026,09/29/2026,"ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 7022","Ach Deposit Internet Transfer From Account Ending In 7022","Payment","Payment","-803.90","Me"
09/27/2026,09/28/2026,"APPLE.COM/BILL ONE APPLE PARK WAY CUPERTINO 95014 CA USA","Apple Services","Other","Purchase","2.99","Me"
09/15/2026,09/17/2026,"CYCLEBOAT BOSTON, LLC (RETURN)","Cycleboat Boston, Llc (return)","Credit","Credit","-108.95","Me"
09/14/2026,09/15/2026,"CYCLEBOAT BOSTON, LLC","Cycleboat Boston, Llc","Other","Purchase","108.95","Me"
'''


def test_manual_accounts_and_apple_card_upload(conn):
    web = app.test_client()
    headers = {"X-CSRF-Token": login(web)}
    made = web.post("/api/accounts/manual", json={"name": "Apple Card", "institution": "Apple", "subtype": "credit_card"},
                    headers=headers).get_json()
    assert made["id"].startswith("manual:")
    assert web.post("/api/accounts/manual", json={"name": "", "subtype": "credit_card"}, headers=headers).status_code == 400

    def post(**form):
        data = {"file": (io.BytesIO(APPLE.encode()), "apple.csv"), **form}
        return web.post("/api/import", data=data, headers=headers, content_type="multipart/form-data").get_json()

    preview = post(account_id=made["id"], commit="0")
    assert preview["flip"] is True and preview["new_count"] == 5  # Apple lists purchases as positive
    post(account_id=made["id"], commit="1")
    conn.rollback()
    rows = {r["description"][:20]: r for r in conn.execute(
        "SELECT description, flow, auto_category FROM transactions WHERE account_id = %s", (made["id"],)).fetchall()}
    assert rows["ACH DEPOSIT INTERNET"]["auto_category"] == "Credit Card Payment"
    assert rows["ACH DEPOSIT INTERNET"]["flow"] == Decimal("803.90")
    assert rows["KP RX CPP 256 300 PU"]["auto_category"] == "Health" and rows["KP RX CPP 256 300 PU"]["flow"] == Decimal("-30.00")
    assert rows["APPLE.COM/BILL ONE A"]["auto_category"] == "Subscriptions"

    s = queries.summary(conn, {"start": datetime.date(2026, 9, 1), "end": TODAY, "account": made["id"]})
    assert amounts(s["expenses"]) == {"Health": Decimal("30.00"), "Subscriptions": Decimal("2.99"), "Other": Decimal("0.00")}

    # Two different new accounts from the same file don't collide.
    other = post(account_id="new", new_name="Apple Card (2)", new_subtype="credit_card", commit="1")
    assert other["new_count"] == 5

    # Only hand-made accounts can be removed, and their transactions go with them.
    assert web.post("/api/accounts/delete", json={"id": K("cc")}, headers=headers).status_code == 400
    assert web.post("/api/accounts/delete", json={"id": made["id"]}, headers=headers).status_code == 200
    conn.rollback()
    assert not conn.execute("SELECT 1 FROM transactions WHERE account_id = %s", (made["id"],)).fetchone()


def test_upload_lists_app_transactions_missing_from_the_statement(conn):
    # The statement has everything SimpleFIN sent except the pending Blue Bottle charge.
    statement = "Transaction Date,Description,Amount\n" \
                "10/02/2026,TRADER JOE S,-45.10\n10/03/2026,NETFLIX,-15.49\n10/05/2026,Payment Thank You,300.00\n" \
                "10/07/2026,CHEWY,-30.00\n10/08/2026,CHEWY RETURN,10.00\n"
    account = account_row(conn, K("cc"))
    new, dups, not_in_file = csv_import.plan(conn, account, csv_import.parse(statement), False)
    assert not new and len(dups) == 5
    assert [(r["description"], r["flow"]) for r in not_in_file] == []  # Blue Bottle (10/09) is after the file ends
    statement += "10/09/2026,SOMETHING ELSE,-1.00\n"
    new, dups, not_in_file = csv_import.plan(conn, account, csv_import.parse(statement), False)
    assert [r["description"] for r in new] == ["SOMETHING ELSE"]
    assert [(r["description"], r["flow"]) for r in not_in_file] == [("TST* BLUE BOTTLE", Decimal("-12.00"))]


def test_chase_html_entities_are_decoded():
    p = csv_import.parse("Transaction Date,Description,Amount\n09/05/2026,H&amp;M 0765CALIFORNIA,-28.25\n")
    assert p["rows"][0]["description"] == "H&M 0765CALIFORNIA"


def test_rules_change_recategorizes_existing_rows_but_not_your_choices(conn):
    conn.execute("UPDATE transactions SET auto_category = 'Other Utilities' WHERE id = %s", (K("cc", "k2"),))  # an old, wrong rule
    conn.execute("UPDATE transactions SET auto_category = 'Rent', override_category = 'Shopping' WHERE id = %s", (K("cc", "k1"),))
    conn.execute("DELETE FROM app_meta")
    conn.commit()
    assert sync.recategorize_all_if_rules_changed(conn) >= 2
    rows = {r["id"]: r for r in conn.execute("SELECT id, auto_category, override_category FROM transactions").fetchall()}
    assert rows[K("cc", "k2")]["auto_category"] == "Subscriptions"
    assert rows[K("cc", "k1")]["override_category"] == "Shopping"  # your choice stays
    assert sync.recategorize_all_if_rules_changed(conn) == 0  # only once per rules version


# --- AI categorization (Claude is faked; nothing is sent anywhere) ---

from budget import ai_categorize  # noqa: E402


def test_ai_fills_in_other_and_never_overrides_you(conn):
    asked = []

    def fake_ask(items):
        asked.append(items)
        answers = {"Old September thing": "Shopping", "Interest": "Other Income"}
        return {i: answers[it["text"]] for i, it in enumerate(items) if it["text"] in answers}

    assert ai_categorize.categorize_unknown(conn, ask=fake_ask) == 1  # Interest stays "Other Income"
    texts = {it["text"]: it for it in asked[0]}
    assert texts["Old September thing"] == {"text": "Old September thing", "direction": "money out", "account": "bank account"}

    sept = {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "budget"}
    assert amounts(queries.summary(conn, sept)["expenses"]) == {"Shopping": Decimal("999.00")}
    row = queries.transactions(conn, {**sept, "account": "all"}, 1, 10)["rows"][0]
    assert row["category"] == "Shopping" and row["ai_suggested"]

    # Already asked: nothing is sent again.
    assert ai_categorize.categorize_unknown(conn, ask=fake_ask) == 0 and len(asked) == 1

    # Your own choice wins over the AI's.
    conn.execute("UPDATE transactions SET override_category = 'Giving' WHERE id = %s", (K("chk", "c6"),))
    row = queries.transactions(conn, {**sept, "account": "all"}, 1, 10)["rows"][0]
    assert row["category"] == "Giving" and not row["ai_suggested"]


def test_ai_unanswered_and_impossible_answers(conn):
    # No answer (e.g. a refusal): nothing saved, so it's asked again next time.
    assert ai_categorize.categorize_unknown(conn, ask=lambda items: {}) == 0
    assert not conn.execute("SELECT 1 FROM ai_categories").fetchone()
    # "Income" for money going out is ignored by the totals.
    ai_categorize.categorize_unknown(conn, ask=lambda items: {i: "Income" for i in range(len(items))})
    sept = {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "budget"}
    assert amounts(queries.summary(conn, sept)["expenses"]) == {"Other": Decimal("999.00")}


def test_ask_claude_request_and_parsing(monkeypatch):
    sent = {}

    class Block:
        type = "text"
        text = json.dumps({"results": [{"id": 0, "category": "Giving"}, {"id": 1, "category": "Not A Category"},
                                       {"id": 7, "category": "Travel"}]})

    class Response:
        stop_reason = "end_turn"
        content = [Block()]

    class FakeMessages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return Response()

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(ai_categorize.anthropic, "Anthropic", FakeClient)
    monkeypatch.setattr(ai_categorize.config, "ANTHROPIC_API_KEY", "test")
    items = [{"text": "THE HOPE COMMUNITY", "direction": "money out", "account": "credit card"},
             {"text": "BDTOURS", "direction": "money out", "account": "credit card"}]
    assert ai_categorize.ask_claude(items) == {0: "Giving"}  # unknown category and out-of-range id dropped
    assert sent["model"] == "claude-haiku-5-5" and "fallbacks" not in sent  # Haiku has no server-side fallback
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"]["schema"]["properties"]["results"]["items"]["properties"]["category"]["enum"]
    assert "THE HOPE COMMUNITY" in sent["messages"][0]["content"]

    Response.stop_reason = "refusal"
    assert ai_categorize.ask_claude(items) == {}


def test_unmatched_transfers_flag_money_that_left_untracked(conn):
    # Budget view (checking + card): the card payment pairs up, the savings transfer counts as Savings.
    s = queries.summary(conn, month_filters())
    assert s["unmatched_transfers"] == [] and s["unmatched_total"] == 0
    # All accounts: savings transfer pairs with its savings-side deposit too.
    assert queries.summary(conn, month_filters(account="all"))["unmatched_transfers"] == []

    # Your situation: savings and card tracked, checking not. Money moved savings <-> checking
    # and checking paid the card, but checking's own spending is invisible.
    conn.execute("UPDATE accounts SET include_in_budget = (id <> %s)", (K("chk"),))
    s = queries.summary(conn, month_filters())
    assert sorted((r["account"], r["flow"]) for r in s["unmatched_transfers"]) == [
        ("Sapphire Preferred", Decimal("300.00")), ("Way2Save Savings", Decimal("500.00"))]
    assert s["unmatched_total"] == Decimal("800.00")

    # Re-labelling the money for what it was (sent to a friend) clears the warning for it.
    conn.execute("UPDATE transactions SET override_category = 'Person-to-Person' WHERE id = %s", (K("sav", "s1"),))
    assert queries.summary(conn, month_filters())["unmatched_total"] == Decimal("300.00")


def test_transfers_are_paired_whatever_the_bank_calls_them(conn):
    # Your September: savings -> checking ("Overdraft Transfer") -> Chase payment, and a
    # transfer that funded a Zelle to a friend.
    def add(acct, tid, date, amount, desc):
        flow = Decimal(amount)
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, description, raw_amount, flow, status, auto_category)
               VALUES (%s, %s, %s, %s, %s, %s, 'posted', %s)""",
            (K(acct, tid), K(acct), date, desc, flow, flow,
             sync.categorize(description=desc, merchant=None, flow=flow,
                             account_type="credit" if acct == "cc" else "depository")))
    conn.execute("DELETE FROM transactions WHERE date < '2026-10-01'")
    add("sav", "x1", "2026-09-22", "-3758.46", "Withdrawal Overdraft transfer to ****022-S9")
    add("chk", "x2", "2026-09-22", "3758.46", "Overdraft Transfer")
    add("chk", "x3", "2026-09-22", "-3750.68", "Credit Card Payment")
    add("cc", "x4", "2026-09-21", "3750.68", "Payment Thank You-Mobile")
    add("sav", "x5", "2026-09-19", "-1231.00", "Withdrawal at G1 Online Transfer 1,231.00 to share 9")
    add("chk", "x6", "2026-09-19", "1231.00", "Online Transfer")
    add("chk", "x7", "2026-09-19", "-1230.00", "Zelle Transfer")
    # A refund and an unrelated card payment of the same amount must not pair.
    add("chk", "x8", "2026-09-25", "-45.00", "PG&E WEB ONLINE PAYMENT")
    add("cc", "x9", "2026-09-25", "45.00", "AMAZON REFUND")
    conn.commit()
    assert sync.link_transfers(conn) == 5  # these 3 plus October's card payment and savings transfer

    rows = {r["id"].rsplit(":", 1)[1]: r for r in queries.transactions(
        conn, {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "all"}, 1, 50)["rows"]}
    # Savings starts outside the budget, so money pulled from it into checking is a savings
    # withdrawal (it adds back what putting money in took away), not "Fees & Interest".
    assert rows["x2"]["category"] == "Savings" and rows["x2"]["paired"]
    assert rows["x1"]["category"] == "Transfer"
    assert rows["x3"]["category"] == rows["x4"]["category"] == "Credit Card Payment"
    assert rows["x7"]["category"] == "Person-to-Person" and not rows["x7"]["paired"]
    assert not rows["x8"]["paired"] and not rows["x9"]["paired"]

    # Budget with savings and checking both tracked: the only spending is the friend payment
    # and the PG&E bill, net of the refund; nothing is flagged as leaking.
    conn.execute("UPDATE accounts SET include_in_budget = TRUE")
    sync.link_transfers(conn)  # as the app does when you tick an account
    s = queries.summary(conn, {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "budget"})
    assert amounts(s["expenses"]) == {"Electricity": Decimal("45.00"), "Shopping": Decimal("-45.00"),
                                      "Person-to-Person": Decimal("1230.00")}
    assert s["unmatched_transfers"] == []

    # You say a "transfer" was really something else: the pairing respects it.
    conn.execute("UPDATE transactions SET override_category = 'Rent' WHERE id = %s", (K("chk", "x3"),))
    conn.commit()
    sync.link_transfers(conn)
    assert not conn.execute("SELECT paired_with FROM transactions WHERE id = %s", (K("cc", "x4"),)).fetchone()["paired_with"]


def test_export_csv(conn):
    web = app.test_client()
    login(web)
    resp = web.get("/api/export?start=2026-10-01&end=2026-10-09&account=all")
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    rows = list(__import__("csv").reader(io.StringIO(resp.get_data(as_text=True))))
    assert rows[0][:6] == ["Date", "Account", "Description", "Merchant", "Category", "Amount"]
    assert len(rows) == 1 + 13
    assert ["2026-10-02", "Sapphire Preferred", "TRADER JOE'S #123", "Trader Joe's", "Groceries", "-45.10"] in [r[:6] for r in rows]


def test_simplefin_midnight_utc_dates_stay_on_their_day():
    tz = ZoneInfo("America/Los_Angeles")
    sept1 = int(datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp())
    assert sync._local_date(sept1, tz) == datetime.date(2026, 9, 1)
    assert sync._local_date(sept1 + 9000, tz) == datetime.date(2026, 8, 31)  # 7:30pm PT the evening before
