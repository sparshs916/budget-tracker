import datetime
import logging
import re
import secrets
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import config, crypto
from .categorize import categorize, merchant_key
from .simplefin import SimpleFINClient, SimpleFINError, error_messages

log = logging.getLogger(__name__)

# SimpleFIN warns when one request spans more than 45 days, so fetch history in
# smaller chunks (with room for daylight-saving shifts).
CHUNK_DAYS = 40

CREDIT_NAME = re.compile(r"credit|card|visa|mastercard|amex|american express|discover|sapphire|freedom|venture|"
                         r"quicksilver|savor|platinum|gold card|double cash|custom cash|bilt", re.IGNORECASE)
SAVINGS_NAME = re.compile(r"saving|money market|\bmma\b|certificate|\bcd\b|high yield|reserve", re.IGNORECASE)
SUBTYPES = {"checking": "depository", "savings": "depository", "credit_card": "credit", "other": "depository"}


def guess_subtype(name, balance):
    """SimpleFIN doesn't say what kind of account it is, so guess from the name."""
    if CREDIT_NAME.search(name or ""):
        return "credit_card"
    if SAVINGS_NAME.search(name or ""):
        return "savings"
    # Positive amounts mean money into the account, so an amount owed on a card is negative.
    if balance is not None and Decimal(str(balance)) < 0:
        return "credit_card"
    return "checking"


def to_flow(raw_amount, account_type):
    """+ is money in, - is money out, for every account type."""
    amount = Decimal(str(raw_amount))
    if account_type == "credit" and config.INVERT_CREDIT_AMOUNTS:
        return -amount
    return amount


def _local_date(timestamp, tz):
    return datetime.datetime.fromtimestamp(int(timestamp), tz).date()


def _epoch(date, tz):
    return int(datetime.datetime.combine(date, datetime.time.min, tz).timestamp())


def save_connection(conn, access_url):
    connection_id = "sfin_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO enrollments (id, institution, access_token, status) VALUES (%s, %s, %s, 'ok')",
        (connection_id, "SimpleFIN", crypto.encrypt(access_url)),
    )
    return connection_id


def sync_all(conn, today=None):
    enrollments = conn.execute("SELECT * FROM enrollments ORDER BY created_at").fetchall()
    return [sync_enrollment(conn, e, today=today) for e in enrollments]


def sync_enrollment(conn, enrollment, today=None, client=None):
    tz = ZoneInfo(config.TIMEZONE)
    today = today or datetime.datetime.now(tz).date()
    result = {"enrollment": enrollment["institution"] or enrollment["id"], "accounts": 0, "transactions": 0}
    try:
        client = client or SimpleFINClient(crypto.decrypt(enrollment["access_token"]))
        fresh = conn.execute(
            "SELECT COUNT(*) = 0 OR bool_or(NOT history_synced) AS fresh FROM accounts WHERE enrollment_id = %s",
            (enrollment["id"],),
        ).fetchone()["fresh"]
        days = config.INITIAL_HISTORY_DAYS if fresh else config.RESYNC_WINDOW_DAYS
        window_start = today - datetime.timedelta(days=days)

        accounts, errors = _fetch(client, window_start, today, tz)
        windows = {key: window_start for key in accounts}

        # A bank added in SimpleFIN after the first sync shows up here as a new
        # account; give it the full history instead of just the recent window.
        synced = {r["id"] for r in conn.execute("SELECT id FROM accounts WHERE history_synced").fetchall()}
        new_keys = [key for key in accounts if key not in synced]
        if new_keys and not fresh:
            history_start = today - datetime.timedelta(days=config.INITIAL_HISTORY_DAYS)
            history, more_errors = _fetch(client, history_start, today, tz)
            errors += [m for m in more_errors if m not in errors]
            for key in new_keys:
                if key in history:
                    accounts[key], windows[key] = history[key], history_start

        for key, account in accounts.items():
            result["transactions"] += _save_account(conn, enrollment, account, windows[key], tz,
                                                    prune=not errors)
            result["accounts"] += 1

        conn.execute(
            "UPDATE enrollments SET status = %s, last_error = %s, last_synced_at = now() WHERE id = %s",
            ("attention" if errors else "ok", " · ".join(errors)[:500] or None, enrollment["id"]),
        )
        if errors:
            result["error"] = " · ".join(errors)
    except SimpleFINError as e:
        log.warning("SimpleFIN sync failed for %s: %s", enrollment["id"], e.message)
        conn.rollback()
        conn.execute(
            "UPDATE enrollments SET status = %s, last_error = %s WHERE id = %s",
            ("reconnect" if e.needs_reconnect else "error", e.message, enrollment["id"]),
        )
        result["error"] = e.message
    except Exception as e:  # Keep other connections syncing.
        log.exception("Sync failed for %s", enrollment["id"])
        conn.rollback()
        conn.execute(
            "UPDATE enrollments SET status = 'error', last_error = %s WHERE id = %s",
            (f"{type(e).__name__}: {e}"[:300], enrollment["id"]),
        )
        result["error"] = str(e)
    conn.commit()
    return result


def _fetch(client, window_start, today, tz):
    """Fetch [window_start, today] in chunks and merge accounts across them."""
    accounts, errors = {}, []
    chunk_start = window_start
    end = today + datetime.timedelta(days=1)
    while chunk_start < end:
        chunk_end = min(chunk_start + datetime.timedelta(days=CHUNK_DAYS), end)
        data = client.accounts(_epoch(chunk_start, tz), _epoch(chunk_end, tz))
        for message in error_messages(data):
            if message not in errors:
                errors.append(message)
        for a in data.get("accounts") or []:
            org = a.get("org") or {}
            key = f"{org.get('domain') or org.get('name') or org.get('sfin-url')}:{a['id']}"
            merged = accounts.setdefault(key, {**a, "key": key, "transactions": []})
            merged.update({k: v for k, v in a.items() if k != "transactions"})
            merged["transactions"].extend(a.get("transactions") or [])
        chunk_start = chunk_end
    return accounts, errors


def _save_account(conn, enrollment, account, window_start, tz, prune):
    org = account.get("org") or {}
    institution = org.get("name") or org.get("domain") or "Bank"
    existing = conn.execute("SELECT type, subtype, type_locked FROM accounts WHERE id = %s", (account["key"],)).fetchone()
    if existing and existing["type_locked"]:
        subtype = existing["subtype"]
    else:
        subtype = guess_subtype(account.get("name"), account.get("balance"))
    account_type = SUBTYPES.get(subtype, "depository")

    conn.execute(
        """
        INSERT INTO accounts (id, enrollment_id, institution, name, type, subtype, currency, status,
                              include_in_budget, ledger_balance, available_balance)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
           SET enrollment_id = EXCLUDED.enrollment_id, institution = EXCLUDED.institution,
               name = EXCLUDED.name, type = EXCLUDED.type, subtype = EXCLUDED.subtype,
               currency = EXCLUDED.currency, ledger_balance = EXCLUDED.ledger_balance,
               available_balance = EXCLUDED.available_balance, updated_at = now()
        """,
        (
            account["key"], enrollment["id"], institution, account.get("name"), account_type, subtype,
            account.get("currency"),
            # Savings accounts start outside the budget so a transfer into savings is
            # counted once (as "Savings" on checking).
            subtype in ("checking", "credit_card"),
            account.get("balance"), account.get("available-balance") or account.get("balance"),
        ),
    )

    seen = []
    for t in account["transactions"]:
        timestamp = t.get("transacted_at") or t.get("posted")
        date = _local_date(timestamp, tz) if timestamp else datetime.datetime.now(tz).date()
        merchant = t.get("payee") or None
        flow = to_flow(t["amount"], account_type)
        txn_id = f"{account['key']}:{t['id']}"
        conn.execute(
            """
            INSERT INTO transactions (id, account_id, date, description, merchant, merchant_key,
                                      raw_amount, flow, status, auto_category)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE
               SET date = EXCLUDED.date, description = EXCLUDED.description,
                   merchant = EXCLUDED.merchant, merchant_key = EXCLUDED.merchant_key,
                   raw_amount = EXCLUDED.raw_amount, flow = EXCLUDED.flow, status = EXCLUDED.status,
                   auto_category = EXCLUDED.auto_category, updated_at = now()
            """,
            (
                txn_id, account["key"], date, t.get("description"), merchant,
                merchant_key(merchant, t.get("description")), t["amount"], flow,
                "pending" if t.get("pending") else "posted",
                categorize(description=t.get("description"), merchant=merchant, flow=flow, account_type=account_type),
            ),
        )
        seen.append(txn_id)

    # Pending transactions can vanish or come back with a new id once they post.
    # Drop anything in the re-fetched window that SimpleFIN no longer returns,
    # unless the bank reported an error (then a missing transaction proves nothing).
    if prune:
        conn.execute(
            "DELETE FROM transactions WHERE account_id = %s AND date >= %s AND NOT (id = ANY(%s))",
            (account["key"], window_start, seen),
        )
        conn.execute("UPDATE accounts SET history_synced = TRUE WHERE id = %s", (account["key"],))
    return len(seen)


def recategorize_account(conn, account_id):
    """Re-run the automatic categories after an account's type changes."""
    account = conn.execute("SELECT type FROM accounts WHERE id = %s", (account_id,)).fetchone()
    rows = conn.execute(
        "SELECT id, description, merchant, raw_amount FROM transactions WHERE account_id = %s", (account_id,)
    ).fetchall()
    for r in rows:
        flow = to_flow(r["raw_amount"], account["type"])
        conn.execute(
            "UPDATE transactions SET flow = %s, auto_category = %s WHERE id = %s",
            (flow, categorize(description=r["description"], merchant=r["merchant"], flow=flow,
                              account_type=account["type"]), r["id"]),
        )


def compare_account(conn, account_id, start, end, client=None):
    """What SimpleFIN returns for one account and date range vs. what the app has saved."""
    tz = ZoneInfo(config.TIMEZONE)
    row = conn.execute(
        """SELECT a.id, e.access_token FROM accounts a JOIN enrollments e ON e.id = a.enrollment_id
           WHERE a.id = %s""",
        (account_id,),
    ).fetchone()
    if not row:
        return None
    client = client or SimpleFINClient(crypto.decrypt(row["access_token"]))
    accounts, errors = _fetch(client, start, end, tz)
    remote = accounts.get(account_id)
    if remote is None:
        return {"found": False, "errors": errors}

    saved = {r["id"] for r in conn.execute("SELECT id FROM transactions WHERE account_id = %s", (account_id,)).fetchall()}
    remote_ids = set()
    missing = []
    for t in remote["transactions"]:
        txn_id = f"{account_id}:{t['id']}"
        remote_ids.add(txn_id)
        if txn_id not in saved:
            timestamp = t.get("transacted_at") or t.get("posted")
            missing.append({"date": _local_date(timestamp, tz).isoformat() if timestamp else None,
                            "description": t.get("description"), "amount": t.get("amount"),
                            "pending": bool(t.get("pending"))})
    in_range = conn.execute(
        "SELECT id, date, description, raw_amount FROM transactions WHERE account_id = %s AND date BETWEEN %s AND %s",
        (account_id, start, end),
    ).fetchall()
    return {
        "found": True,
        "errors": errors,
        "simplefin_count": len(remote_ids),
        "app_count": len(in_range),
        "missing": sorted(missing, key=lambda m: m["date"] or "")[:100],
        "not_returned": [{"date": r["date"].isoformat(), "description": r["description"], "amount": str(r["raw_amount"])}
                         for r in in_range if r["id"] not in remote_ids][:100],
    }
