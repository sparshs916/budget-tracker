"""Turn a Teller transaction into one budget category.

Every category has a kind:
  income   - counted in Income
  expense  - counted in Total Expenses
  savings  - money you moved to savings/investing; shown as its own line and
             included in Total Expenses (matches the spreadsheet layout)
  excluded - moving money between your own accounts (credit card payments,
             internal transfers). Excluded from totals so nothing is counted
             twice: the purchase is already counted on the card.
"""
import re

# Display order of the budget table.
CATEGORIES = [
    ("Income", "income"),
    ("Other Income", "income"),
    ("Rent", "expense"),
    ("Electricity", "expense"),
    ("Other Utilities", "expense"),
    ("Internet", "expense"),
    ("Phone", "expense"),
    ("Renter's Insurance", "expense"),
    ("Car Insurance", "expense"),
    ("Groceries", "expense"),
    ("Dining Out", "expense"),
    ("Pet Food & Supplies", "expense"),
    ("Pet Insurance", "expense"),
    ("Car Payment", "expense"),
    ("Gas", "expense"),
    ("Transportation", "expense"),
    ("Subscriptions", "expense"),
    ("Shopping", "expense"),
    ("Health", "expense"),
    ("Entertainment", "expense"),
    ("Travel", "expense"),
    ("Giving", "expense"),
    ("Education", "expense"),
    ("Fees & Interest", "expense"),
    ("Taxes", "expense"),
    ("Person-to-Person", "expense"),
    ("Other", "expense"),
    ("Savings", "savings"),
    ("Credit Card Payment", "excluded"),
    ("Transfer", "excluded"),
]
CATEGORY_KIND = dict(CATEGORIES)
CATEGORY_NAMES = [name for name, _ in CATEGORIES]


def _rx(*words):
    # Lookarounds instead of \b so patterns like "tst\*" or "apple.com/bill" still work.
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?![a-z0-9])", re.I)


CARD_PAYMENT = _rx(
    r"payment thank you", r"autopay", r"auto pay", r"epayment", r"e-payment", r"online payment",
    r"mobile payment", r"crd pmt", r"card pmt", r"cardmember serv", r"credit card", r"card payment",
    r"applecard", r"apple card", r"gsbank", r"chase card", r"chase credit", r"amex", r"american express",
    r"discover", r"capital one", r"citi card", r"citicard", r"barclaycard", r"bank of america credit",
    r"wells fargo card", r"synchrony", r"bilt",
)
SAVINGS = _rx(
    r"to sav\w*", r"to savings", r"transfer to share", r"ally bank", r"marcus", r"wealthfront",
    r"betterment", r"robinhood", r"vanguard", r"fidelity", r"schwab", r"acorns", r"stash",
    r"sofi sav\w*", r"high yield", r"m1 finance", r"roth", r"brokerage",
)
INTERNAL_TRANSFER = _rx(
    r"online transfer", r"internal transfer", r"transfer from", r"transfer to", r"xfer", r"from sav\w*",
    r"from chk", r"to chk", r"from checking", r"to checking", r"funds transfer",
)
P2P = _rx(r"zelle", r"venmo", r"cash app", r"cashapp", r"square cash", r"paypal transfer", r"apple cash")
PAYCHECK = _rx(r"payroll", r"direct dep\w*", r"dir dep", r"salary", r"paycheck", r"adp", r"gusto", r"paychex")

# First match wins, so put specific rules above broad ones.
KEYWORD_RULES = [
    ("Pet Insurance", _rx(r"trupanion", r"healthy paws", r"embrace pet", r"fetch pet", r"pets best", r"pet insurance", r"lemonade pet", r"nationwide pet")),
    ("Renter's Insurance", _rx(r"lemonade", r"renters? ins\w*", r"assurant", r"sure renters")),
    ("Car Insurance", _rx(r"geico", r"progressive", r"state farm", r"allstate", r"usaa", r"farmers ins\w*", r"liberty mutual", r"auto insurance")),
    ("Car Payment", _rx(r"auto loan", r"car payment", r"toyota (?:financial|motor credit)", r"honda financial", r"american honda", r"ally auto", r"ally payment", r"tesla financ\w*", r"gm financial", r"ford credit", r"hyundai motor finance", r"nissan motor", r"carmax auto", r"capital one auto")),
    ("Rent", _rx(r"rent", r"apartments?", r"property (?:management|mgmt)", r"leasing", r"appfolio", r"rentcafe", r"yardi", r"buildium", r"avalon", r"equity residential", r"essex property")),
    ("Electricity", _rx(r"pg&e", r"pge", r"pacific gas", r"edison", r"smud", r"duke energy", r"con ?ed", r"electric\w*", r"power co\w*", r"dominion energy", r"xcel energy", r"georgia power", r"fpl")),
    ("Other Utilities", _rx(r"water", r"sewer", r"waste management", r"republic services", r"recology", r"trash", r"utilit\w*", r"gas co", r"socalgas", r"so cal gas", r"nicor", r"spire")),
    ("Internet", _rx(r"comcast", r"xfinity", r"spectrum", r"charter comm\w*", r"cox comm\w*", r"sonic\.net", r"google fiber", r"fios", r"at&t internet", r"att internet", r"starlink", r"frontier comm\w*")),
    ("Phone", _rx(r"t-mobile", r"tmobile", r"verizon", r"mint mobile", r"visible", r"cricket", r"at&t", r"att\*", r"google fi", r"us mobile")),
    ("Subscriptions", _rx(r"netflix", r"spotify", r"hulu", r"disney\s?\+?", r"disneyplus", r"apple\.com/bill", r"itunes", r"youtube", r"prime video", r"amazon prime", r"hbo", r"max\.com", r"paramount", r"peacock", r"audible", r"icloud", r"chatgpt", r"openai", r"anthropic", r"claude\.ai", r"adobe", r"microsoft 365", r"dropbox", r"patreon", r"substack", r"nytimes", r"duolingo", r"siriusxm", r"github")),
    ("Pet Food & Supplies", _rx(r"chewy", r"petco", r"petsmart", r"pet supplies", r"pet food", r"bark ?box", r"farmer'?s dog", r"ollie", r"veterinar\w*", r"vet clinic", r"banfield", r"vca")),
    ("Gas", _rx(r"shell", r"chevron", r"arco", r"exxon", r"mobil", r"valero", r"circle k", r"speedway", r"76", r"phillips 66", r"costco gas", r"sunoco", r"marathon petro\w*", r"quiktrip", r"qt", r"wawa", r"sheetz", r"bp", r"texaco", r"gas station", r"fuel")),
    ("Groceries", _rx(r"trader joe'?s?", r"safeway", r"whole foods", r"wholefds", r"costco", r"kroger", r"aldi", r"sprouts", r"h-e-b", r"heb", r"publix", r"raley'?s", r"lucky", r"food ?4 ?less", r"winco", r"vons", r"albertsons", r"ralphs", r"smart ?& ?final", r"grocery", r"market", r"instacart", r"wegmans", r"stop & shop", r"giant", r"meijer", r"fred meyer", r"99 ranch", r"h mart")),
    ("Dining Out", _rx(r"doordash", r"uber ?eats", r"grubhub", r"postmates", r"starbucks", r"mcdonald'?s", r"chipotle", r"taco bell", r"chick-fil-a", r"in-n-out", r"panera", r"subway", r"domino'?s", r"pizza", r"cafe", r"coffee", r"restaurant", r"grill", r"kitchen", r"bar & grill", r"sushi", r"burger", r"tst\*", r"sq \*")),
    ("Transportation", _rx(r"uber", r"lyft", r"parking", r"toll", r"fastrak", r"ez ?pass", r"clipper", r"bart", r"caltrain", r"metro", r"transit", r"dmv", r"jiffy lube", r"auto repair", r"car wash", r"tire")),
    ("Travel", _rx(r"airlines?", r"united", r"delta", r"southwest", r"alaska air", r"jetblue", r"american air\w*", r"hotel", r"marriott", r"hilton", r"hyatt", r"airbnb", r"vrbo", r"expedia", r"booking\.com")),
    ("Giving", _rx(r"church", r"tithe\w*", r"tithely", r"pushpay", r"planning center", r"ministr\w*", r"donation", r"charit\w*", r"compassion intl", r"world vision", r"gofundme")),
    ("Health", _rx(r"cvs", r"walgreens", r"rite aid", r"pharmacy", r"kaiser", r"dental", r"dentist", r"medical", r"clinic", r"hospital", r"optometr\w*", r"gym", r"fitness", r"planet fitness", r"24 hour fitness")),
    ("Entertainment", _rx(r"amc", r"cinema", r"theat(?:er|re)", r"ticketmaster", r"stubhub", r"steam", r"playstation", r"xbox", r"nintendo", r"bowling", r"concert")),
    ("Shopping", _rx(r"amazon", r"amzn", r"target", r"walmart", r"best buy", r"home depot", r"lowe'?s", r"ikea", r"etsy", r"ebay", r"tj ?maxx", r"marshalls", r"ross", r"old navy", r"nike", r"apple store")),
    ("Fees & Interest", _rx(r"interest charge", r"finance charge", r"late fee", r"annual fee", r"overdraft", r"service fee", r"atm fee", r"foreign transaction fee")),
    ("Taxes", _rx(r"irs", r"franchise tax", r"tax payment", r"dept of revenue", r"treas tax")),
]

TELLER_CATEGORY_MAP = {
    "accommodation": "Travel",
    "advertising": "Other",
    "bar": "Dining Out",
    "charity": "Giving",
    "clothing": "Shopping",
    "dining": "Dining Out",
    "education": "Education",
    "electronics": "Shopping",
    "entertainment": "Entertainment",
    "fuel": "Gas",
    "general": "Other",
    "groceries": "Groceries",
    "health": "Health",
    "home": "Shopping",
    "income": "Income",
    "insurance": "Other",
    "investment": "Savings",
    "loan": "Other",
    "office": "Shopping",
    "phone": "Phone",
    "service": "Other",
    "shopping": "Shopping",
    "software": "Subscriptions",
    "sport": "Entertainment",
    "tax": "Taxes",
    "transport": "Transportation",
    "transportation": "Transportation",
    "utilities": "Other Utilities",
}

_NOISE = re.compile(r"[^a-z&' ]+")


def merchant_key(merchant, description):
    """Stable-ish key for "always categorize this merchant as X" rules."""
    text = (merchant or description or "").lower()
    text = _NOISE.sub(" ", text)
    words = [w for w in text.split() if len(w) > 1][:4]
    return " ".join(words) or None


def categorize(*, description, merchant, teller_category, teller_type, flow, account_type):
    text = f"{description or ''} {merchant or ''}"
    teller_type = (teller_type or "").lower()
    is_credit = account_type == "credit"

    # Money moving between your own accounts.
    if is_credit and flow > 0 and (teller_type in ("payment", "transfer") or CARD_PAYMENT.search(text)
                                   or re.search(r"\bpayment\b", text, re.I)):
        return "Credit Card Payment"
    if not is_credit and flow < 0 and CARD_PAYMENT.search(text) and not SAVINGS.search(text):
        return "Credit Card Payment"
    if not is_credit and SAVINGS.search(text):
        return "Savings" if flow < 0 else "Transfer"
    if not is_credit and INTERNAL_TRANSFER.search(text) and not P2P.search(text):
        return "Transfer"

    if P2P.search(text):
        return "Person-to-Person"
    if flow > 0 and not is_credit and PAYCHECK.search(text):
        return "Income"

    for category, pattern in KEYWORD_RULES:
        if pattern.search(text):
            return category

    mapped = TELLER_CATEGORY_MAP.get((teller_category or "").lower())
    if mapped == "Income" and flow < 0:
        mapped = None
    if mapped:
        return mapped

    if flow > 0 and not is_credit:
        return "Other Income"
    return "Other"
