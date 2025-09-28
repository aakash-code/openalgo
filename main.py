import configparser
import time
import logging
from src.brokers.zerodha_broker import ZerodhaBroker
from src.brokers.dhan_broker import DhanBroker
from src.core.master_account import MasterAccount
from src.core.child_account import ChildAccount

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def initialize_broker(config_section):
    """
    Initializes and returns a broker instance based on the config section name.
    """
    section_name = config_section.name

    if section_name.startswith('ZERODHA'):
        api_key = config_section.get('api_key')
        api_secret = config_section.get('api_secret')
        access_token = config_section.get('access_token')

        if not all([api_key, api_secret, access_token]):
            logging.error(f"Section '{section_name}' is missing api_key, api_secret, or access_token.")
            return None

        return ZerodhaBroker(api_key=api_key, api_secret=api_secret, access_token=access_token)

    elif section_name.startswith('DHAN'):
        client_id = config_section.get('client_id')
        access_token = config_section.get('access_token')

        if not all([client_id, access_token]):
            logging.error(f"Section '{section_name}' is missing client_id or access_token.")
            return None

        return DhanBroker(client_id=client_id, access_token=access_token)

    else:
        logging.warning(f"Unknown broker type for section: {section_name}")
        return None

def main():
    """
    Main function to run the copy trading application.
    """
    config = configparser.ConfigParser()
    try:
        config.read_file(open('config/config.ini'))
    except FileNotFoundError:
        logging.error("Configuration file 'config/config.ini' not found. "
                      "Please copy 'config.ini.template' to 'config.ini' and fill it out.")
        return

    master_account = None
    child_accounts = []

    # --- Initialize Master and Child Accounts ---
    for section_name in config.sections():
        config_section = config[section_name]

        if section_name.endswith('_MASTER'):
            if master_account:
                logging.error("Multiple master accounts defined in config.ini. Only one is allowed.")
                return

            logging.info(f"Initializing master account: {section_name}")
            master_broker = initialize_broker(config_section)
            if master_broker:
                master_account = MasterAccount(broker=master_broker)
                logging.info(f"Master account {section_name} initialized successfully.")

        elif section_name.endswith('_CHILD'):
            logging.info(f"Initializing child account: {section_name}")
            child_broker = initialize_broker(config_section)
            if child_broker:
                child_account = ChildAccount(broker=child_broker, account_id=section_name)
                child_accounts.append(child_account)
                logging.info(f"Child account {section_name} initialized successfully.")

    if not master_account:
        logging.error("No master account was initialized. Please check your config.ini file.")
        return

    if not child_accounts:
        logging.warning("No child accounts were initialized. The application will run but not copy any trades.")

    # --- Main Application Loop ---
    logging.info("Starting copy trading loop...")
    POLLING_INTERVAL = 10  # seconds

    try:
        while True:
            logging.info("Checking for new trades from master account...")

            # 1. Check for new trades from the master account
            new_trade_signals = master_account.check_for_new_trades()

            # 2. If new trades are found, execute them on all child accounts
            if new_trade_signals:
                logging.info(f"Found {len(new_trade_signals)} new trade(s). Replicating to child accounts.")
                for child in child_accounts:
                    child.execute_trades(new_trade_signals)
            else:
                logging.info("No new trades detected.")

            # 3. Wait for the next polling interval
            time.sleep(POLLING_INTERVAL)

    except KeyboardInterrupt:
        logging.info("Application stopped by user.")
    except Exception as e:
        logging.error(f"An unexpected error occurred in the main loop: {e}")

if __name__ == "__main__":
    main()