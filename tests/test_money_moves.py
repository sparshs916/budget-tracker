"""Money moving between your own accounts: every way it can happen, and what the budget must show.

The rule being tested: moving money between two accounts in the budget is not
spending and nets to zero; spending is counted once, where the money actually
leaves you (a purchase, a bill, a payment to a person); and anything that
moves to or from an account the budget doesn't see is flagged, never silently
dropped.

Run with a throwaway Postgres:  TEST_DATABASE_URL=postgresql://... pytest
"""
import datetime
import os
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet

DB_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
os.environ.setdefault("DATABASE_URL", DB_URL or "")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

from budget import db, queries, sync  # noqa: E402
from budget.categorize import categorize  # noqa: E402

SEPT = {"start": datetime.date(2026, 9, 1), "end": datetime.date(2026, 9, 30), "account": "budget"}
AUG = {"start": datetime.date(2026, 8, 1), "end": datetime.date(2026, 8, 31), "account": "budget"}

ACCOUNTS = {
    # id: (type, subtype, in budget)
    "chk": ("depository", "checking", True),
    "sav": ("depository", "savings", True),
    "card": ("credit", "credit_card", True),
    "apple": ("credit", "credit_card", True),
    "nest": ("depository", "savings", False),  # a savings account you keep out of the budget
}


@pytest.fixture()
def book():
    with db.connect() as conn:
        conn.execute("TRUNCATE enrollments, accounts, transactions, merchant_rules, ai_categories CASCADE")
        for key, (typ, sub, included) in ACCOUNTS.items():
            conn.execute("INSERT INTO accounts (id, name, type, subtype, include_in_budget) VALUES (%s, %s, %s, %s, %s)",
                         (key, key, typ, sub, included))
        conn.commit()
        yield Book(conn)


class Book:
    def __init__(self, conn):
        self.conn = conn
        self.n = 0

    def add(self, account, date, amount, description):
        self.n += 1
        flow = Decimal(amount)
        typ = ACCOUNTS[account][0]
        tid = f"{account}:{self.n}"
        self.conn.execute(
            """INSERT INTO transactions (id, account_id, date, description, raw_amount, flow, status, auto_category)
               VALUES (%s, %s, %s, %s, %s, %s, 'posted', %s)""",
            (tid, account, datetime.date.fromisoformat(date), description, flow, flow,
             categorize(description=description, merchant=None, flow=flow, account_type=typ)))
        return tid

    def settle(self):
        self.conn.commit()
        return sync.link_transfers(self.conn)

    def summary(self, filters=SEPT):
        s = queries.summary(self.conn, filters)
        s["by"] = {r["category"]: r["amount"] for r in s["income"] + s["expenses"]}
        return s

    def category(self, tid):
        rows = queries.transactions(self.conn, {**SEPT, "start": datetime.date(2026, 1, 1),
                                                "end": datetime.date(2026, 12, 31), "account": "all"}, 1, 500)["rows"]
        return next(r["category"] for r in rows if r["id"] == tid)

    def override(self, tid, category):
        self.conn.execute("UPDATE transactions SET override_category = %s WHERE id = %s", (category, tid))
        self.settle()


def test_savings_to_checking_nets_to_zero(book):
    out = book.add("sav", "2026-09-14", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    inn = book.add("chk", "2026-09-14", "200.00", "Checking Deposit Overdraft transfer from ****022-S0")
    assert book.settle() == 1
    s = book.summary()
    assert s["total_income"] == 0 and s["total_expenses"] == 0
    assert book.category(out) == book.category(inn) == "Transfer"
    assert s["unmatched_transfers"] == []


def test_checking_to_savings_nets_to_zero_when_both_are_in_the_budget(book):
    book.add("chk", "2026-09-20", "-100.00", "G1 Online Transfer to share 0")
    book.add("sav", "2026-09-20", "100.00", "Deposit at G1 Online Transfer from share 9")
    book.settle()
    s = book.summary()
    assert s["total_expenses"] == 0 and "Savings" not in s["by"] and s["unmatched_transfers"] == []


def test_moving_money_to_savings_outside_the_budget_counts_as_savings(book):
    book.add("chk", "2026-09-05", "-500.00", "Online Transfer to SAV ...4821")
    book.add("nest", "2026-09-05", "500.00", "Online Transfer from CHK ...1111")
    book.settle()
    s = book.summary()
    assert s["by"] == {"Savings": Decimal("500.00")} and s["unmatched_transfers"] == []


def test_savings_to_checking_then_zelle_counts_the_zelle_once(book):
    book.add("sav", "2026-09-19", "-1231.00", "Withdrawal at G1 Online Transfer 1,231.00 to share 9")
    book.add("chk", "2026-09-19", "1231.00", "Checking Deposit at G1 Online Transfer 1,231.00 from share 0")
    book.add("chk", "2026-09-19", "-1230.00", "Withdrawal at ZELLE MONEY TRANSFER (Zelle)")
    book.add("chk", "2026-09-19", "-1.00", "Withdrawal at ZELLE MONEY TRANSFER (Zelle)")
    book.settle()
    s = book.summary()
    assert s["by"] == {"Person-to-Person": Decimal("1231.00")}
    assert s["total_expenses"] == Decimal("1231.00") and s["unmatched_transfers"] == []


def test_savings_to_checking_then_card_payment_counts_only_the_purchases(book):
    book.add("card", "2026-09-05", "-3000.00", "PRICELN*HOLIDAY INN EX")
    book.add("card", "2026-09-06", "-750.68", "CHEESECAKE SACRAMENTO")
    book.add("sav", "2026-09-22", "-3758.46", "Withdrawal Overdraft transfer to ****022-S9")
    book.add("chk", "2026-09-22", "3758.46", "Checking Deposit Overdraft transfer from ****022-S0")
    book.add("chk", "2026-09-22", "-3750.68", "Withdrawal-ACH-A-866353 WEBCHASE CREDIT CRD (EPAY)")
    book.add("card", "2026-09-21", "3750.68", "Payment Thank You-Mobile")
    assert book.settle() == 2
    s = book.summary()
    assert s["by"] == {"Travel": Decimal("3000.00"), "Dining Out": Decimal("750.68")}
    assert s["unmatched_transfers"] == []


def test_paying_a_card_straight_from_savings(book):
    book.add("apple", "2026-09-16", "-803.90", "SP BYLT BASICS")
    book.add("sav", "2026-09-29", "-803.90", "Withdrawal-ACH-A-1374866 WEBAPPLECARD GSBANK (PAYMENT)")
    book.add("apple", "2026-09-29", "803.90", "ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 7022")
    book.settle()
    s = book.summary()
    assert s["by"] == {"Shopping": Decimal("803.90")} and s["unmatched_transfers"] == []


def test_paying_a_bill_straight_from_savings_counts_once(book):
    book.add("sav", "2026-09-23", "-351.75", "Withdrawal-ACH-A-987134 WEBALLY (ALLY PAYMT)")
    book.add("sav", "2026-09-30", "-49.29", "Withdrawal-ACH-A-1608460 WEBHealthy Paws (debitpmt)")
    book.settle()
    assert book.summary()["by"] == {"Car Payment": Decimal("351.75"), "Pet Insurance": Decimal("49.29")}


@pytest.mark.parametrize("lag,paired", [(0, True), (1, True), (3, True), (4, False)])
def test_transfers_pair_up_to_three_days_apart(book, lag, paired):
    book.add("sav", "2026-09-10", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    book.add("chk", (datetime.date(2026, 9, 10) + datetime.timedelta(days=lag)).isoformat(), "200.00",
             "Checking Deposit Overdraft transfer from ****022-S0")
    assert book.settle() == (1 if paired else 0)
    s = book.summary()
    assert s["total_expenses"] == 0  # a transfer is never spending, matched or not...
    assert (s["unmatched_transfers"] == []) is paired  # ...but an unmatched one is flagged


def test_several_identical_transfers_pair_one_to_one(book):
    for day in ("2026-09-09", "2026-09-09", "2026-09-10"):
        book.add("sav", day, "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    for day in ("2026-09-09", "2026-09-10"):
        book.add("chk", day, "200.00", "Checking Deposit Overdraft transfer from ****022-S0")
    assert book.settle() == 2
    s = book.summary()
    assert s["unmatched_total"] == Decimal("-200.00") and len(s["unmatched_transfers"]) == 1


def test_money_to_an_account_you_dont_track_is_flagged_until_you_say_what_it_was(book):
    tid = book.add("sav", "2026-08-18", "-3000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    book.settle()
    s = book.summary(AUG)
    assert s["total_expenses"] == 0 and s["unmatched_total"] == Decimal("-3000.00")
    book.override(tid, "Savings")  # it went to your Capital One savings
    s = book.summary(AUG)
    assert s["by"] == {"Savings": Decimal("3000.00")} and s["unmatched_transfers"] == []


def test_money_from_an_account_you_dont_track_is_flagged_not_counted_as_income(book):
    book.add("sav", "2026-09-22", "1000.00", "Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER) RT0675D686ABBE0")
    book.settle()
    s = book.summary()
    assert s["total_income"] == 0 and s["unmatched_total"] == Decimal("1000.00")


def test_unticked_checking_is_caught(book):
    # Savings -> checking -> Zelle, but checking isn't in the budget: the Zelle is invisible,
    # so the transfer out of savings must be flagged.
    book.conn.execute("UPDATE accounts SET include_in_budget = FALSE WHERE id = 'chk'")
    book.add("sav", "2026-09-19", "-1231.00", "Withdrawal at G1 Online Transfer 1,231.00 to share 9")
    book.add("chk", "2026-09-19", "1231.00", "Checking Deposit at G1 Online Transfer 1,231.00 from share 0")
    book.add("chk", "2026-09-19", "-1230.00", "Withdrawal at ZELLE MONEY TRANSFER (Zelle)")
    book.settle()
    s = book.summary()
    assert s["total_expenses"] == 0 and s["unmatched_total"] == Decimal("-1231.00")
    book.conn.execute("UPDATE accounts SET include_in_budget = TRUE WHERE id = 'chk'")
    book.settle()
    s = book.summary()
    assert s["by"] == {"Person-to-Person": Decimal("1230.00")} and s["unmatched_transfers"] == []


def test_a_transfer_across_the_end_of_a_month_counts_in_neither(book):
    book.add("sav", "2026-08-31", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    book.add("chk", "2026-09-01", "200.00", "Checking Deposit Overdraft transfer from ****022-S0")
    book.settle()
    for month in (AUG, SEPT):
        s = book.summary(month)
        assert s["total_expenses"] == 0 and s["total_income"] == 0 and s["unmatched_transfers"] == []


def test_coincidental_same_amounts_are_not_mistaken_for_a_transfer(book):
    bill = book.add("chk", "2026-09-25", "-45.00", "PG&E WEB ONLINE PAYMENT")
    refund = book.add("card", "2026-09-25", "45.00", "AMAZON MKTPL REFUND")
    pay = book.add("sav", "2026-09-28", "2915.90", "Deposit-ACH-1296959 HP ENTERPRISE CO (PAYROLL)")
    book.add("chk", "2026-09-28", "-2915.90", "Online Transfer to CHK ...9999")
    book.settle()
    assert book.category(bill) == "Electricity" and book.category(refund) == "Shopping"
    assert book.category(pay) == "Income"
    s = book.summary()
    assert s["total_income"] == Decimal("2915.90")
    assert s["by"]["Electricity"] == Decimal("45.00") and s["by"]["Shopping"] == Decimal("-45.00")


def test_saying_a_transfer_was_something_else_breaks_the_pair(book):
    out = book.add("sav", "2026-09-14", "-200.00", "Withdrawal Overdraft transfer to ****022-S9")
    inn = book.add("chk", "2026-09-14", "200.00", "Checking Deposit Overdraft transfer from ****022-S0")
    book.settle()
    book.override(inn, "Other Income")  # e.g. it was really a friend paying you back
    assert book.category(out) == "Transfer"
    s = book.summary()
    assert s["by"] == {"Other Income": Decimal("200.00")} and s["unmatched_total"] == Decimal("-200.00")


def test_card_payment_and_refund_on_the_same_card(book):
    book.add("card", "2026-09-02", "-300.00", "TARGET T-2268")
    book.add("card", "2026-09-04", "25.00", "TARGET T-2268 RETURN")
    book.add("chk", "2026-09-09", "-275.00", "Withdrawal-ACH-A-670654 WEBDISCOVER (E-PAYMENT)")
    book.add("card", "2026-09-07", "275.00", "INTERNET PAYMENT - THANK YOU")
    book.settle()
    s = book.summary()
    assert s["by"] == {"Shopping": Decimal("275.00")} and s["unmatched_transfers"] == []


def test_card_payment_without_the_cards_own_record_is_flagged(book):
    # Your August: checking paid Discover, but SimpleFIN had no Discover payment to match it.
    book.add("chk", "2026-08-14", "-248.22", "Discover Card Payment Financial Services/Credit Card Payment")
    book.settle()
    s = book.summary(AUG)
    assert s["total_expenses"] == 0 and s["unmatched_total"] == Decimal("-248.22")
    book.add("card", "2026-08-12", "248.22", "INTERNET PAYMENT - THANK YOU")  # uploading the Discover CSV adds it
    book.settle()
    assert book.summary(AUG)["unmatched_transfers"] == []


# ---------- best guess: one side is vague about being a transfer ----------

def banks(book, **institutions):
    for account, name in institutions.items():
        book.conn.execute("UPDATE accounts SET institution = %s WHERE id = %s", (name, account))


def test_transfer_described_vaguely_by_the_receiving_bank_still_pairs(book):
    # August: Golden 1 says "CAPITAL ONE (TRANSFER)", Capital One just says "THE GOLDEN 1 CREDIT UNION".
    banks(book, sav="Golden 1 Credit Union", nest="Capital One")
    book.conn.execute("UPDATE accounts SET include_in_budget = TRUE WHERE id = 'nest'")
    out = book.add("sav", "2026-08-18", "-3000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER) (Capital One Transfer)")
    inn = book.add("nest", "2026-08-17", "3000.00", "THE GOLDEN 1 CREDIT UNION")
    assert book.category(inn) == "Other Income"  # before: $3,000 of "income"
    book.settle()
    s = book.summary(AUG)
    assert s["total_income"] == 0 and s["total_expenses"] == 0 and s["unmatched_transfers"] == []
    assert book.category(out) == book.category(inn) == "Transfer"


def test_vague_money_out_pairs_with_a_clear_transfer_in(book):
    banks(book, chk="Golden 1 Credit Union", sav="Ally Bank")
    book.add("chk", "2026-09-03", "-75.00", "ALLY BANK $TRANSFER")      # vague, but names the other bank
    book.add("sav", "2026-09-04", "75.00", "Online Transfer from Golden 1")
    book.settle()
    assert book.summary()["total_expenses"] == 0


def test_large_vague_bank_to_bank_move_pairs_without_a_bank_name(book):
    book.add("sav", "2026-09-10", "-1500.00", "Withdrawal Online Transfer")
    book.add("chk", "2026-09-11", "1500.00", "CREDIT ID NUMBER 88213")  # Other Income on its own
    book.settle()
    s = book.summary()
    assert s["total_income"] == 0 and s["unmatched_transfers"] == []


@pytest.mark.parametrize("out_desc,in_account,in_desc", [
    ("Online Transfer to ...4411", "chk", "GUSTO PAY 123 PAYROLL"),          # a paycheck is never a transfer
    ("Online Transfer to ...4411", "chk", "Zelle from Jordan"),              # nor a person paying you
    ("Online Transfer to ...4411", "chk", "MYSTERY CREDIT"),                 # small, and no bank named
    ("MYSTERY WITHDRAWAL", "chk", "MYSTERY CREDIT"),                         # two unknowns aren't a transfer
    ("Online Transfer to ...4411", "card", "MERCHANT CREDIT"),               # a card credit, no bank named
])
def test_best_guess_leaves_these_alone(book, out_desc, in_account, in_desc):
    amount = "2915.90" if "GUSTO" in in_desc else "60.00"
    book.add("sav", "2026-09-10", f"-{amount}", out_desc)
    inn = book.add(in_account, "2026-09-11", amount, in_desc)
    before = book.category(inn)
    book.settle()
    assert book.category(inn) == before


def test_best_guess_respects_your_own_label(book):
    banks(book, sav="Golden 1 Credit Union", nest="Capital One")
    book.add("sav", "2026-08-18", "-3000.00", "Withdrawal-ACH WEBCAPITAL ONE (TRANSFER)")
    inn = book.add("nest", "2026-08-17", "3000.00", "THE GOLDEN 1 CREDIT UNION")
    book.override(inn, "Other Income")  # e.g. it really was a gift that happened to match
    book.settle()
    assert book.category(inn) == "Other Income"


def test_a_clear_pair_wins_over_a_vague_one(book):
    book.add("sav", "2026-09-10", "-500.00", "Online Transfer to CHK")
    clear = book.add("chk", "2026-09-12", "500.00", "Online Transfer from SAV")
    vague = book.add("apple", "2026-09-10", "500.00", "SOMETHING")  # closer in time, but vague
    book.settle()
    assert book.category(clear) == "Transfer" and book.category(vague) != "Transfer"


def test_bank_words_ignore_generic_parts_of_names():
    assert sync._bank_words("Golden 1 Credit Union") == {"golden"}
    assert sync._bank_words("Capital One") == {"capital"}
    assert sync._bank_words("The Bank of America, N.A.") == {"america"}
    assert sync._bank_words(None) == set() and sync._bank_words("Manual") == set()


# ---------- a savings account kept outside the budget (Capital One 360) ----------

@pytest.fixture()
def capital_one(book):
    banks(book, sav="Golden 1 Credit Union", nest="Capital One")  # nest: savings, not in the budget
    return book


def test_moving_money_into_savings_and_back_shows_what_you_kept(capital_one):
    book = capital_one
    book.add("chk", "2026-09-01", "3000.00", "GUSTO PAY 123 PAYROLL")
    book.add("sav", "2026-09-05", "-1000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    book.add("nest", "2026-09-04", "1000.00", "THE GOLDEN 1 CREDIT UNION")
    book.add("nest", "2026-09-20", "-300.00", "Withdrawal to GOLDEN 1 CREDIT UNION")
    back = book.add("sav", "2026-09-21", "300.00", "Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    book.add("nest", "2026-09-30", "2.15", "Monthly Interest Paid")  # stays with the savings, not budget income
    book.settle()
    s = book.summary()
    assert s["by"]["Savings"] == Decimal("700.00")  # 1,000 in, 300 back
    assert s["savings_in"] == Decimal("1000.00") and s["savings_out"] == Decimal("300.00")
    # Saving doesn't touch Remaining: it's income minus spending.
    assert s["total_income"] == Decimal("3000.00") and s["remaining"] == Decimal("3000.00") and s["total_expenses"] == 0
    assert book.category(back) == "Savings" and s["unmatched_transfers"] == []


def test_same_moves_with_capital_one_inside_the_budget_just_cancel(capital_one):
    book = capital_one
    book.conn.execute("UPDATE accounts SET include_in_budget = TRUE WHERE id = 'nest'")
    book.add("sav", "2026-09-05", "-1000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    book.add("nest", "2026-09-04", "1000.00", "THE GOLDEN 1 CREDIT UNION")
    book.add("nest", "2026-09-20", "-300.00", "Withdrawal to GOLDEN 1 CREDIT UNION")
    book.add("sav", "2026-09-21", "300.00", "Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    book.settle()
    s = book.summary()
    assert s["total_expenses"] == 0 and s["total_income"] == 0 and "Savings" not in s["by"]


def test_checkup_suggests_treating_a_fill_only_account_as_savings(capital_one):
    book = capital_one
    book.conn.execute("UPDATE accounts SET include_in_budget = TRUE WHERE id = 'nest'")
    for day in ("2026-08-04", "2026-09-04"):
        book.add("sav", day, "-500.00", "Withdrawal-ACH WEBCAPITAL ONE (TRANSFER)")
        book.add("nest", day, "500.00", "THE GOLDEN 1 CREDIT UNION")
    book.add("sav", "2026-09-12", "2915.90", "Deposit-ACH HP ENTERPRISE CO (PAYROLL)")  # Golden 1 gets pay: not savings
    book.settle()
    found = [i for i in queries.health(book.conn, datetime.date(2026, 10, 10)) if i["kind"] == "savings_account"]
    assert [i["account_id"] for i in found] == ["nest"] and found[0]["action"] == "make_savings"


def test_labeling_a_deposit_into_an_in_budget_account_as_savings_is_undoable(capital_one):
    # What happened in August: the +$3,000 arriving at Capital One was labeled Savings by hand,
    # which reads as $3,000 taken OUT of savings (Savings -3,000; it used to inflate Remaining too).
    book = capital_one
    book.conn.execute("UPDATE accounts SET include_in_budget = TRUE WHERE id = 'nest'")
    book.add("sav", "2026-08-18", "-3000.00", "Withdrawal-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)")
    inn = book.add("nest", "2026-08-17", "3000.00", "THE GOLDEN 1 CREDIT UNION")
    book.settle()
    book.override(inn, "Savings")
    s = book.summary(AUG)
    assert s["by"]["Savings"] == Decimal("-3000.00")
    assert s["remaining"] == 0 and s["total_expenses"] == 0  # Savings never moves Remaining, even mislabeled
    book.override(inn, None)  # "↺ Automatic"
    s = book.summary(AUG)
    assert "Savings" not in s["by"] and s["total_expenses"] == 0 and s["total_income"] == 0
