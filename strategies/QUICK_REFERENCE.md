# Survivor Strategy - Quick Reference Card

## Quick Start

```bash
# 1. Setup
chmod +x setup_survivor.sh
./setup_survivor.sh

# 2. Test (Analyzer Mode)
# Enable Analyzer in OpenAlgo UI first
./run_survivor.sh

# 3. Live Trading
# Disable Analyzer in OpenAlgo UI
./run_survivor.sh
```

## Command Line Usage

### Basic
```bash
python3 survivor_strategy.py \
    --api-key "your-key" \
    --symbol-initials NIFTY25JAN30
```

### Full Parameters
```bash
python3 survivor_strategy.py \
    --api-key "your-key" \
    --host "http://127.0.0.1:5000" \
    --ws-url "ws://127.0.0.1:8765" \
    --symbol-initials NIFTY25JAN30 \
    --pe-gap 25 \
    --ce-gap 25 \
    --pe-quantity 50 \
    --ce-quantity 50 \
    --min-price-to-sell 15.0 \
    --max-loss 100.0 \
    --target-profit 50.0
```

### WebSocket Version (Faster)
```bash
python3 survivor_strategy_ws.py \
    --api-key "your-key" \
    --symbol-initials NIFTY25JAN30
```

## Common Symbols

| Index | Symbol Format | Example |
|-------|---------------|---------|
| NIFTY | NIFTY[YY][MMM][DD] | NIFTY25JAN30 |
| BANKNIFTY | BANKNIFTY[YY][MMM][DD] | BANKNIFTY25FEB28 |
| FINNIFTY | FINNIFTY[YY][MMM][DD] | FINNIFTY25MAR25 |

## Parameter Guide

| Parameter | Description | Default | Range |
|-----------|-------------|---------|-------|
| PE Gap | Strike offset for PE | 25 | 0-500 |
| CE Gap | Strike offset for CE | 25 | 0-500 |
| PE Qty | PE quantity | 50 | 1-1000+ |
| CE Qty | CE quantity | 50 | 1-1000+ |
| Min Price | Min premium to enter | 15.0 | 0-1000 |
| Max Loss | Stop loss per lot | 100.0 | 10-10000 |
| Target | Profit target per lot | 50.0 | 10-10000 |

## API Endpoints Quick Reference

### Authentication
```python
from openalgo import api
client = api(api_key="key", host="http://127.0.0.1:5000")
```

### Place Order
```python
client.placeorder(
    strategy="name",
    symbol="NIFTY25JAN3024000PE",
    action="SELL",      # BUY or SELL
    exchange="NFO",
    price_type="MARKET", # MARKET, LIMIT, SL, SL-M
    product="MIS",      # MIS, CNC, NRML
    quantity=50
)
```

### Get Quotes
```python
client.quotes(symbol="NIFTY25JAN3024000PE", exchange="NFO")
```

### Get Positions
```python
client.positionbook()
```

### Close All Positions
```python
client.closeposition(strategy="name")
```

### WebSocket Subscribe
```python
instruments = [{"exchange": "NFO", "symbol": "NIFTY25JAN3024000PE"}]

def callback(data):
    print(data['ltp'])

client.connect()
client.subscribe_quote(instruments, on_data_received=callback)
```

### Analyzer Mode
```python
# Enable test mode
client.analyzertoggle(mode=True)

# Check status
status = client.analyzerstatus()

# Disable for live trading
client.analyzertoggle(mode=False)
```

## File Locations

```
strategies/
├── survivor_strategy.py          # Main strategy (REST)
├── survivor_strategy_ws.py       # WebSocket version
├── survivor_config.env           # Your configuration
├── survivor_config.env.example   # Template
├── setup_survivor.sh             # Setup script
├── run_survivor.sh               # Run script (REST)
├── run_survivor_ws.sh            # Run script (WebSocket)
├── SURVIVOR_STRATEGY_GUIDE.md    # Full documentation
├── MIGRATION_GUIDE.md            # Migration from other brokers
└── QUICK_REFERENCE.md            # This file

log/strategies/
└── survivor_*.log                # Log files
```

## Monitoring Commands

```bash
# View live logs
tail -f log/strategies/survivor_*.log

# Check running processes
ps aux | grep survivor

# View latest log file
ls -lt log/strategies/survivor_*.log | head -1 | awk '{print $NF}' | xargs tail -f

# Search for errors
grep -i error log/strategies/survivor_*.log
```

## Important Times (IST)

| Event | Time |
|-------|------|
| Market Open | 09:15 AM |
| Strategy Entry Window | 09:15 - 09:30 AM (typical) |
| Market Close | 03:30 PM |
| Auto Exit | 03:25 PM (before close) |

## Exit Conditions

Strategy exits when ANY of these conditions are met:

1. **Stop Loss**: Total P&L < -MAX_LOSS
2. **Target**: Total P&L > TARGET_PROFIT
3. **PE Stop**: PE premium > 2x entry
4. **CE Stop**: CE premium > 2x entry
5. **Market Close**: After 3:30 PM

## Troubleshooting

### Issue: API Key Invalid
```bash
# Test API key
curl -X GET "http://127.0.0.1:5000/api/v1/ping" \
    -H "X-API-KEY: your-key"
```

### Issue: Symbol Not Found
```python
# Search for symbol
response = client.search(query="NIFTY 24000 CE", exchange="NFO")
print(response)
```

### Issue: WebSocket Connection Failed
```bash
# Check WebSocket is running
ps aux | grep websocket
nc -zv 127.0.0.1 8765
```

### Issue: Orders Not Executing
- Check Analyzer Mode is OFF
- Verify sufficient funds
- Check market hours
- Review order book in OpenAlgo UI

## Log Analysis

### Success Indicators
```
✓ "Survivor Strategy initialized"
✓ "POSITIONS ENTERED SUCCESSFULLY"
✓ "Sell order placed"
✓ "Total P&L: 65.50" (profit)
```

### Warning Indicators
```
⚠ "Premiums below threshold"
⚠ "Market is closed"
⚠ "Failed to get option premiums"
```

### Error Indicators
```
✗ "Invalid API key"
✗ "Failed to place order"
✗ "Insufficient funds"
✗ "Symbol not found"
```

## Configuration Template

```env
# Copy to survivor_config.env and modify
OPENALGO_API_KEY=your-api-key-here
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

## Safety Checklist

Before going live:

- [ ] Tested in Analyzer Mode
- [ ] Verified API key works
- [ ] Checked broker account has sufficient funds
- [ ] Confirmed symbol format is correct
- [ ] Set appropriate position sizes
- [ ] Reviewed stop loss and target
- [ ] Analyzer Mode is DISABLED
- [ ] Tested exit functionality
- [ ] Log monitoring is set up
- [ ] Know how to manually stop strategy (Ctrl+C)

## Emergency Stop

```bash
# Method 1: Interrupt running process
# Press Ctrl+C in terminal

# Method 2: Kill process
ps aux | grep survivor
kill -9 <PID>

# Method 3: Close positions via UI
# OpenAlgo UI → Position Book → Close All

# Method 4: Close via API
python3 -c "
from openalgo import api
client = api(api_key='your-key', host='http://127.0.0.1:5000')
client.closeposition(strategy='Survivor Strategy')
"
```

## Performance Tuning

### For Faster Execution
1. Use WebSocket version (`survivor_strategy_ws.py`)
2. Reduce monitoring interval (default: 5-15s)
3. Run on server near exchange
4. Use SSD for log storage

### For Stability
1. Use REST version (`survivor_strategy.py`)
2. Increase monitoring interval
3. Add retry logic
4. Use larger timeouts

## Resource Usage

| Version | CPU | Memory | Network |
|---------|-----|--------|---------|
| REST | Low | 50-100 MB | Low |
| WebSocket | Medium | 100-150 MB | Medium |

## OpenAlgo UI Locations

- **Dashboard**: http://127.0.0.1:5000/
- **Position Book**: http://127.0.0.1:5000/positionbook
- **Order Book**: http://127.0.0.1:5000/orderbook
- **Trade Book**: http://127.0.0.1:5000/tradebook
- **Strategy Manager**: http://127.0.0.1:5000/python
- **API Settings**: http://127.0.0.1:5000/apikey
- **Analyzer**: http://127.0.0.1:5000/analyzer

## Support Resources

- **Documentation**: https://docs.openalgo.in
- **GitHub**: https://github.com/marketcalls/openalgo
- **Community**: https://community.openalgo.in
- **Issues**: https://github.com/marketcalls/openalgo/issues

## Version Info

```bash
# Check OpenAlgo package version
python3 -c "import openalgo; print(openalgo.__version__)"

# Check Python version
python3 --version

# Check strategy version
head -n 20 survivor_strategy.py | grep -i version
```

---

**Quick Reference Version**: 1.0
**Compatible with OpenAlgo**: 1.x+
**Last Updated**: October 2025

---

## One-Liner Commands

```bash
# Quick test with minimal params
python3 survivor_strategy.py --api-key "$OPENALGO_API_KEY" --symbol-initials NIFTY25JAN30

# Background execution
nohup python3 survivor_strategy.py --api-key "$OPENALGO_API_KEY" --symbol-initials NIFTY25JAN30 > survivor.log 2>&1 &

# Check if strategy is running
ps aux | grep -i survivor | grep -v grep

# Monitor logs in real-time
watch -n 5 'tail -20 log/strategies/survivor_*.log | tail -20'
```
