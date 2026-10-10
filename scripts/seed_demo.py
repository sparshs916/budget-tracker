"""Load ~3 months of fake transactions so the app can be tried without Teller.

    python scripts/seed_demo.py

Goes through the real sync code with a fake Teller client. Only touches the
"demo" enrollment, so it is safe to run against a database with real data,
but don't: demo rows would show up in your budget.
"""
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from budget import db  # noqa: E402
from budget.sync import save_enrollment, sync_enrollment  # noqa: E402

ENROLLMENT = "demo-enrollment"


def build(today):
    rng = random.Random(42)
    chk, cc, sav = [], [], []
    n = 0

    def add(target, date, amount, description, type="card_payment", category=None, merchant=None):
        nonlocal n
        if date > today:
            return
        n += 1
        status = "pending" if (today - date).days < 2 else "posted"
        target.append({"id": f"demo_{n}", "date": date.isoformat(), "amount": f"{amount:.2f}",
                       "description": description, "type": type, "status": status,
                       "details": {"category": category, "counterparty": {"name": merchant}}})

    first = (today.replace(day=1) - datetime.timedelta(days=62)).replace(day=1)
    month = first
    while month <= today:
        d = lambda day: month.replace(day=day)  # noqa: E731
        # Checking (Teller: money out is negative)
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
        # Credit card (Teller: purchases positive, payments negative)
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


class FakeTeller:
    def __init__(self, txns):
        self.txns = txns

    def accounts(self):
        def account(id, name, type, subtype, last_four):
            return {"id": id, "enrollment_id": ENROLLMENT, "name": name, "type": type, "subtype": subtype,
                    "last_four": last_four, "status": "open", "institution": {"name": "Demo Bank"},
                    "links": {"transactions": "x", "balances": "x"}}
        return [
            account("demo_chk", "Everyday Checking", "depository", "checking", "1199"),
            account("demo_sav", "High Yield Savings", "depository", "savings", "4821"),
            account("demo_cc", "Sapphire Card", "credit", "credit_card", "7310"),
        ]

    def balances(self, account_id):
        return {"demo_chk": {"ledger": "3412.55", "available": "3412.55"},
                "demo_sav": {"ledger": "12500.00", "available": "12500.00"},
                "demo_cc": {"ledger": "842.17", "available": "9157.83"}}[account_id]

    def transactions(self, account_id, start_date=None):
        return list(reversed(self.txns[account_id]))


def main():
    today = datetime.date.today()
    with db.connect() as conn:
        conn.execute("DELETE FROM enrollments WHERE id = %s", (ENROLLMENT,))
        save_enrollment(conn, ENROLLMENT, "Demo Bank", "demo-token")
        conn.commit()
        enrollment = conn.execute("SELECT * FROM enrollments WHERE id = %s", (ENROLLMENT,)).fetchone()
        result = sync_enrollment(conn, enrollment, today=today, client=FakeTeller(build(today)))
    print(f"Loaded {result['transactions']} demo transactions across {result['accounts']} accounts.")


if __name__ == "__main__":
    main()
