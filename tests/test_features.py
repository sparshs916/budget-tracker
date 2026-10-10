"""Display-only features: targets, review of unmatched transfers, notes, trends, recurring.

None of them may change what the budget counts; the last test uses all of them
on the frozen dataset from test_golden.py and checks every total is unchanged.

Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest
"""
import csv
import datetime
import io
import json
import os
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
for key, value in {
    "DATABASE_URL": DB_URL or "",
    "APP_PASSWORD_HASH": generate_password_hash("correct horse battery"),
    "SECRET_KEY": "test-secret",
    "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "CRON_SECRET": "cron-secret",
}.items():
    os.environ.setdefault(key, value)

from budget import app as app_module, db, queries, sync  # noqa: E402
from budget.app import app  # noqa: E402
from budget.categorize import categorize, merchant_key  # noqa: E402

from tests import test_golden  # noqa: E402

TODAY = datetime.date(2026, 10, 10)
SEPT = {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "budget"}
ACCOUNTS = {"chk": ("depository", "checking", True), "sav": ("depository", "savings", True),
            "card": ("credit", "credit_card", True), "off": ("credit", "credit_card", False)}


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setattr(app_module, "today", lambda: TODAY)
    with db.connect() as c:
        c.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, login_attempts, ai_categories, "
                  "budget_targets CASCADE")
        for key, (typ, sub, included) in ACCOUNTS.items():
            c.execute("INSERT INTO accounts (id, name, type, subtype, include_in_budget) VALUES (%s, %s, %s, %s, %s)",
                      (key, key, typ, sub, included))
        c.commit()
        yield c


_n = 0


def add(conn, account, date, amount, description, status="posted"):
    global _n
    _n += 1
    flow = Decimal(amount)
    tid = f"{account}:{_n}"
    conn.execute(
        """INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key, raw_amount, flow, status,
                                     auto_category)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (tid, account, date, description, description, merchant_key(description, description), flow, flow, status,
         categorize(description=description, merchant=description, flow=flow, account_type=ACCOUNTS[account][0])))
    return tid


def client_for():
    client = app.test_client()
    client.get("/login")
    with client.session_transaction() as s:
        csrf = s["csrf"]
    assert client.post("/login", data={"password": "correct horse battery", "csrf": csrf}).status_code == 302
    with client.session_transaction() as s:
        csrf = s["csrf"]

    def post(path, body):
        return client.post(path, json=body, headers={"X-CSRF-Token": csrf})
    return client, post


def totals(conn, filters=SEPT):
    s = queries.summary(conn, filters)
    return {k: s[k] for k in ("income", "expenses", "excluded", "total_income", "total_expenses", "total_spending",
                              "total_savings", "remaining")}


# ---------- targets ----------

def test_targets_are_saved_listed_and_removed(conn):
    client, post = client_for()
    assert client.get("/api/targets").get_json() == {"targets": {}}
    r = post("/api/targets", {"targets": {"Groceries": "400", "Dining Out": 150.5, "Savings": "500"}})
    assert r.status_code == 200
    assert r.get_json()["targets"] == {"Groceries": "400.00", "Dining Out": "150.50", "Savings": "500.00"}
    # Blank or 0 removes one; others are untouched.
    r = post("/api/targets", {"targets": {"Dining Out": "", "Savings": 0}})
    assert r.get_json()["targets"] == {"Groceries": "400.00"}


@pytest.mark.parametrize("body", [
    {"targets": {"Income": 100}},                   # income and moves can't have spending targets
    {"targets": {"Transfer": 100}},
    {"targets": {"Not a category": 100}},
    {"targets": {"Groceries": -5}},
    {"targets": {"Groceries": "lots"}},
    {"targets": {"Groceries": "NaN"}},
    {"targets": {"Groceries": "Infinity"}},
    {"targets": {"Groceries": 1e12}},
    {"targets": ["Groceries"]},
    {},
])
def test_bad_targets_are_rejected_and_nothing_is_saved(conn, body):
    client, post = client_for()
    post("/api/targets", {"targets": {"Rent": 1650}})
    assert post("/api/targets", body).status_code == 400
    assert client.get("/api/targets").get_json() == {"targets": {"Rent": "1650.00"}}


def test_targets_need_login_and_csrf(conn):
    client = app.test_client()
    assert client.get("/api/targets").status_code == 401
    client, _ = client_for()
    assert client.post("/api/targets", json={"targets": {"Rent": 1}}).status_code == 403


def test_targets_dont_change_the_budget(conn):
    add(conn, "card", "2026-09-03", "-62.10", "SAFEWAY #1234")
    add(conn, "chk", "2026-09-01", "2900.00", "GUSTO PAY 123 PAYROLL")
    conn.commit()
    before = totals(conn)
    _, post = client_for()
    post("/api/targets", {"targets": {"Groceries": 10, "Rent": 2000, "Travel": 1}})
    assert totals(conn) == before


# ---------- reviewing unmatched transfers ----------

def test_saying_an_unmatched_transfer_is_your_own_money_stops_the_warning_only(conn):
    # The user's Capital One case: $1,000 of their own money arrives from an untracked bank.
    tid = add(conn, "sav", "2026-09-22", "1000.00", "Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER) RT0675D686ABBE0")
    add(conn, "card", "2026-09-03", "-62.10", "SAFEWAY #1234")
    conn.commit()
    sync.link_transfers(conn)
    s = queries.summary(conn, SEPT)
    assert [r["id"] for r in s["unmatched_transfers"]] == [tid] and s["unmatched_total"] == Decimal("1000.00")
    before = totals(conn)

    _, post = client_for()
    assert post("/api/transactions/review", {"id": tid, "reviewed": True}).status_code == 200
    s = queries.summary(conn, SEPT)
    assert s["unmatched_transfers"] == [] and s["unmatched_total"] == 0
    assert totals(conn) == before and s["total_income"] == 0  # still not income

    # Undo brings the warning back.
    post("/api/transactions/review", {"id": tid, "reviewed": False})
    assert len(queries.summary(conn, SEPT)["unmatched_transfers"]) == 1
    assert post("/api/transactions/review", {"id": "nope", "reviewed": True}).status_code == 404


def test_choosing_what_an_unmatched_transfer_really_was_counts_it(conn):
    tid = add(conn, "chk", "2026-09-14", "-200.00", "Online Transfer to CHK ...9999")  # an account not tracked
    conn.commit()
    sync.link_transfers(conn)
    assert queries.summary(conn, SEPT)["total_expenses"] == 0
    _, post = client_for()
    post("/api/transactions/category", {"id": tid, "category": "Person-to-Person", "remember": False})
    s = queries.summary(conn, SEPT)
    assert s["total_expenses"] == Decimal("200.00") and s["unmatched_transfers"] == []


def test_a_reviewed_transfer_still_pairs_when_its_other_half_shows_up(conn):
    out = add(conn, "chk", "2026-09-14", "-300.00", "Online Transfer to SAV")
    conn.commit()
    sync.link_transfers(conn)
    _, post = client_for()
    post("/api/transactions/review", {"id": out, "reviewed": True})
    add(conn, "sav", "2026-09-15", "300.00", "Online Transfer from CHK")  # e.g. a later CSV upload
    conn.commit()
    sync.link_transfers(conn)
    s = queries.summary(conn, SEPT)
    assert s["unmatched_transfers"] == [] and s["total_expenses"] == 0 and s["total_income"] == 0


# ---------- notes ----------

def test_notes_are_saved_searchable_exported_and_cleared(conn):
    tid = add(conn, "card", "2026-09-03", "-62.10", "SAFEWAY #1234")
    add(conn, "card", "2026-09-04", "-12.00", "TST* BLUE BOTTLE COFFEE")
    conn.commit()
    before = totals(conn)
    client, post = client_for()
    assert post("/api/transactions/note", {"id": tid, "note": "  birthday party snacks  "}).status_code == 200
    rows = client.get("/api/transactions?start=2026-09-01&end=2026-09-30").get_json()["rows"]
    assert {r["id"]: r["note"] for r in rows}[tid] == "birthday party snacks"
    found = client.get("/api/transactions?start=2026-09-01&end=2026-09-30&q=birthday").get_json()
    assert [r["id"] for r in found["rows"]] == [tid]
    exported = list(csv.reader(io.StringIO(client.get("/api/export?start=2026-09-01&end=2026-09-30").get_data(as_text=True))))
    assert exported[0][-1] == "Note" and ["birthday party snacks"] == [r[-1] for r in exported[1:] if r[-1]]
    assert totals(conn) == before

    post("/api/transactions/note", {"id": tid, "note": "x" * 900})
    assert len(conn.execute("SELECT note FROM transactions WHERE id = %s", (tid,)).fetchone()["note"]) == 500
    post("/api/transactions/note", {"id": tid, "note": "   "})
    assert conn.execute("SELECT note FROM transactions WHERE id = %s", (tid,)).fetchone()["note"] is None
    assert post("/api/transactions/note", {"id": "nope", "note": "x"}).status_code == 404


# ---------- trends ----------

def test_each_trend_month_is_exactly_the_budget_for_that_month(conn):
    conn.execute("TRUNCATE accounts CASCADE")
    test_golden.build(conn)
    months = queries.trends(conn, datetime.date(2026, 10, 10), 13)
    assert [m["month"] for m in months][0] == "2025-10" and months[-1]["month"] == "2026-10"
    assert months[-1]["partial"] and months[-1]["end"] == datetime.date(2026, 10, 10)
    assert not any(m["partial"] for m in months[:-1])
    for m in months:
        s = queries.summary(conn, {"start": m["start"], "end": m["end"], "account": "budget"})
        for key in ("total_income", "total_expenses", "total_spending", "total_savings", "remaining"):
            assert m[key] == s[key], (m["month"], key)
        assert m["categories"] == {r["category"]: r["amount"] for r in s["income"] + s["expenses"]}


def test_trends_api_limits_and_account_filter(conn):
    client, _ = client_for()
    assert len(client.get("/api/trends").get_json()["months"]) == 12
    assert len(client.get("/api/trends?months=500").get_json()["months"]) == 24
    assert len(client.get("/api/trends?months=-3").get_json()["months"]) == 1
    # Never looks past today.
    assert client.get("/api/trends?months=1&end=2030-01-01").get_json()["months"][0]["month"] == "2026-10"
    add(conn, "off", "2026-09-03", "-50.00", "SAFEWAY #1234")
    conn.commit()
    sept = lambda q: next(m for m in client.get("/api/trends?" + q).get_json()["months"] if m["month"] == "2026-09")  # noqa: E731
    assert sept("account=budget")["total_expenses"] == "0.00"
    assert sept("account=all")["total_expenses"] == "50.00"
    assert client.get("/api/trends?end=junk").status_code == 400


def test_month_helpers_cross_year_boundaries():
    assert queries.month_start(datetime.date(2026, 1, 15), 1) == datetime.date(2025, 12, 1)
    assert queries.month_start(datetime.date(2026, 1, 15), 13) == datetime.date(2024, 12, 1)
    assert queries.month_end(datetime.date(2024, 2, 10)) == datetime.date(2024, 2, 29)
    assert queries.month_end(datetime.date(2026, 12, 3)) == datetime.date(2026, 12, 31)


# ---------- recurring ----------

def monthly(conn, account, description, amounts, day=5, last=datetime.date(2026, 10, 1)):
    for back, amount in enumerate(reversed(amounts)):
        start = queries.month_start(last, back)
        add(conn, account, start.replace(day=day), amount, description)


def test_recurring_finds_subscriptions_bills_and_rent(conn):
    monthly(conn, "card", "NETFLIX.COM", ["-15.49"] * 6)
    monthly(conn, "chk", "PG&E WEB ONLINE PAYMENT", ["-81.20", "-95.10", "-102.33", "-77.90", "-88.00"], day=18,
            last=datetime.date(2026, 9, 1))
    monthly(conn, "chk", "ZELLE PAYMENT TO LANDLORD RENT", ["-1650.00"] * 4, day=1)
    monthly(conn, "card", "SPOTIFY USA", ["-11.99"] * 5 + ["-12.99"])  # price went up
    add(conn, "card", "2025-10-08", "-139.00", "AMAZON PRIME ANNUAL")
    add(conn, "card", "2026-10-06", "-139.00", "AMAZON PRIME ANNUAL")
    conn.commit()
    found = {r["name"]: r for r in queries.recurring(conn, TODAY)}
    assert found["NETFLIX.COM"]["frequency"] == "monthly"
    assert found["NETFLIX.COM"]["monthly_cost"] == Decimal("15.49") and found["NETFLIX.COM"]["yearly_cost"] == Decimal("185.88")
    assert found["NETFLIX.COM"]["next_date"] == datetime.date(2026, 11, 5) and found["NETFLIX.COM"]["price_change"] is None
    assert found["PG&E WEB ONLINE PAYMENT"]["frequency"] == "monthly"
    assert found["ZELLE PAYMENT TO LANDLORD RENT"]["monthly_cost"] == Decimal("1650.00")
    assert found["SPOTIFY USA"]["price_change"] == Decimal("1.00")
    assert found["AMAZON PRIME ANNUAL"]["frequency"] == "yearly"
    assert found["AMAZON PRIME ANNUAL"]["monthly_cost"] == Decimal("11.58")
    # Biggest first.
    assert list(found)[0] == "ZELLE PAYMENT TO LANDLORD RENT"


def test_recurring_ignores_irregular_spending_moves_cancelled_and_untracked(conn):
    # Groceries: frequent but irregular amounts.
    for d, a in [(2, "-140.22"), (9, "-23.10"), (16, "-88.40"), (23, "-12.99"), (30, "-201.75")]:
        for m in (6, 7, 8, 9):
            add(conn, "card", datetime.date(2026, m, min(d, 28)), a, "SAFEWAY #1234")
    monthly(conn, "chk", "CHASE CREDIT CRD AUTOPAY", ["-500.00"] * 6)        # card payments aren't spending
    monthly(conn, "chk", "Online Transfer to SAV ...2222", ["-300.00"] * 6)  # nor are transfers
    monthly(conn, "card", "HULU", ["-19.99"] * 5, last=datetime.date(2026, 5, 1))  # cancelled in May
    monthly(conn, "off", "DISNEY PLUS", ["-13.99"] * 6)                        # card left out of the budget
    monthly(conn, "card", "PENDING GYM", ["-40.00"] * 2)
    add(conn, "card", "2026-10-05", "-40.00", "PENDING GYM", status="pending")
    conn.commit()
    names = {r["name"] for r in queries.recurring(conn, TODAY)}
    assert names == set(), names
    assert {r["name"] for r in queries.recurring(conn, TODAY, account="all")} == {"DISNEY PLUS"}


def test_recurring_treats_two_charges_on_one_day_as_one(conn):
    for m in (5, 6, 7, 8, 9):
        add(conn, "card", datetime.date(2026, m, 12), "-5.00", "GOOGLE ONE")
        add(conn, "card", datetime.date(2026, m, 12), "-5.00", "GOOGLE ONE")
    conn.commit()
    (r,) = queries.recurring(conn, TODAY)
    assert r["typical"] == Decimal("10.00") and r["count"] == 5 and r["monthly_cost"] == Decimal("10.00")


def test_recurring_api(conn):
    monthly(conn, "card", "NETFLIX.COM", ["-15.49"] * 4)
    conn.commit()
    client, _ = client_for()
    (r,) = client.get("/api/recurring").get_json()["recurring"]
    assert r["name"] == "NETFLIX.COM" and r["typical"] == "15.49" and r["next_date"] == "2026-11-05"


# ---------- the whole page, and the promise that nothing moved ----------

def test_page_has_the_new_pages_and_theme_switch(conn):
    client, _ = client_for()
    html = client.get("/").get_data(as_text=True)
    for marker in ('id="theme-btn"', 'id="view-trends"', 'id="view-recurring"', 'id="targets-btn"',
                   'data-theme="dark"', "prefers-color-scheme: dark"):
        assert marker in html
    client.get("/logout")
    assert "prefers-color-scheme: dark" in app.test_client().get("/login").get_data(as_text=True)


def test_using_every_new_feature_leaves_the_frozen_budget_math_untouched(conn):
    conn.execute("TRUNCATE accounts CASCADE")
    test_golden.build(conn)
    want = json.loads(test_golden.GOLDEN.read_text())
    client, post = client_for()

    # Targets for everything, a note on every 5th row, every unmatched transfer reviewed.
    post("/api/targets", {"targets": {"Groceries": 400, "Dining Out": 150, "Shopping": 300, "Savings": 500, "Other": 1}})
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY id").fetchall()]
    for tid in ids[::5]:
        post("/api/transactions/note", {"id": tid, "note": f"note {tid}"})
    year = {"start": datetime.date(2025, 10, 1), "end": datetime.date(2026, 10, 10), "account": "all"}
    for r in queries.summary(conn, year)["unmatched_transfers"]:
        post("/api/transactions/review", {"id": r["id"], "reviewed": True})
    assert client.get("/api/trends?months=13").status_code == 200
    assert client.get("/api/recurring").status_code == 200
    sync.link_transfers(conn)  # as after any sync

    got = test_golden.snapshot(conn)
    for view in want:
        summary_now = {k: v for k, v in got[view]["summary"].items() if k != "unmatched_total"}
        summary_then = {k: v for k, v in want[view]["summary"].items() if k != "unmatched_total"}
        assert summary_now == summary_then, view
        assert got[view]["transactions"] == want[view]["transactions"], view  # notes don't mention "amazon"
    # Everything flagged across all accounts was reviewed. (A single-account view still flags
    # transfers whose other half is in another account, as it should.)
    assert got["year all"]["unmatched"] == [] and got["year budget"]["unmatched"] == []
