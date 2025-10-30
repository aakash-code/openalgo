# Migration Guide: From Direct Broker APIs to OpenAlgo

## Overview

This guide helps you migrate your trading strategies from direct broker integrations (Fyers, Zerodha, Upstox, etc.) to the OpenAlgo unified platform.

## Why Migrate to OpenAlgo?

### Problems with Direct Broker Integration
- ❌ TOTP authentication complexity
- ❌ Different API for each broker
- ❌ Code rewrite when switching brokers
- ❌ Inconsistent WebSocket implementations
- ❌ Manual error handling for each broker
- ❌ No built-in testing framework
- ❌ Complex order management
- ❌ Broker-specific symbol formats

### Benefits of OpenAlgo
- ✅ Single API key authentication
- ✅ One API for 23+ brokers
- ✅ Switch brokers without code changes
- ✅ Unified WebSocket layer
- ✅ Built-in error handling
- ✅ API Analyzer for safe testing
- ✅ Smart order management
- ✅ Unified symbol format

## Migration Process

### Step 1: Identify Components to Migrate

Review your existing strategy and identify these components:

1. **Authentication Code** → Remove, replace with API key
2. **Order Placement** → Replace with OpenAlgo API
3. **Market Data** → Replace with OpenAlgo WebSocket
4. **Position Management** → Replace with OpenAlgo position APIs
5. **Order Management** → Replace with OpenAlgo order APIs
6. **Symbol Handling** → Update to OpenAlgo format

### Step 2: Component-by-Component Migration

#### Authentication Migration

**Before (Fyers)**:
```python
from fyers_api import fyersModel
import pyotp

# Complex TOTP authentication
client_id = "YOUR_CLIENT_ID"
secret_key = "YOUR_SECRET_KEY"
totp_secret = "YOUR_TOTP_SECRET"

totp = pyotp.TOTP(totp_secret)
totp_code = totp.now()

# Generate access token
session = fyersModel.SessionModel(client_id, secret_key, totp_code)
response = session.generate_authcode()
access_token = response["access_token"]

fyers = fyersModel.FyersModel(client_id=client_id, token=access_token)
```

**After (OpenAlgo)**:
```python
from openalgo import api

# Simple API key authentication
client = api(
    api_key="your-openalgo-api-key",
    host="http://127.0.0.1:5000"
)
```

#### Order Placement Migration

**Before (Zerodha)**:
```python
from kiteconnect import KiteConnect

kite = KiteConnect(api_key="your_api_key")
kite.set_access_token(access_token)

# Place order
order_id = kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange=kite.EXCHANGE_NFO,
    tradingsymbol="NIFTY25JAN3024000PE",
    transaction_type=kite.TRANSACTION_TYPE_SELL,
    quantity=50,
    product=kite.PRODUCT_MIS,
    order_type=kite.ORDER_TYPE_MARKET
)
```

**After (OpenAlgo)**:
```python
from openalgo import api

client = api(api_key="your-key", host="http://127.0.0.1:5000")

# Place order - broker agnostic
response = client.placeorder(
    strategy="My Strategy",
    symbol="NIFTY25JAN3024000PE",
    action="SELL",
    exchange="NFO",
    price_type="MARKET",
    product="MIS",
    quantity=50
)
order_id = response.get('orderid')
```

#### Market Data Migration

**Before (Upstox WebSocket)**:
```python
import upstox_client
from upstox_client.rest import ApiException

# Complex WebSocket setup
configuration = upstox_client.Configuration()
configuration.access_token = access_token

streamer = MarketDataStreamer(
    upstox_client.ApiClient(configuration),
    ["NFO|NIFTY25JAN3024000PE"]
)

def on_message(message):
    data = json.loads(message)
    ltp = data['ltp']
    # Process data

streamer.on("message", on_message)
streamer.connect()
```

**After (OpenAlgo)**:
```python
from openalgo import api

client = api(
    api_key="your-key",
    host="http://127.0.0.1:5000",
    ws_url="ws://127.0.0.1:8765"
)

# Unified WebSocket
instruments = [
    {"exchange": "NFO", "symbol": "NIFTY25JAN3024000PE"}
]

def on_quote_update(data):
    ltp = data.get('ltp')
    # Process data

client.connect()
client.subscribe_quote(instruments, on_data_received=on_quote_update)
```

#### Position Management Migration

**Before (Fyers)**:
```python
# Get positions
response = fyers.positions()
positions = response['netPositions']

for pos in positions:
    symbol = pos['symbol']
    qty = pos['netQty']
    pnl = pos['pl']
    # Process position
```

**After (OpenAlgo)**:
```python
# Get positions - unified format
response = client.positionbook()

if response.get('status') == 'success':
    for pos in response['data']:
        symbol = pos['symbol']
        qty = int(pos['quantity'])
        pnl = float(pos['pnl'])
        # Process position
```

#### Exit Positions Migration

**Before (Multiple broker-specific calls)**:
```python
# Fyers - close position
fyers.exit_positions(id="position_id")

# Zerodha - place opposite order
kite.place_order(
    variety=kite.VARIETY_REGULAR,
    exchange=kite.EXCHANGE_NFO,
    tradingsymbol=symbol,
    transaction_type=kite.TRANSACTION_TYPE_BUY,  # Opposite of entry
    quantity=qty,
    product=kite.PRODUCT_MIS,
    order_type=kite.ORDER_TYPE_MARKET
)
```

**After (OpenAlgo)**:
```python
# Close all positions with one call
response = client.closeposition(strategy="My Strategy")

# Or close specific position
response = client.placeorder(
    strategy="My Strategy",
    symbol=symbol,
    action="BUY",  # Opposite of entry
    exchange="NFO",
    price_type="MARKET",
    product="MIS",
    quantity=qty
)
```

### Step 3: Symbol Format Conversion

| Broker | Format | OpenAlgo Format |
|--------|--------|-----------------|
| Fyers | NSE:NIFTY25JAN24000PE | NIFTY25JAN3024000PE |
| Zerodha | NIFTY25JAN24000PE | NIFTY25JAN3024000PE |
| Upstox | NIFTY25JAN24000PE | NIFTY25JAN3024000PE |
| Angel | NIFTY25JAN24000PE | NIFTY25JAN3024000PE |

**Note**: OpenAlgo uses a consistent format across all brokers. Symbol mapping is handled automatically based on your active broker.

### Step 4: Configuration Migration

**Before (.env file)**:
```env
# Broker-specific credentials
FYERS_CLIENT_ID=xxxxx
FYERS_SECRET_KEY=xxxxx
FYERS_TOTP_SECRET=xxxxx
FYERS_ACCESS_TOKEN=xxxxx

# Strategy parameters
SYMBOL=NIFTY25JAN30
PE_GAP=25
CE_GAP=25
```

**After (OpenAlgo config)**:
```env
# Single API key
OPENALGO_API_KEY=your-api-key
OPENALGO_HOST=http://127.0.0.1:5000
OPENALGO_WS_URL=ws://127.0.0.1:8765

# Strategy parameters (unchanged)
SYMBOL_INITIALS=NIFTY25JAN30
PE_GAP=25
CE_GAP=25
```

### Step 5: Error Handling

**Before (Broker-specific errors)**:
```python
try:
    order_id = fyers.place_order(...)
except Exception as e:
    if "token expired" in str(e):
        # Re-authenticate
        regenerate_token()
    elif "insufficient funds" in str(e):
        # Handle insufficient funds
        pass
    # Many broker-specific error cases
```

**After (Unified error handling)**:
```python
response = client.placeorder(...)

if response.get('status') == 'success':
    order_id = response['orderid']
    print(f"Order placed: {order_id}")
else:
    error_msg = response.get('message', 'Unknown error')
    print(f"Order failed: {error_msg}")
    # Handle error uniformly
```

## Complete Example Migration

### Original Strategy (Fyers)

```python
# old_survivor_strategy.py (Fyers version)
from fyers_api import fyersModel, accessToken
from fyers_api.Websocket import ws
import pyotp
import time

# Authentication
client_id = os.getenv("FYERS_CLIENT_ID")
totp_secret = os.getenv("FYERS_TOTP_SECRET")

totp = pyotp.TOTP(totp_secret)
session = accessToken.SessionModel(
    client_id=client_id,
    secret_key=os.getenv("FYERS_SECRET_KEY"),
    redirect_uri="https://example.com",
    response_type='code',
    grant_type='authorization_code'
)

access_token = session.generate_token()
fyers = fyersModel.FyersModel(client_id=client_id, token=access_token)

# Get underlying LTP
quote = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX"})
nifty_ltp = quote['d'][0]['v']['lp']

# Calculate strikes
atm = round(nifty_ltp / 50) * 50
pe_strike = atm - 25
ce_strike = atm + 25

# Construct symbols (Fyers format)
pe_symbol = f"NSE:NIFTY25JAN{pe_strike}PE"
ce_symbol = f"NSE:NIFTY25JAN{ce_strike}CE"

# Get premiums
pe_quote = fyers.quotes({"symbols": pe_symbol})
pe_premium = pe_quote['d'][0]['v']['lp']

ce_quote = fyers.quotes({"symbols": ce_symbol})
ce_premium = ce_quote['d'][0]['v']['lp']

# Place orders
if pe_premium > 15 and ce_premium > 15:
    # Sell PE
    pe_order = fyers.place_order({
        "symbol": pe_symbol,
        "qty": 50,
        "type": 2,  # Market
        "side": -1,  # Sell
        "productType": "INTRADAY"
    })

    # Sell CE
    ce_order = fyers.place_order({
        "symbol": ce_symbol,
        "qty": 50,
        "type": 2,
        "side": -1,
        "productType": "INTRADAY"
    })

# WebSocket for monitoring
data_type = "symbolData"
symbol_list = [pe_symbol, ce_symbol]

def onmessage(message):
    data = json.loads(message)
    # Process updates
    pass

def onerror(message):
    print("Error:", message)

def onclose(message):
    print("Connection closed")

def onopen():
    fyers_ws.subscribe(symbol_list)

fyers_ws = ws.FyersSocket(
    access_token=access_token,
    run_background=False,
    log_path=""
)

fyers_ws.websocket_data = onmessage
fyers_ws.on_error = onerror
fyers_ws.on_close = onclose
fyers_ws.on_open = onopen

fyers_ws.connect()
```

### Migrated Strategy (OpenAlgo)

```python
# survivor_strategy_openalgo.py (OpenAlgo version)
from openalgo import api
import time
import os

# Simple authentication
client = api(
    api_key=os.getenv("OPENALGO_API_KEY"),
    host="http://127.0.0.1:5000",
    ws_url="ws://127.0.0.1:8765"
)

# Get underlying LTP
response = client.quotes(symbol="NIFTY", exchange="NSE_INDEX")
nifty_ltp = response['data']['ltp']

# Calculate strikes (same logic)
atm = round(nifty_ltp / 50) * 50
pe_strike = atm - 25
ce_strike = atm + 25

# Construct symbols (OpenAlgo format - broker agnostic)
pe_symbol = f"NIFTY25JAN{pe_strike}PE"
ce_symbol = f"NIFTY25JAN{ce_strike}CE"

# Get premiums
pe_response = client.quotes(symbol=pe_symbol, exchange="NFO")
pe_premium = pe_response['data']['ltp']

ce_response = client.quotes(symbol=ce_symbol, exchange="NFO")
ce_premium = ce_response['data']['ltp']

# Place orders (unified API)
if pe_premium > 15 and ce_premium > 15:
    # Sell PE
    pe_order = client.placeorder(
        strategy="Survivor",
        symbol=pe_symbol,
        action="SELL",
        exchange="NFO",
        price_type="MARKET",
        product="MIS",
        quantity=50
    )

    # Sell CE
    ce_order = client.placeorder(
        strategy="Survivor",
        symbol=ce_symbol,
        action="SELL",
        exchange="NFO",
        price_type="MARKET",
        product="MIS",
        quantity=50
    )

# WebSocket for monitoring (unified across brokers)
instruments = [
    {"exchange": "NFO", "symbol": pe_symbol},
    {"exchange": "NFO", "symbol": ce_symbol}
]

def on_quote_update(data):
    # Process updates (unified format)
    symbol = data.get('symbol')
    ltp = data.get('ltp')
    print(f"{symbol}: {ltp}")

client.connect()
client.subscribe_quote(instruments, on_data_received=on_quote_update)

# Keep running
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    client.disconnect()
```

## Migration Checklist

Use this checklist to ensure complete migration:

### Code Changes
- [ ] Remove broker-specific authentication code
- [ ] Replace with OpenAlgo API key authentication
- [ ] Update all order placement calls to OpenAlgo API
- [ ] Replace WebSocket connection with OpenAlgo WebSocket
- [ ] Update position management calls
- [ ] Update symbol format to OpenAlgo standard
- [ ] Simplify error handling
- [ ] Remove broker-specific dependencies

### Configuration
- [ ] Create OpenAlgo API key
- [ ] Update configuration file
- [ ] Remove broker credentials from .env
- [ ] Add OpenAlgo endpoints to config
- [ ] Update symbol format in config

### Testing
- [ ] Enable OpenAlgo Analyzer mode
- [ ] Test authentication
- [ ] Test order placement (simulated)
- [ ] Test WebSocket connection
- [ ] Test position queries
- [ ] Verify all strategy logic
- [ ] Check error handling
- [ ] Test market hours logic

### Deployment
- [ ] Disable Analyzer mode
- [ ] Start with small position sizes
- [ ] Monitor first few trades closely
- [ ] Verify P&L calculations
- [ ] Check order execution
- [ ] Monitor logs for errors

### Documentation
- [ ] Document configuration
- [ ] Create deployment guide
- [ ] Note any strategy-specific changes
- [ ] Document monitoring procedures

## Common Migration Pitfalls

### 1. Symbol Format Confusion
❌ **Wrong**: Using broker-specific format
```python
symbol = "NSE:NIFTY25JAN24000PE"  # Fyers format
```

✅ **Correct**: Use OpenAlgo format
```python
symbol = "NIFTY25JAN3024000PE"  # OpenAlgo format
```

### 2. Forgetting to Disable Analyzer Mode
❌ **Wrong**: Running live without checking analyzer status
```python
# Running without checking mode
strategy.run()
```

✅ **Correct**: Verify analyzer mode is off
```python
status = client.analyzerstatus()
if status['data']['analyze_mode']:
    print("WARNING: Analyzer mode is ON - orders will be simulated")
    response = input("Continue? (yes/no): ")
    if response.lower() != 'yes':
        sys.exit(1)

strategy.run()
```

### 3. Not Handling API Responses
❌ **Wrong**: Assuming orders always succeed
```python
order_id = client.placeorder(...)  # Might return error
```

✅ **Correct**: Check response status
```python
response = client.placeorder(...)
if response.get('status') == 'success':
    order_id = response['orderid']
else:
    print(f"Order failed: {response.get('message')}")
```

### 4. WebSocket Connection Management
❌ **Wrong**: Not managing WebSocket lifecycle
```python
client.connect()
# Strategy runs...
# No disconnect
```

✅ **Correct**: Proper connection management
```python
try:
    client.connect()
    # Strategy logic
finally:
    client.disconnect()
```

## Broker-Specific Migration Notes

### From Fyers
- Remove TOTP authentication
- Update symbol format (remove "NSE:" prefix)
- Replace `place_order` with `placeorder`
- Update WebSocket implementation

### From Zerodha (Kite)
- Remove login flow and access token generation
- Update `place_order` calls to `placeorder`
- Replace `positions()` with `positionbook()`
- Update variety/product parameters

### From Upstox
- Simplify authentication (no OAuth flow needed)
- Update WebSocket to OpenAlgo WebSocket
- Replace market data subscriptions
- Update symbol format

### From Angel Broking
- Remove SmartAPI authentication
- Replace `placeOrder` with OpenAlgo `placeorder`
- Update WebSocket to unified implementation
- Simplify token management

## Testing Your Migrated Strategy

### 1. Enable Analyzer Mode
```python
client.analyzertoggle(mode=True)
```

### 2. Run Strategy with Small Quantities
```bash
python3 survivor_strategy_openalgo.py \
    --pe-quantity 1 \
    --ce-quantity 1
```

### 3. Verify Order Flow
Check OpenAlgo UI:
- Dashboard → Order Book
- Look for simulated orders
- Verify order parameters

### 4. Check Logs
```bash
tail -f log/strategies/survivor_*.log
```

### 5. Disable Analyzer and Go Live
```python
client.analyzertoggle(mode=False)
```

## Getting Help

If you encounter issues during migration:

1. **Check Documentation**: https://docs.openalgo.in
2. **Review Examples**: Check `/strategies/examples/`
3. **Community Support**: https://community.openalgo.in
4. **GitHub Issues**: https://github.com/marketcalls/openalgo/issues

## Conclusion

Migrating to OpenAlgo provides:
- ✅ Simplified authentication
- ✅ Broker independence
- ✅ Unified API across brokers
- ✅ Built-in testing tools
- ✅ Better error handling
- ✅ Consistent data formats

The migration effort is worthwhile for the long-term maintainability and flexibility it provides.

---

**Version**: 1.0
**Last Updated**: October 2025
