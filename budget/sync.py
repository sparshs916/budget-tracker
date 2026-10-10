import datetime
import html
import logging
import re
import secrets
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import config, crypto
from .categorize import _WEB_PREFIX, RULES_VERSION, TRANSFER_WORDING, categorize, merchant_key
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
    """The calendar date of a SimpleFIN timestamp.

    Banks report dates, which SimpleFIN sends as midnight UTC. Converted to
    Pacific time that's the evening before, which moved Sept 1 charges into
    August and Oct 1 charges into September. A midnight-UTC timestamp is a
    plain date; anything else is a real moment, read in your time zone.
    """
    timestamp = int(timestamp)
    if timestamp % 86400 == 0:
        return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).date()
    return datetime.datetime.fromtimestamp(timestamp, tz).date()


def _epoch(date, tz):
    return int(datetime.datetime.combine(date, datetime.time.min, tz).timestamp())


def save_connection(conn, access_url):
    connection_id = "sfin_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO enrollments (id, institution, access_token, status) VALUES (%s, %s, %s, 'ok')",
        (connection_id, "SimpleFIN", crypto.encrypt(access_url)),
    )
    return connection_id


DATES_VERSION = "utc-midnight"


def sync_all(conn, today=None):
    # Once after the date fix: re-fetch full history so earlier rows get their dates corrected.
    meta = conn.execute("SELECT value FROM app_meta WHERE key = 'simplefin_dates'").fetchone()
    if not meta or meta["value"] != DATES_VERSION:
        conn.execute("UPDATE accounts SET history_synced = FALSE WHERE enrollment_id IS NOT NULL")
        conn.execute("""INSERT INTO app_meta (key, value) VALUES ('simplefin_dates', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (DATES_VERSION,))
        conn.commit()
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

    existing_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE account_id = %s AND source = 'simplefin'", (account["key"],)).fetchall()}
    uploaded = conn.execute(
        "SELECT id, date, flow FROM transactions WHERE account_id = %s AND source = 'csv' ORDER BY date, id",
        (account["key"],),
    ).fetchall()
    taken = set()

    seen = []
    for t in account["transactions"]:
        timestamp = t.get("transacted_at") or t.get("posted")
        date = _local_date(timestamp, tz) if timestamp else datetime.datetime.now(tz).date()
        merchant = html.unescape(t["payee"]) if t.get("payee") else None
        if t.get("description"):
            t["description"] = html.unescape(t["description"])
        flow = to_flow(t["amount"], account_type)
        txn_id = f"{account['key']}:{t['id']}"
        if txn_id not in existing_ids and not t.get("pending"):
            # The same transaction may already be here from a CSV upload: same amount
            # within a few days. Take that row over (keeping any category you set).
            match = _closest_upload(uploaded, taken, date, flow)
            if match:
                taken.add(match["id"])
                conn.execute("UPDATE transactions SET id = %s, source = 'simplefin', import_id = NULL WHERE id = %s",
                             (txn_id, match["id"]))
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
            """DELETE FROM transactions
               WHERE account_id = %s AND source = 'simplefin' AND date >= %s AND NOT (id = ANY(%s))""",
            (account["key"], window_start, seen),
        )
        conn.execute("UPDATE accounts SET history_synced = TRUE WHERE id = %s", (account["key"],))
    return len(seen)


def _closest_upload(uploaded, taken, date, flow, days=3):
    best = None
    for u in uploaded:
        if u["id"] in taken or Decimal(u["flow"]) != flow:
            continue
        distance = abs((u["date"] - date).days)
        if distance <= days and (best is None or distance < best[0]):
            best = (distance, u)
    return best[1] if best else None


def recategorize_account(conn, account_id):
    """Re-run the automatic categories after an account's type changes."""
    account = conn.execute("SELECT type FROM accounts WHERE id = %s", (account_id,)).fetchone()
    rows = conn.execute(
        "SELECT id, description, merchant, raw_amount, flow, source, bank_category FROM transactions WHERE account_id = %s",
        (account_id,),
    ).fetchall()
    for r in rows:
        # SimpleFIN amounts are normalized by account type; uploads were signed when imported.
        flow = to_flow(r["raw_amount"], account["type"]) if r["source"] == "simplefin" else Decimal(r["flow"])
        conn.execute(
            "UPDATE transactions SET flow = %s, auto_category = %s WHERE id = %s",
            (flow, categorize(description=r["description"], merchant=r["merchant"], flow=flow,
                              account_type=account["type"], hint=r["bank_category"]), r["id"]),
        )


def recategorize_all_if_rules_changed(conn):
    """Re-run the automatic categories once after the rules change. Your own
    category choices (per transaction or per merchant) are untouched."""
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'rules_version'").fetchone()
    if row and row["value"] == RULES_VERSION:
        return 0
    rows = conn.execute(
        """SELECT t.id, t.description, t.merchant, t.flow, t.auto_category, t.bank_category, a.type
           FROM transactions t JOIN accounts a ON a.id = t.account_id"""
    ).fetchall()
    changes = []
    for r in rows:
        new = categorize(description=r["description"], merchant=r["merchant"], flow=Decimal(r["flow"]),
                         account_type=r["type"], hint=r["bank_category"])
        # Uploads from before bank categories were stored may have been categorized by
        # one; don't downgrade those to Other.
        if new != r["auto_category"] and not (new in ("Other", "Other Income") and r["bank_category"] is None):
            changes.append((new, r["id"]))
    with conn.cursor() as cur:
        cur.executemany("UPDATE transactions SET auto_category = %s WHERE id = %s", changes)
    conn.execute(
        """INSERT INTO app_meta (key, value) VALUES ('rules_version', %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (RULES_VERSION,),
    )
    conn.commit()
    return len(changes)


def compare_account(conn, account_id, start, end, client=None):
    """What SimpleFIN returns for one account and date range vs. what the app has saved."""
    tz = ZoneInfo(config.TIMEZONE)
    row = conn.execute(
        """SELECT a.id, a.type, e.access_token FROM accounts a JOIN enrollments e ON e.id = a.enrollment_id
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
    uploaded = conn.execute(
        "SELECT id, date, flow FROM transactions WHERE account_id = %s AND source = 'csv'", (account_id,)).fetchall()
    taken = set()
    remote_ids = set()
    missing = []
    for t in remote["transactions"]:
        txn_id = f"{account_id}:{t['id']}"
        remote_ids.add(txn_id)
        timestamp = t.get("transacted_at") or t.get("posted")
        date = _local_date(timestamp, tz) if timestamp else end
        upload = None if txn_id in saved else _closest_upload(uploaded, taken, date, to_flow(t["amount"], row["type"]))
        if upload:
            taken.add(upload["id"])  # already here from a CSV upload
        elif txn_id not in saved:
            missing.append({"date": _local_date(timestamp, tz).isoformat() if timestamp else None,
                            "description": t.get("description"), "amount": t.get("amount"),
                            "pending": bool(t.get("pending"))})
    in_range = conn.execute(
        """SELECT id, date, description, raw_amount FROM transactions
           WHERE account_id = %s AND source = 'simplefin' AND date BETWEEN %s AND %s""",
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


PAIR_DAYS = 3
_MOVE_CATEGORIES = ("Transfer", "Credit Card Payment", "Savings")
# Words in bank names that say nothing about which bank it is.
_GENERIC_BANK_WORDS = {"bank", "banks", "credit", "union", "federal", "financial", "savings", "checking", "national",
                       "association", "the", "of", "and", "card", "cards", "services", "company", "trust", "manual",
                       "one", "com", "www", "online", "direct", "first", "community", "fcu", "na", "usa", "us"}


def _bank_words(institution):
    """'Golden 1 Credit Union' -> {'golden'}; 'Capital One' -> {'capital'}."""
    return {w for w in re.findall(r"[a-z]+", (institution or "").lower()) if len(w) >= 4 and w not in _GENERIC_BANK_WORDS}


def link_transfers(conn):
    """Find both halves of money moving between your own accounts and label them.

    Money out of one account and the same amount into a different account
    within 3 days, where both sides read like a transfer or payment (or are
    already categorized as one), is one move - whatever each bank calls it.
    ("Withdrawal Overdraft transfer to ****022-S9" on savings and "Overdraft
    Transfer" on checking.) Both halves are then left out of the totals, as a
    card payment if either side is a card. Requiring transfer-like wording on
    both sides keeps a refund and an unrelated payment of the same amount from
    being paired. Your own category choices still win.

    A second, best-guess pass then looks at what's left: one side reads like a
    transfer and the other is unexplained (Other / Other Income), e.g.
    "WEBCAPITAL ONE (TRANSFER)" leaving Golden 1 and "The Golden Credit Union"
    arriving at Capital One. They're paired when one side names the other
    account's bank, or both are bank accounts and the amount is $100 or more.
    A wrong guess there can't change Remaining: it hides one money-in and one
    money-out of the same amount, which cancel anyway.
    """
    rows = conn.execute(
        """SELECT t.id, t.account_id, t.date, t.flow, t.description, t.merchant, t.auto_category,
                  t.override_category, a.type AS account_type, a.subtype, a.include_in_budget, a.institution
           FROM transactions t JOIN accounts a ON a.id = t.account_id
           WHERE abs(t.flow) >= 1 AND NOT t.hidden
           ORDER BY t.date, t.id"""
    ).fetchall()

    def movable(r):
        if r["override_category"]:
            return r["override_category"] in _MOVE_CATEGORIES  # you said what this was
        if r["auto_category"] in ("Income", "Person-to-Person"):
            return False  # a paycheck or a payment to someone else is never one of your own moves
        text = f"{r['description'] or ''} {r['merchant'] or ''}"
        return r["auto_category"] in _MOVE_CATEGORIES or bool(TRANSFER_WORDING.search(text))

    buckets = {}
    for r in rows:
        if movable(r):
            buckets.setdefault(abs(Decimal(r["flow"])), []).append(r)

    pairs = []
    used = set()

    def match(outs, ins, fits=lambda out, inn: True):
        for distance in range(PAIR_DAYS + 1):
            for out in outs:
                if out["id"] in used:
                    continue
                for inn in ins:
                    if (inn["id"] not in used and inn["account_id"] != out["account_id"]
                            and abs((inn["date"] - out["date"]).days) == distance and fits(out, inn)):
                        used.update((out["id"], inn["id"]))
                        pairs.append((out, inn))
                        break

    for group in buckets.values():
        match([r for r in group if r["flow"] < 0], [r for r in group if r["flow"] > 0])

    # Best guess for the leftovers: a transfer whose other half the other bank describes vaguely.
    def unexplained(r):
        return not r["override_category"] and r["auto_category"] in ("Other", "Other Income")

    def names_bank(r, other):
        text = _WEB_PREFIX.sub("", f"{r['description'] or ''} {r['merchant'] or ''}").lower()
        words = set(re.findall(r"[a-z]+", text))
        return bool(words & _bank_words(other["institution"]))

    def fits(out, inn):
        if names_bank(out, inn) or names_bank(inn, out):
            return True
        return out["account_type"] == inn["account_type"] == "depository" and abs(Decimal(out["flow"])) >= 100

    loose = {}
    for r in rows:
        if r["id"] not in used and (movable(r) or unexplained(r)):
            loose.setdefault(abs(Decimal(r["flow"])), []).append(r)
    for group in loose.values():
        outs = [r for r in group if r["flow"] < 0]
        ins = [r for r in group if r["flow"] > 0]
        # Exactly one side may be the vague one; two vague rows are just two unknowns.
        match([r for r in outs if movable(r)], [r for r in ins if unexplained(r)], fits)
        match([r for r in outs if unexplained(r)], [r for r in ins if movable(r)], fits)

    updates = []
    for out, inn in pairs:
        if "credit" in (out["account_type"], inn["account_type"]):
            out_category = in_category = "Credit Card Payment"
        else:
            # Between two budget accounts a move nets to zero. With a savings account kept outside
            # the budget, money going in shows as Savings on the way out, and money coming back
            # shows as Savings on the way in (a withdrawal), so Savings is what you actually kept.
            out_category = ("Savings" if out["include_in_budget"] and not inn["include_in_budget"]
                            and inn["subtype"] == "savings" else "Transfer")
            in_category = ("Savings" if inn["include_in_budget"] and not out["include_in_budget"]
                           and out["subtype"] == "savings" else "Transfer")
        updates += [(inn["id"], out_category, out["id"]), (out["id"], in_category, inn["id"])]

    conn.execute("UPDATE transactions SET paired_with = NULL, pair_category = NULL WHERE paired_with IS NOT NULL")
    with conn.cursor() as cur:
        cur.executemany("UPDATE transactions SET paired_with = %s, pair_category = %s WHERE id = %s", updates)
    conn.commit()
    return len(pairs)
