import configparser
import time
import logging
from src.brokers.zerodha_broker import ZerodhaBroker
from src.core.master_account import MasterAccount
from src.core.child_account import ChildAccount

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    """
    Main function to run the copy trading application.
    """
    config = configparser.ConfigParser()
    try:
        # NOTE: The user must rename this file to 'config.ini' and fill in their details.
        config.read_file(open('config/config.ini'))
    except FileNotFoundError:
        logging.error("Configuration file 'config/config.ini' not found. "
                      "Please copy 'config.ini.template' to 'config.ini' and fill it out.")
        return

    # --- Initialize Master Account ---
    try:
        master_api_key = config['ZERODHA_MASTER']['api_key']
        master_api_secret = config['ZERODHA_MASTER']['api_secret']
        master_access_token = config['ZERODHA_MASTER']['access_token']

        if not all([master_api_key, master_api_secret, master_access_token]):
            logging.error("Master account 'api_key', 'api_secret', or 'access_token' is missing in config.ini.")
            return

        master_broker = ZerodhaBroker(
            api_key=master_api_key,
            api_secret=master_api_secret,
            access_token=master_access_token
        )
        master_account = MasterAccount(broker=master_broker)
        logging.info("Master account initialized successfully.")

    except (KeyError, AttributeError) as e:
        logging.error(f"Error initializing master account from config: {e}. "
                      "Ensure [ZERODHA_MASTER] section is correct and complete.")
        return

    # --- Initialize Child Accounts ---
    child_accounts = []
    for section in config.sections():
        if section.startswith('ZERODHA_CHILD'):
            try:
                child_api_key = config[section]['api_key']
                child_api_secret = config[section]['api_secret']
                child_access_token = config[section]['access_token']

                if not all([child_api_key, child_api_secret, child_access_token]):
                    logging.warning(f"Skipping {section} due to missing 'api_key', 'api_secret', or 'access_token'.")
                    continue

                child_broker = ZerodhaBroker(
                    api_key=child_api_key,
                    api_secret=child_api_secret,
                    access_token=child_access_token
                )
                child_account = ChildAccount(broker=child_broker, account_id=section)
                child_accounts.append(child_account)
                logging.info(f"Child account {section} initialized successfully.")

            except (KeyError, AttributeError) as e:
                logging.error(f"Error initializing child account {section}: {e}.")

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