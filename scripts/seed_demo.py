"""Load ~3 months of fake transactions so the app can be tried without SimpleFIN.

    python scripts/seed_demo.py

Goes through the real sync code with a fake SimpleFIN server. Only touches the
"demo" connection, so it is safe to run against a database with real data,
but don't: demo rows would show up in your budget.
"""
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zoneinfo import ZoneInfo  # noqa: E402

from budget import config, crypto, db  # noqa: E402
from budget.sync import sync_enrollment  # noqa: E402

CONNECTION = "demo-connection"
TZ = ZoneInfo(config.TIMEZONE)


def build(today):
    rng = random.Random(42)
    chk, cc, sav = [], [], []
    n = 0

    def add(target, date, amount, description, type=None, category=None, merchant=None):
        # Amounts below are written as "money out of checking is negative, card
        # purchases are positive"; SimpleFIN uses + for money into every account.
        nonlocal n
        if date > today:
            return
        n += 1
        if target is cc:
            amount = -amount
        stamp = int(datetime.datetime.combine(date, datetime.time(12), TZ).timestamp())
        pending = (today - date).days < 2
        t = {"id": f"demo_{n}", "amount": f"{amount:.2f}", "description": description,
             "transacted_at": stamp, "posted": 0 if pending else stamp, "pending": pending}
        if merchant:
            t["payee"] = merchant
        target.append(t)

    first = (today.replace(day=1) - datetime.timedelta(days=62)).replace(day=1)
    month = first
    while month <= today:
        d = lambda day: month.replace(day=day)  # noqa: E731
        # Checking
        add(chk, d(1), 2701.00, "ACME CORP PAYROLL PPD", "ach", "income")
        add(chk, d(15), 2701.00, "ACME CORP PAYROLL PPD", "ach", "income")
        add(chk, d(1), -1650.00, "ESSEX PROPERTY RENT WEB PMT", "ach")
        add(chk, d(2), -500.00, "Online Transfer to SAV ...4821", "transfer")
        add(chk, d(16), -500.00, "Online Transfer to SAV ...4821", "transfer")
        add(chk, d(5), -351.00, "TOYOTA FINANCIAL SERVICES", "ach")
        add(chk, d(8), -round(rng.uniform(80, 110), 2), "PGANDE WEB ONLINE PG&E", "ach")
        add(chk, d(12), -round(rng.uniform(40, 60), 2), "CITY WATER UTILITY", "ach")
        add(chk, d(3), -250.00, "TITHELY GRACE CHURCH", "ach", "charity")
        add(chk, d(18), -40.00, "Zelle payment to Sam", "transfer")
        # Savings
        add(sav, d(2), 500.00, "Online Transfer from CHK ...1199", "transfer")
        add(sav, d(16), 500.00, "Online Transfer from CHK ...1199", "transfer")
        add(sav, d(28), 3.12, "Interest Payment", "interest")
        # Credit card (purchases written positive here, flipped in add())
        card_total = 0.0
        recurring = [
            (4, 55.00, "COMCAST CABLE COMM", "utilities", "Comcast"),
            (6, 70.00, "T-MOBILE AUTOPAY", "phone", "T-Mobile"),
            (7, 12.00, "LEMONADE INSURANCE", "insurance", "Lemonade"),
            (9, 50.00, "TRUPANION PET INSURANCE", "insurance", "Trupanion"),
            (10, 15.49, "NETFLIX.COM", "entertainment", "Netflix"),
            (11, 11.99, "SPOTIFY USA", "entertainment", "Spotify"),
            (20, 29.99, "CHEWY.COM", "shopping", "Chewy"),
        ]
        for day, amount, desc, cat, merch in recurring:
            add(cc, d(day), amount, desc, category=cat, merchant=merch)
            card_total += amount
        for day in (3, 10, 17, 24):
            amount = round(rng.uniform(60, 140), 2)
            add(cc, d(day), amount, "TRADER JOE'S #552", category="groceries", merchant="Trader Joe's")
            card_total += amount
        for day in rng.sample(range(1, 28), 6):
            name = rng.choice(["CHIPOTLE 2231", "TST* BLUE BOTTLE", "DOORDASH*THAI HOUSE", "IN-N-OUT BURGER"])
            amount = round(rng.uniform(9, 45), 2)
            add(cc, d(day), amount, name, category="dining")
            card_total += amount
        for day in (6, 19):
            amount = round(rng.uniform(45, 65), 2)
            add(cc, d(day), amount, "SHELL OIL 57444", category="fuel", merchant="Shell")
            card_total += amount
        amount = round(rng.uniform(20, 120), 2)
        add(cc, d(14), amount, "AMAZON MKTPL*2K4", category="shopping", merchant="Amazon")
        add(cc, d(21), -round(amount / 2, 2), "AMAZON MKTPL REFUND", category="shopping", merchant="Amazon")
        # Pay last month's card from checking.
        add(cc, d(25), -round(card_total, 2), "AUTOMATIC PAYMENT - THANK YOU", "payment")
        add(chk, d(25), -round(card_total, 2), "CHASE CREDIT CRD AUTOPAY", "ach")

        month = (month + datetime.timedelta(days=32)).replace(day=1)
    return {"demo_chk": chk, "demo_sav": sav, "demo_cc": cc}


class FakeSimpleFIN:
    def __init__(self, txns):
        self.txns = txns

    def accounts(self, start, end):
        def account(id, name, balance):
            return {"org": {"domain": "demo.bank", "name": "Demo Bank", "sfin-url": "https://demo.invalid"},
                    "id": id, "name": name, "currency": "USD", "balance": balance, "balance-date": end,
                    "transactions": [t for t in self.txns[id] if start <= (t["posted"] or t["transacted_at"]) < end]}
        return {"errors": [], "accounts": [
            account("demo_chk", "Everyday Checking", "3412.55"),
            account("demo_sav", "High Yield Savings", "12500.00"),
            account("demo_cc", "Sapphire Card", "-842.17"),
        ]}


def main():
    today = datetime.datetime.now(TZ).date()
    with db.connect() as conn:
        conn.execute("DELETE FROM enrollments WHERE id = %s", (CONNECTION,))
        conn.execute("INSERT INTO enrollments (id, institution, access_token) VALUES (%s, 'Demo Bank', %s)",
                     (CONNECTION, crypto.encrypt("https://demo:demo@demo.invalid/simplefin")))
        conn.commit()
        connection = conn.execute("SELECT * FROM enrollments WHERE id = %s", (CONNECTION,)).fetchone()
        result = sync_enrollment(conn, connection, today=today, client=FakeSimpleFIN(build(today)))
    print(f"Loaded {result['transactions']} demo transactions across {result['accounts']} accounts.")


if __name__ == "__main__":
    main()
