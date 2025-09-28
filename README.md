# Python Copy Trading System

This is a simple yet powerful copy trading application written in Python. It allows you to replicate trades from a master trading account to one or more child accounts automatically.

The current version supports the **Zerodha (Kite Connect)** broker.

## Features

- **Master-Child Architecture:** Replicates trades from one master account to multiple child accounts.
- **Real-time Synchronization:** Polls the master account for new trades and executes them on child accounts near-instantly.
- **Extensible Broker Support:** Built with an abstract broker interface, making it easy to add support for other brokers in the future (e.g., Dhan).
- **Secure Configuration:** Keeps your sensitive API keys and tokens separate from the source code.

## How It Works

1.  **Configuration:** You provide your broker API credentials in a secure configuration file.
2.  **Authentication:** A helper script guides you through a one-time login process to generate a secure session token.
3.  **Monitoring:** The application continuously monitors the master account's positions for any changes (new positions or changes in quantity).
4.  **Replication:** When a change is detected, the application generates a trade signal and executes the corresponding order on all configured child accounts.

## Getting Started

Follow these steps to set up and run the application.

### 1. Prerequisites

- Python 3.8 or higher
- A Zerodha developer account with API access (`Kite Connect`).

### 2. Installation

First, clone the repository and install the required Python packages:

```bash
git clone <repository-url>
cd <repository-directory>
pip install -r requirements.txt
```

### 3. Configuration

You need to provide your API credentials.

1.  Navigate to the `config/` directory.
2.  Make a copy of the template file: `cp config.ini.template config.ini`
3.  Open `config.ini` with a text editor and fill in the `api_key` and `api_secret` for your **[ZERODHA_MASTER]** account. You can leave the `access_token` field blank for now.
4.  If you have child accounts, create sections like `[ZERODHA_CHILD_1]`, `[ZERODHA_CHILD_2]`, etc., and add their respective `api_key` and `api_secret`.

### 4. Authentication (One-Time Setup)

To use the Zerodha API, you must generate an `access_token`. The included helper script makes this easy.

Run the following command:

```bash
python generate_token.py
```

This will:
1.  Open a Zerodha login page in your web browser.
2.  Prompt you to log in.
3.  After you log in, copy the full URL you are redirected to.
4.  Paste the URL back into the terminal.

The script will then automatically generate your `access_token` and save it to your `config.ini` file.

### 5. Run the Application

Once authentication is complete, you can start the copy trading system:

```bash
python main.py
```

The application will now run continuously, checking for trades on the master account and replicating them to the child accounts. To stop the application, press `Ctrl+C`.