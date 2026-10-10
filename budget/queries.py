import datetime
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


def _where(filters, include_category=True):
    clauses = ["t.date BETWEEN %(start)s AND %(end)s"]
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
        clauses.append("(t.description ILIKE %(q)s OR t.merchant ILIKE %(q)s OR t.note ILIKE %(q)s)")
        params["q"] = f"%{filters['q']}%"
    return " AND ".join(clauses), params


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
    where, params = _where(filters)
    total = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(t.flow), 0) AS net {BASE_FROM} WHERE {where}", params).fetchone()
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.description, t.merchant, t.merchant_key, t.flow, t.raw_amount,
               t.status, t.type, t.source, {EFFECTIVE_CATEGORY} AS category,
               t.override_category IS NOT NULL AS overridden,
               (t.override_category IS NULL AND r.category IS NULL AND t.pair_category IS NULL
                AND {AI_CATEGORY} IS NOT NULL) AS ai_suggested,
               t.paired_with IS NOT NULL AS paired, t.note,
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
               a.ledger_balance, a.available_balance, a.status,
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


def recurring(conn, today, account="budget"):
    """Charges that repeat on a schedule (subscriptions, bills, rent), found from
    the last 13 months of money out. Counted spending is untouched; this only reads it."""
    where, params = _where({"start": month_start(today, 13), "end": today, "account": account}, include_category=False)
    rows = conn.execute(
        f"""
        SELECT t.date, t.flow, t.merchant_key, COALESCE(t.merchant, t.description) AS name, {EFFECTIVE_CATEGORY} AS category
        {BASE_FROM}
        WHERE {where} AND t.flow < 0 AND t.merchant_key IS NOT NULL AND t.status IS DISTINCT FROM 'pending'
        ORDER BY t.date
        """,
        params,
    ).fetchall()
    groups = {}
    for r in rows:
        if CATEGORY_KIND.get(r["category"]) in ("excluded", "savings"):  # moves, not costs
            continue
        groups.setdefault(r["merchant_key"], []).append(r)

    found = []
    for charges in groups.values():
        # Several charges on one day (e.g. two Amazon orders) count as one visit.
        by_day = {}
        for c in charges:
            by_day.setdefault(c["date"], []).append(-c["flow"])
        dates = sorted(by_day)
        if len(dates) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        gap = sorted(gaps)[len(gaps) // 2]
        if 25 <= gap <= 35 and len(dates) >= 3:
            frequency, per_year = "monthly", 12
        elif 6 <= gap <= 8 and len(dates) >= 4:
            frequency, per_year = "weekly", 52
        elif 12 <= gap <= 16 and len(dates) >= 3:
            frequency, per_year = "every 2 weeks", 26
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
            "name": charges[-1]["name"], "category": charges[-1]["category"], "frequency": frequency,
            "typical": typical, "last_amount": last, "last_date": dates[-1], "count": len(dates),
            "next_date": dates[-1] + datetime.timedelta(days=gap),
            "monthly_cost": (typical * per_year / 12).quantize(Decimal("0.01")),
            "yearly_cost": (typical * per_year).quantize(Decimal("0.01")),
            "price_change": (last - previous) if changed else None,
        })
    found.sort(key=lambda r: -r["monthly_cost"])
    return found
