"""Search, hiding rows that aren't real, the Checkup, Cash flow and Giving pages, and audit fixes.

Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest
"""
import csv
import datetime
import io
from decimal import Decimal

import pytest

from tests.test_features import DB_URL, SEPT, TODAY, add, client_for, conn, monthly, totals  # noqa: F401
from budget import app as app_module, queries, sync  # noqa: E402

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
ALL = {"start": datetime.date(2000, 1, 1), "end": TODAY, "account": "all"}


def found(conn, q, filters=ALL):
    return {r["description"] for r in queries.transactions(conn, {**filters, "q": q}, 1, 500)["rows"]}


# ---------- search ----------

@pytest.fixture()
def shop(conn):
    conn.execute("UPDATE accounts SET name = 'SAVINGS', institution = 'Golden 1 Credit Union', last_four = '7022' WHERE id = 'sav'")
    add(conn, "card", "2026-08-14", "-45.10", "AMAZON MKTPL*5Q1")
    add(conn, "card", "2026-08-20", "-139.99", "AMAZON.COM*BIG")
    add(conn, "card", "2026-09-02", "-45.99", "WHOLE FOODS MARKET")
    add(conn, "card", "2026-09-03", "-12.00", "WHOLEFOODS LOOKALIKE")
    add(conn, "sav", "2026-09-14", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    tid = add(conn, "chk", "2025-08-14", "2900.00", "GUSTO PAY 123 PAYROLL")
    conn.execute("UPDATE transactions SET note = %s WHERE id = %s", ("bonus month 100% done", tid))
    conn.commit()
    return conn


@pytest.mark.parametrize("q,expected", [
    ("amazon", {"AMAZON MKTPL*5Q1", "AMAZON.COM*BIG"}),
    ("AMAZON >100", {"AMAZON.COM*BIG"}),                                    # words and amounts together
    ("45.10", {"AMAZON MKTPL*5Q1"}),                                        # exact amount
    ("$45", {"AMAZON MKTPL*5Q1", "WHOLE FOODS MARKET"}),                    # any cents
    ("-200.00", {"Withdrawal Overdraft transfer to ****022-S9"}),
    ("<=12", {"WHOLEFOODS LOOKALIKE"}),
    ("40..50", {"AMAZON MKTPL*5Q1", "WHOLE FOODS MARKET"}),
    ("2026-08", {"AMAZON MKTPL*5Q1", "AMAZON.COM*BIG"}),
    ("2026-08-14", {"AMAZON MKTPL*5Q1"}),
    ("8/14", {"AMAZON MKTPL*5Q1", "GUSTO PAY 123 PAYROLL"}),               # any year
    ("8/14/25", {"GUSTO PAY 123 PAYROLL"}),
    ('"whole foods"', {"WHOLE FOODS MARKET"}),
    ("groceries", {"WHOLE FOODS MARKET", "WHOLEFOODS LOOKALIKE"} & set()), # see below: category names match
    ("income", {"GUSTO PAY 123 PAYROLL"}),                                  # by category
    ("golden 1 overdraft", {"Withdrawal Overdraft transfer to ****022-S9"}),  # by bank name
    ("7022", {"Withdrawal Overdraft transfer to ****022-S9"}),              # by last four
    ("bonus", {"GUSTO PAY 123 PAYROLL"}),                                   # by note
    ("100%", {"GUSTO PAY 123 PAYROLL"}),                                    # % is a character, not a wildcard
    ("_", set()),
    ("nothing-like-this", set()),
])
def test_search_matches_almost_anything(shop, q, expected):
    if q == "groceries":
        expected = {r["description"] for r in queries.transactions(shop, {**ALL}, 1, 500)["rows"] if r["category"] == "Groceries"}
        assert expected  # Whole Foods is groceries
    assert found(shop, q) == expected


def test_search_still_respects_the_date_range_and_account(shop):
    assert found(shop, "amazon", {**SEPT, "account": "all"}) == set()
    assert found(shop, "overdraft", {**ALL, "account": "card"}) == set()


def test_searching_doesnt_change_unsearched_totals(shop):
    before = totals(shop)
    queries.transactions(shop, {**SEPT, "q": "amazon >100"}, 1, 50)
    assert totals(shop) == before


def test_search_api_handles_odd_input(shop):
    client, _ = client_for()
    for q in ["'; drop table transactions; --", "\\", '"', '"unclosed', ">", "..", "13/45", "2026-13", "9" * 300]:
        r = client.get("/api/transactions", query_string={"start": "2000-01-01", "end": "2026-10-10", "q": q, "account": "all"})
        assert r.status_code == 200, q
    assert shop.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 6


# ---------- hiding rows that aren't real ----------

def test_hidden_rows_leave_every_total_but_stay_listed(conn):
    real = add(conn, "chk", "2026-09-08", "100.00", "Online Transfer from SAV")
    add(conn, "sav", "2026-09-08", "-100.00", "Online Transfer to CHK")
    phantom = add(conn, "chk", "2026-09-26", "200.00", "Overdraft Transfer")  # not on the bank statement
    add(conn, "card", "2026-09-03", "-62.10", "SAFEWAY #1234")
    conn.commit()
    sync.link_transfers(conn)
    s = queries.summary(conn, SEPT)
    assert [r["id"] for r in s["unmatched_transfers"]] == [phantom]

    client, post = client_for()
    assert post("/api/transactions/hide", {"id": phantom, "hidden": True}).status_code == 200
    s = queries.summary(conn, SEPT)
    assert s["unmatched_transfers"] == [] and s["total_expenses"] == Decimal("62.10")
    listed = client.get("/api/transactions?start=2026-09-01&end=2026-09-30").get_json()
    assert {r["id"]: r["hidden"] for r in listed["rows"]}[phantom] is True
    assert listed["net"] == "-62.10"  # hidden +200 not in the net
    exported = client.get("/api/export?start=2026-09-01&end=2026-09-30").get_data(as_text=True)
    assert "Hidden (not counted)" in exported

    # Hidden rows never pair, and unhiding puts everything back.
    assert conn.execute("SELECT paired_with FROM transactions WHERE id = %s", (real,)).fetchone()["paired_with"]
    post("/api/transactions/hide", {"id": phantom, "hidden": False})
    assert len(queries.summary(conn, SEPT)["unmatched_transfers"]) == 1
    assert post("/api/transactions/hide", {"id": "nope"}).status_code == 404


def test_a_hidden_duplicate_lets_the_real_one_pair(conn):
    out = add(conn, "sav", "2026-09-14", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    dupe = add(conn, "chk", "2026-09-14", "200.00", "Overdraft Transfer")
    real = add(conn, "chk", "2026-09-15", "200.00", "Overdraft Transfer")
    conn.commit()
    sync.link_transfers(conn)
    assert conn.execute("SELECT paired_with FROM transactions WHERE id = %s", (out,)).fetchone()["paired_with"] == dupe
    _, post = client_for()
    post("/api/transactions/hide", {"id": dupe, "hidden": True})
    assert conn.execute("SELECT paired_with FROM transactions WHERE id = %s", (out,)).fetchone()["paired_with"] == real
    assert queries.summary(conn, SEPT)["unmatched_transfers"] == []


# ---------- checkup ----------

def kinds(conn):
    return {i["kind"]: i for i in queries.health(conn, TODAY)}


def test_checkup_is_quiet_when_everything_is_fine(conn):
    monthly(conn, "card", "NETFLIX.COM", ["-15.49"] * 3, last=datetime.date(2026, 10, 1))
    add(conn, "chk", "2026-09-20", "-300.00", "CHASE CREDIT CRD AUTOPAY")
    add(conn, "card", "2026-09-20", "300.00", "AUTOMATIC PAYMENT - THANK YOU")
    conn.execute("UPDATE accounts SET name = 'Chase Sapphire', institution = 'Chase' WHERE id = 'card'")
    conn.commit()
    sync.link_transfers(conn)
    # Only "fyi" notes about the test's empty accounts are left.
    assert {i["kind"] for i in queries.health(conn, TODAY)} <= {"empty"}


def test_checkup_finds_a_card_you_pay_but_dont_track(conn):
    conn.execute("UPDATE accounts SET name = 'Apple Card', institution = 'Manual' WHERE id = 'card'")
    add(conn, "chk", "2026-08-14", "-248.22", "Discover Credit Card")
    add(conn, "chk", "2026-08-31", "-324.89", "Discover Credit Card")
    add(conn, "chk", "2026-09-29", "-14.98", "APPLECARD GSBANK PAYMENT")  # Apple Card is tracked: no warning for it
    add(conn, "chk", "2026-09-01", "-20.00", "Credit Card Payment")       # names no card: can't tell
    conn.commit()
    issue = kinds(conn)["untracked_card"]
    assert issue["severity"] == "high" and "Discover" in issue["title"] and "$573.11" in issue["detail"]
    assert len(issue["rows"]) == 2
    # Add the account and it goes away.
    conn.execute("INSERT INTO accounts (id, name, institution, type, subtype) VALUES ('disc', 'Discover it', 'Manual', 'credit', 'credit_card')")
    assert "untracked_card" not in kinds(conn)


def test_checkup_finds_spending_on_a_card_left_out_of_the_budget(conn):
    add(conn, "off", "2026-09-20", "-80.00", "AMAZON MKTPL*5Q1")
    conn.commit()
    issue = kinds(conn)["unticked_card"]
    assert issue["account_id"] == "off" and issue["action"] == "tick"


def test_checkup_finds_stale_accounts_gaps_old_pending_duplicates_and_lost_transfers(conn):
    conn.execute("INSERT INTO enrollments (id, institution, access_token) VALUES ('e1', 'Chase', 'x')")
    conn.execute("UPDATE accounts SET enrollment_id = 'e1' WHERE id = 'card'")
    for d in range(1, 31):  # busy card that went quiet after Sept 15 and has a hole Aug 10-25
        day = datetime.date(2026, 8, d)
        if not 10 < d < 26:
            add(conn, "card", day, "-5.00", f"COFFEE {d}")
    for d in range(1, 16):
        add(conn, "card", datetime.date(2026, 9, d), "-5.00", f"LUNCH {d}")
    add(conn, "chk", "2026-09-01", "-75.00", "pending store", status="pending")
    a = add(conn, "chk", "2026-09-10", "-40.00", "WINCO FOODS")
    b = add(conn, "chk", "2026-09-11", "-40.00", "WINCO FOODS")
    conn.execute("UPDATE transactions SET source = 'csv' WHERE id = %s", (a,))
    add(conn, "chk", "2026-09-26", "200.00", "Overdraft Transfer")
    conn.commit()
    sync.link_transfers(conn)
    found_kinds = kinds(conn)
    assert "No new transactions" in found_kinds["stale"]["title"]
    assert "2026-08-10" in found_kinds["gap"]["title"] and "2026-08-26" in found_kinds["gap"]["title"]
    assert found_kinds["old_pending"]["rows"][0]["description"] == "pending store"
    assert found_kinds["duplicates"]["rows"][0]["id"] == b
    assert found_kinds["unmatched"]["rows"][0]["unmatched"] is True
    # Hiding the extras clears those findings.
    _, post = client_for()
    for row in found_kinds["old_pending"]["rows"] + found_kinds["duplicates"]["rows"] + found_kinds["unmatched"]["rows"]:
        post("/api/transactions/hide", {"id": row["id"], "hidden": True})
    assert not {"old_pending", "duplicates", "unmatched"} & set(kinds(conn))


def test_checkup_api(conn):
    client, _ = client_for()
    issues = client.get("/api/health").get_json()["issues"]
    assert issues and all(i["kind"] == "empty" and i["severity"] == "low" for i in issues)


# ---------- cash flow ----------

def test_forecast_projects_pay_bills_cards_and_everyday_spending(conn):
    conn.execute("UPDATE accounts SET ledger_balance = 400 WHERE id = 'chk'")
    conn.execute("UPDATE accounts SET ledger_balance = 1600 WHERE id = 'sav'")
    for m in range(5, 10):  # paid on the 12th and 27th into savings; car payment on the 25th
        add(conn, "sav", datetime.date(2026, m, 12), "2915.90", "Deposit-ACH HP ENTERPRISE CO (PAYROLL)")
        add(conn, "sav", datetime.date(2026, m, 27), "2917.60", "Deposit-ACH HP ENTERPRISE CO (PAYROLL)")
        add(conn, "sav", datetime.date(2026, m, 25), "-351.75", "Withdrawal-ACH WEBALLY (ALLY PAYMT)")
    for m in (7, 8, 9):  # card payments around the 20th, ~$900/month
        add(conn, "chk", datetime.date(2026, m, 20), "-900.00", "CHASE CREDIT CRD AUTOPAY")
    for d in range(1, 91, 3):  # Venmo here and there: $30 every 3 days
        add(conn, "chk", TODAY - datetime.timedelta(days=d), "-30.00", f"Transfer to Venmo {d}")
    conn.commit()
    sync.link_transfers(conn)
    f = queries.forecast(conn, TODAY, 35)
    assert f["start_balance"] == Decimal("2000.00") and not f["missing_balance"]
    assert f["daily_spending"] == Decimal("10.00")
    pay = [e for e in f["events"] if e["kind"] == "income"]
    assert [e["date"] for e in pay] == [datetime.date(2026, 10, 12), datetime.date(2026, 10, 27), datetime.date(2026, 11, 12)]
    assert pay[0]["frequency"] == "twice a month"
    bills = [(e["date"], e["amount"]) for e in f["events"] if e["kind"] == "bill"]
    assert bills == [(datetime.date(2026, 10, 25), Decimal("-351.75"))]
    cards = [(e["date"], e["amount"]) for e in f["events"] if e["kind"] == "cards"]
    assert cards == [(datetime.date(2026, 10, 20), Decimal("-900.00"))]
    # The math: start - 10/day + events.
    by_date = {p["date"]: p["balance"] for p in f["series"]}
    assert by_date[datetime.date(2026, 10, 11)] == Decimal("1990.00")
    assert by_date[datetime.date(2026, 10, 12)] == Decimal("1990.00") - 10 + Decimal(pay[0]["amount"])
    assert f["low"]["balance"] == min(by_date.values())
    assert len(f["series"]) == 36


def test_forecast_uses_typed_balances_for_hand_added_accounts_plus_later_rows(conn):
    conn.execute("UPDATE accounts SET ledger_balance = 500, balance_date = '2026-10-01' WHERE id = 'chk'")
    add(conn, "chk", "2026-10-03", "-120.00", "WINCO FOODS")
    add(conn, "chk", "2026-10-01", "-999.00", "SAME DAY AS THE BALANCE: ALREADY IN IT")
    conn.commit()
    f = queries.forecast(conn, TODAY)
    assert f["start_balance"] == Decimal("380.00")
    assert [a["name"] for a in f["missing_balance"]] == ["sav"]


def test_forecast_with_no_balances_says_so(conn):
    f = queries.forecast(conn, TODAY)
    assert f["accounts"] == [] and f["series"] == [] and len(f["missing_balance"]) == 2


def test_forecast_and_balance_api(conn):
    client, post = client_for()
    assert post("/api/accounts/balance", {"id": "chk", "balance": "1,200"}).status_code == 400  # commas: client strips them
    assert post("/api/accounts/balance", {"id": "chk", "balance": "1200.50"}).status_code == 200
    conn.execute("INSERT INTO enrollments (id, institution, access_token) VALUES ('e1', 'Chase', 'x')")
    conn.execute("UPDATE accounts SET enrollment_id = 'e1' WHERE id = 'sav'")
    conn.commit()
    assert post("/api/accounts/balance", {"id": "sav", "balance": "5"}).status_code == 400  # SimpleFIN's own
    assert post("/api/settings/low-balance", {"amount": "250"}).status_code == 200
    assert post("/api/settings/low-balance", {"amount": "-1"}).status_code == 400
    f = client.get("/api/forecast").get_json()
    assert f["start_balance"] == "1200.50" and f["threshold"] == "250.00"
    assert len(client.get("/api/forecast?days=500").get_json()["series"]) == 91


# ---------- giving ----------

def test_giving_totals_match_the_budget_and_group_by_organization(conn):
    add(conn, "chk", "2026-01-15", "5000.00", "GUSTO PAY 123 PAYROLL")
    add(conn, "card", "2026-02-01", "-100.00", "ST JUDE Internet DNTN")
    add(conn, "card", "2026-03-01", "-100.00", "ST JUDE Internet DNTN")
    add(conn, "card", "2026-03-05", "-250.00", "THE HOPE COMMUNITY CHURCH")
    add(conn, "card", "2025-12-24", "-75.00", "CHARITY:WATER-DONATION")
    add(conn, "off", "2026-04-01", "-999.00", "ST JUDE Internet DNTN")  # card not in the budget
    conn.commit()
    g = queries.giving(conn, 2026, TODAY)
    budget = queries.summary(conn, {"start": datetime.date(2026, 1, 1), "end": TODAY, "account": "budget"})
    assert g["given"] == next(r["amount"] for r in budget["expenses"] if r["category"] == "Giving") == Decimal("450.00")
    assert g["percent"] == Decimal("9.0") and g["last_year"] == Decimal("75.00")
    assert [(o["name"], o["total"], o["count"]) for o in g["organizations"]] == [
        ("THE HOPE COMMUNITY CHURCH", Decimal("250.00"), 1), ("ST JUDE Internet DNTN", Decimal("200.00"), 2)]
    assert g["by_month"][2] == Decimal("350.00")
    assert queries.giving(conn, 2026, TODAY, account="all")["given"] == Decimal("1449.00")


def test_giving_goal_and_tax_export(conn):
    add(conn, "card", "2026-02-01", "-100.00", "=HYPERLINK(\"http://x\") ST JUDE DNTN")
    conn.commit()
    client, post = client_for()
    assert post("/api/giving/goal", {"percent": "10"}).status_code == 200
    assert client.get("/api/giving").get_json()["goal_percent"] == "10.00"
    assert post("/api/giving/goal", {"percent": "101"}).status_code == 400
    assert post("/api/giving/goal", {"percent": ""}).status_code == 200
    assert client.get("/api/giving").get_json()["goal_percent"] is None
    rows = list(csv.reader(io.StringIO(client.get("/api/giving/export?year=2026").get_data(as_text=True))))
    assert rows[0] == ["Date", "Organization", "Amount", "Account", "Note"]
    assert rows[1][1].startswith("'=")  # can't run as a spreadsheet formula
    assert rows[-1] == ["Total", "", "100.00", "", ""]
    assert client.get("/api/giving?year=1999").status_code == 400
    assert client.get("/api/giving?year=2030").status_code == 400


# ---------- audit fixes ----------

def test_export_neutralizes_spreadsheet_formulas_but_not_amounts(conn):
    add(conn, "chk", "2026-09-03", "-20.00", "=cmd|'/c calc'!A1")
    add(conn, "chk", "2026-09-04", "-5.00", "@SUM(1)")
    conn.commit()
    client, _ = client_for()
    rows = list(csv.reader(io.StringIO(client.get("/api/export?start=2026-09-01&end=2026-09-30").get_data(as_text=True))))
    assert {r[2] for r in rows[1:]} == {"'=cmd|'/c calc'!A1", "'@SUM(1)"}
    assert {r[5] for r in rows[1:]} == {"-20.00", "-5.00"}


def test_import_into_a_new_account_returns_its_id(conn):
    client, _ = client_for()
    with client.session_transaction() as s:
        csrf = s["csrf"]
    data = {"account_id": "new", "new_name": "Discover it", "new_subtype": "credit_card", "flip": "auto", "commit": "1",
            "file": (io.BytesIO(b"Trans. Date,Description,Amount\n09/02/2026,AMAZON,21.84\n"), "d.csv")}
    r = client.post("/api/import", data=data, headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data").get_json()
    assert r["account_id"].startswith("manual:")
    assert conn.execute("SELECT name FROM accounts WHERE id = %s", (r["account_id"],)).fetchone()["name"] == "Discover it"


def test_a_few_strangers_cant_lock_you_out():
    assert app_module.MAX_FAILURES_TOTAL >= 100 and app_module.MAX_FAILURES_PER_IP <= 10


def test_every_page_and_control_is_on_the_page(conn):
    client, _ = client_for()
    html = client.get("/").get_data(as_text=True)
    for marker in ['data-view="cashflow"', 'data-view="giving"', 'data-view="checkup"', 'id="checkup-badge"', 'id="qall"',
                   'id="category-dialog"', 'id="note-dialog"', 'id="toast"', 'id="cf-threshold"', 'id="giving-export"']:
        assert marker in html, marker


def test_automatic_undoes_your_choice_and_your_merchant_rule(conn):
    tid = add(conn, "card", "2026-09-03", "-62.10", "SAFEWAY #1234")
    other = add(conn, "card", "2026-09-10", "-20.00", "SAFEWAY #1234")
    conn.commit()
    client, post = client_for()
    post("/api/transactions/category", {"id": tid, "category": "Savings", "remember": True})
    rows = {r["id"]: r for r in client.get("/api/transactions?start=2026-09-01&end=2026-09-30").get_json()["rows"]}
    assert rows[other]["category"] == "Savings" and rows[other]["ruled"]
    post("/api/transactions/category", {"id": tid, "category": "", "remember": True})
    rows = {r["id"]: r for r in client.get("/api/transactions?start=2026-09-01&end=2026-09-30").get_json()["rows"]}
    assert rows[tid]["category"] == rows[other]["category"] == "Groceries"
    assert not rows[tid]["ruled"] and not rows[tid]["overridden"]
