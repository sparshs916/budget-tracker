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


def parse(data):
    """Rows from a CSV export: [{date, description, merchant, amount, hint}] in file order."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            data = data.decode("latin-1")
    lines = list(csv.reader(io.StringIO(data)))

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

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ""

    rows, skipped = [], 0
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
        rows.append({
            "date": date,
            "description": cell(row, description_col) or cell(row, merchant_col),
            "merchant": cell(row, merchant_col) or None,
            "amount": amount,
            "hint": cell(row, category_col) or None,
        })
    if not rows:
        raise CSVImportError("No transactions found in that file.")
    return {"rows": rows, "skipped": skipped, "two_columns": two_columns}


def suggest_flip(parsed, account_type):
    """Card exports often write purchases as positive numbers; the app needs them negative."""
    if parsed["two_columns"] or account_type != "credit":
        return False
    positive = sum(1 for r in parsed["rows"] if r["amount"] > 0)
    return positive > len(parsed["rows"]) - positive


def _normalize(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def plan(conn, account, parsed, flip):
    """Split parsed rows into new rows and duplicates, without writing anything."""
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

    if new:
        start = min(r["date"] for r in new) - datetime.timedelta(days=MATCH_DAYS)
        end = max(r["date"] for r in new) + datetime.timedelta(days=MATCH_DAYS)
        candidates = conn.execute(
            """SELECT id, date, flow, description FROM transactions
               WHERE account_id = %s AND date BETWEEN %s AND %s AND NOT (id = ANY(%s))
               ORDER BY date, id""",
            (account["id"], start, end, ids),
        ).fetchall()
        claimed = set()
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
    return new, duplicates


def save(conn, account, new_rows, filename):
    """Insert the planned new rows as one undoable import. Returns the import id."""
    import_id = conn.execute(
        "INSERT INTO imports (account_id, filename, row_count) VALUES (%s, %s, %s) RETURNING id",
        (account["id"], (filename or "")[:200], len(new_rows)),
    ).fetchone()["id"]
    for r in new_rows:
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key, raw_amount,
                                         flow, status, auto_category, source, import_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'posted', %s, 'csv', %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                r["id"], account["id"], r["date"], r["description"], r["merchant"],
                merchant_key(r["merchant"], r["description"]), r["amount"], r["flow"],
                categorize(description=r["description"], merchant=r["merchant"], flow=r["flow"],
                           account_type=account["type"], hint=r["hint"]),
                import_id,
            ),
        )
    return import_id
