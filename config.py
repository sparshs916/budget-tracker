import logging
from pathlib import Path
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set global variables
## APP ID
APP_ID= os.getenv("_APP_ID")
## Automatically resolve base directory
BASE_DIR = Path(__file__).resolve().parent
CERTS = (
    str(BASE_DIR / os.getenv("_CERT_PATH")),
    str(BASE_DIR / os.getenv("_PRIVATE_KEY_PATH"))
)

BANK1 = os.getenv("_BANK1")
BANK2 = os.getenv("_BANK2")
BANK1_TOKEN = os.getenv("_BANK1_TOKEN")
BANK2_TOKEN = os.getenv("_BANK2_TOKEN")

## Bank token dict
BANK_TOKENS = {
    BANK1: BANK1_TOKEN,
    BANK2: BANK2_TOKEN,
}

## Teller API base url
BASE_URL = "https://api.teller.io"

## Create "data" directory to store all data. (i.e. response data)
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ACCOUNT_FILE = DATA_DIR / "all_accounts.json"
TRANSACTIONS_FILE = DATA_DIR / "all_transactions.json"
TRANSACTIONS = "transactions"
BALANCES = "balances"
LOG_FILE="budget_tracker.log"

## Set logging 
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

