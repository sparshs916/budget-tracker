import csv
import datetime
import hashlib
import io
import hmac
import logging
import secrets
from decimal import Decimal
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from flask.json.provider import DefaultJSONProvider
from werkzeug.security import check_password_hash

from . import ai_categorize, config, csv_import, db, queries
from .categorize import CATEGORIES, CATEGORY_KIND, CATEGORY_NAMES
from .simplefin import SimpleFINError, claim_setup_token
from .sync import (SUBTYPES, compare_account, link_transfers, recategorize_account, recategorize_all_if_rules_changed,
                   save_connection, sync_all, sync_enrollment)

logging.basicConfig(level=logging.INFO)


class JSONProvider(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, Decimal):
            return str(o.quantize(Decimal("0.01")))
        if isinstance(o, (datetime.date, datetime.datetime)):
            return o.isoformat()
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = JSONProvider(app)
app.config.update(
    SECRET_KEY=config.SECRET_KEY or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=config.IS_PRODUCTION,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # CSV uploads
)

MAX_FAILURES_PER_IP = 5
# Across all addresses: high enough that a stranger can't lock you out with a few tries,
# low enough to stop a spread-out guessing attack.
MAX_FAILURES_TOTAL = 200
LOCKOUT_MINUTES = 15


def today():
    return datetime.datetime.now(ZoneInfo(config.TIMEZONE)).date()


def _password_fingerprint():
    # Changing APP_PASSWORD_HASH logs out every existing session.
    return hashlib.sha256((config.APP_PASSWORD_HASH or "").encode()).hexdigest()[:16]


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return request.headers.get("X-Real-IP") or forwarded.split(",")[0].strip() or request.remote_addr


def _csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _logged_in():
    return session.get("auth") is True and session.get("pw") == _password_fingerprint()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _logged_in():
            if request.path.startswith("/api/"):
                return jsonify(error="Not logged in"), 401
            return redirect(url_for("login"))
        if request.method != "GET":
            sent = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(sent, session.get("csrf", "")):
                return jsonify(error="Bad CSRF token, reload the page"), 403
        return view(*args, **kwargs)

    return wrapper


@app.before_request
def check_config():
    missing = config.missing_settings()
    if missing:
        return render_template("setup.html", missing=missing), 503


_rules_checked = False


@app.before_request
def apply_new_category_rules():
    global _rules_checked
    if _rules_checked or not request.path.startswith("/api/"):
        return
    _rules_checked = True
    try:
        with db.connect() as conn:
            changed = recategorize_all_if_rules_changed(conn)
            _relink(conn)  # once per server start: picks up pairing for data synced before it existed
        if changed:
            logging.info("Re-categorized %s transactions for new rules", changed)
    except Exception:
        logging.exception("Re-categorizing failed")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store"
    if config.IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


# ---------- auth ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if _logged_in():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        ip = _client_ip()
        with db.connect() as conn:
            counts = conn.execute(
                """
                SELECT COUNT(*) FILTER (WHERE ip = %s) AS ip_failures, COUNT(*) AS total_failures
                FROM login_attempts
                WHERE NOT success AND created_at > now() - make_interval(mins => %s)
                """,
                (ip, LOCKOUT_MINUTES),
            ).fetchone()
            if counts["ip_failures"] >= MAX_FAILURES_PER_IP or counts["total_failures"] >= MAX_FAILURES_TOTAL:
                return render_template("login.html", error=f"Too many attempts. Try again in {LOCKOUT_MINUTES} minutes.",
                                       csrf=_csrf_token()), 429

            form_csrf = request.form.get("csrf", "")
            ok = hmac.compare_digest(form_csrf, session.get("csrf", "")) and check_password_hash(
                config.APP_PASSWORD_HASH, request.form.get("password", ""))
            conn.execute("INSERT INTO login_attempts (ip, success) VALUES (%s, %s)", (ip, ok))
            conn.execute("DELETE FROM login_attempts WHERE created_at < now() - interval '30 days'")
        if ok:
            session.clear()
            session.permanent = True
            session["auth"] = True
            session["pw"] = _password_fingerprint()
            _csrf_token()
            return redirect(url_for("index"))
        error = "Wrong password."
    return render_template("login.html", error=error, csrf=_csrf_token())


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return jsonify(ok=True)


# ---------- page ----------

@app.get("/")
@login_required
def index():
    return render_template(
        "index.html",
        csrf=_csrf_token(),
        categories=CATEGORY_NAMES,
        category_kinds=dict(CATEGORIES),
        today=today().isoformat(),
        ai_enabled=ai_categorize.enabled(),
    )


# ---------- API ----------

def _filters():
    t = today()
    try:
        start = datetime.date.fromisoformat(request.args.get("start") or t.replace(day=1).isoformat())
        end = datetime.date.fromisoformat(request.args.get("end") or t.isoformat())
    except ValueError:
        abort(400, "Dates must be YYYY-MM-DD")
    if start > end:
        start, end = end, start
    category = request.args.get("category") or None
    if category and category not in CATEGORY_NAMES:
        abort(400, "Unknown category")
    return {
        "start": start,
        "end": end,
        "account": request.args.get("account") or "budget",
        "category": category,
        "q": (request.args.get("q") or "").strip()[:100] or None,
    }


@app.get("/api/summary")
@login_required
def api_summary():
    filters = _filters()
    with db.connect() as conn:
        return jsonify(queries.summary(conn, filters))


@app.get("/api/transactions")
@login_required
def api_transactions():
    filters = _filters()
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(10, request.args.get("page_size", 50, type=int)))
    with db.connect() as conn:
        result = queries.transactions(conn, filters, page, page_size)
    result.update(page=page, page_size=page_size)
    return jsonify(result)


@app.get("/api/export")
@login_required
def api_export():
    """The filtered transactions as CSV, with the category the budget uses."""
    filters = _filters()
    with db.connect() as conn:
        rows = queries.transactions(conn, filters, 1, 100000)["rows"]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Account", "Description", "Merchant", "Category", "Amount", "Status", "Source", "Category from",
                     "Note"])
    for r in rows:
        origin = ("you" if r["overridden"] else "transfer match" if r["paired"] else "AI" if r["ai_suggested"] else "rules")
        category = "Hidden (not counted)" if r["hidden"] else r["category"]
        writer.writerow([r["date"], _cell(r["account_name"]), _cell(r["description"]), _cell(r["merchant"]), category,
                         f"{r['flow']:.2f}", r["status"], r["source"], origin, _cell(r["note"])])
    name = f"transactions-{filters['start']}-to-{filters['end']}.csv"
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _cell(text):
    """Bank text can start with = + - @; spreadsheets would run it as a formula."""
    text = text or ""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/api/accounts")
@login_required
def api_accounts():
    with db.connect() as conn:
        return jsonify(accounts=queries.accounts(conn), enrollments=queries.enrollments(conn))


# Account and transaction ids contain ":" and other characters that don't
# survive URL paths on every host, so they travel in the query string or body.
@app.get("/api/accounts/compare")
@login_required
def api_compare_account():
    account_id = request.args.get("check_account", "")
    filters = _filters()
    end = min(filters["end"], today())
    # Each 40 days is one SimpleFIN request, and SimpleFIN rate-limits them.
    start = max(filters["start"], end - datetime.timedelta(days=89))
    with db.connect() as conn:
        try:
            result = compare_account(conn, account_id, start, end)
        except SimpleFINError as e:
            return jsonify(error=e.message), 502
    if result is None:
        return jsonify(error="Not found"), 404
    result.update(start=start, end=end)
    return jsonify(result)


@app.post("/api/accounts/update")
@login_required
def api_update_account():
    body = request.get_json(force=True)
    account_id = body.get("id", "")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,)).fetchone():
            return jsonify(error="Account not found. Reload the page and try again."), 404
        if "subtype" in body:
            subtype = body["subtype"]
            if subtype not in SUBTYPES:
                return jsonify(error="Unknown account type"), 400
            # A hand-picked type sticks across syncs and resets the budget default for it.
            conn.execute(
                """UPDATE accounts SET subtype = %s, type = %s, type_locked = TRUE, include_in_budget = %s
                   WHERE id = %s""",
                (subtype, SUBTYPES[subtype], subtype in ("checking", "credit_card"), account_id),
            )
            recategorize_account(conn, account_id)
            conn.commit()
            _relink(conn)
        if "include_in_budget" in body:
            conn.execute("UPDATE accounts SET include_in_budget = %s WHERE id = %s",
                         (bool(body["include_in_budget"]), account_id))
            conn.commit()
            _relink(conn)
    return jsonify(ok=True)


@app.post("/api/transactions/category")
@login_required
def api_set_category():
    body = request.get_json(force=True)
    txn_id = body.get("id", "")
    category = body.get("category") or None
    if category and category not in CATEGORY_NAMES:
        return jsonify(error="Unknown category"), 400
    with db.connect() as conn:
        txn = conn.execute("SELECT merchant_key FROM transactions WHERE id = %s", (txn_id,)).fetchone()
        if not txn:
            return jsonify(error="Not found"), 404
        if body.get("remember") and txn["merchant_key"]:
            if category:
                conn.execute(
                    """INSERT INTO merchant_rules (merchant_key, category) VALUES (%s, %s)
                       ON CONFLICT (merchant_key) DO UPDATE SET category = EXCLUDED.category""",
                    (txn["merchant_key"], category),
                )
            else:
                conn.execute("DELETE FROM merchant_rules WHERE merchant_key = %s", (txn["merchant_key"],))
            conn.execute("UPDATE transactions SET override_category = NULL WHERE id = %s", (txn_id,))
        else:
            conn.execute("UPDATE transactions SET override_category = %s WHERE id = %s", (category, txn_id))
        conn.commit()
        _relink(conn)
    return jsonify(ok=True)


@app.post("/api/transactions/review")
@login_required
def api_review_transfer():
    """Mark a transfer with no matching other half as checked (its other end is an
    account you don't track), or flag it again. Totals are unaffected either way."""
    body = request.get_json(force=True)
    with db.connect() as conn:
        updated = conn.execute("UPDATE transactions SET transfer_reviewed = %s WHERE id = %s RETURNING id",
                               (bool(body.get("reviewed", True)), body.get("id", ""))).fetchone()
    if not updated:
        return jsonify(error="Not found"), 404
    return jsonify(ok=True)


@app.post("/api/transactions/note")
@login_required
def api_note():
    body = request.get_json(force=True)
    note = (body.get("note") or "").strip()[:500] or None
    with db.connect() as conn:
        updated = conn.execute("UPDATE transactions SET note = %s WHERE id = %s RETURNING id",
                               (note, body.get("id", ""))).fetchone()
    if not updated:
        return jsonify(error="Not found"), 404
    return jsonify(ok=True)


@app.get("/api/targets")
@login_required
def api_targets():
    with db.connect() as conn:
        return jsonify(targets=queries.targets(conn))


@app.post("/api/targets")
@login_required
def api_set_targets():
    values = request.get_json(force=True).get("targets")
    if not isinstance(values, dict):
        return jsonify(error="Send {targets: {category: amount}}"), 400
    clean = {}
    for category, amount in values.items():
        if CATEGORY_KIND.get(category) not in ("expense", "savings"):
            return jsonify(error=f"Can't set a target for {category}"), 400
        if amount in (None, ""):
            clean[category] = None
            continue
        try:
            value = Decimal(str(amount)).quantize(Decimal("0.01"))
        except ArithmeticError:
            return jsonify(error=f"{category}: not a number"), 400
        if not value.is_finite() or value < 0 or value > 10_000_000:
            return jsonify(error=f"{category}: enter an amount from 0 to 10,000,000"), 400
        clean[category] = value
    with db.connect() as conn:
        queries.set_targets(conn, clean)
        return jsonify(targets=queries.targets(conn))


@app.get("/api/trends")
@login_required
def api_trends():
    t = today()
    try:
        end = datetime.date.fromisoformat(request.args.get("end") or t.isoformat())
    except ValueError:
        abort(400, "Dates must be YYYY-MM-DD")
    months = min(24, max(1, request.args.get("months", 12, type=int)))
    with db.connect() as conn:
        return jsonify(months=queries.trends(conn, min(end, t), months, request.args.get("account") or "budget"))


@app.post("/api/transactions/hide")
@login_required
def api_hide_transaction():
    """Hide a row that isn't real (a duplicate, or a stray row from the bank feed), or bring it back."""
    body = request.get_json(force=True)
    with db.connect() as conn:
        updated = conn.execute("UPDATE transactions SET hidden = %s WHERE id = %s RETURNING id",
                               (bool(body.get("hidden", True)), body.get("id", ""))).fetchone()
        conn.commit()
        if updated:
            _relink(conn)
    if not updated:
        return jsonify(error="Not found"), 404
    return jsonify(ok=True)


@app.get("/api/health")
@login_required
def api_health():
    with db.connect() as conn:
        return jsonify(issues=queries.health(conn, today()))


@app.get("/api/forecast")
@login_required
def api_forecast():
    days = min(90, max(7, request.args.get("days", 35, type=int)))
    with db.connect() as conn:
        return jsonify(queries.forecast(conn, today(), days))


def _money_arg(value, low=0, high=10_000_000):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except ArithmeticError:
        return None
    return amount if amount.is_finite() and low <= amount <= high else None


@app.post("/api/settings/low-balance")
@login_required
def api_low_balance():
    amount = _money_arg(request.get_json(force=True).get("amount"))
    if amount is None:
        return jsonify(error="Enter an amount from 0 to 10,000,000"), 400
    with db.connect() as conn:
        queries.set_setting(conn, "low_balance_alert", amount)
    return jsonify(ok=True)


@app.post("/api/accounts/balance")
@login_required
def api_set_balance():
    """For accounts added by hand: today's balance, so the cash-flow forecast can use it."""
    body = request.get_json(force=True)
    amount = _money_arg(body.get("balance"), low=-10_000_000)
    if amount is None:
        return jsonify(error="Enter the balance as a number"), 400
    with db.connect() as conn:
        updated = conn.execute(
            """UPDATE accounts SET ledger_balance = %s, balance_date = %s
               WHERE id = %s AND enrollment_id IS NULL RETURNING id""",
            (amount, today(), body.get("id", ""))).fetchone()
    if not updated:
        return jsonify(error="Only accounts you added by hand take a typed balance; SimpleFIN sends the others."), 400
    return jsonify(ok=True)


def _year_arg():
    year = request.args.get("year", today().year, type=int)
    if not 2000 <= year <= today().year:
        abort(400, "Pick a year from 2000 to this year")
    return year


@app.get("/api/giving")
@login_required
def api_giving():
    with db.connect() as conn:
        return jsonify(queries.giving(conn, _year_arg(), today(), request.args.get("account") or "budget"))


@app.post("/api/giving/goal")
@login_required
def api_giving_goal():
    value = request.get_json(force=True).get("percent")
    percent = None if value in (None, "") else _money_arg(value, high=100)
    if value not in (None, "") and percent is None:
        return jsonify(error="Enter a percentage from 0 to 100, or leave it blank"), 400
    with db.connect() as conn:
        queries.set_setting(conn, "giving_goal_pct", percent if percent else None)
    return jsonify(ok=True)


@app.get("/api/giving/export")
@login_required
def api_giving_export():
    """Every gift in the year, for tax records."""
    year = _year_arg()
    with db.connect() as conn:
        data = queries.giving(conn, year, today(), request.args.get("account") or "budget")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Date", "Organization", "Amount", "Account", "Note"])
    for g in data["gifts"]:
        writer.writerow([g["date"], _cell(g["name"]), f"{g['amount']:.2f}", _cell(g["account"]), _cell(g["note"])])
    writer.writerow([])
    writer.writerow(["Total", "", f"{data['given']:.2f}", "", ""])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="giving-{year}.csv"'})


@app.get("/api/recurring")
@login_required
def api_recurring():
    with db.connect() as conn:
        return jsonify(recurring=queries.recurring(conn, today(), request.args.get("account") or "budget"))


def _relink(conn):
    try:
        link_transfers(conn)
    except Exception:
        logging.exception("Linking transfers failed")
        conn.rollback()


def _auto_categorize(conn, limit=300):
    """After new data: pair transfers, then let Claude fill in merchants still in
    Other if an API key is set. Never fails the caller."""
    _relink(conn)
    if not ai_categorize.enabled():
        return None
    try:
        return ai_categorize.categorize_unknown(conn, limit=limit)
    except Exception:
        logging.exception("AI categorization failed")
        conn.rollback()
        return None


@app.post("/api/connections")
@login_required
def api_add_connection():
    token = (request.get_json(force=True).get("setupToken") or "").strip()
    if not token:
        return jsonify(error="Paste a SimpleFIN setup token."), 400
    try:
        access_url = claim_setup_token(token)
    except SimpleFINError as e:
        return jsonify(error=e.message), 400
    with db.connect() as conn:
        # Save before syncing: the setup token only works once.
        connection_id = save_connection(conn, access_url)
        conn.commit()
        connection = conn.execute("SELECT * FROM enrollments WHERE id = %s", (connection_id,)).fetchone()
        result = sync_enrollment(conn, connection, today=today())
        result["ai_categorized"] = _auto_categorize(conn)
        # Connections whose accounts all moved to this one are leftovers from a reconnect.
        conn.execute(
            """DELETE FROM enrollments e WHERE e.id <> %s
               AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.enrollment_id = e.id)""",
            (connection_id,),
        )
    return jsonify(result)


@app.delete("/api/connections/<connection_id>")
@login_required
def api_remove_connection(connection_id):
    with db.connect() as conn:
        conn.execute("DELETE FROM enrollments WHERE id = %s", (connection_id,))
    return jsonify(ok=True)


def _create_manual_account(conn, name, subtype, institution=None):
    account_id = "manual:" + secrets.token_hex(8)
    conn.execute(
        """INSERT INTO accounts (id, institution, name, type, subtype, status, include_in_budget, type_locked)
           VALUES (%s, %s, %s, %s, %s, 'open', %s, TRUE)""",
        (account_id, institution or "Manual", name, SUBTYPES[subtype], subtype, subtype in ("checking", "credit_card")),
    )
    return account_id


@app.post("/api/accounts/manual")
@login_required
def api_add_manual_account():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()[:100]
    subtype = body.get("subtype") or ""
    if not name or subtype not in SUBTYPES:
        return jsonify(error="Give the account a name and type."), 400
    with db.connect() as conn:
        account_id = _create_manual_account(conn, name, subtype, (body.get("institution") or "").strip()[:100] or None)
    return jsonify(id=account_id)


@app.post("/api/accounts/delete")
@login_required
def api_delete_manual_account():
    account_id = request.get_json(force=True).get("id", "")
    with db.connect() as conn:
        # Only hand-made accounts; SimpleFIN accounts would just come back on the next sync.
        deleted = conn.execute("DELETE FROM accounts WHERE id = %s AND enrollment_id IS NULL RETURNING id",
                               (account_id,)).fetchone()
        conn.commit()
        _relink(conn)
    if not deleted:
        return jsonify(error="Only accounts you added by hand can be removed."), 400
    return jsonify(ok=True)


@app.post("/api/import")
@login_required
def api_import():
    """Preview (commit=0) or save (commit=1) a CSV upload into one account."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="Choose a CSV file."), 400
    try:
        parsed = csv_import.parse(upload.read())
    except csv_import.CSVImportError as e:
        return jsonify(error=str(e)), 400

    form = request.form
    with db.connect() as conn:
        if form.get("account_id") == "new":
            name = (form.get("new_name") or "").strip()[:100]
            subtype = form.get("new_subtype") or "checking"
            if not name or subtype not in SUBTYPES:
                return jsonify(error="Give the new account a name and type."), 400
            account = {"id": "manual:preview", "name": name, "type": SUBTYPES[subtype], "subtype": subtype}
            if form.get("commit") == "1":
                # Create it first: imported rows are fingerprinted with the account's id.
                account["id"] = _create_manual_account(conn, name, subtype)
        else:
            account = conn.execute("SELECT id, name, type, subtype FROM accounts WHERE id = %s",
                                   (form.get("account_id") or "",)).fetchone()
            if not account:
                return jsonify(error="Pick the account these transactions belong to."), 400

        suggested = csv_import.suggest_flip(parsed, account["type"])
        flip = suggested if form.get("flip", "auto") == "auto" else form.get("flip") == "1"
        new_rows, duplicates, not_in_file = csv_import.plan(conn, account, parsed, flip)

        result = {
            "account": account["name"], "flip": flip, "flip_suggested": suggested,
            "parsed": len(parsed["rows"]), "ignored_lines": parsed["skipped"],
            "new_count": len(new_rows), "duplicate_count": len(duplicates),
            "start": min(r["date"] for r in parsed["rows"]), "end": max(r["date"] for r in parsed["rows"]),
            "new": [_import_row(r) for r in sorted(new_rows, key=lambda r: r["date"], reverse=True)[:200]],
            "duplicates": [_import_row(r) for r in sorted(duplicates, key=lambda r: r["date"], reverse=True)[:200]],
            "not_in_file": [_import_row(r) for r in sorted(not_in_file, key=lambda r: r["date"], reverse=True)[:200]],
            # Most of these rows already in another account: probably the wrong account picked.
            "elsewhere": csv_import.matches_elsewhere(conn, account["id"], new_rows),
        }
        if form.get("commit") == "1":
            result["import_id"] = csv_import.save(conn, account, new_rows, upload.filename) if new_rows else None
            result["account_id"] = account["id"]
            result["saved"] = True
            conn.commit()
            result["ai_categorized"] = _auto_categorize(conn, limit=100)
    return jsonify(result)


def _import_row(r):
    return {"date": r["date"], "description": r["description"], "flow": r["flow"], "reason": r.get("reason")}


@app.get("/api/imports")
@login_required
def api_imports():
    with db.connect() as conn:
        return jsonify(imports=queries.imports(conn))


@app.delete("/api/imports/<int:import_id>")
@login_required
def api_undo_import(import_id):
    with db.connect() as conn:
        # Rows a later SimpleFIN sync took over are no longer part of the upload and stay.
        conn.execute("DELETE FROM imports WHERE id = %s", (import_id,))
        conn.commit()
        _relink(conn)
    return jsonify(ok=True)


@app.post("/api/sync")
@login_required
def api_sync():
    with db.connect() as conn:
        if request.args.get("full"):
            # Re-fetch the full history window, e.g. to fill gaps from an earlier sync.
            conn.execute("UPDATE accounts SET history_synced = FALSE")
            conn.commit()
        results = sync_all(conn, today=today())
        return jsonify(results=results, ai_categorized=_auto_categorize(conn))


@app.post("/api/ai-categorize")
@login_required
def api_ai_categorize():
    if not ai_categorize.enabled():
        return jsonify(error="Add ANTHROPIC_API_KEY in Vercel to turn this on."), 400
    with db.connect() as conn:
        return jsonify(ai_categorized=ai_categorize.categorize_unknown(conn))


@app.get("/api/cron/sync")
def api_cron_sync():
    expected = f"Bearer {config.CRON_SECRET}" if config.CRON_SECRET else None
    if not expected or not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        return jsonify(error="Unauthorized"), 401
    with db.connect() as conn:
        results = sync_all(conn, today=today())
        return jsonify(results=results, ai_categorized=_auto_categorize(conn))


@app.errorhandler(413)
def too_large(e):
    return jsonify(error="That file is too big (5 MB max)."), 413


@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=e.description), 400
