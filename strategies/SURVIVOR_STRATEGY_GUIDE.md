# Survivor Strategy for OpenAlgo - Complete Guide

## Table of Contents
1. [Overview](#overview)
2. [Strategy Description](#strategy-description)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Usage](#usage)
6. [Strategy Versions](#strategy-versions)
7. [Migration from Direct Broker APIs](#migration-from-direct-broker-apis)
8. [Testing with API Analyzer](#testing-with-api-analyzer)
9. [Deployment](#deployment)
10. [Monitoring](#monitoring)
11. [Troubleshooting](#troubleshooting)

## Overview

The Survivor Strategy is an options trading strategy that has been converted from direct broker APIs (Fyers/Zerodha) to work seamlessly with the OpenAlgo platform. This provides broker-agnostic execution across 23+ supported brokers including Upstox.

### Key Benefits of OpenAlgo Conversion

- **Broker Agnostic**: Switch between 23+ brokers without code changes
- **Unified API**: Single interface for all brokers
- **Built-in WebSocket**: Real-time market data streaming
- **API Analyzer**: Test strategies without real orders
- **Automatic Logging**: Built-in audit trails
- **Strategy Management**: Web-based control panel

## Strategy Description

### What is the Survivor Strategy?

The Survivor Strategy is a short straddle/strangle options strategy that:

1. **Entry Logic**:
   - Identifies ATM (At-The-Money) strike based on underlying price
   - Sells PE (Put) option at ATM - PE_GAP
   - Sells CE (Call) option at ATM + CE_GAP
   - Only enters when premiums are above minimum threshold

2. **Exit Logic**:
   - Takes profit when total P&L exceeds target
   - Cuts losses when total P&L falls below stop loss
   - Exits individual legs if premium doubles from entry
   - Automatically squares off at market close (3:30 PM)

3. **Risk Management**:
   - Configurable position sizes
   - Maximum loss per lot
   - Target profit per lot
   - Individual option stop losses

### Strategy Parameters

| Parameter | Description | Default | Example |
|-----------|-------------|---------|---------|
| `symbol-initials` | Option symbol prefix | - | NIFTY25JAN30 |
| `pe-gap` | Strike distance for PE from ATM | 25 | 25 |
| `ce-gap` | Strike distance for CE from ATM | 25 | 25 |
| `pe-quantity` | Quantity of PE options | 50 | 50 |
| `ce-quantity` | Quantity of CE options | 50 | 50 |
| `min-price-to-sell` | Minimum premium to enter | 15.0 | 15.0 |
| `max-loss` | Maximum loss per lot | 100.0 | 100.0 |
| `target-profit` | Target profit per lot | 50.0 | 50.0 |

## Installation

### Prerequisites

1. **OpenAlgo Platform**:
   ```bash
   # Make sure OpenAlgo is installed and running
   # Default: http://127.0.0.1:5000
   ```

2. **Python Dependencies**:
   ```bash
   pip install openalgo
   ```

3. **API Key**:
   - Login to OpenAlgo web interface
   - Navigate to Settings → API Keys
   - Generate a new API key
   - Copy the API key for use in strategy

### Download Strategy Files

```bash
# Navigate to strategies folder
cd /path/to/openalgo/strategies

# Strategy files should include:
# - survivor_strategy.py         (Basic version with REST API)
# - survivor_strategy_ws.py      (Advanced with WebSocket)
# - survivor_config.env.example  (Configuration template)
# - SURVIVOR_STRATEGY_GUIDE.md   (This guide)
```

## Configuration

### Method 1: Environment Variables

1. **Copy the example config**:
   ```bash
   cp survivor_config.env.example survivor_config.env
   ```

2. **Edit the configuration**:
   ```bash
   nano survivor_config.env
   ```

3. **Update values**:
   ```env
   OPENALGO_API_KEY=your-actual-api-key-here
   OPENALGO_HOST=http://127.0.0.1:5000
   OPENALGO_WS_URL=ws://127.0.0.1:8765

   SYMBOL_INITIALS=NIFTY25JAN30
   PE_GAP=25
   CE_GAP=25
   PE_QUANTITY=50
   CE_QUANTITY=50
   MIN_PRICE_TO_SELL=15.0

   MAX_LOSS_PER_LOT=100.0
   TARGET_PROFIT_PER_LOT=50.0
   ```

4. **Load and run**:
   ```bash
   source survivor_config.env
   python3 survivor_strategy.py --symbol-initials $SYMBOL_INITIALS
   ```

### Method 2: Command Line Arguments

Run strategy with explicit parameters:

```bash
python3 survivor_strategy.py \
    --api-key "your-api-key-here" \
    --symbol-initials NIFTY25JAN30 \
    --pe-gap 25 \
    --ce-gap 25 \
    --pe-quantity 50 \
    --ce-quantity 50 \
    --min-price-to-sell 15.0 \
    --max-loss 100.0 \
    --target-profit 50.0
```

## Usage

### Basic Usage (REST API Version)

```bash
# Simple execution with defaults
python3 survivor_strategy.py \
    --api-key "your-api-key" \
    --symbol-initials NIFTY25JAN30

# With custom parameters
python3 survivor_strategy.py \
    --api-key "your-api-key" \
    --symbol-initials BANKNIFTY25FEB28 \
    --pe-gap 100 \
    --ce-gap 100 \
    --pe-quantity 25 \
    --ce-quantity 25 \
    --min-price-to-sell 20.0
```

### Advanced Usage (WebSocket Version)

For faster execution with real-time streaming:

```bash
python3 survivor_strategy_ws.py \
    --api-key "your-api-key" \
    --symbol-initials NIFTY25JAN30 \
    --pe-gap 25 \
    --ce-gap 25 \
    --pe-quantity 50 \
    --ce-quantity 50
```

### Symbol Format Guide

| Underlying | Symbol Format | Example |
|------------|---------------|---------|
| NIFTY | NIFTY[YY][MMM][DD] | NIFTY25JAN30 |
| BANKNIFTY | BANKNIFTY[YY][MMM][DD] | BANKNIFTY25FEB28 |
| FINNIFTY | FINNIFTY[YY][MMM][DD] | FINNIFTY25MAR25 |

**Note**: The strategy automatically constructs full option symbols by appending strike and option type (e.g., NIFTY25JAN3024000PE)

## Strategy Versions

### Version 1: survivor_strategy.py (REST API)

**Characteristics**:
- Uses REST API for all operations
- Polls for price updates every 15 seconds
- Lower resource usage
- Suitable for slower-paced strategies

**When to use**:
- Testing and development
- Lower frequency monitoring
- Limited network bandwidth

### Version 2: survivor_strategy_ws.py (WebSocket)

**Characteristics**:
- Real-time WebSocket streaming
- Instant price updates
- Checks positions every 5 seconds
- Lower latency execution

**When to use**:
- Production trading
- Fast-moving markets
- Tight stop losses
- Maximum performance

## Migration from Direct Broker APIs

### What Changed?

| Aspect | Old (Direct Broker) | New (OpenAlgo) |
|--------|---------------------|----------------|
| **Authentication** | TOTP + Broker credentials | OpenAlgo API Key |
| **Order Placement** | `broker.fyers.place_order()` | `client.placeorder()` |
| **Market Data** | Custom WebSocket per broker | Unified WebSocket |
| **Position Tracking** | Broker-specific API | `client.positionbook()` |
| **Symbol Format** | Broker-specific | OpenAlgo unified format |

### Migration Checklist

- [x] Remove TOTP authentication code
- [x] Replace broker-specific API calls with OpenAlgo SDK
- [x] Update WebSocket connection to OpenAlgo WS
- [x] Change symbol format to OpenAlgo standard
- [x] Update configuration from .env to OpenAlgo settings
- [x] Add OpenAlgo API key authentication
- [x] Replace custom order management with OpenAlgo APIs

### Code Comparison

**OLD (Direct Broker API)**:
```python
# Old approach with Fyers
from fyers_api import fyersModel
import pyotp

# TOTP Authentication
totp = pyotp.TOTP(totp_secret)
access_token = fyers.generate_token(totp.now())

# Place order
fyers.place_order(
    symbol="NSE:NIFTY25JAN3024000PE",
    qty=50,
    side=1,  # Sell
    type=2   # Market
)

# Custom WebSocket
ws = fyersModel.FyersSocket(access_token)
ws.subscribe(['NSE:NIFTY25JAN3024000PE'])
```

**NEW (OpenAlgo)**:
```python
# New approach with OpenAlgo
from openalgo import api

# Simple API key authentication
client = api(api_key="your-key", host="http://127.0.0.1:5000")

# Place order - broker agnostic
client.placeorder(
    strategy="Survivor",
    symbol="NIFTY25JAN3024000PE",
    action="SELL",
    exchange="NFO",
    price_type="MARKET",
    product="MIS",
    quantity=50
)

# Unified WebSocket
client.connect()
client.subscribe_quote(
    [{"exchange": "NFO", "symbol": "NIFTY25JAN3024000PE"}],
    on_data_received=callback
)
```

## Testing with API Analyzer

OpenAlgo provides an API Analyzer for testing strategies without placing real orders.

### Enable Analyzer Mode

1. **Via OpenAlgo UI**:
   - Navigate to Settings → API Analyzer
   - Toggle "Analyze Mode" to ON
   - All orders will be simulated

2. **Via Python SDK**:
   ```python
   # Enable analyzer mode
   client.analyzertoggle(mode=True)

   # Check analyzer status
   status = client.analyzerstatus()
   print(status)
   # Output: {'status': 'success', 'data': {'analyze_mode': True}}
   ```

### Test Your Strategy

```bash
# Run strategy in analyzer mode (set via UI first)
python3 survivor_strategy.py \
    --api-key "your-api-key" \
    --symbol-initials NIFTY25JAN30 \
    --pe-quantity 1 \
    --ce-quantity 1
```

**What happens**:
- Orders are logged but not sent to broker
- Simulated responses are returned
- Full strategy logic is executed
- Logs show what would have happened

### Disable Analyzer Mode

```python
# Disable for live trading
client.analyzertoggle(mode=False)
```

**⚠️ WARNING**: Always verify analyzer mode is OFF before live trading!

## Deployment

### Option 1: Run Directly

```bash
# Run in foreground
python3 survivor_strategy.py \
    --api-key "your-api-key" \
    --symbol-initials NIFTY25JAN30
```

### Option 2: Run with nohup

```bash
# Run in background
nohup python3 survivor_strategy.py \
    --api-key "your-api-key" \
    --symbol-initials NIFTY25JAN30 \
    > survivor.log 2>&1 &

# Check process
ps aux | grep survivor_strategy

# Stop process
kill <PID>
```

### Option 3: OpenAlgo Strategy Manager (Recommended)

1. **Upload Strategy**:
   - Login to OpenAlgo UI
   - Navigate to `/python` (Strategy Management)
   - Click "Add Strategy"
   - Upload `survivor_strategy.py`
   - Set name: "Survivor Strategy"

2. **Configure Parameters**:
   ```
   SYMBOL_INITIALS=NIFTY25JAN30
   PE_GAP=25
   CE_GAP=25
   PE_QUANTITY=50
   CE_QUANTITY=50
   MIN_PRICE_TO_SELL=15.0
   ```

3. **Set Schedule**:
   - Start Time: 09:15
   - Stop Time: 15:30
   - Days: Mon-Fri

4. **Start Strategy**:
   - Click "Start" button
   - Monitor via "Logs" button

### Option 4: Systemd Service (Linux)

1. **Create service file**:
   ```bash
   sudo nano /etc/systemd/system/survivor-strategy.service
   ```

2. **Service configuration**:
   ```ini
   [Unit]
   Description=Survivor Strategy OpenAlgo
   After=network.target

   [Service]
   Type=simple
   User=your-username
   WorkingDirectory=/path/to/openalgo/strategies
   Environment="OPENALGO_API_KEY=your-api-key"
   ExecStart=/usr/bin/python3 /path/to/survivor_strategy.py \
       --symbol-initials NIFTY25JAN30 \
       --pe-gap 25 \
       --ce-gap 25 \
       --pe-quantity 50 \
       --ce-quantity 50
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

3. **Enable and start**:
   ```bash
   sudo systemctl enable survivor-strategy
   sudo systemctl start survivor-strategy
   sudo systemctl status survivor-strategy
   ```

## Monitoring

### Log Files

Logs are stored in `log/strategies/`:

```bash
# View latest log
tail -f log/strategies/survivor_*.log

# View all logs
ls -lh log/strategies/

# Search for errors
grep -i error log/strategies/survivor_*.log
```

### Log Format

```
2025-10-30 09:15:00 - INFO - Survivor Strategy initialized
2025-10-30 09:15:05 - INFO - NIFTY LTP: 24500.50
2025-10-30 09:15:05 - INFO - PE Strike: 24475, CE Strike: 24525
2025-10-30 09:15:06 - INFO - NIFTY25JAN3024475PE Premium: 125.50
2025-10-30 09:15:06 - INFO - NIFTY25JAN3024525CE Premium: 118.75
2025-10-30 09:15:07 - INFO - Sell order: NIFTY25JAN3024475PE x 50, OrderID: 123456
2025-10-30 09:15:08 - INFO - Sell order: NIFTY25JAN3024525CE x 50, OrderID: 123457
2025-10-30 09:15:08 - INFO - POSITIONS ENTERED SUCCESSFULLY
```

### OpenAlgo Dashboard

Monitor via OpenAlgo web interface:

1. **Position Book** (`/positionbook`):
   - View real-time positions
   - See P&L for each leg
   - Monitor total exposure

2. **Order Book** (`/orderbook`):
   - Track all orders
   - Check order status
   - View execution prices

3. **Trade Book** (`/tradebook`):
   - Complete trade history
   - Entry and exit details
   - Performance analytics

4. **Strategy Manager** (`/python`):
   - View running strategies
   - Check logs
   - Start/Stop controls

### Real-time Monitoring Script

```python
from openalgo import api
import time

client = api(api_key="your-key", host="http://127.0.0.1:5000")

while True:
    positions = client.positionbook()
    if positions.get('status') == 'success':
        for pos in positions['data']:
            print(f"{pos['symbol']}: Qty={pos['quantity']}, P&L={pos['pnl']}")
    time.sleep(10)
```

## Troubleshooting

### Common Issues

#### 1. API Key Invalid

**Error**: `"Invalid API key"`

**Solution**:
```bash
# Verify API key
echo $OPENALGO_API_KEY

# Test API key
curl -X GET "http://127.0.0.1:5000/api/v1/ping" \
    -H "X-API-KEY: your-api-key"
```

#### 2. Symbol Not Found

**Error**: `"Symbol not found"`

**Solution**:
- Check symbol format matches broker's master contract
- Use Search API to find correct symbol:
  ```python
  response = client.search(query="NIFTY 24000 CE", exchange="NFO")
  print(response)
  ```

#### 3. WebSocket Connection Failed

**Error**: `"Failed to connect to WebSocket"`

**Solution**:
```bash
# Check WebSocket server is running
ps aux | grep websocket

# Check port is accessible
nc -zv 127.0.0.1 8765

# Restart WebSocket proxy
# (From OpenAlgo root directory)
python3 websocket_proxy/server.py
```

#### 4. Insufficient Funds

**Error**: `"Insufficient funds"`

**Solution**:
- Check available margin:
  ```python
  funds = client.funds()
  print(f"Available: {funds['data']['availablecash']}")
  ```
- Reduce position size
- Add funds to broker account

#### 5. Market Closed

**Error**: `"Market is closed"`

**Solution**:
- Strategy only trades during 9:15 AM - 3:30 PM IST
- Wait for market hours
- Use analyzer mode for testing outside market hours

### Debug Mode

Enable detailed logging:

```python
import logging

logging.basicConfig(level=logging.DEBUG)
```

### Getting Help

1. **Check OpenAlgo Docs**: https://docs.openalgo.in
2. **GitHub Issues**: https://github.com/marketcalls/openalgo/issues
3. **Community Forum**: https://community.openalgo.in
4. **Strategy Logs**: Always check `log/strategies/` for detailed error messages

## Advanced Topics

### Multiple Strategies

Run multiple instances with different parameters:

```bash
# NIFTY Strategy
python3 survivor_strategy.py \
    --api-key "key" \
    --symbol-initials NIFTY25JAN30 &

# BANKNIFTY Strategy
python3 survivor_strategy.py \
    --api-key "key" \
    --symbol-initials BANKNIFTY25FEB28 \
    --pe-gap 100 \
    --ce-gap 100 &
```

### Custom Modifications

**Adjust Entry Logic**:
```python
# In enter_positions() method
# Add your custom entry conditions
if underlying_ltp > 24000 and underlying_ltp < 25000:
    # Enter only in this range
    ...
```

**Add Trailing Stop Loss**:
```python
# Track highest profit
self.highest_pnl = 0

# In monitor_positions()
if total_pnl > self.highest_pnl:
    self.highest_pnl = total_pnl

# Exit if profit drops by 20%
if total_pnl < self.highest_pnl * 0.8:
    self.exit_positions()
```

### Performance Optimization

1. **Use WebSocket version** for faster execution
2. **Reduce monitoring interval** for tight stops
3. **Pre-calculate strikes** before market opens
4. **Use smart order APIs** for better fills

## Best Practices

1. **Always test in Analyzer mode first**
2. **Start with small position sizes**
3. **Monitor strategy closely on first day**
4. **Keep logs for at least 30 days**
5. **Have manual exit plan ready**
6. **Don't modify strategy during market hours**
7. **Use proper risk management**
8. **Backup configuration files**

## Disclaimer

This strategy is provided as an example for educational purposes. Trading involves substantial risk of loss. Always:
- Test thoroughly before live trading
- Use appropriate position sizing
- Have proper risk management
- Understand the strategy completely
- Trade at your own risk

---

**Version**: 1.0
**Last Updated**: October 2025
**License**: MIT
**Author**: Converted to OpenAlgo Platform
