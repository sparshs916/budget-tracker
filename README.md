# budget-tracker

A one-page personal budget, password-protected for a single user. It pulls bank and credit card transactions from [SimpleFIN Bridge](https://beta-bridge.simplefin.org) ($15/year), sorts them into categories, and shows two things:

- **Budget table**: income, each expense category, total expenses and what remains, for any month or date range. Click a row to filter the transactions to that category.
- **Transactions**: searchable and paginated, filterable by account, account type and category. Change a category inline, for that one transaction or for every transaction from that merchant.

The app is Flask on Vercel with Postgres (Neon) for storage. A Vercel cron syncs once a day, and the **Sync now** button syncs on demand.

## How the math works

SimpleFIN uses one sign convention for every account: positive means money came into the account, so a card purchase is negative and a card payment is positive. Each transaction is stored as `raw_amount` (exactly what SimpleFIN sent) and as `flow`, where **+ is money in and − is money out**.

- **Income** is the sum of `flow` over the income categories.
- **Each expense line** is `−flow` summed over that category, so a refund lowers the category.
- **Credit card payments and transfers between your own accounts** are left out of both totals. The purchases are already counted on the card, so counting the payment from checking as well would double them.
- **Savings** is money moved from checking into savings or investing. It is listed as its own line and included in Total Expenses, like the spreadsheet. Savings accounts start outside "Budget accounts" so the incoming side of that transfer isn't counted too. You can change this under **Accounts**.
- **Pending** transactions count. When one posts, the next sync replaces it.

SimpleFIN doesn't say whether an account is checking, savings or a credit card, so the app guesses from the account name and balance. The type decides how card payments and transfers are counted, so fix any wrong guess in the **Accounts** panel; your choice sticks across syncs. If card purchases still show up as income, set `INVERT_CREDIT_AMOUNTS=true`.

## Setup

1. **Secrets**: run this locally. It asks for a login password and appends the secret env vars to `.env`:
   ```bash
   pip install -r requirements.txt
   python scripts/setup_secrets.py >> .env
   ```
2. **Vercel**: import this GitHub repo as a new project (Framework Preset: Flask). Under **Storage**, add a **Neon Postgres** database; that sets `DATABASE_URL`. Under **Settings → Environment Variables**, add the four values from step 1, then deploy.
3. **SimpleFIN**: sign up at [SimpleFIN Bridge](https://beta-bridge.simplefin.org) and link your banks there. Then create a new app connection there and copy its setup token.
4. Open the site, log in, click **+ Connect account** and paste the setup token. Each token works once; the app stores the resulting access URL encrypted.

See `.env.example` for all settings.

## CSV uploads

For banks SimpleFIN can't reach (or accounts it only sends balances for), download a CSV from the bank's website and use **Upload CSV**. Pick the account, or create a new one, preview, then import. Every upload can be undone under **Recent uploads**.

Duplicates are skipped two ways:

- Each row gets a fingerprint from its account, date, amount, description and position among identical rows, so re-uploading a file or uploading overlapping date ranges adds nothing twice. Two genuinely identical rows in one file are both kept.
- A row that matches a transaction the account already has (same amount, within 3 days, paired one-to-one, closest date first) is skipped, whatever the description says. When SimpleFIN later sends a transaction that was uploaded, the sync takes over that row instead of adding a second one, and the sync never deletes uploaded rows.

Card exports that list purchases as positive numbers are detected and flipped; the preview shows the signs so you can check before importing.

## AI categories (optional)

Set `ANTHROPIC_API_KEY` and the app asks Claude (`claude-haiku-5-5`, low effort) to categorize merchants the keyword rules leave as **Other**, after each sync and upload, or on demand with **Categorize "Other" with AI** under the budget table. Only the merchant text, the direction of the money, and whether it was a card or bank account are sent; no amounts, dates, names or account numbers. Each merchant is asked about once and the answer is saved. Suggestions only fill in where the rules said Other, are marked "AI" in the list, and never override a category you chose. A batch Claude declines is retried on the next sync.

## Security notes

- There is one password. Vercel stores only its scrypt hash. After 5 failed logins from one IP, or 20 in total, login locks for 15 minutes.
- The session cookie is signed, HttpOnly, Secure and SameSite=Lax, and lasts 30 days. Every write needs a CSRF token. Changing `APP_PASSWORD_HASH` logs out every session.
- The SimpleFIN access URL is encrypted in the database with `TOKEN_ENCRYPTION_KEY`, so a leaked database alone exposes no bank data. SimpleFIN access is read-only: it cannot move money.
- The cron endpoint only runs when the request carries `CRON_SECRET`.

## Local development

```bash
cp .env.example .env   # fill in
pip install -r requirements.txt
flask run --debug
TEST_DATABASE_URL=postgresql://... pytest   # tests need an empty, throwaway Postgres DB
```
