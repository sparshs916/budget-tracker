# Entry point. Vercel's Flask support looks for `app` in app.py; locally run
# `flask run` (or `flask --app app run`).
from budget.app import app  # noqa: F401
