"""Import transactions from a bank's CSV export without creating duplicates.

Duplicates are caught two ways:

1. Every imported row gets a fingerprint id built from the account, date,
   amount, description and how many identical rows came before it in the
   file. Uploading the same file twice, or two exports whose dates overlap,
   produces the same ids, so rows already imported are skipped. Two genuinely
   identical rows in one file (two $5 coffees on the same day) get different
   ids and are both kept.

2. Rows that aren't exact repeats are matched against transactions the account
   already has from any source (SimpleFIN, or a CSV in a different format):
   same amount, dated within 3 days, one-to-one, closest date first. Bank
   exports and SimpleFIN word descriptions differently and date by purchase or
   posting, so description and exact date can't be required.

The SimpleFIN sync applies the same date/amount rule in the other direction,
so a CSV row is taken over (not duplicated) when SimpleFIN later sends it.
"""
import csv
import datetime
import hashlib
import html
import io
import re
from decimal import Decimal, InvalidOperation

from .categorize import categorize, merchant_key

MATCH_DAYS = 3

_DATE_COLUMNS = [r"^trans(action)?\.? ?date$", r"^date$", r"^post(ing|ed)?\.? ?date$", r"^clearing date$", r"date"]
_DESCRIPTION_COLUMNS = [r"^description$", r"^payee$", r"^name$", r"^details$", r"^memo$", r"description", r"^merchant$"]
_MERCHANT_COLUMNS = [r"^merchant$", r"^payee$"]
_AMOUNT_COLUMNS = [r"^amount\b", r"^transaction amount$", r"amount"]
_CREDIT_COLUMNS = [r"^credit", r"^deposit", r"^money in"]
_DEBIT_COLUMNS = [r"^debit", r"^withdrawal", r"^money out"]
_CATEGORY_COLUMNS = [r"^category$"]
_DATE_FORMATS = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"]


class CSVImportError(Exception):
    pass


def _find(header, patterns, used=()):
    for pattern in patterns:
        for i, cell in enumerate(header):
            if i not in used and re.search(pattern, cell):
                return i
    return None


def _amount(text):
    text = (text or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text in ("", "-", "+"):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    return -abs(value) if negative else value


def _date(text):
    text = (text or "").strip()
    if "T" in text[:11]:
        text = text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decode(data):
    if isinstance(data, bytes):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return data.decode("latin-1")
    return data


# A line naming the account a block of rows belongs to, e.g. Golden 1's
# "FREE CHECKING","*******197958" or "SAVINGS","*******7022-0".
_MASKED = re.compile(r"^[*xX#•.]{2,}\s*(\d[\d-]*)$")


def _masked_last_four(row):
    for cell in row:
        m = _MASKED.match(cell.strip())
        if m:
            digits = re.sub(r"-\d{1,2}$", "", m.group(1)).replace("-", "")
            if len(digits) >= 4:
                return digits[-4:]
    return None


def parse(data):
    """Rows from a CSV export of one account: {rows: [{date, description, merchant, amount, hint}], ...}."""
    sections = parse_sections(data)
    if len(sections) > 1:
        names = ", ".join(s["label"] or f"…{s['last_four']}" for s in sections)
        raise CSVImportError(f"This file has {len(sections)} accounts in it ({names}). Choose “Detect automatically” "
                             "so each one goes into its own account.")
    return sections[0]


def parse_sections(data, filename=""):
    """A file can hold several accounts (Golden 1's multi-account export). Returns one parsed
    block per account, each with clues for finding the account: label, last_four, bank."""
    lines = list(csv.reader(io.StringIO(_decode(data))))
    # Split at account-title lines; a file with none is one block.
    blocks, current = [], {"label": None, "last_four": None, "lines": []}
    for row in lines:
        last_four = _masked_last_four(row)
        if last_four and not any(_date(c) for c in row):
            if any(_date(c) for line in current["lines"] for c in line):
                blocks.append(current)
            current = {"label": next((c.strip() for c in row if c.strip() and not _MASKED.match(c.strip())), None),
                       "last_four": last_four, "lines": []}
        else:
            current["lines"].append(row)
    blocks.append(current)

    sections, errors = [], []
    for block in blocks:
        try:
            parsed = _parse_lines(block["lines"])
        except CSVImportError as e:
            errors.append(e)
            continue
        found_in_column = parsed.pop("column_last_four")
        parsed.update(label=block["label"],
                      last_four=block["last_four"] or found_in_column or _filename_last_four(filename))
        sections.append(parsed)
    if not sections:
        raise errors[0] if errors else CSVImportError("No transactions found in that file.")
    return sections


def _filename_last_four(filename):
    # "Chase2827_Activity_20261009.csv": the account's last four, next to the bank's name.
    m = re.search(r"(?i)(?:chase|card|acct|account|x{2,}|ending)[ _-]*(\d{4})(?!\d)", filename or "")
    return m.group(1) if m else None


# Column sets that identify a bank's export, for files that don't name the account.
_FORMATS = [
    ("apple", {"transaction date", "clearing date", "purchased by"}),
    ("discover", {"trans. date", "post date", "description", "amount", "category"}),
    ("chase", {"transaction date", "post date", "description", "category", "type", "amount"}),
    ("capital one", {"account number", "transaction description", "transaction date", "transaction type"}),
    ("capital one", {"card no.", "posted date", "transaction date", "debit", "credit"}),
    ("golden 1", {"date", "account", "account type", "description", "check #", "credit", "debit"}),
]


def _parse_lines(lines):
    # Some banks put a title line or two above the header (Golden 1 does).
    for header_index, row in enumerate(lines[:20]):
        header = [cell.strip().lower() for cell in row]
        date_col = _find(header, _DATE_COLUMNS)
        if date_col is None:
            continue
        amount_col = _find(header, _AMOUNT_COLUMNS, used={date_col})
        credit_col = _find(header, _CREDIT_COLUMNS, used={date_col})
        debit_col = _find(header, _DEBIT_COLUMNS, used={date_col, credit_col})
        if amount_col is not None or (credit_col is not None and debit_col is not None):
            break
    else:
        raise CSVImportError("Couldn't find the column headers. The file needs a date column and either an "
                             "amount column or credit and debit columns.")

    two_columns = amount_col is None
    used = {date_col, amount_col, credit_col, debit_col}
    description_col = _find(header, _DESCRIPTION_COLUMNS, used=used)
    merchant_col = _find(header, _MERCHANT_COLUMNS, used=used | {description_col})
    category_col = _find(header, _CATEGORY_COLUMNS, used=used)
    # Columns that say which account a row is from (Capital One, Golden 1).
    number_col = _find(header, [r"^account number$", r"^card no\.?$"])
    kind_col = _find(header, [r"^account type$"])
    name_col = _find(header, [r"^account$"])
    columns = set(header)
    bank = next((name for name, needed in _FORMATS if needed <= columns), None)

    def cell(row, i):
        # Some banks (Chase) write "&" as "&amp;" in their CSVs.
        return html.unescape(row[i]).strip() if i is not None and i < len(row) else ""

    rows, skipped = [], 0
    numbers, kinds, names = set(), set(), set()
    for row in lines[header_index + 1:]:
        if not any(c.strip() for c in row):
            continue
        date = _date(cell(row, date_col))
        if two_columns:
            credit, debit = _amount(cell(row, credit_col)), _amount(cell(row, debit_col))
            # Debit columns are written positive by some banks and negative by others.
            amount = None if credit is None and debit is None else abs(credit or 0) - abs(debit or 0)
        else:
            amount = _amount(cell(row, amount_col))
        if date is None or amount is None:
            skipped += 1  # totals lines, footers, blank amounts
            continue
        if number_col is not None:
            numbers.add(re.sub(r"\D", "", cell(row, number_col))[-4:])
        kinds.add(cell(row, kind_col).lower())
        names.add(cell(row, name_col))
        rows.append({
            "date": date,
            "description": cell(row, description_col) or cell(row, merchant_col),
            "merchant": cell(row, merchant_col) or None,
            "amount": amount,
            "hint": cell(row, category_col) or None,
        })
    if not rows:
        raise CSVImportError("No transactions found in that file.")
    numbers.discard("")
    kinds.discard("")
    names.discard("")
    return {"rows": rows, "skipped": skipped, "two_columns": two_columns, "bank": bank,
            "column_last_four": next(iter(numbers)) if len(numbers) == 1 else None,
            "account_type": next(iter(kinds)) if len(kinds) == 1 else None,
            "account_name": next(iter(names)) if len(names) == 1 else None}


def identify(conn, section):
    """Which account a parsed block belongs to. Returns (account, candidates, how): account is
    set only when exactly one account fits; otherwise candidates lists the plausible ones."""
    accounts = conn.execute("SELECT id, name, institution, last_four, type, subtype FROM accounts ORDER BY name").fetchall()

    def squash(text):
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    def has_last_four(a, four):
        return a["last_four"] == four or bool(re.search(rf"(?<!\d){four}(?!\d)", a["name"] or ""))

    def narrow(group):
        # Use what the file says the account is (Golden 1: "Checking"/"Savings", "FREE CHECKING").
        kind = section.get("account_type")
        name = squash(section.get("account_name") or section.get("label"))
        by_kind = [a for a in group if kind and a["subtype"] == kind] or group
        by_name = [a for a in by_kind if name and (name in squash(a["name"]) or squash(a["name"]) in name)]
        return by_name or by_kind

    four = section.get("last_four")
    if four:
        found = [a for a in accounts if has_last_four(a, four)]
        if len(found) > 1:
            found = narrow(found)
        if len(found) == 1:
            return found[0], found, f"account ending {four}"
        if found:
            return None, found, f"more than one account ends in {four}"
    bank = section.get("bank")
    if bank:
        word = squash(bank)
        found = [a for a in accounts if word in squash(f"{a['institution']} {a['name']}")]
        if bank in ("apple", "discover", "chase"):
            found = [a for a in found if a["type"] == "credit"] or found
        if len(found) > 1:
            found = narrow(found)
        if len(found) == 1:
            return found[0], found, f"{bank.title()} export"
        if found:
            return None, found, f"more than one {bank.title()} account"
    return None, accounts, "the file doesn't say which account it is"


# How each known card export writes a purchase. Guessing from the file only when unknown:
# a month with more payments and refunds than purchases would otherwise come out backwards.
_PURCHASES_POSITIVE = {"discover": True, "apple": True, "chase": False}


def suggest_flip(parsed, account_type):
    """Card exports often write purchases as positive numbers; the app needs them negative."""
    if parsed["two_columns"] or account_type != "credit":
        return False
    if parsed.get("bank") in _PURCHASES_POSITIVE:
        return _PURCHASES_POSITIVE[parsed["bank"]]
    positive = sum(1 for r in parsed["rows"] if r["amount"] > 0)
    return positive > len(parsed["rows"]) - positive


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def plan(conn, account, parsed, flip):
    """Split parsed rows into new rows and duplicates, without writing anything.

    Also returns the account's existing transactions inside the file's date
    range that no row in the file accounts for, so a statement can be used to
    reconcile what SimpleFIN sent.
    """
    occurrences = {}
    rows = []
    for r in sorted(parsed["rows"], key=lambda r: r["date"]):
        flow = -r["amount"] if flip else r["amount"]
        # The file's own amount (not the possibly flipped one), so re-uploading with the
        # sign toggled still counts as a repeat.
        base = f"{r['date'].isoformat()}|{r['amount']:.2f}|{_normalize(r['description'])}"
        n = occurrences[base] = occurrences.get(base, -1) + 1
        digest = hashlib.sha256(f"{account['id']}|{base}|{n}".encode()).hexdigest()[:32]
        rows.append({**r, "flow": flow, "id": f"csv:{digest}"})

    ids = [r["id"] for r in rows]
    already = {x["id"] for x in conn.execute("SELECT id FROM transactions WHERE id = ANY(%s)", (ids,)).fetchall()}
    new, duplicates = [], []
    for r in rows:
        (duplicates if r["id"] in already else new).append(r)
        if r["id"] in already:
            r["reason"] = "Already uploaded"

    file_start, file_end = rows[0]["date"], rows[-1]["date"]
    start = file_start - datetime.timedelta(days=MATCH_DAYS)
    end = file_end + datetime.timedelta(days=MATCH_DAYS)
    candidates = conn.execute(
        """SELECT id, date, flow, description FROM transactions
           WHERE account_id = %s AND date BETWEEN %s AND %s AND NOT (id = ANY(%s))
           ORDER BY date, id""",
        (account["id"], start, end, ids),
    ).fetchall()
    claimed = set()
    if new:
        matched = {}
        # Exact-date matches first, then 1, 2, 3 days apart, so the closest pairing wins.
        for distance in range(MATCH_DAYS + 1):
            for r in new:
                if r["id"] in matched:
                    continue
                for c in candidates:
                    if (c["id"] not in claimed and Decimal(c["flow"]) == r["flow"]
                            and abs((c["date"] - r["date"]).days) == distance):
                        claimed.add(c["id"])
                        matched[r["id"]] = c
                        break
        still_new = []
        for r in new:
            c = matched.get(r["id"])
            if c:
                r["reason"] = f"Matches {c['date'].isoformat()} {c['description'] or ''}".strip()
                duplicates.append(r)
            else:
                still_new.append(r)
        new = still_new
    not_in_file = [{"date": c["date"], "description": c["description"], "flow": Decimal(c["flow"])}
                   for c in candidates if c["id"] not in claimed and file_start <= c["date"] <= file_end]
    return new, duplicates, not_in_file


def save(conn, account, new_rows, filename):
    """Insert the planned new rows as one undoable import. Returns the import id."""
    import_id = conn.execute(
        "INSERT INTO imports (account_id, filename, row_count) VALUES (%s, %s, %s) RETURNING id",
        (account["id"], (filename or "")[:200], len(new_rows)),
    ).fetchone()["id"]
    for r in new_rows:
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key, raw_amount,
                                         flow, status, auto_category, source, import_id, bank_category)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'posted', %s, 'csv', %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                r["id"], account["id"], r["date"], r["description"], r["merchant"],
                merchant_key(r["merchant"], r["description"]), r["amount"], r["flow"],
                categorize(description=r["description"], merchant=r["merchant"], flow=r["flow"],
                           account_type=account["type"], hint=r["hint"]),
                import_id, r["hint"],
            ),
        )
    return import_id


def matches_elsewhere(conn, account_id, rows):
    """The other account that already holds most of these rows (same amount, within a few
    days), if any - a sign the file was uploaded into the wrong account."""
    if not rows:
        return None
    start = min(r["date"] for r in rows) - datetime.timedelta(days=MATCH_DAYS)
    end = max(r["date"] for r in rows) + datetime.timedelta(days=MATCH_DAYS)
    others = conn.execute(
        """SELECT t.account_id, a.name, t.date, t.flow FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE t.account_id <> %s AND t.date BETWEEN %s AND %s AND NOT t.hidden""",
        (account_id, start, end)).fetchall()
    by_account = {}
    for o in others:
        by_account.setdefault((o["account_id"], o["name"]), {}).setdefault(Decimal(o["flow"]), []).append(o["date"])
    best = None
    for (other_id, name), amounts in by_account.items():
        used = set()
        hits = 0
        for r in rows:
            for i, d in enumerate(amounts.get(Decimal(r["flow"]), [])):
                if (Decimal(r["flow"]), i) not in used and abs((d - r["date"]).days) <= MATCH_DAYS:
                    used.add((Decimal(r["flow"]), i))
                    hits += 1
                    break
        if not best or hits > best["count"]:
            best = {"account_id": other_id, "account": name, "count": hits}
    if best and best["count"] >= max(5, len(rows) // 2):
        return best
    return None
