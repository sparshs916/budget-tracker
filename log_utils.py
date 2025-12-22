import logging

def log_error(error):
    return logging.error(error)

def log_info(info):
    return logging.info(info)

def log_account_info(bank,account, i):
    logging.info(f"\n{bank} Account:" \
                f"\n Account Name: {account['name']}" \
                f"\n Account ID: {account['id']}" \
                f"\n Type: {account['subtype']}" \
                f"\n Last Four: {account['last_four']}\n" \
                f"\n Links: {account['links']}\n")