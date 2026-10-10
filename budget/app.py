import datetime
import hashlib
import hmac
import logging
import secrets
from decimal import Decimal
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask.json.provider import DefaultJSONProvider
from werkzeug.security import check_password_hash

from . import config, db, queries
from .categorize import CATEGORIES, CATEGORY_NAMES
from .simplefin import SimpleFINError, claim_setup_token
from .sync import SUBTYPES, recategorize_account, save_connection, sync_all, sync_enrollment

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
)

MAX_FAILURES_PER_IP = 5
MAX_FAILURES_TOTAL = 20
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


@app.get("/api/accounts")
@login_required
def api_accounts():
    with db.connect() as conn:
        return jsonify(accounts=queries.accounts(conn), enrollments=queries.enrollments(conn))


@app.post("/api/accounts/<account_id>")
@login_required
def api_update_account(account_id):
    body = request.get_json(force=True)
    with db.connect() as conn:
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
        if "include_in_budget" in body:
            conn.execute("UPDATE accounts SET include_in_budget = %s WHERE id = %s",
                         (bool(body["include_in_budget"]), account_id))
    return jsonify(ok=True)


@app.post("/api/transactions/<txn_id>/category")
@login_required
def api_set_category(txn_id):
    body = request.get_json(force=True)
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
    return jsonify(ok=True)


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


@app.post("/api/sync")
@login_required
def api_sync():
    with db.connect() as conn:
        return jsonify(results=sync_all(conn, today=today()))


@app.get("/api/cron/sync")
def api_cron_sync():
    expected = f"Bearer {config.CRON_SECRET}" if config.CRON_SECRET else None
    if not expected or not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        return jsonify(error="Unauthorized"), 401
    with db.connect() as conn:
        return jsonify(results=sync_all(conn, today=today()))


@app.errorhandler(400)
def bad_request(e):
    return jsonify(error=e.description), 400
