# Survivor Strategy - Deployment Checklist

Use this checklist to ensure safe and successful deployment of the Survivor strategy.

## Pre-Deployment Checklist

### Environment Setup
- [ ] OpenAlgo platform installed and running
- [ ] OpenAlgo accessible at configured host (default: http://127.0.0.1:5000)
- [ ] WebSocket proxy running on port 8765
- [ ] Python 3.8+ installed
- [ ] OpenAlgo Python SDK installed (`pip install openalgo`)

### Broker Configuration
- [ ] Broker account connected to OpenAlgo
- [ ] Broker authentication verified in OpenAlgo UI
- [ ] Broker set as "Active" in OpenAlgo
- [ ] Master contract downloaded for broker
- [ ] Sufficient margin available for options trading

### API Configuration
- [ ] OpenAlgo API key generated
- [ ] API key saved securely
- [ ] API key tested with ping endpoint
- [ ] API rate limits understood

### Strategy Configuration
- [ ] `survivor_config.env` created from example
- [ ] API key configured in env file
- [ ] Symbol initials set correctly (e.g., NIFTY25JAN30)
- [ ] PE and CE gaps configured
- [ ] Position sizes set appropriately
- [ ] Risk parameters (stop loss, target) configured
- [ ] Configuration reviewed and validated

## Testing Phase

### Unit Testing
- [ ] Strategy files syntax checked (`python3 -m py_compile survivor_strategy.py`)
- [ ] Import test successful (`python3 -c "from openalgo import api"`)
- [ ] Configuration loading tested
- [ ] Log directory creation verified

### Analyzer Mode Testing
- [ ] Analyzer Mode ENABLED in OpenAlgo UI
- [ ] Verified analyzer status via API
- [ ] Strategy executed with test parameters
- [ ] Simulated orders appear in order book
- [ ] Position calculations verified
- [ ] Entry logic tested
- [ ] Exit logic tested
- [ ] Stop loss triggers tested
- [ ] Target profit triggers tested
- [ ] Market close exit tested
- [ ] Logs reviewed for errors
- [ ] No warnings or critical issues found

### Symbol and Strike Testing
- [ ] Underlying LTP retrieval working
- [ ] Strike calculation logic verified
- [ ] Option symbol construction correct
- [ ] Option premium retrieval working
- [ ] Symbol exists in broker master contract
- [ ] Liquidity in selected strikes confirmed

### Risk Testing
- [ ] Position sizing appropriate for account
- [ ] Maximum loss acceptable
- [ ] Stop loss working correctly
- [ ] Individual leg stops tested
- [ ] Emergency exit procedure tested

## Pre-Live Checklist

### Final Configuration Review
- [ ] API key is production key (not test)
- [ ] Analyzer Mode is DISABLED
- [ ] Position sizes are final (not test sizes)
- [ ] Symbol initials correct for current expiry
- [ ] PE/CE gaps appropriate for market conditions
- [ ] Stop loss and target realistic
- [ ] Minimum price threshold reasonable

### Risk Management
- [ ] Maximum loss per day calculated
- [ ] Total capital at risk understood
- [ ] Account has 2x required margin as buffer
- [ ] Emergency contacts available
- [ ] Manual intervention plan ready

### Monitoring Setup
- [ ] Log file location known
- [ ] Real-time log monitoring setup (`tail -f`)
- [ ] OpenAlgo dashboard bookmarked
- [ ] Position book monitoring ready
- [ ] Order book monitoring ready
- [ ] Alert system configured (if any)
- [ ] Mobile access to OpenAlgo tested

### Contingency Planning
- [ ] Manual exit procedure documented
- [ ] Emergency stop commands ready
- [ ] Broker app access available
- [ ] Customer care numbers saved
- [ ] Internet backup available
- [ ] Power backup available (if applicable)

## Deployment Day

### Morning Checks (Before Market Open)
- [ ] OpenAlgo platform running
- [ ] WebSocket proxy running
- [ ] Broker connection active
- [ ] API key valid
- [ ] Sufficient margin available
- [ ] Strategy files present and unchanged
- [ ] Configuration file correct
- [ ] Log directory writable
- [ ] Analyzer Mode DISABLED (verify again!)

### Market Open Preparation (9:00-9:15 AM)
- [ ] Underlying price checked
- [ ] Option chain reviewed for liquidity
- [ ] Strikes calculated manually to verify
- [ ] Premium levels acceptable
- [ ] Volatility conditions suitable
- [ ] No major news events pending
- [ ] Network connectivity stable

### Strategy Launch (9:15 AM)
- [ ] Start strategy: `./run_survivor.sh` or `./run_survivor_ws.sh`
- [ ] Confirm strategy process running (`ps aux | grep survivor`)
- [ ] Monitor logs for initialization messages
- [ ] Watch for "Survivor Strategy initialized" message
- [ ] Check for any error messages

### First Position Entry (First 15 minutes)
- [ ] Entry signal detected and logged
- [ ] Underlying LTP logged correctly
- [ ] Strikes calculated and logged
- [ ] Option symbols constructed correctly
- [ ] Premiums checked and logged
- [ ] Minimum price threshold met
- [ ] Sell orders placed successfully
- [ ] Order IDs received and logged
- [ ] Orders confirmed in OpenAlgo order book
- [ ] Orders filled (check trade book)
- [ ] Fill prices acceptable
- [ ] Positions appear in position book
- [ ] Entry prices recorded correctly

### Initial Monitoring (First Hour)
- [ ] Strategy running continuously
- [ ] No process crashes
- [ ] Logs being written
- [ ] Position monitoring working
- [ ] P&L calculations correct
- [ ] No error messages in logs
- [ ] Network connectivity stable
- [ ] OpenAlgo responsive

### Ongoing Monitoring
- [ ] Check logs every 15 minutes
- [ ] Monitor P&L in position book
- [ ] Watch for exit signals in logs
- [ ] Ensure strategy responding to price changes
- [ ] No repeated errors in logs
- [ ] System resources (CPU, memory) normal

### Exit Monitoring
Watch for any of these exit conditions:
- [ ] Stop loss hit
- [ ] Target profit reached
- [ ] Individual option stop triggered
- [ ] Market close approaching
- [ ] Manual exit triggered

### Upon Exit
- [ ] Exit signal logged
- [ ] Close position API called
- [ ] Orders placed to square off
- [ ] Orders confirmed in order book
- [ ] Orders filled
- [ ] Positions closed (quantity = 0)
- [ ] Final P&L recorded
- [ ] Strategy continues or stops gracefully

### End of Day (After 3:30 PM)
- [ ] All positions closed
- [ ] No open positions in position book
- [ ] Final P&L calculated
- [ ] Trade log reviewed
- [ ] Performance analysis done
- [ ] Logs saved
- [ ] Strategy stopped gracefully
- [ ] Issues documented (if any)

## Post-Deployment

### Daily Review
- [ ] Review all logs
- [ ] Analyze trade performance
- [ ] Check for any errors or warnings
- [ ] Verify all positions closed
- [ ] Document any issues
- [ ] Update configuration if needed
- [ ] Plan for next trading day

### Weekly Review
- [ ] Aggregate performance analysis
- [ ] Review strike selection accuracy
- [ ] Analyze entry and exit timing
- [ ] Check stop loss effectiveness
- [ ] Review target achievement rate
- [ ] Assess slippage and execution quality
- [ ] Identify improvement opportunities

### Monthly Review
- [ ] Full performance review
- [ ] Risk-adjusted returns calculation
- [ ] Strategy parameter optimization
- [ ] Market condition analysis
- [ ] Code improvements identification
- [ ] Documentation updates

## Issue Response Plan

### If Orders Fail
1. Check error message in logs
2. Verify Analyzer Mode is OFF
3. Check available margin
4. Verify broker connection
5. Check symbol format
6. Try manual order via OpenAlgo UI
7. Contact broker support if needed

### If Strategy Crashes
1. Check logs for error
2. Note position status
3. Restart strategy if positions open
4. Or close positions manually
5. Fix issue before next run
6. Document incident

### If Network Fails
1. Strategy will attempt to continue
2. Check OpenAlgo connectivity
3. Use mobile/backup internet
4. If extended: close positions manually via broker app
5. Stop strategy once network restored

### If Broker API Down
1. Monitor broker status page
2. Positions safe if already entered
3. May need manual monitoring
4. Use broker app for emergency exits
5. Wait for API restoration

## Emergency Contacts

Document these before going live:

- Broker Customer Care: _________________
- OpenAlgo Support: _____________________
- Network Provider: _____________________
- IT Support: ___________________________
- Backup Contact: _______________________

## Notes Section

Use this space for deployment-specific notes:

```
Deployment Date: __________
Symbol Used: __________
Position Sizes: PE=_____ CE=_____
Entry Time: __________
Entry Premiums: PE=_____ CE=_____
Exit Time: __________
Exit Reason: __________
Final P&L: __________
Issues Encountered: __________
Lessons Learned: __________
```

---

## Sign-Off

- [ ] I have completed all checklist items
- [ ] I understand the risks involved
- [ ] I have tested thoroughly in Analyzer Mode
- [ ] I have emergency procedures ready
- [ ] I accept responsibility for trading outcomes

**Trader Name**: _________________
**Date**: _________________
**Signature**: _________________

---

**Keep this checklist for every deployment and review it before each trading day!**
