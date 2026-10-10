"""Ask Claude to categorize merchants the keyword rules left as "Other".

Only the merchant text, whether money went in or out, and whether it was a
card or a bank account are sent - no amounts, dates, names or account numbers.
Answers are saved per merchant, so each merchant is asked about once.
"""
import json
import logging

import anthropic

from . import config
from .categorize import CATEGORY_NAMES

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
BATCH_SIZE = 100

SYSTEM = """You categorize transactions for a personal budget app. Each item is one merchant as it appears \
on a bank or credit card statement, with the direction of the money and the kind of account.

For each item, choose exactly one category from the allowed list:
- Use what the merchant is (a restaurant, a church, a hotel, a pharmacy...) from its name and wording. \
Statement text is often abbreviated or prefixed by a payment processor (SQ *, TST*, PAYPAL *, SP ...); look past that.
- "Money in" on a credit card is usually a refund in the same category as the purchase, unless it is a payment.
- Use "Credit Card Payment" or "Transfer" only for money moving between the person's own accounts.
- Use "Income" only for paychecks; other money in that isn't a refund is "Other Income".
- If you can't tell what the merchant is, answer "Other". A wrong guess is worse than "Other"."""

OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "category": {"type": "string", "enum": CATEGORY_NAMES},
                    },
                    "required": ["id", "category"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    },
}


def enabled():
    return bool(config.ANTHROPIC_API_KEY)


def ask_claude(items):
    """items: [{"text", "direction", "account"}] -> {index: category}. Empty on refusal."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=2, timeout=120)
    listing = "\n".join(f'{i}. "{it["text"]}" ({it["direction"]}, {it["account"]})' for i, it in enumerate(items))
    # Claude Haiku 5.5 has no server-side fallback; a declined batch is retried on the next sync.
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "low", "format": OUTPUT_FORMAT},
        messages=[{"role": "user", "content": f"Categorize these {len(items)} merchants:\n{listing}"}],
    )
    if response.stop_reason == "refusal":
        log.warning("Claude declined to categorize a batch")
        return {}
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        results = json.loads(text)["results"]
    except (ValueError, KeyError, TypeError):
        log.warning("Couldn't read Claude's categories: %r", text[:200])
        return {}
    return {r["id"]: r["category"] for r in results
            if isinstance(r.get("id"), int) and 0 <= r["id"] < len(items) and r.get("category") in CATEGORY_NAMES}


def categorize_unknown(conn, limit=300, ask=None):
    """Fill in categories for merchants still in Other. Returns how many merchants got one."""
    if not enabled() and ask is None:
        return 0
    ask = ask or ask_claude
    pending = conn.execute(
        """
        SELECT t.merchant_key, CASE WHEN t.flow < 0 THEN 'out' ELSE 'in' END AS direction,
               MIN(COALESCE(t.merchant, t.description)) AS text, bool_or(a.type = 'credit') AS credit
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN merchant_rules r ON r.merchant_key = t.merchant_key
        LEFT JOIN ai_categories ai ON ai.merchant_key = t.merchant_key
                                  AND ai.direction = CASE WHEN t.flow < 0 THEN 'out' ELSE 'in' END
        WHERE t.auto_category IN ('Other', 'Other Income') AND t.override_category IS NULL AND t.pair_category IS NULL
          AND r.category IS NULL AND ai.merchant_key IS NULL AND t.merchant_key IS NOT NULL
        GROUP BY 1, 2
        ORDER BY COUNT(*) DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()

    found = 0
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        items = [{"text": (p["text"] or "")[:120], "direction": "money " + p["direction"],
                  "account": "credit card" if p["credit"] else "bank account"} for p in batch]
        try:
            answers = ask(items)
        except anthropic.APIError as e:
            log.warning("AI categorization failed: %s", e)
            break
        for i, p in enumerate(batch):
            category = answers.get(i)
            if category is None:
                continue  # not answered; try again next time
            conn.execute(
                """INSERT INTO ai_categories (merchant_key, direction, category, model) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (merchant_key, direction) DO UPDATE SET category = EXCLUDED.category""",
                (p["merchant_key"], p["direction"], category, MODEL),
            )
            if category not in ("Other", "Other Income"):
                found += 1
        conn.commit()
    return found
