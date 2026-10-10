"""Turn a bank transaction into one budget category.

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

# Bump when the rules below change; existing transactions are then re-categorized.
RULES_VERSION = "2026-10-10.4"

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


# Card issuers: money to them from a bank account is a card payment.
CARD_ISSUER = _rx(
    r"cardmember serv", r"credit card", r"card payment", r"crd pmt", r"card pmt", r"crd autopay", r"payment thank you",
    r"applecard", r"apple card", r"gsbank", r"chase card", r"chase credit", r"amex", r"american express",
    r"discover", r"capital one", r"citi card", r"citicard", r"barclaycard", r"bank of america credit",
    r"wells fargo card", r"synchrony", r"bilt",
)
# Generic payment words: a card payment only if nothing more specific (PG&E, rent...) matches.
PAYMENT_WORDS = _rx(r"autopay", r"auto pay", r"epayment", r"e-payment", r"online payment", r"mobile payment")
CARD_PAYMENT = re.compile(f"(?:{CARD_ISSUER.pattern})|(?:{PAYMENT_WORDS.pattern})", re.IGNORECASE)
SAVINGS = _rx(
    r"to sav\w*", r"to savings", r"transfer to share", r"ally bank", r"marcus", r"wealthfront",
    r"betterment", r"robinhood", r"vanguard", r"fidelity", r"schwab", r"acorns", r"stash",
    r"sofi sav\w*", r"high yield", r"m1 finance", r"roth", r"brokerage",
)
INTERNAL_TRANSFER = _rx(
    r"online transfer", r"internal transfer", r"transfer from", r"transfer to", r"xfer", r"from sav\w*",
    r"from chk", r"to chk", r"from checking", r"to checking", r"funds transfer",
    r"overdraft (?:transfer|protection)", r"from share", r"to share",
)
# Wording that suggests money moving between accounts; used to pair the two halves.
TRANSFER_WORDING = _rx(r"\w*transfer\w*", r"xfer", r"payment", r"pymt", r"pmt", r"paymt", r"autopay", r"auto pay",
                       r"overdraft", r"thank you", r"deposit")
P2P = _rx(r"zelle", r"venmo", r"cash app", r"cashapp", r"square cash", r"paypal transfer", r"apple cash")
PAYCHECK = _rx(r"payroll", r"direct dep\w*", r"dir dep", r"salary", r"paycheck", r"adp", r"gusto", r"paychex")

# First match wins, so put specific rules above broad ones.
KEYWORD_RULES = [
    ("Giving", _rx(r"church", r"tithe\w*", r"tithely", r"pushpay", r"planning center", r"ministr\w*", r"donation", r"charit\w*", r"compassion intl", r"world vision", r"gofundme", r"foundation", r"st jude", r"alsac\w*", r"\w*relief\w*", r"chur(?:ch)?", r"faith", r"dntn")),
    ("Pet Insurance", _rx(r"trupanion", r"healthy paws", r"embrace pet", r"fetch pet", r"pets best", r"pet insurance", r"lemonade pet", r"nationwide pet")),
    ("Renter's Insurance", _rx(r"lemonade", r"renters? ins\w*", r"assurant", r"sure renters")),
    ("Car Insurance", _rx(r"geico", r"progressive", r"state farm", r"allstate", r"usaa", r"farmers ins\w*", r"liberty mutual", r"auto insurance")),
    ("Car Payment", _rx(r"auto loan", r"car payment", r"toyota (?:financial|motor credit)", r"honda financial", r"american honda", r"ally auto", r"ally payment", r"ally paymt", r"tesla financ\w*", r"gm financial", r"ford credit", r"hyundai motor finance", r"nissan motor", r"carmax auto", r"capital one auto")),
    ("Rent", _rx(r"rent", r"apartments?", r"property (?:management|mgmt)", r"leasing", r"appfolio", r"rentcafe", r"yardi", r"buildium", r"avalon", r"equity residential", r"essex property", r"domuso", r"bilt rent")),
    ("Electricity", _rx(r"pg&e", r"pge", r"pacific gas", r"edison", r"smud", r"duke energy", r"con ?ed", r"electric\w*", r"power co\w*", r"dominion energy", r"xcel energy", r"georgia power", r"fpl")),
    ("Other Utilities", _rx(r"water (?:dept|district|utility|util|bill|co)", r"city water", r"sewer", r"waste management", r"republic services", r"recology", r"trash", r"utilit\w*", r"gas co", r"socalgas", r"so cal gas", r"nicor", r"spire")),
    ("Internet", _rx(r"comcast", r"xfinity", r"spectrum", r"charter comm\w*", r"cox comm\w*", r"sonic\.net", r"google fiber", r"fios", r"at&t internet", r"att internet", r"starlink", r"frontier comm\w*")),
    ("Phone", _rx(r"t-mobile", r"tmobile", r"verizon", r"mint mobile", r"visible", r"cricket", r"at&t", r"att\*", r"google fi", r"us mobile")),
    ("Subscriptions", _rx(r"google one", r"google \*google", r"google storage", r"netflix", r"spotify", r"hulu", r"disney\s?\+?", r"disneyplus", r"apple\.com/bill", r"itunes", r"youtube", r"prime video", r"amazon prime", r"hbo", r"max\.com", r"paramount", r"peacock", r"audible", r"icloud", r"chatgpt", r"openai", r"anthropic", r"claude\.ai", r"adobe", r"microsoft 365", r"dropbox", r"patreon", r"substack", r"nytimes", r"duolingo", r"siriusxm", r"github")),
    ("Pet Food & Supplies", _rx(r"chewy", r"petco", r"petsmart", r"pet supplies", r"pet food", r"bark ?box", r"farmer'?s dog", r"ollie", r"veterinar\w*", r"vet clinic", r"banfield", r"vca")),
    ("Gas", _rx(r"gas", r"7-eleven", r"shell", r"chevron", r"arco", r"exxon", r"mobil", r"valero", r"circle k", r"speedway", r"76", r"phillips 66", r"costco gas", r"sunoco", r"marathon petro\w*", r"quiktrip", r"qt", r"wawa", r"sheetz", r"bp", r"texaco", r"gas station", r"fuel")),
    ("Groceries", _rx(r"grocer\w*", r"convenience", r"salumeria", r"nugget", r"trader joe'?s?", r"safeway", r"whole foods", r"wholefds", r"costco", r"kroger", r"aldi", r"sprouts", r"h-e-b", r"heb", r"publix", r"raley'?s", r"lucky", r"food ?4 ?less", r"winco", r"vons", r"albertsons", r"ralphs", r"smart ?& ?final", r"grocery", r"market", r"instacart", r"wegmans", r"stop & shop", r"giant", r"meijer", r"fred meyer", r"99 ranch", r"h mart")),
    ("Dining Out", _rx(r"doordash", r"uber ?eats", r"grubhub", r"postmates", r"starbucks", r"mcdonald'?s", r"chipotle", r"taco bell", r"chick.fil.a", r"bakery", r"taqueria", r"pizzeria", r"taco\w*", r"pub", r"cheesecake", r"sea ?foods?", r"bistro", r"diner", r"ramen", r"bagel", r"donut", r"boba", r"chai", r"too good to go", r"in-n-out", r"panera", r"subway", r"domino'?s", r"pizza", r"cafe", r"coffee", r"restaurant", r"grill", r"kitchen", r"bar & grill", r"sushi", r"burger", r"tst\*")),
    ("Transportation", _rx(r"mbta", r"automotive", r"super clean", r"smog", r"oil change", r"uber", r"lyft", r"parking", r"toll", r"fastrak", r"ez ?pass", r"clipper", r"bart", r"caltrain", r"metro", r"transit", r"dmv", r"jiffy lube", r"auto repair", r"car wash", r"tire")),
    ("Travel", _rx(r"priceln\w*", r"priceline", r"holiday inn", r"inn", r"motel", r"resort", r"baggage fee", r"airlines?", r"united air\w*", r"delta", r"southwest", r"alaska air", r"jetblue", r"american air\w*", r"hotel", r"marriott", r"hilton", r"hyatt", r"airbnb", r"vrbo", r"expedia", r"booking\.com")),
    ("Health", _rx(r"hims", r"healthcare", r"goodrx", r"kp rx", r"rx", r"cvs", r"walgreens", r"rite aid", r"pharmacy", r"kaiser", r"dental", r"dentist", r"medical", r"clinic", r"hospital", r"optometr\w*", r"gym", r"fitness", r"planet fitness", r"24 hour fitness")),
    ("Entertainment", _rx(r"\w*tours?", r"museum", r"aquarium", r"zoo", r"amc", r"cinema", r"theat(?:er|re)", r"ticketmaster", r"stubhub", r"steam", r"playstation", r"xbox", r"nintendo", r"bowling", r"concert")),
    ("Shopping", _rx(r"sephora", r"ulta", r"uniqlo", r"h&m", r"american eagle", r"abercrombie", r"galleria", r"arden fair", r"adidas", r"bylt", r"nordstrom", r"macy'?s", r"kohl'?s", r"zara", r"goodwill", r"daiso", r"amazon", r"amzn", r"target", r"walmart", r"best buy", r"home depot", r"lowe'?s", r"ikea", r"etsy", r"ebay", r"tj ?maxx", r"marshalls", r"ross", r"old navy", r"nike", r"apple store")),
    ("Education", _rx(r"academy", r"acdmy", r"parchment", r"univ\w*", r"tuition", r"college", r"coursera", r"udemy")),
    ("Fees & Interest", _rx(r"interest charge", r"finance charge", r"late fee", r"annual fee", r"overdraft fee", r"nsf", r"service fee", r"atm fee", r"foreign transaction fee")),
    ("Taxes", _rx(r"irs", r"franchise tax", r"tax payment", r"dept of revenue", r"treas tax")),
]

_NOISE = re.compile(r"[^a-z&' ]+")


def merchant_key(merchant, description):
    """Stable-ish key for "always categorize this merchant as X" rules."""
    text = (merchant or description or "").lower()
    text = _NOISE.sub(" ", text)
    words = [w for w in text.split() if len(w) > 1][:4]
    return " ".join(words) or None


# Some banks glue "WEB" onto the payee in ACH descriptions ("WEBHealthy Paws").
_WEB_PREFIX = re.compile(r"(?<![A-Za-z])WEB(?=[A-Za-z])")

# A bank's own category (from a CSV export), used only when nothing better matches.
HINTS = [
    (re.compile(r"credit card payment", re.IGNORECASE), "Credit Card Payment"),
    (re.compile(r"transfer", re.IGNORECASE), "Transfer"),
    (re.compile(r"payroll|paycheck|salary", re.IGNORECASE), "Income"),
    (re.compile(r"interest|dividend", re.IGNORECASE), "Other Income"),
    (re.compile(r"rent|mortgage", re.IGNORECASE), "Rent"),
    (re.compile(r"pet", re.IGNORECASE), "Pet Food & Supplies"),
    (re.compile(r"invest", re.IGNORECASE), "Savings"),
    (re.compile(r"tax", re.IGNORECASE), "Taxes"),
    (re.compile(r"grocer", re.IGNORECASE), "Groceries"),
    (re.compile(r"restaurant|dining|fast food|coffee|food & drink", re.IGNORECASE), "Dining Out"),
    (re.compile(r"gas|fuel", re.IGNORECASE), "Gas"),
    (re.compile(r"utilit", re.IGNORECASE), "Other Utilities"),
    (re.compile(r"entertain", re.IGNORECASE), "Entertainment"),
    (re.compile(r"health|medical|pharmacy", re.IGNORECASE), "Health"),
    (re.compile(r"travel|airline|hotel", re.IGNORECASE), "Travel"),
    (re.compile(r"transport", re.IGNORECASE), "Transportation"),
    (re.compile(r"software|subscription|streaming", re.IGNORECASE), "Subscriptions"),
    (re.compile(r"shopping|merchandise", re.IGNORECASE), "Shopping"),
]


def categorize(*, description, merchant, flow, account_type, hint=None):
    category = _categorize(description=description, merchant=merchant, flow=flow, account_type=account_type)
    # Money coming onto a card from your bank is a payment, however it's worded
    # ("ACH DEPOSIT INTERNET TRANSFER FROM ACCOUNT ENDING IN 7022").
    if (account_type == "credit" and flow > 0 and category != "Credit Card Payment"
            and (INTERNAL_TRANSFER.search(description or "") or re.search(r"\bach deposit\b", description or "", re.IGNORECASE)
                 or (hint and re.fullmatch(r"\s*payment\s*", hint, re.IGNORECASE)))):
        return "Credit Card Payment"
    if category in ("Other", "Other Income") and hint:
        for pattern, hinted in HINTS:
            if pattern.search(hint):
                if hinted == "Income" and flow < 0:
                    break
                return hinted
    return category


def _categorize(*, description, merchant, flow, account_type):
    text = _WEB_PREFIX.sub("WEB ", f"{description or ''} {merchant or ''}")
    is_credit = account_type == "credit"

    # Money moving between your own accounts.
    if is_credit and flow > 0 and (CARD_PAYMENT.search(text) or re.search(r"\b(?:payment|pymt|pmt)\b", text, re.IGNORECASE)):
        return "Credit Card Payment"
    if not is_credit and flow < 0 and not SAVINGS.search(text) and (
            CARD_ISSUER.search(text)
            or (PAYMENT_WORDS.search(text) and not any(p.search(text) for _, p in KEYWORD_RULES))):
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

    if flow > 0 and not is_credit:
        return "Other Income"
    return "Other"
