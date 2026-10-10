from decimal import Decimal

from .categorize import CATEGORIES, CATEGORY_KIND

# Your choice for the transaction, then your rule for the merchant, then the
# keyword rules - with Claude's suggestion filling in only where those said Other.
AI_CATEGORY = ("CASE WHEN t.auto_category IN ('Other', 'Other Income') AND ai.category NOT IN ('Other', 'Other Income') "
               "AND NOT (ai.category = 'Income' AND t.flow < 0) THEN ai.category END")
EFFECTIVE_CATEGORY = f"COALESCE(t.override_category, r.category, {AI_CATEGORY}, t.auto_category)"

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
        clauses.append("(t.description ILIKE %(q)s OR t.merchant ILIKE %(q)s)")
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
    }


def transactions(conn, filters, page, page_size):
    where, params = _where(filters)
    total = conn.execute(f"SELECT COUNT(*) AS n, COALESCE(SUM(t.flow), 0) AS net {BASE_FROM} WHERE {where}", params).fetchone()
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.description, t.merchant, t.merchant_key, t.flow, t.raw_amount,
               t.status, t.type, t.source, {EFFECTIVE_CATEGORY} AS category,
               t.override_category IS NOT NULL AS overridden,
               (t.override_category IS NULL AND r.category IS NULL AND {AI_CATEGORY} IS NOT NULL) AS ai_suggested,
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
