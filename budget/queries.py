import datetime
import re
from decimal import Decimal

from .categorize import CATEGORIES, CATEGORY_KIND

# Your choice for the transaction, then your rule for the merchant, then the
# keyword rules - with Claude's suggestion filling in only where those said Other.
AI_CATEGORY = ("CASE WHEN t.auto_category IN ('Other', 'Other Income') AND ai.category NOT IN ('Other', 'Other Income') "
               "AND NOT (ai.category = 'Income' AND t.flow < 0) THEN ai.category END")
# A matched move between your own accounts (link_transfers) outranks the wording rules.
EFFECTIVE_CATEGORY = f"COALESCE(t.override_category, r.category, t.pair_category, {AI_CATEGORY}, t.auto_category)"

BASE_FROM = """
FROM transactions t
JOIN accounts a ON a.id = t.account_id
LEFT JOIN merchant_rules r ON r.merchant_key = t.merchant_key
LEFT JOIN ai_categories ai ON ai.merchant_key = t.merchant_key
                          AND ai.direction = CASE WHEN t.flow < 0 THEN 'out' ELSE 'in' END
"""


def _where(filters, include_category=True, include_hidden=False):
    clauses = ["t.date BETWEEN %(start)s AND %(end)s"]
    if not include_hidden:
        clauses.append("NOT t.hidden")  # rows you said aren't real never count
    params = {"start": filters["start"], "end": filters["end"]}

    account = filters.get("account") or "budget"
    if account == "budget":
        clauses.append("a.include_in_budget")
    elif account == "all":
        pass
    elif account.startswith("type:"):
        kind = account[5:]
        if kind == "credit":
            clauses.append("a.type = 'credit'")
        else:
            clauses.append("a.subtype = %(subtype)s")
            params["subtype"] = kind
    else:
        clauses.append("a.id = %(account_id)s")
        params["account_id"] = account

    if include_category and filters.get("category"):
        clauses.append(f"{EFFECTIVE_CATEGORY} = %(category)s")
        params["category"] = filters["category"]
    if filters.get("q"):
        for i, (sql, values) in enumerate(search_terms(filters["q"])):
            names = {k: f"q{i}_{k}" for k in values}
            clauses.append(sql.format(**{k: f"%({v})s" for k, v in names.items()}))
            params.update({names[k]: v for k, v in values.items()})
    return " AND ".join(clauses), params


# Everything a search word is compared with.
_SEARCH_TEXT = ("t.description", "t.merchant", "t.note", "t.bank_category", "t.status", "t.source", "a.name",
                "a.institution", "a.last_four", EFFECTIVE_CATEGORY)
_TERM = re.compile(r'"([^"]*)"|(\S+)')
_AMOUNT = re.compile(r"^\$?(\d{1,3}(?:,\d{3})*|\d+)(\.\d{1,2})?$")
_COMPARE = re.compile(r"^(>=|<=|>|<)\$?(\d+(?:\.\d{1,2})?)$")
_RANGE = re.compile(r"^\$?(\d+(?:\.\d{1,2})?)(?:\.\.|-)\$?(\d+(?:\.\d{1,2})?)$")
_YMD = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")
_MD = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?$")


def _like(text):
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_terms(q):
    """Split a search into terms that must all match. Each term is text (any field,
    including category and account), an amount ("45.10", "$45", ">100", "<=20",
    "50..200"), or a date ("2026-08", "2026-08-14", "8/14"). Quote phrases: "whole foods"."""
    terms = []
    text_match = "(" + " OR ".join(f"{col} ILIKE {{text}}" for col in _SEARCH_TEXT) + ")"
    for quoted, word in _TERM.findall(q[:200]):
        term = (quoted if quoted else word).strip()
        if not term:
            continue
        values = {"text": _like(term)}
        if m := _COMPARE.match(term):
            terms.append((f"abs(t.flow) {m.group(1)} {{amount}}", {"amount": Decimal(m.group(2))}))
            continue
        if not quoted and (m := _RANGE.match(term)) and not _YMD.match(term):
            low, high = sorted((Decimal(m.group(1)), Decimal(m.group(2))))
            terms.append(("abs(t.flow) BETWEEN {low} AND {high}", {"low": low, "high": high}))
            continue
        extra = []
        if not quoted and (m := _AMOUNT.match(term.lstrip("+-"))):
            amount = Decimal(m.group(1).replace(",", "") + (m.group(2) or ""))
            if m.group(2):  # 45.10: that exact amount
                extra.append("abs(t.flow) = {amount}")
            else:  # 45: anything from $45.00 to $45.99
                extra.append("(abs(t.flow) >= {amount} AND abs(t.flow) < {amount} + 1)")
            values["amount"] = amount
        elif not quoted and (m := _YMD.match(term)):
            year, month, day = int(m.group(1)), int(m.group(2)), m.group(3)
            if 1 <= month <= 12 and (not day or 1 <= int(day) <= 31):
                extra.append("(EXTRACT(YEAR FROM t.date) = {y} AND EXTRACT(MONTH FROM t.date) = {m}"
                             + (" AND EXTRACT(DAY FROM t.date) = {d})" if day else ")"))
                values.update(y=year, m=month, **({"d": int(day)} if day else {}))
        elif not quoted and (m := _MD.match(term)):
            month, day, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if 1 <= month <= 12 and 1 <= day <= 31:
                sql = "(EXTRACT(MONTH FROM t.date) = {m} AND EXTRACT(DAY FROM t.date) = {d}"
                values.update(m=month, d=day)
                if year:
                    sql += " AND EXTRACT(YEAR FROM t.date) = {y}"
                    values["y"] = int(year) + (2000 if len(year) == 2 else 0)
                extra.append(sql + ")")
        terms.append(("(" + " OR ".join([text_match, *extra]) + ")", values))
    return terms


def summary(conn, filters):
    where, params = _where(filters, include_category=False)
    rows = conn.execute(
        f"""
        SELECT {EFFECTIVE_CATEGORY} AS category, SUM(t.flow) AS flow, COUNT(*) AS count
        {BASE_FROM}
        WHERE {where}
        GROUP BY 1
        """,
        params,
    ).fetchall()
    by_category = {r["category"]: r for r in rows}

    income, expenses, excluded = [], [], []
    for name, kind in CATEGORIES:
        r = by_category.get(name)
        if not r:
            continue
        flow = Decimal(r["flow"])
        if kind == "income":
            income.append({"category": name, "amount": flow, "count": r["count"]})
        elif kind == "excluded":
            excluded.append({"category": name, "amount": -flow, "count": r["count"]})
        else:
            # A refund (+) inside an expense category reduces that category.
            expenses.append({"category": name, "kind": kind, "amount": -flow, "count": r["count"]})

    total_income = sum((r["amount"] for r in income), Decimal(0))
    total_expenses = sum((r["amount"] for r in expenses), Decimal(0))
    savings = sum((r["amount"] for r in expenses if r["kind"] == "savings"), Decimal(0))
    return {
        "income": income,
        "expenses": expenses,
        "excluded": excluded,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "total_spending": total_expenses - savings,
        "total_savings": savings,
        "remaining": total_income - total_expenses,
        **unmatched_transfers(conn, filters),
    }


PAIR_DAYS = 3


def unmatched_transfers(conn, filters):
    """Transfers and card payments whose other half isn't in the accounts being viewed.

    They're left out of the totals on the assumption that both ends are tracked:
    savings -> checking nets to zero, a card payment's purchases are on the card.
    If one end isn't tracked, money left (or arrived) without being counted, e.g.
    savings -> an untracked checking account -> Zelle to a friend.
    """
    padded = {**filters, "start": filters["start"] - datetime.timedelta(days=PAIR_DAYS),
              "end": filters["end"] + datetime.timedelta(days=PAIR_DAYS), "q": None}
    where, params = _where(padded, include_category=False)
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.flow, t.description, t.merchant, a.name AS account_name, a.id AS account_id,
               t.transfer_reviewed, {EFFECTIVE_CATEGORY} AS category
        {BASE_FROM}
        WHERE {where} AND {EFFECTIVE_CATEGORY} IN ('Credit Card Payment', 'Transfer', 'Savings')
        ORDER BY t.date, t.id
        """,
        params,
    ).fetchall()
    # Pair each money-out with a money-in of the same amount in a different account,
    # closest date first.
    paired = set()
    for distance in range(PAIR_DAYS + 1):
        for out in rows:
            if out["id"] in paired or out["flow"] >= 0:
                continue
            for inn in rows:
                if (inn["id"] not in paired and inn["flow"] == -out["flow"] and inn["account_id"] != out["account_id"]
                        and abs((inn["date"] - out["date"]).days) == distance):
                    paired.update((out["id"], inn["id"]))
                    break
    unmatched = [
        {"id": r["id"], "date": r["date"], "description": r["merchant"] or r["description"], "account": r["account_name"],
         "category": r["category"], "flow": r["flow"]}
        for r in rows if r["id"] not in paired and filters["start"] <= r["date"] <= filters["end"]
        # Savings is already counted as money out; it's only here to pair with its other half.
        and r["category"] != "Savings"
        # You said this one is fine (its other half is in an account you don't track).
        and not r["transfer_reviewed"]
    ]
    return {"unmatched_transfers": unmatched,
            "unmatched_total": sum((Decimal(r["flow"]) for r in unmatched), Decimal(0))}


def transactions(conn, filters, page, page_size):
    # Hidden rows are listed (greyed, so you can undo) but left out of the net.
    where, params = _where(filters, include_hidden=True)
    total = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(t.flow) FILTER (WHERE NOT t.hidden), 0) AS net "
                         f"{BASE_FROM} WHERE {where}", params).fetchone()
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.description, t.merchant, t.merchant_key, t.flow, t.raw_amount,
               t.status, t.type, t.source, {EFFECTIVE_CATEGORY} AS category,
               t.override_category IS NOT NULL AS overridden,
               (t.override_category IS NULL AND r.category IS NULL AND t.pair_category IS NULL
                AND {AI_CATEGORY} IS NOT NULL) AS ai_suggested,
               t.paired_with IS NOT NULL AS paired, t.note, t.hidden,
               a.name AS account_name, a.last_four, a.institution, a.type AS account_type
        {BASE_FROM}
        WHERE {where}
        ORDER BY t.date DESC, t.id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params,
    ).fetchall()
    for r in rows:
        r["kind"] = CATEGORY_KIND.get(r["category"], "expense")
    return {"total": total["n"], "net": total["net"], "rows": rows}


def accounts(conn):
    return conn.execute(
        """
        SELECT a.id, a.name, a.type, a.subtype, a.last_four, a.institution, a.include_in_budget,
               a.ledger_balance, a.available_balance, a.status, a.balance_date,
               e.id AS enrollment_id, COALESCE(e.status, 'manual') AS enrollment_status, e.last_error, e.last_synced_at,
               (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id) AS transaction_count,
               (SELECT MAX(t.date) FROM transactions t WHERE t.account_id = a.id) AS latest_transaction
        FROM accounts a LEFT JOIN enrollments e ON e.id = a.enrollment_id
        ORDER BY a.institution, a.type, a.name
        """
    ).fetchall()


def enrollments(conn):
    return conn.execute(
        "SELECT id, institution, status, last_error, last_synced_at FROM enrollments ORDER BY created_at"
    ).fetchall()


def imports(conn):
    return conn.execute(
        """
        SELECT i.id, i.account_id, i.filename, i.row_count, i.created_at, a.name AS account_name
        FROM imports i JOIN accounts a ON a.id = i.account_id
        ORDER BY i.created_at DESC
        LIMIT 20
        """
    ).fetchall()


# ---------- display-only views (they read the budget, never change it) ----------

def targets(conn):
    return {r["category"]: r["amount"] for r in conn.execute("SELECT category, amount FROM budget_targets").fetchall()}


def set_targets(conn, values):
    """values: {category: amount or None}. None or 0 removes the target."""
    for category, amount in values.items():
        if amount is None or Decimal(amount) == 0:
            conn.execute("DELETE FROM budget_targets WHERE category = %s", (category,))
        else:
            conn.execute(
                """INSERT INTO budget_targets (category, amount) VALUES (%s, %s)
                   ON CONFLICT (category) DO UPDATE SET amount = EXCLUDED.amount, updated_at = now()""",
                (category, Decimal(amount)))


def month_start(d, back=0):
    index = d.year * 12 + d.month - 1 - back
    return datetime.date(index // 12, index % 12 + 1, 1)


def month_end(d):
    return month_start(d, -1) - datetime.timedelta(days=1)


def trends(conn, end, months, account="budget"):
    """One summary() per calendar month, oldest first, so every number matches
    what the budget table shows for that month."""
    out = []
    for back in range(months - 1, -1, -1):
        start = month_start(end, back)
        last = min(month_end(start), end)
        s = summary(conn, {"start": start, "end": last, "account": account})
        out.append({
            "month": start.strftime("%Y-%m"), "start": start, "end": last, "partial": last < month_end(start),
            "total_income": s["total_income"], "total_expenses": s["total_expenses"],
            "total_spending": s["total_spending"], "total_savings": s["total_savings"], "remaining": s["remaining"],
            "categories": {r["category"]: r["amount"] for r in s["income"] + s["expenses"]},
        })
    return out


def _anchor_days(dates):
    """Days of the month a twice-a-month schedule lands on (e.g. the 12th and 27th), or None."""
    days = sorted(d.day for d in dates)
    groups = [[days[0]]]
    for d in days[1:]:
        if d - groups[-1][-1] > 5:
            groups.append([])
        groups[-1].append(d)
    if len(groups) != 2 or min(len(g) for g in groups) < 2:
        return None
    return [sorted(g)[len(g) // 2] for g in groups]


def _next_anchor(after, anchors):
    for back in (0, -1, -2):
        month = month_start(after, back)
        for day in anchors:
            candidate = month.replace(day=min(day, month_end(month).day))
            if candidate > after:
                return candidate
    return after + datetime.timedelta(days=15)


def find_schedules(rows, today):
    """rows: dicts with date, amount (positive), merchant_key, name, category - one direction only.
    Returns merchants that repeat on a schedule with a steady amount."""
    groups = {}
    for r in rows:
        groups.setdefault(r["merchant_key"], []).append(r)

    found = []
    for charges in groups.values():
        # Several charges on one day (e.g. two Amazon orders) count as one visit.
        by_day = {}
        for c in charges:
            by_day.setdefault(c["date"], []).append(Decimal(c["amount"]))
        dates = sorted(by_day)
        if len(dates) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        gap = sorted(gaps)[len(gaps) // 2]
        anchors = None
        if 25 <= gap <= 35 and len(dates) >= 3:
            frequency, per_year = "monthly", 12
            # Monthly bills keep their day of the month (the 25th), rather than drifting by 30-31 days.
            days_of_month = sorted(d.day for d in dates)
            anchors = [days_of_month[len(days_of_month) // 2]]
        elif 6 <= gap <= 8 and len(dates) >= 4:
            frequency, per_year = "weekly", 52
        elif 12 <= gap <= 17 and len(dates) >= 3:
            anchors = _anchor_days(dates)
            frequency, per_year = ("twice a month", 24) if anchors else ("every 2 weeks", 26)
        elif 85 <= gap <= 95 and len(dates) >= 2:
            frequency, per_year = "quarterly", 4
        elif 350 <= gap <= 380:
            frequency, per_year = "yearly", 1
        else:
            continue
        # Regular timing alone isn't enough: most gaps must fit the schedule...
        if sum(1 for g in gaps if abs(g - gap) <= max(3, gap * 0.2)) < len(gaps) * 0.6:
            continue
        amounts = [sum(by_day[d]) for d in dates]
        typical = sorted(amounts)[len(amounts) // 2]
        # ...and amounts must be steady (a bill can vary; groceries vary far more).
        if sum(1 for a in amounts if abs(a - typical) <= max(Decimal(2), typical * Decimal("0.35"))) < len(amounts) * 0.6:
            continue
        # A schedule that stopped two cycles ago is probably cancelled.
        if (today - dates[-1]).days > gap * 2 + 5:
            continue
        last = amounts[-1]
        previous = sorted(amounts[:-1])[(len(amounts) - 1) // 2]
        changed = abs(last - previous) >= max(Decimal("0.50"), previous * Decimal("0.03"))
        found.append({
            "name": charges[-1]["name"], "category": charges[-1]["category"], "merchant_key": charges[-1]["merchant_key"],
            "frequency": frequency, "interval_days": gap, "anchor_days": anchors,
            "typical": typical, "last_amount": last, "last_date": dates[-1], "count": len(dates),
            "next_date": _next_anchor(dates[-1], anchors) if anchors else dates[-1] + datetime.timedelta(days=gap),
            "monthly_cost": (typical * per_year / 12).quantize(Decimal("0.01")),
            "yearly_cost": (typical * per_year).quantize(Decimal("0.01")),
            "price_change": (last - previous) if changed else None,
        })
    found.sort(key=lambda r: -r["monthly_cost"])
    return found


def _schedule_rows(conn, today, account, direction):
    where, params = _where({"start": month_start(today, 13), "end": today, "account": account}, include_category=False)
    sign = "<" if direction == "out" else ">"
    rows = conn.execute(
        f"""
        SELECT t.date, abs(t.flow) AS amount, t.merchant_key, COALESCE(t.merchant, t.description) AS name,
               {EFFECTIVE_CATEGORY} AS category
        {BASE_FROM}
        WHERE {where} AND t.flow {sign} 0 AND t.merchant_key IS NOT NULL AND t.status IS DISTINCT FROM 'pending'
        ORDER BY t.date
        """,
        params,
    ).fetchall()
    return rows


def recurring(conn, today, account="budget"):
    """Charges that repeat on a schedule (subscriptions, bills, rent), found from
    the last 13 months of money out. Counted spending is untouched; this only reads it."""
    rows = [r for r in _schedule_rows(conn, today, account, "out")
            if CATEGORY_KIND.get(r["category"]) not in ("excluded", "savings")]  # moves, not costs
    return find_schedules(rows, today)


# ---------- settings kept in app_meta ----------

def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_meta WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM app_meta WHERE key = %s", (key,))
    else:
        conn.execute("""INSERT INTO app_meta (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, str(value)))


# ---------- cash-flow forecast ----------

def current_balance(account, conn, today):
    """Today's balance: SimpleFIN's, or for a hand-added account the balance you typed
    plus everything recorded after that day."""
    if account["ledger_balance"] is None:
        return None
    balance = Decimal(account["ledger_balance"])
    if account["balance_date"]:
        later = conn.execute("""SELECT COALESCE(SUM(flow), 0) AS s FROM transactions
                                WHERE account_id = %s AND date > %s AND date <= %s AND NOT hidden""",
                             (account["id"], account["balance_date"], today)).fetchone()["s"]
        balance += Decimal(later)
    return balance


def forecast(conn, today, days=35):
    """Where the cash in your budget bank accounts (checking + savings) is heading.

    Money moving between those accounts cancels out, so they're treated as one pool.
    Coming in: paychecks and other income on a schedule. Going out: bills on a schedule,
    card payments (your recent monthly average, on the day you usually pay), and
    everyday spending from these accounts (the recent daily average). An estimate, not a promise.
    """
    pool = conn.execute("""SELECT id, name, institution, last_four, subtype, ledger_balance, balance_date,
                                  enrollment_id IS NULL AS manual
                           FROM accounts WHERE type = 'depository' AND include_in_budget
                           ORDER BY name""").fetchall()
    with_balance, missing = [], []
    for a in pool:
        balance = current_balance(a, conn, today)
        (missing if balance is None else with_balance).append({**a, "balance": balance})
    start_balance = sum((a["balance"] for a in with_balance), Decimal(0))
    ids = [a["id"] for a in with_balance]
    result = {"accounts": with_balance, "missing_balance": missing, "start_balance": start_balance,
              "threshold": Decimal(get_setting(conn, "low_balance_alert", "100")), "days": days,
              "events": [], "series": [], "low": None, "daily_spending": Decimal(0)}
    if not ids:
        return result

    def schedule_rows(direction):
        rows = []
        for account_id in ids:
            rows += _schedule_rows(conn, today, account_id, direction)
        return rows

    incoming = [r for r in schedule_rows("in") if CATEGORY_KIND.get(r["category"]) == "income"]
    outgoing = [r for r in schedule_rows("out") if CATEGORY_KIND.get(r["category"]) in ("expense", "savings")]
    # Savings counts here when it leaves the pool (e.g. into a savings account kept out of the budget).
    pay = find_schedules(incoming, today)
    bills = find_schedules(outgoing, today)
    horizon = today + datetime.timedelta(days=days)

    events = []

    def repeat(item, sign, kind):
        when = item["next_date"]
        # A due date that already passed without the charge: assume it's coming soon.
        if when <= today:
            when = today + datetime.timedelta(days=1)
        while when <= horizon:
            events.append({"date": when, "name": item["name"], "amount": sign * item["typical"], "kind": kind,
                           "frequency": item["frequency"]})
            when = (_next_anchor(when, item["anchor_days"]) if item["anchor_days"]
                    else when + datetime.timedelta(days=item["interval_days"]))

    for item in pay:
        repeat(item, 1, "income")
    for item in bills:
        repeat(item, -1, "bill")

    # Card payments: what you've paid toward cards from these accounts per month lately.
    since = today - datetime.timedelta(days=90)
    payments = conn.execute(f"""
        SELECT t.date, -t.flow AS amount {BASE_FROM}
        WHERE t.account_id = ANY(%(ids)s) AND t.date > %(since)s AND t.date <= %(today)s AND t.flow < 0
          AND NOT t.hidden AND {EFFECTIVE_CATEGORY} = 'Credit Card Payment'""",
        {"ids": ids, "since": since, "today": today}).fetchall()
    if payments:
        monthly = (sum(Decimal(p["amount"]) for p in payments) / 3).quantize(Decimal("0.01"))
        days_paid = sorted(p["date"].day for p in payments)
        pay_day = days_paid[len(days_paid) // 2]
        month = month_start(today)
        for back in (0, -1, -2):
            m = month_start(month, back)
            when = m.replace(day=min(pay_day, month_end(m).day))
            if today < when <= horizon:
                events.append({"date": when, "name": "Credit card payments (recent monthly average)",
                               "amount": -monthly, "kind": "cards", "frequency": "monthly"})

    # Everyday spending straight from these accounts (Zelle, Venmo, debit card), minus what's scheduled.
    scheduled = {b["merchant_key"] for b in bills}
    rows = conn.execute(f"""
        SELECT t.merchant_key, -t.flow AS amount, {EFFECTIVE_CATEGORY} AS category {BASE_FROM}
        WHERE t.account_id = ANY(%(ids)s) AND t.date > %(since)s AND t.date <= %(today)s AND t.flow < 0
          AND NOT t.hidden AND t.status IS DISTINCT FROM 'pending'""",
        {"ids": ids, "since": since, "today": today}).fetchall()
    everyday = sum((Decimal(r["amount"]) for r in rows
                    if CATEGORY_KIND.get(r["category"]) == "expense" and r["merchant_key"] not in scheduled), Decimal(0))
    daily = (everyday / 90).quantize(Decimal("0.01"))
    result["daily_spending"] = daily

    events.sort(key=lambda e: (e["date"], e["amount"]))
    balance, series, low = start_balance, [], None
    by_day = {}
    for e in events:
        by_day.setdefault(e["date"], []).append(e)
    for offset in range(days + 1):
        day = today + datetime.timedelta(days=offset)
        if offset:
            balance -= daily
            balance += sum((e["amount"] for e in by_day.get(day, [])), Decimal(0))
        point = {"date": day, "balance": balance.quantize(Decimal("0.01"))}
        series.append(point)
        if low is None or point["balance"] < low["balance"]:
            low = point
    result.update(events=events, series=series, low=low, end_balance=series[-1]["balance"])
    return result


# ---------- checkup: is the data complete? ----------

def health(conn, today):
    """Things that make the budget's numbers wrong without looking wrong."""
    from .categorize import CARD_ISSUER

    issues = []
    accounts = conn.execute("""
        SELECT a.id, a.name, a.institution, a.last_four, a.type, a.subtype, a.include_in_budget,
               a.enrollment_id, e.status AS connection_status, e.last_error,
               (SELECT MAX(date) FROM transactions t WHERE t.account_id = a.id AND NOT t.hidden) AS latest,
               (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id
                 AND t.date > %(today)s::date - 120 AND NOT t.hidden) AS recent
        FROM accounts a LEFT JOIN enrollments e ON e.id = a.enrollment_id ORDER BY a.name""", {"today": today}).fetchall()

    def label(a):
        bank = "" if a["institution"] == "Manual" else a["institution"] or ""
        return f"{bank} {a['name']}{' ·' + a['last_four'] if a['last_four'] else ''}".strip()

    def add(severity, kind, title, detail, account=None, action=None, rows=None):
        issues.append({"severity": severity, "kind": kind, "title": title, "detail": detail,
                       "account_id": account["id"] if account else None, "action": action, "rows": rows or []})

    # Bank connections that stopped working.
    for e in conn.execute("SELECT institution, status, last_error FROM enrollments WHERE status <> 'ok'").fetchall():
        add("high", "connection", f"{e['institution'] or 'A bank'} connection needs attention",
            e["last_error"] or "SimpleFIN couldn't sync it. Open SimpleFIN Bridge to fix the login.", action="simplefin")

    # A card you pay from checking that isn't in the app at all: its purchases are never counted.
    # Compare squashed names so "APPLECARD" matches an account called "Apple Card".
    # Any account at that bank counts: "CAPITAL ONE" may be a savings account you have there.
    tracked = [re.sub(r"[^a-z]", "", f"{a['institution'] or ''}{a['name'] or ''}".lower()) for a in accounts]
    generic = ("payment", "thankyou", "credit", "card", "crd", "autopay", "pmt", "bank", "serv", "member")
    untracked = {}
    for r in conn.execute(f"""
            SELECT t.date, t.flow, t.description, t.merchant {BASE_FROM}
            WHERE a.type = 'depository' AND t.flow < 0 AND t.date > %(since)s AND t.paired_with IS NULL AND NOT t.hidden
              AND {EFFECTIVE_CATEGORY} = 'Credit Card Payment'
            ORDER BY t.date DESC""", {"since": today - datetime.timedelta(days=120)}).fetchall():
        m = CARD_ISSUER.search(f"{r['description'] or ''} {r['merchant'] or ''}")
        if not m:
            continue
        issuer = m.group(0).lower()
        key = re.sub(r"[^a-z]", "", issuer)
        for word in generic:
            key = key.replace(word, "")
        if len(key) >= 4 and not any(key in t for t in tracked):
            name = " ".join(w.capitalize() for w in re.findall(r"[a-z]+", issuer) if w not in ("card", "credit", "crd"))
            untracked.setdefault(name or key.capitalize(), []).append(r)
    for name, rows in untracked.items():
        total = sum(-Decimal(r["flow"]) for r in rows)
        add("high", "untracked_card", f"You pay a {name} card that isn't in the app",
            f"{len(rows)} payment{'s' if len(rows) != 1 else ''} (${total:,.2f}) in the last 4 months went to {name}, "
            f"but no {name} account is here, so what you bought on it isn't counted as spending. "
            "Add it (Accounts → + Add account by hand) and upload its CSV, or connect it in SimpleFIN.", action="add_account",
            rows=[{"date": r["date"], "description": r["merchant"] or r["description"], "flow": r["flow"]} for r in rows[:6]])

    for a in accounts:
        # Spending on an account left out of the budget.
        if not a["include_in_budget"] and a["type"] == "credit":
            spent = conn.execute("""SELECT COALESCE(-SUM(flow), 0) AS s, COUNT(*) AS n FROM transactions
                                    WHERE account_id = %s AND flow < 0 AND date > %s AND NOT hidden""",
                                 (a["id"], today - datetime.timedelta(days=60))).fetchone()
            if spent["n"]:
                add("high", "unticked_card", f"{label(a)} is left out of the budget",
                    f"It has {spent['n']} purchases (${Decimal(spent['s']):,.2f}) in the last 60 days that aren't counted. "
                    "Tick it under Accounts unless you really mean to leave it out.", a, action="tick")
        # Stopped getting new transactions.
        if a["latest"]:
            quiet = (today - a["latest"]).days
            if a["enrollment_id"] and quiet > 14 and a["recent"] >= 4:
                add("medium", "stale", f"No new transactions from {label(a)} for {quiet} days",
                    f"The newest is from {a['latest']}. SimpleFIN may have stopped sending this account; "
                    "press Check on it under Accounts, or upload a CSV to fill the gap.", a, action="check")
            elif not a["enrollment_id"] and quiet > 30:
                add("medium", "stale_csv", f"{label(a)} hasn't been updated for {quiet} days",
                    f"It's filled from CSV files and the newest row is from {a['latest']}. Download a fresh CSV from "
                    "the bank and upload it (duplicates are skipped).", a, action="upload")
        elif a["enrollment_id"] is None:
            add("low", "empty", f"{label(a)} has no transactions", "Upload a CSV from the bank to fill it.", a, action="upload")

        # A hole in an otherwise busy account, e.g. SimpleFIN skipping a week.
        dates = [r["date"] for r in conn.execute(
            """SELECT DISTINCT date FROM transactions WHERE account_id = %s AND date > %s AND NOT hidden ORDER BY date""",
            (a["id"], today - datetime.timedelta(days=120))).fetchall()]
        if len(dates) >= 15:
            gaps = sorted(((b - a_).days, a_, b) for a_, b in zip(dates, dates[1:]))
            # The average, not the median: a card used in bursts (a trip) isn't "busy".
            typical = max(1, round((dates[-1] - dates[0]).days / (len(dates) - 1)))
            days, before, after = gaps[-1]
            if days >= max(9, typical * 5):
                add("medium", "gap", f"{label(a)} has no transactions from {before} to {after}",
                    f"That's {days - 1} days with nothing on an account that usually has activity every "
                    f"{typical} day{'s' if typical != 1 else ''}. If you used it then, upload a CSV covering those dates.",
                    a, action="upload")

    # Pending rows that never cleared - often a charge that posted under a different amount, or one that was dropped.
    stale = conn.execute(f"""
        SELECT t.id, t.date, t.flow, COALESCE(t.merchant, t.description) AS description, a.name AS account {BASE_FROM}
        WHERE t.status = 'pending' AND t.date < %(cutoff)s AND NOT t.hidden ORDER BY t.date""",
        {"cutoff": today - datetime.timedelta(days=10)}).fetchall()
    if stale:
        add("medium", "old_pending", f"{len(stale)} pending transaction{'s' if len(stale) != 1 else ''} never cleared",
            "Pending for over 10 days usually means it posted under another amount or was cancelled. If the bank "
            "statement doesn't have it, hide it so it isn't counted.", action="review_rows",
            rows=[{"id": r["id"], "date": r["date"], "description": r["description"], "flow": r["flow"],
                   "account": r["account"]} for r in stale[:20]])

    # Same account, same amount, same merchant, a day or two apart, one from a CSV and one from SimpleFIN.
    dupes = conn.execute("""
        SELECT x.id, x.date, x.flow, COALESCE(x.merchant, x.description) AS description, a.name AS account,
               y.id AS other_id, y.date AS other_date
        FROM transactions x JOIN transactions y
          ON y.account_id = x.account_id AND y.flow = x.flow AND y.id > x.id AND y.merchant_key = x.merchant_key
         AND abs(y.date - x.date) <= 3 AND y.source <> x.source
        JOIN accounts a ON a.id = x.account_id
        WHERE NOT x.hidden AND NOT y.hidden AND x.date > %s
        ORDER BY x.date DESC LIMIT 20""", (today - datetime.timedelta(days=180),)).fetchall()
    if dupes:
        add("medium", "duplicates", f"{len(dupes)} possible duplicate{'s' if len(dupes) != 1 else ''}",
            "The same charge appears from both a CSV upload and SimpleFIN. If it happened once, hide one copy.",
            action="review_rows",
            rows=[{"id": r["other_id"], "date": r["other_date"], "description": r["description"], "flow": r["flow"],
                   "account": r["account"], "twin_date": r["date"]} for r in dupes])

    # Transfers with no other half (the warning on the Budget page, across all months).
    lost = unmatched_transfers(conn, {"start": today - datetime.timedelta(days=120), "end": today, "account": "budget"})
    if lost["unmatched_transfers"]:
        n = len(lost["unmatched_transfers"])
        add("medium", "unmatched", f"{n} transfer{'s' if n != 1 else ''} in the last 4 months with no other half",
            "Each one is left out of the totals. Say what each was so nothing is missed.", action="review_rows",
            rows=[{"id": r["id"], "date": r["date"], "description": r["description"], "flow": r["flow"],
                   "account": r["account"], "unmatched": True} for r in lost["unmatched_transfers"][:30]])

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return issues


# ---------- giving ----------

def giving(conn, year, today, account="budget"):
    start = datetime.date(year, 1, 1)
    end = min(datetime.date(year, 12, 31), today)
    if end < start:
        end = start
    s = summary(conn, {"start": start, "end": end, "account": account})
    given = next((r["amount"] for r in s["expenses"] if r["category"] == "Giving"), Decimal(0))
    where, params = _where({"start": start, "end": end, "account": account, "category": "Giving"})
    rows = conn.execute(f"""
        SELECT t.id, t.date, -t.flow AS amount, COALESCE(t.merchant, t.description) AS name, t.merchant_key,
               t.note, a.name AS account
        {BASE_FROM} WHERE {where} ORDER BY t.date""", params).fetchall()
    orgs = {}
    for r in rows:
        org = orgs.setdefault(r["merchant_key"] or r["name"], {"name": r["name"], "total": Decimal(0), "count": 0})
        org["name"] = r["name"]
        org["total"] += Decimal(r["amount"])
        org["count"] += 1
        org["last_date"] = r["date"]
    by_month = [Decimal(0)] * 12
    for r in rows:
        by_month[r["date"].month - 1] += Decimal(r["amount"])
    last_year = summary(conn, {"start": datetime.date(year - 1, 1, 1),
                               "end": min(datetime.date(year - 1, 12, 31), today), "account": account})
    goal = get_setting(conn, "giving_goal_pct")
    return {
        "year": year, "start": start, "end": end, "given": given, "income": s["total_income"],
        "percent": (given / s["total_income"] * 100).quantize(Decimal("0.1")) if s["total_income"] > 0 else None,
        "goal_percent": Decimal(goal) if goal else None,
        "organizations": sorted(orgs.values(), key=lambda o: -o["total"]),
        "by_month": by_month, "gifts": rows,
        "last_year": next((r["amount"] for r in last_year["expenses"] if r["category"] == "Giving"), Decimal(0)),
    }
