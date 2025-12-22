import requests
from pprint import pprint 
import os
import json
from logging import *
from log_utils import *
from config import *
from utils import *
from pathlib import Path

class TellerAPI:
    ## Private methods
    @staticmethod 
    def _get_bank_token(bank):
        bank = bank.lower()
        token = BANK_TOKENS.get(bank)
        if not token:
            log_error(f"Bank token for {bank} not found")
        return token
    
    @staticmethod
    def _get_transaction_total(datum):
        total = 0.0
        i = 0
        for data in datum:
            amount = float(data['amount'])
            i+=1
            if data['type'] == 'withdrawal':
                # Convert to float and use absolute value
                log_info(f"transaction {i}: {amount}")
                total += abs(amount)
            elif data['type'] == 'card_payment':
                total += abs(amount)
            else:
                continue
        return total

    @staticmethod
    def _populate_accounts(bank,account_data):
        """
        populate JSON account data into dict
        """
        account_list = list()
        i = 0
        for account in account_data:
            i += 1
            account_dict = {}
            account_dict['account_number'] = i
            account_dict['account_id'] = account['id']
            account_dict['enrollment_id'] = account['enrollment_id']
            account_dict['name'] = account['name']
            account_dict['account_type'] = account['subtype']
            account_dict['last_four'] = account['last_four']
            account_dict['links'] = account['links']
            account_list.append(account_dict)
        return account_list

    def _fetch_data(self, url, start_date=None, end_date=None):
        try:
            params = {}
            if start_date:
                if hasattr(start_date, 'isoformat'):
                    params['start_date'] = start_date.isoformat() 
                else:
                    params['start_date'] = start_date
            if end_date:
                if hasattr(end_date, 'isoformat'):
                    params['end_date'] = end_date.isoformat() 
                else:
                    params['end_date'] = end_date

            response = requests.get(url, 
                auth=(self.token, ''),
                params=params,
                cert=CERTS)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log_error(f"Fetching data failed for {self.bank}: {e}")
            return []
    
    def _fetch_all_data_for_key(self,url,key):
        try:
            response = requests.get(url, auth=(self.token, ''), 
                cert=CERTS)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            log_error(f"Fetching \"{key}\" data failed for {self.bank}: {e}")
            return []

    def _get_all_data_for_key(self,key):
        """
        return a list of json objects containing data pertaining to the key
        """
        accounts = self.accounts
        if not accounts:
            log_error(f"account data NULL for {self.bank}")
            return
        
        key_data = list()
        for i in range(len(accounts)):
            # Check if the key exists in links before accessing
            if key not in accounts[i]["links"]:
                log_info(f"no {key} for {accounts[i]['name']}")
                continue
            url = accounts[i]["links"][key]

            if not url:
                log_info("url is not valid")
                continue

            key_data = self._fetch_all_data_for_key(url, key)
            if not key_data:
                log_error(f"{key} data not found for {self.bank}")
                continue
        return key_data

    def _get_data_for_key(self, key, start_date=None, end_date=None):
        """
        Get Data for a key by start and/or end date from ALL accounts
        """
        if not self.accounts:
            log_error(f"Account NULL for {self.bank}")
            return
        elif not start_date or not end_date:
            log_error(f"No start or end date specified when gathering {key} for {self.bank}")
            return
        
        all_data = []
        for account_data in self.accounts:
            url = account_data["links"].get(key)
            if not url:
                log_info(f"No {key} URL for account {account_data['account_id']}")
                continue
            
            data = self._fetch_data(url,
                                    start_date=start_date,
                                    end_date=end_date)
            if data:
                if isinstance(data, list):
                    all_data.extend(data)
                else:
                    all_data.append(data)
            else:
                log_error(f"Failed to fetch {key} for account {account_data['account_id']}")
        
        return all_data if all_data else None
        
    def _fetch_accounts_if_needed(self):
        """
        To avoid unnecessary API requests, first try to fetch account data from the local cache.
        If the account data for this bank does not exist, fetch it and append to the cache file.
        
        Cache format: {"chase": [...accounts...], "golden1": [...accounts...]}
        """
        filepath = Path(os.path.join(DATA_DIR, "accounts.json"))
        all_accounts = {}
        
        # Load existing cache if it exists
        if filepath.exists():
            try:
                with open(filepath, "r") as file:
                    loaded_data = json.load(file)
                    if isinstance(loaded_data, list):
                        log_info("Converting old account cache format to new multi-bank format")
                        all_accounts = {}
                    elif isinstance(loaded_data, dict):
                        all_accounts = loaded_data
                    else:
                        log_error("Unexpected cache format, starting fresh")
                        all_accounts = {}
                log_info(f"Loaded accounts cache from {filepath}")
            except Exception as e:
                log_error(f"Error reading cache file: {e}")
                all_accounts = {}
        
        # Check if this bank's data is already cached
        bank = self.bank.lower()
        if bank in all_accounts and all_accounts[bank]:
            log_info(f"Using cached accounts for {bank}")
            accounts = self._populate_accounts(bank, all_accounts[bank])
            return accounts
        
        # Bank not in cache, fetch from API
        log_info(f"Fetching new account data for {bank}")
        accounts_url = BASE_URL + "/accounts"
        account_data = self._fetch_data(accounts_url)
        
        if not account_data:
            log_error(f"Account data is invalid for {bank}")
            return []
        
        # Add this bank's data to the cache
        all_accounts[bank] = account_data
        
        # Write updated cache back to file
        try:
            with open(filepath, "w") as file:
                json.dump(all_accounts, file, indent=2)
            log_info(f"Cached accounts for {bank} to {filepath}")
        except Exception as e:
            log_error(f"Failed to cache accounts: {e}")
        
        # Populate and return this bank's accounts
        accounts = self._populate_accounts(bank, account_data)
        return accounts

    def __init__(self, bank):
        self.bank = bank
        # Call the static helper
        self.token = self._get_bank_token(bank)
        # Fetch and store the accounts for this bank
        self.accounts = self._fetch_accounts_if_needed()

    ## Public methods
    def get_transactions(self, start_date=None, end_date=None):
        return self._get_data_for_key("transactions", start_date=start_date, end_date=end_date)
    
    def get_balances(self, start_date=None, end_date=None):
        return self._get_data_for_key("balances", start_date=start_date, end_date=end_date)
    
    def get_payments(self, start_date=None, end_date=None):
        return self._get_data_for_key("payments", start_date=start_date, end_date=end_date)

    def get_transactions_total(self,start_date=None, end_date=None):
        transactions_data = self.get_transactions(start_date,end_date)
        return self._get_transaction_total(transactions_data)
        