## Project Guidelines

This is a copy trading system built in Python.

### Directory Structure
- `src/`: Main source code.
  - `brokers/`: Broker-specific implementations.
  - `core/`: Core copy trading logic.
- `config/`: Configuration files (`config.ini.template`).
- `tests/`: Test files.
- `generate_token.py`: A helper script for one-time user authentication.
- `main.py`: The main entry point for the application.

### Running the System
1.  **Install dependencies:** `pip install -r requirements.txt`
2.  **Configure:** Copy `config/config.ini.template` to `config/config.ini` and fill in your API key and secret.
3.  **Authenticate:** Run `python generate_token.py` once to generate and save the access token for the master account.
4.  **Run:** Execute the main application with `python main.py`.

### Coding Standards
- Follow PEP 8 guidelines.
- Use type hinting.
- Write unit tests for new functionality.
- Ensure all sensitive information is handled via the `config.ini` file and never hard-coded.
- The `.gitignore` file should be used to prevent `config.ini` from being committed.