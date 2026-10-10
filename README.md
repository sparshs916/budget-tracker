# budget-tracker

A one-page personal budget, password-protected for a single user. It pulls bank and credit card transactions from [Teller](https://teller.io), sorts them into categories, and shows two things:

- **Budget table**: income, each expense category, total expenses and what remains, for any month or date range. Click a row to filter the transactions to that category.
- **Transactions**: searchable and paginated, filterable by account, account type and category. Change a category inline, for that one transaction or for every transaction from that merchant.

The app is Flask on Vercel with Postgres (Neon) for storage. A Vercel cron syncs once a day, and the **Sync now** button syncs on demand.

## How the math works

Teller signs amounts differently by account type. On checking and savings, money out is negative. On credit cards, a purchase is positive (it raises what you owe) and a payment or refund is negative. Each transaction is stored as `raw_amount` (exactly what Teller sent) and as `flow`, with one convention for every account: **+ is money in, − is money out**.

- **Income** is the sum of `flow` over the income categories.
- **Each expense line** is `−flow` summed over that category, so a refund lowers the category.
- **Credit card payments and transfers between your own accounts** are left out of both totals. The purchases are already counted on the card, so counting the payment from checking as well would double them.
- **Savings** is money moved from checking into savings or investing. It is listed as its own line and included in Total Expenses, like the spreadsheet. Savings accounts start outside "Budget accounts" so the incoming side of that transfer isn't counted too. You can change this under **Accounts**.
- **Pending** transactions count. When one posts, the next sync replaces it.

If credit card purchases show up as income, set `CREDIT_PURCHASES_ARE_POSITIVE=false`.

## Setup

1. **Teller**: in the [Teller dashboard](https://teller.io), copy your **Application ID** and download your **certificate.pem** and **private_key.pem**. Use the `development` environment for real accounts on the free tier.
2. **Secrets**: run this locally:
   ```bash
   pip install -r requirements.txt
   python scripts/setup_secrets.py path/to/certificate.pem path/to/private_key.pem
   ```
   It asks for a login password and prints every secret env var.
3. **Vercel**: import this GitHub repo as a new project. Under **Storage**, add a **Neon Postgres** database; that sets `DATABASE_URL`. Under **Settings → Environment Variables**, add the output of step 2 plus `TELLER_APP_ID` and `TELLER_ENV=development`, then deploy.
4. Open the site, log in, and click **+ Connect account** for each bank.

See `.env.example` for all settings.

## Security notes

- There is one password. Vercel stores only its scrypt hash. After 5 failed logins from one IP, or 20 in total, login locks for 15 minutes.
- The session cookie is signed, HttpOnly, Secure and SameSite=Lax, and lasts 30 days. Every write needs a CSRF token. Changing `APP_PASSWORD_HASH` logs out every session.
- Teller access tokens are encrypted in the database with `TOKEN_ENCRYPTION_KEY`, so a leaked database alone exposes no bank access.
- The cron endpoint only runs when the request carries `CRON_SECRET`.

## Local development

```bash
cp .env.example .env   # fill in
pip install -r requirements.txt
flask run --debug
TEST_DATABASE_URL=postgresql://... pytest   # tests need an empty, throwaway Postgres DB
```
