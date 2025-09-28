import configparser
import logging
import webbrowser
from kiteconnect import KiteConnect

logging.basicConfig(level=logging.INFO)

def generate_zerodha_session():
    """
    Guides the user through the Zerodha login flow to generate and
    save an access token in the configuration file.
    """
    config_path = 'config/config.ini'
    config = configparser.ConfigParser()

    try:
        config.read_file(open(config_path))
    except FileNotFoundError:
        logging.error(f"Configuration file '{config_path}' not found. "
                      "Please copy 'config.ini.template' to 'config.ini' and fill it out.")
        return

    try:
        api_key = config['ZERODHA_MASTER']['api_key']
        api_secret = config['ZERODHA_MASTER']['api_secret']
    except KeyError:
        logging.error("'api_key' or 'api_secret' not found in the [ZERODHA_MASTER] "
                      "section of your config.ini file.")
        return

    if not api_key or not api_secret:
        logging.error("API key or secret is missing from the [ZERODHA_MASTER] section.")
        return

    # --- Step 1: Generate Login URL ---
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    logging.info("Please follow these steps to authenticate:")
    logging.info(f"1. A login page will now open in your web browser.")
    logging.info(f"2. Log in with your Zerodha credentials.")
    logging.info(f"3. After logging in, you will be redirected to a blank page.")
    logging.info(f"4. Copy the FULL URL from your browser's address bar.")

    webbrowser.open(login_url)

    # --- Step 2: Get Request Token from User ---
    try:
        redirect_url = input("\nPaste the full redirect URL here and press Enter: ")
        request_token = redirect_url.split('request_token=')[1].split('&')[0]
    except (IndexError, KeyboardInterrupt):
        logging.error("\nCould not extract request_token. Please try again.")
        return

    logging.info(f"Request token received: {request_token}")

    # --- Step 3: Generate and Save Access Token ---
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get("access_token")

        if not access_token:
            logging.error("Failed to generate access token. Response: %s", data)
            return

        # Update the configuration file
        config.set('ZERODHA_MASTER', 'access_token', access_token)
        with open(config_path, 'w') as configfile:
            config.write(configfile)

        logging.info("\nSUCCESS! Your access token has been generated and saved to config/config.ini")
        logging.info("You can now run the main application using: python main.py")

    except Exception as e:
        logging.error(f"Authentication failed: {e}")

if __name__ == "__main__":
    generate_zerodha_session()