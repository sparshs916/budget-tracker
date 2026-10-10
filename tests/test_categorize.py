import pytest

from budget.categorize import categorize


@pytest.mark.parametrize("description,flow,account_type,expected", [
    ("TST* BLUE BOTTLE COFFEE", -6, "credit", "Dining Out"),
    ("APPLE.COM/BILL", -2.99, "credit", "Subscriptions"),
    ("SHELL OIL 57444", -48, "credit", "Gas"),
    ("CHEWY.COM", -30, "credit", "Pet Food & Supplies"),
    ("TRUPANION", -50, "depository", "Pet Insurance"),
    ("LEMONADE INSURANCE", -12, "credit", "Renter's Insurance"),
    ("TOYOTA FINANCIAL SERVICES", -351, "depository", "Car Payment"),
    ("COMCAST CABLE COMM", -55, "credit", "Internet"),
    ("PAYMENT THANK YOU-MOBILE", 500, "credit", "Credit Card Payment"),
    ("DISCOVER E-PAYMENT 1234", -500, "depository", "Credit Card Payment"),
    ("Zelle payment to John", -40, "depository", "Person-to-Person"),
    ("GUSTO PAY 123", 2400, "depository", "Income"),
    ("Online Transfer to SAV ...2222", -500, "depository", "Savings"),
    ("Online Transfer from CHK ...1111", 500, "depository", "Transfer"),
    ("Deposit-ACH-A-1171471 WEBCAPITAL ONE (TRANSFER)", 1000, "depository", "Transfer"),
    ("MYSTERY DEPOSIT", 20, "depository", "Other Income"),
    ("MYSTERY CHARGE", -20, "credit", "Other"),
    ("AMAZON MKTPL REFUND", 25, "credit", "Shopping"),
])
def test_categorize(description, flow, account_type, expected):
    assert categorize(description=description, merchant=None, flow=flow, account_type=account_type) == expected


@pytest.mark.parametrize("description,expected", [
    ("CHARITY:WATER-DONATION", "Giving"),  # not a water bill
    ("WWW.GCRELIEF.ORG", "Giving"),
    ("TIM TEBOW FOUNDATION", "Giving"),
    ("ST JUDE Internet DNTN", "Giving"),
    ("GRACEPOINTE FAITH CHUR", "Giving"),
    ("CITY WATER UTILITY", "Other Utilities"),
    ("SQ *EVE'S AUTOMOTIVE", "Transportation"),  # Square isn't always a restaurant
    ("SQ *FARIA BAKERY", "Dining Out"),
    ("PRICELN*HOLIDAY INN EX", "Travel"),
    ("MBTA-550052586736", "Transportation"),
    ("EXCEL GAS AND MARKET 3", "Gas"),
    ("ADAMS CONVENIENCE STORE", "Groceries"),
    ("CHICK FIL A 80845", "Dining Out"),
    ("UNITED HEALTHCARE", "Health"),
    ("UNITED AIRLINES", "Travel"),
    ("BIBLICAL MASTERY ACDMY", "Education"),
])
def test_categorize_card_purchases(description, expected):
    assert categorize(description=description, merchant=None, flow=-10, account_type="credit") == expected

