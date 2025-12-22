import datetime
import calendar

def get_month_range(date_obj):
    """
    For a given date object, returns the start and end date for that month.
    Returns a tuple of datetime.date objects (first_day, last_day).
    """
    # Get the first day of the month by replacing the day with 1
    first_day = date_obj.replace(day=1)

    # Use calendar.monthrange to get the number of days in the month
    # monthrange returns a tuple (weekday of first day, number of days in month)
    _, num_days = calendar.monthrange(date_obj.year, date_obj.month)

    # Get the last day of the month by replacing the day with num_days
    last_day = date_obj.replace(day=num_days)

    return first_day, last_day

# Example usage for the current month
today = datetime.date.today()
start_of_month, end_of_month = get_month_range(today)

print(f"Start of the month: {start_of_month}")
print(f"End of the month: {end_of_month}")

