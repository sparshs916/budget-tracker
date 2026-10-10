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
    ("MYSTERY DEPOSIT", 20, "depository", "Other Income"),
    ("MYSTERY CHARGE", -20, "credit", "Other"),
    ("AMAZON MKTPL REFUND", 25, "credit", "Shopping"),
])
def test_categorize(description, flow, account_type, expected):
    assert categorize(description=description, merchant=None, teller_category=None, teller_type=None,
                      flow=flow, account_type=account_type) == expected
