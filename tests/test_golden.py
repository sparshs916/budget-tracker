"""Frozen budget math.

A year of realistic, messy transactions goes through the real pipeline
(categorize -> rules/overrides/AI -> transfer pairing), and every number the
budget shows is compared with tests/golden.json, saved before any of the
display-only features (dark mode, targets, trends, recurring, review) existed.
If this fails, a change altered how money is counted. Only regenerate the
snapshot (GOLDEN_UPDATE=1 pytest tests/test_golden.py) for a change that is
meant to alter the math.
"""
import datetime
import json
import os
import pathlib
import random
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
os.environ.setdefault("DATABASE_URL", DB_URL or "")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

from budget import db, queries, sync  # noqa: E402
from budget.categorize import categorize, merchant_key as _key  # noqa: E402

GOLDEN = pathlib.Path(__file__).with_name("golden.json")

ACCOUNTS = {
    "chk": ("depository", "checking", True),
    "sav": ("depository", "savings", True),
    "hysa": ("depository", "savings", False),
    "card": ("credit", "credit_card", True),
    "apple": ("credit", "credit_card", True),
    "disc": ("credit", "credit_card", False),
}

# (account, description, low, high) - spending is negative.
SPENDING = [
    ("card", "TST* BLUE BOTTLE COFFEE", -9, -4), ("card", "SAFEWAY #1234", -140, -20), ("card", "SHELL OIL 57444", -70, -30),
    ("card", "AMAZON MKTPL*5Q1FZ3NI2", -120, -8), ("apple", "APPLE.COM/BILL", -15, -2), ("card", "UBER *TRIP", -40, -9),
    ("card", "CHEWY.COM", -60, -25), ("card", "MYSTERY MERCHANT 77", -50, -5), ("card", "CGK ON NEWBURY", -30, -10),
    ("apple", "BYLT BASICS", -250, -40), ("card", "UNITED AIRLINES", -400, -150), ("chk", "PG&E WEB ONLINE PAYMENT", -120, -60),
    ("chk", "Zelle payment to Alex", -200, -20), ("disc", "PAYPAL *SPOTIFY", -13, -12), ("chk", "TOYOTA FINANCIAL SERVICES", -352, -350),
    ("chk", "PAYPAL *GOFUNDME", -50, -10), ("card", "HIMS & HERS HEALTH", -175, -40), ("chk", "ATM WITHDRAWAL", -100, -20),
]


def merchant_key(text):
    return _key(text, text)


def build(conn):
    rng = random.Random(916)
    n = 0

    def add(account, date, amount, description, status="posted"):
        nonlocal n
        n += 1
        flow = Decimal(amount).quantize(Decimal("0.01"))
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key, raw_amount, flow,
                                         status, auto_category)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (f"{account}:{n}", account, date, description, description, merchant_key(description), flow, flow, status,
             categorize(description=description, merchant=description, flow=flow, account_type=ACCOUNTS[account][0])))
        return f"{account}:{n}"

    for key, (typ, sub, included) in ACCOUNTS.items():
        conn.execute("INSERT INTO accounts (id, name, type, subtype, include_in_budget) VALUES (%s, %s, %s, %s, %s)",
                     (key, key, typ, sub, included))

    day = datetime.date(2025, 10, 1)
    while day <= datetime.date(2026, 10, 10):
        if day.day in (1, 15):
            add("chk", day, "2915.90", "GUSTO PAY 123 PAYROLL")
        if day.day == 1:
            add("chk", day, "-1650.00", "ZELLE PAYMENT TO LANDLORD RENT")
        for account, description, low, high in SPENDING:
            if rng.random() < 0.08:
                add(account, day, f"{rng.uniform(low, high):.2f}", description)
        if day.day == 20:
            # Card payments, sometimes posting a day or two apart.
            amt = f"{rng.uniform(200, 900):.2f}"
            add("chk", day, f"-{amt}", "CHASE CREDIT CRD AUTOPAY")
            add("card", day + datetime.timedelta(days=rng.choice((0, 1, 2))), amt, "AUTOMATIC PAYMENT - THANK YOU")
            amt = f"{rng.uniform(10, 300):.2f}"
            add("chk", day, f"-{amt}", "APPLECARD GSBANK PAYMENT")
            add("apple", day, amt, "ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 7022")
            add("chk", day, "-30.00", "DISCOVER E-PAYMENT")  # card not in budget
        if day.day == 5:
            add("chk", day, "-500.00", "Online Transfer to SAV ...2222")
            add("sav", day + datetime.timedelta(days=1), "500.00", "Online Transfer from CHK ...1111")
            add("chk", day, "-300.00", "Online Transfer to HYSA")
            add("hysa", day, "300.00", "Online Transfer from CHK")
        if day.day == 12 and day.month % 2:
            add("sav", day, "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
            add("chk", day, "200.00", "Checking Deposit Overdraft transfer from ****022-S0")
        if day.day == 25 and day.month % 3 == 0:
            add("sav", day, "-1000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")  # untracked
        if day.day == 22 and day.month % 4 == 0:
            add("sav", day, "1000.00", "Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
        if day.day == 9:
            add("card", day, f"{rng.uniform(5, 40):.2f}", "AMAZON MKTPL REFUND")
            add("sav", day, "0.41", "Dividend")
        if day.day == 28:
            add("chk", day, "-12.00", "OVERDRAFT FEE")
            add("chk", day, "48.00", "Zelle from Jordan")
            add("chk", day, "-75.00", "pending store", status="pending")
        day += datetime.timedelta(days=1)

    # The user's own choices: a merchant rule, one-off overrides, and an AI answer.
    conn.execute("INSERT INTO merchant_rules (merchant_key, category) VALUES (%s, 'Dining Out')", (merchant_key("CGK ON NEWBURY"),))
    conn.execute("INSERT INTO ai_categories (merchant_key, direction, category) VALUES (%s, 'out', 'Shopping')",
                 (merchant_key("MYSTERY MERCHANT 77"),))
    conn.execute("INSERT INTO ai_categories (merchant_key, direction, category) VALUES (%s, 'out', 'Entertainment')",
                 (merchant_key("ATM WITHDRAWAL"),))
    ids = [r["id"] for r in conn.execute("SELECT id FROM transactions ORDER BY id").fetchall()]
    for tid, category in zip(ids[::37], ["Gas", "Savings", "Other Income", "Travel", "Transfer", "Giving"] * 10):
        conn.execute("UPDATE transactions SET override_category = %s WHERE id = %s", (category, tid))
    conn.commit()
    sync.link_transfers(conn)


def views():
    months = [(datetime.date(2025, 10, 1) + datetime.timedelta(days=31 * i)).replace(day=1) for i in range(13)]
    for m in months:
        end = (m + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)
        yield f"{m:%Y-%m} budget", {"start": m, "end": end, "account": "budget"}
    year = {"start": datetime.date(2025, 10, 1), "end": datetime.date(2026, 10, 10)}
    for account in ("budget", "all", "type:checking", "type:savings", "type:credit", "chk", "card", "hysa"):
        yield f"year {account}", {**year, "account": account}
    yield "year search", {**year, "account": "all", "q": "amazon"}
    yield "odd range", {"start": datetime.date(2026, 2, 17), "end": datetime.date(2026, 5, 3), "account": "budget"}


def snapshot(conn):
    def clean(v):
        if isinstance(v, Decimal):
            return str(v.quantize(Decimal("0.01")))
        if isinstance(v, (datetime.date,)):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    out = {}
    for name, f in views():
        s = queries.summary(conn, f)
        t = queries.transactions(conn, f, 1, 5000)
        out[name] = clean({
            "summary": {k: s[k] for k in ("income", "expenses", "excluded", "total_income", "total_expenses",
                                          "total_spending", "total_savings", "remaining", "unmatched_total")},
            "unmatched": [[r["date"], r["account"], r["flow"], r["category"]] for r in s["unmatched_transfers"]],
            "transactions": {"total": t["total"], "net": t["net"],
                             # Every row's category once (the whole year, all accounts) is enough.
                             "rows": [] if name not in ("year all", "year search") else [[r["id"], r["category"], r["kind"], r["overridden"], r["ai_suggested"], r["paired"]]
                                      for r in t["rows"]]},
        })
    return out


@pytest.fixture()
def conn():
    with db.connect() as c:
        c.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, ai_categories CASCADE")
        c.commit()
        yield c


def test_budget_math_matches_the_frozen_snapshot(conn):
    build(conn)
    got = snapshot(conn)
    if os.getenv("GOLDEN_UPDATE"):
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert sorted(got) == sorted(want)
    for view in want:
        assert got[view]["summary"] == want[view]["summary"], view
        assert got[view]["unmatched"] == want[view]["unmatched"], view
        assert got[view]["transactions"] == want[view]["transactions"], view
