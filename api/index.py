import os
import sys

# Make the repo root importable so `budget` resolves inside the Vercel function.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from budget.app import app  # noqa: E402,F401
