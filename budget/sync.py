import datetime
import logging
from decimal import Decimal

from . import config, crypto
from .categorize import categorize, merchant_key
from .teller import TellerClient, TellerError

log = logging.getLogger(__name__)

BUDGET_SUBTYPES = {"checking", "credit_card"}


def to_flow(raw_amount, account_type):
    """Normalize Teller's sign so that + is money in and - is money out."""
    amount = Decimal(str(raw_amount))
    if account_type == "credit" and config.CREDIT_PURCHASES_ARE_POSITIVE:
        return -amount
    return amount


def save_enrollment(conn, enrollment_id, institution, access_token):
    conn.execute(
        """
        INSERT INTO enrollments (id, institution, access_token, status, last_error)
        VALUES (%s, %s, %s, 'ok', NULL)
        ON CONFLICT (id) DO UPDATE
           SET access_token = EXCLUDED.access_token,
               institution = COALESCE(EXCLUDED.institution, enrollments.institution),
               status = 'ok', last_error = NULL
        """,
        (enrollment_id, institution, crypto.encrypt(access_token)),
    )


def sync_all(conn, today=None):
    enrollments = conn.execute("SELECT * FROM enrollments ORDER BY created_at").fetchall()
    return [sync_enrollment(conn, e, today=today) for e in enrollments]


def sync_enrollment(conn, enrollment, today=None, client=None):
    today = today or datetime.date.today()
    result = {"enrollment": enrollment["institution"] or enrollment["id"], "accounts": 0, "transactions": 0}
    try:
        client = client or TellerClient(crypto.decrypt(enrollment["access_token"]))
        for account in client.accounts():
            if account.get("enrollment_id") and account["enrollment_id"] != enrollment["id"]:
                continue
            result["transactions"] += _sync_account(conn, client, enrollment, account, today)
            result["accounts"] += 1
        conn.execute(
            "UPDATE enrollments SET status = 'ok', last_error = NULL, last_synced_at = now() WHERE id = %s",
            (enrollment["id"],),
        )
    except TellerError as e:
        log.warning("Teller sync failed for %s: %s", enrollment["id"], e)
        conn.rollback()
        conn.execute(
            "UPDATE enrollments SET status = %s, last_error = %s WHERE id = %s",
            ("reconnect" if e.needs_reconnect else "error", e.message, enrollment["id"]),
        )
        result["error"] = e.message
    except Exception as e:  # network trouble etc. Keep other enrollments syncing.
        log.exception("Sync failed for %s", enrollment["id"])
        conn.rollback()
        conn.execute(
            "UPDATE enrollments SET status = 'error', last_error = %s WHERE id = %s",
            (str(e)[:300], enrollment["id"]),
        )
        result["error"] = str(e)
    conn.commit()
    return result


def _sync_account(conn, client, enrollment, account, today):
    account_type = account.get("type")
    subtype = account.get("subtype")
    row = conn.execute(
        """
        INSERT INTO accounts (id, enrollment_id, institution, name, type, subtype, last_four,
                              currency, status, include_in_budget)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
           SET name = EXCLUDED.name, type = EXCLUDED.type, subtype = EXCLUDED.subtype,
               last_four = EXCLUDED.last_four, status = EXCLUDED.status,
               institution = EXCLUDED.institution, updated_at = now()
        RETURNING history_synced
        """,
        (
            account["id"], enrollment["id"],
            (account.get("institution") or {}).get("name") or enrollment["institution"],
            account.get("name"), account_type, subtype, account.get("last_four"),
            account.get("currency"), account.get("status"),
            # Savings/investment accounts are left out of the budget by default so a
            # transfer into savings is counted once (as "Savings" on checking).
            subtype in BUDGET_SUBTYPES or account_type == "credit",
        ),
    ).fetchone()

    if account.get("status") == "closed":
        return 0

    links = account.get("links") or {}
    if links.get("balances"):
        try:
            bal = client.balances(account["id"])
            conn.execute(
                "UPDATE accounts SET ledger_balance = %s, available_balance = %s WHERE id = %s",
                (bal.get("ledger"), bal.get("available"), account["id"]),
            )
        except TellerError as e:
            if e.needs_reconnect:
                raise
            log.warning("Balance fetch failed for %s: %s", account["id"], e)

    if not links.get("transactions"):
        return 0

    days = config.RESYNC_WINDOW_DAYS if row["history_synced"] else config.INITIAL_HISTORY_DAYS
    start = today - datetime.timedelta(days=days)
    txns = client.transactions(account["id"], start_date=start)

    seen = []
    for t in txns:
        date = datetime.date.fromisoformat(t["date"])
        if date < start:
            continue
        details = t.get("details") or {}
        counterparty = details.get("counterparty") or {}
        merchant = counterparty.get("name")
        flow = to_flow(t["amount"], account_type)
        auto = categorize(
            description=t.get("description"), merchant=merchant,
            teller_category=details.get("category"), teller_type=t.get("type"),
            flow=flow, account_type=account_type,
        )
        conn.execute(
            """
            INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key,
                                      raw_amount, flow, type, status, teller_category, auto_category)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
               SET date = EXCLUDED.date, description = EXCLUDED.description,
                   merchant = EXCLUDED.merchant, merchant_key = EXCLUDED.merchant_key,
                   raw_amount = EXCLUDED.raw_amount, flow = EXCLUDED.flow, type = EXCLUDED.type,
                   status = EXCLUDED.status, teller_category = EXCLUDED.teller_category,
                   auto_category = EXCLUDED.auto_category, updated_at = now()
            """,
            (
                t["id"], account["id"], date, t.get("description"), merchant,
                merchant_key(merchant, t.get("description")), t["amount"], flow, t.get("type"),
                t.get("status"), details.get("category"), auto,
            ),
        )
        seen.append(t["id"])

    # Pending transactions can disappear or come back with a new id once they
    # post. Drop anything in the re-fetched window that Teller no longer returns.
    conn.execute(
        "DELETE FROM transactions WHERE account_id = %s AND date >= %s AND NOT (id = ANY(%s))",
        (account["id"], start, seen),
    )
    conn.execute("UPDATE accounts SET history_synced = TRUE WHERE id = %s", (account["id"],))
    return len(seen)
