# Python Copy Trading System

This is a simple yet powerful copy trading application written in Python. It allows you to replicate trades from a master trading account to one or more child accounts automatically.

The current version supports **Zerodha (Kite Connect)** and **Dhan (DhanHQ)**.

## Features

- **Master-Child Architecture:** Replicates trades from one master account to multiple child accounts.
- **Multi-Broker Support:** Configure master and child accounts from different supported brokers (e.g., a Zerodha master and Dhan children).
- **Real-time Synchronization:** Polls the master account for new trades and executes them on child accounts near-instantly.
- **Extensible Broker Support:** Built with an abstract broker interface, making it easy to add more brokers.
- **Secure Configuration:** Keeps your sensitive API keys and tokens separate from the source code.

## How It Works

1.  **Configuration:** You provide your broker API credentials in a secure `config.ini` file.
2.  **Authentication:** For first-time setup, you generate the necessary access tokens for your accounts.
3.  **Monitoring:** The application continuously monitors the master account's positions for any changes.
4.  **Replication:** When a change is detected, the application generates a trade signal and executes the corresponding order on all configured child accounts, regardless of their broker.

## Getting Started

Follow these steps to set up and run the application.

### 1. Prerequisites

- Python 3.8 or higher
- A developer account with API access for your chosen broker(s) (e.g., Zerodha Kite Connect, DhanHQ).

### 2. Installation

First, clone the repository and install the required Python packages:

```bash
git clone <repository-url>
cd <repository-directory>
pip install -r requirements.txt
```

### 3. Configuration

You need to provide your API credentials in a single configuration file.

1.  Navigate to the `config/` directory.
2.  Make a copy of the template file: `cp config.ini.template config.ini`
3.  Open `config.ini` with a text editor.

You must define **one** master account and one or more child accounts using the following naming convention:
-   Master account section must end with `_MASTER` (e.g., `[ZERODHA_MASTER]`).
-   Child account sections must end with `_CHILD` (e.g., `[DHAN_CHILD_1]`).

#### For Zerodha Accounts:
-   Section Name: `[ZERODHA_MASTER]` or `[ZERODHA_CHILD_1]`
-   `api_key`: Your Kite Connect API key.
-   `api_secret`: Your Kite Connect API secret.
-   `access_token`: Leave this blank initially.

#### For Dhan Accounts:
-   Section Name: `[DHAN_MASTER]` or `[DHAN_CHILD_1]`
-   `client_id`: Your DhanHQ client ID.
-   `access_token`: Your DhanHQ access token. (See Authentication section below).


### 4. Authentication

Authentication methods differ by broker.

#### For Zerodha (Master Account Only):
The system needs to generate a long-lived `access_token`. A helper script is provided for this one-time setup.

Run the following command:
```bash
python generate_token.py
```
This script will open a Zerodha login page in your browser. After you log in, copy the full redirect URL from your browser's address bar and paste it into the terminal. The script will automatically generate and save the `access_token` to your `config.ini` file.

*Note: This script only needs to be run for the `[ZERODHA_MASTER]` account.*

#### For Dhan:
Dhan provides a permanent `access_token` directly from their developer portal. You must copy this token and paste it directly into the `access_token` field for your Dhan account(s) in the `config.ini` file.

### 5. Run the Application

Once authentication is complete, you can start the copy trading system:

```bash
python main.py
```

The application will now run continuously, checking for trades on the master account and replicating them to the child accounts. To stop the application, press `Ctrl+C`.