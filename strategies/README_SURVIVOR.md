# Survivor Strategy for OpenAlgo

**Professional Options Trading Strategy - Broker Agnostic Implementation**

## 📋 What's Included

This complete package includes everything you need to run the Survivor options strategy on OpenAlgo:

### Strategy Files
- **`survivor_strategy.py`** - Main strategy using REST API (polling)
- **`survivor_strategy_ws.py`** - Advanced version with WebSocket streaming (recommended)

### Configuration
- **`survivor_config.env.example`** - Configuration template
- **`setup_survivor.sh`** - Interactive setup script

### Documentation
- **`SURVIVOR_STRATEGY_GUIDE.md`** - Complete guide (⭐ Start here!)
- **`MIGRATION_GUIDE.md`** - Migrate from Fyers/Zerodha/Upstox
- **`QUICK_REFERENCE.md`** - Command cheat sheet
- **`README_SURVIVOR.md`** - This file

## 🚀 Quick Start (3 Steps)

### 1. Setup
```bash
cd /path/to/openalgo/strategies
chmod +x setup_survivor.sh
./setup_survivor.sh
```

### 2. Test (Safe Mode)
```bash
# Enable Analyzer Mode in OpenAlgo UI first!
# Then run:
./run_survivor.sh
```

### 3. Go Live
```bash
# Disable Analyzer Mode in OpenAlgo UI
# Then run:
./run_survivor.sh
```

That's it! 🎉

## 📖 Documentation

Choose your path:

1. **New User?** → Read [SURVIVOR_STRATEGY_GUIDE.md](./SURVIVOR_STRATEGY_GUIDE.md)
2. **Migrating from another broker?** → Read [MIGRATION_GUIDE.md](./MIGRATION_GUIDE.md)
3. **Need quick commands?** → Check [QUICK_REFERENCE.md](./QUICK_REFERENCE.md)

## 🎯 Strategy Overview

The Survivor Strategy is a **short straddle/strangle options strategy** that:

- ✅ Sells both PE and CE options at calculated strikes
- ✅ Manages risk with automatic stop losses
- ✅ Takes profit at target levels
- ✅ Exits individual legs if they move against you
- ✅ Automatically squares off before market close

### Example Trade

```
Underlying: NIFTY @ 24,500
Strategy: Sell 24,475 PE + 24,525 CE

Entry:
- PE Premium: 125 @ 50 qty = ₹6,250
- CE Premium: 118 @ 50 qty = ₹5,900
- Total Credit: ₹12,150

Exit Conditions:
- Target: ₹2,500 profit (Total P&L > 50/lot)
- Stop Loss: ₹5,000 loss (Total P&L < -100/lot)
- Individual Stop: If PE or CE premium doubles
```

## 🔧 Requirements

### Software
- Python 3.8 or higher
- OpenAlgo platform installed and running
- OpenAlgo Python SDK: `pip install openalgo`

### Configuration
- OpenAlgo API key (get from OpenAlgo UI)
- Broker account connected to OpenAlgo
- Sufficient margin for options trading

## 💡 Key Features

### Broker Agnostic
Works with **23+ brokers** including:
- Upstox
- Zerodha
- Angel Broking
- Fyers
- IIFL
- 5Paisa
- And many more!

### Two Versions Available

| Feature | REST Version | WebSocket Version |
|---------|-------------|-------------------|
| **File** | `survivor_strategy.py` | `survivor_strategy_ws.py` |
| **Speed** | Checks every 15s | Real-time updates |
| **Latency** | Higher | Lower |
| **Resource Use** | Low | Medium |
| **Best For** | Testing, Development | Production Trading |

### Built-in Safety Features
- ✅ Market hours validation
- ✅ Minimum premium checks
- ✅ Automatic stop losses
- ✅ Position monitoring
- ✅ Graceful error handling
- ✅ Comprehensive logging

## 📊 Parameters

All parameters are configurable via command line or environment variables:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `symbol-initials` | - | Option symbol (e.g., NIFTY25JAN30) |
| `pe-gap` | 25 | PE strike offset from ATM |
| `ce-gap` | 25 | CE strike offset from ATM |
| `pe-quantity` | 50 | Number of PE lots |
| `ce-quantity` | 50 | Number of CE lots |
| `min-price-to-sell` | 15.0 | Minimum premium to enter |
| `max-loss` | 100.0 | Maximum loss per lot |
| `target-profit` | 50.0 | Target profit per lot |

## 🧪 Testing

**Always test with Analyzer Mode first!**

### Enable Analyzer Mode
1. Open OpenAlgo UI
2. Go to Settings → Analyzer
3. Toggle Analyzer Mode ON
4. All orders will now be simulated (no real trades)

### Run Test
```bash
./run_survivor.sh
# Or
python3 survivor_strategy.py \
    --api-key "your-key" \
    --symbol-initials NIFTY25JAN30 \
    --pe-quantity 1 \
    --ce-quantity 1
```

### Check Logs
```bash
tail -f log/strategies/survivor_*.log
```

### Disable Analyzer Mode
Only after thorough testing:
1. OpenAlgo UI → Settings → Analyzer
2. Toggle Analyzer Mode OFF
3. Now ready for live trading

## 📂 Project Structure

```
strategies/
├── survivor_strategy.py              # REST version
├── survivor_strategy_ws.py           # WebSocket version
├── survivor_config.env.example       # Config template
├── survivor_config.env               # Your config (created by setup)
├── setup_survivor.sh                 # Setup wizard
├── run_survivor.sh                   # Launch script (REST)
├── run_survivor_ws.sh                # Launch script (WebSocket)
├── SURVIVOR_STRATEGY_GUIDE.md        # Full documentation
├── MIGRATION_GUIDE.md                # Migration guide
├── QUICK_REFERENCE.md                # Quick reference
└── README_SURVIVOR.md                # This file

log/strategies/
└── survivor_YYYYMMDD_HHMMSS.log     # Log files
```

## 🔍 Monitoring

### View Live Logs
```bash
tail -f log/strategies/survivor_*.log
```

### OpenAlgo Dashboard
- **Positions**: http://127.0.0.1:5000/positionbook
- **Orders**: http://127.0.0.1:5000/orderbook
- **Trades**: http://127.0.0.1:5000/tradebook

### Check Strategy Status
```bash
ps aux | grep survivor_strategy
```

## 🛑 Stopping the Strategy

### Method 1: Graceful Stop (Recommended)
Press `Ctrl+C` in the terminal where strategy is running.
This will:
- Close all open positions
- Cleanup resources
- Save logs

### Method 2: Kill Process
```bash
ps aux | grep survivor_strategy
kill <PID>
```

### Method 3: Emergency Close via UI
OpenAlgo UI → Position Book → Close All Positions

## 🐛 Troubleshooting

### Common Issues

**Problem**: "Invalid API key"
```bash
# Solution: Verify API key
curl -H "X-API-KEY: your-key" http://127.0.0.1:5000/api/v1/ping
```

**Problem**: "Symbol not found"
```python
# Solution: Search for correct symbol
from openalgo import api
client = api(api_key="key", host="http://127.0.0.1:5000")
response = client.search(query="NIFTY 24000 CE", exchange="NFO")
print(response)
```

**Problem**: Orders not executing
- Check Analyzer Mode is OFF
- Verify sufficient funds
- Confirm market is open
- Check broker connection in OpenAlgo

**Problem**: WebSocket connection failed
```bash
# Check WebSocket server is running
ps aux | grep websocket
nc -zv 127.0.0.1 8765
```

See [SURVIVOR_STRATEGY_GUIDE.md](./SURVIVOR_STRATEGY_GUIDE.md#troubleshooting) for more.

## 📚 Learning Resources

### Step-by-Step Guides
1. [Complete Strategy Guide](./SURVIVOR_STRATEGY_GUIDE.md) - Everything you need
2. [Migration Guide](./MIGRATION_GUIDE.md) - Coming from other platforms
3. [Quick Reference](./QUICK_REFERENCE.md) - Command cheat sheet

### OpenAlgo Resources
- **Docs**: https://docs.openalgo.in
- **GitHub**: https://github.com/marketcalls/openalgo
- **Community**: https://community.openalgo.in

## 🎓 Example Usage

### Simple Usage
```bash
python3 survivor_strategy.py \
    --api-key "your-key" \
    --symbol-initials NIFTY25JAN30
```

### Full Configuration
```bash
python3 survivor_strategy.py \
    --api-key "your-key" \
    --symbol-initials BANKNIFTY25FEB28 \
    --pe-gap 100 \
    --ce-gap 100 \
    --pe-quantity 25 \
    --ce-quantity 25 \
    --min-price-to-sell 20.0 \
    --max-loss 150.0 \
    --target-profit 75.0
```

### WebSocket Version (Faster)
```bash
python3 survivor_strategy_ws.py \
    --api-key "your-key" \
    --symbol-initials NIFTY25JAN30
```

## 🔐 Security Best Practices

1. **Never commit API keys** to version control
2. **Use environment variables** for sensitive data
3. **Keep logs secure** - they may contain trade data
4. **Regularly rotate API keys**
5. **Monitor for unusual activity**
6. **Use strong passwords** for OpenAlgo

## 📈 Performance Tips

### For Best Results
1. ✅ Use WebSocket version in production
2. ✅ Run on stable network connection
3. ✅ Start with conservative position sizes
4. ✅ Monitor first few days closely
5. ✅ Keep OpenAlgo and strategy updated

### Resource Optimization
- **REST version**: Lower CPU, good for testing
- **WebSocket version**: Real-time updates, better for live trading

## 🤝 Contributing

Found an issue or want to improve the strategy?

1. Check [GitHub Issues](https://github.com/marketcalls/openalgo/issues)
2. Submit bug reports with logs
3. Share improvements with the community

## ⚠️ Disclaimer

**Important**: Trading involves substantial risk of loss.

- This strategy is provided as an example
- Test thoroughly before live trading
- Use appropriate position sizing
- Understand the risks completely
- Past performance doesn't guarantee future results
- Trade at your own risk

The authors are not responsible for any trading losses incurred using this strategy.

## 📞 Support

### Need Help?

1. **Read the docs**: [SURVIVOR_STRATEGY_GUIDE.md](./SURVIVOR_STRATEGY_GUIDE.md)
2. **Check logs**: `log/strategies/survivor_*.log`
3. **Community**: https://community.openalgo.in
4. **GitHub**: https://github.com/marketcalls/openalgo/issues

### Before Asking for Help

Please provide:
- OpenAlgo version
- Python version
- Operating system
- Error messages from logs
- Steps to reproduce the issue

## 🎯 Next Steps

1. ✅ Run `./setup_survivor.sh`
2. ✅ Read [SURVIVOR_STRATEGY_GUIDE.md](./SURVIVOR_STRATEGY_GUIDE.md)
3. ✅ Test with Analyzer Mode
4. ✅ Start with small positions
5. ✅ Monitor and learn
6. ✅ Scale up gradually

## 📜 License

MIT License - See LICENSE file for details

## 👏 Acknowledgments

- OpenAlgo platform team
- OpenAlgo community
- Original strategy developers
- All contributors

---

**Version**: 1.0
**Last Updated**: October 2025
**Maintained by**: OpenAlgo Community

**Ready to start?** Run `./setup_survivor.sh` and begin your journey! 🚀

---

For detailed information, see [SURVIVOR_STRATEGY_GUIDE.md](./SURVIVOR_STRATEGY_GUIDE.md)
