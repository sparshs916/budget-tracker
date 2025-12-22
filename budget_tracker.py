from teller_api import *
from utils import * 
import datetime
from config import *

def main():
    bank1 = TellerAPI(BANK1)
    bank2 = TellerAPI(BANK2)
    
    today = datetime.date.today()
    start_of_month, end_of_month = get_month_range(today)
    bank1_total = bank1.get_transactions_total(start_date=start_of_month,end_date=end_of_month)
    bank2_total = bank2.get_transactions_total(start_date=start_of_month,end_date=end_of_month)

    print(f"Total spent at {bank1.bank} from {start_of_month} to {end_of_month} was {bank1_total}")
    print(f"Total spent at {bank2.bank} from {start_of_month} to {end_of_month} was {bank2_total}")


if __name__ == "__main__":
    main()
