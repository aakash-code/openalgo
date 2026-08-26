"""Proves the instrument-download retry rides through a transient failure."""
import sys, types, time
import pandas as pd

sys.path.insert(0, '/Users/Shared/Project/openalgo/strategies')

# Stub the openalgo SDK so importing Survivor does not build a real client.
fake = types.ModuleType('openalgo')
class _FakeApi:
    def __init__(self, *a, **k): pass
fake.api = _FakeApi
sys.modules['openalgo'] = fake

import Survivor

calls = {'n': 0}
TIMEOUT = {'status': 'error', 'message': 'Request timed out.', 'error_type': 'timeout_error'}

def flaky_instruments(exchange=None):
    """Fails twice (as it did at 09:14), succeeds on the third attempt."""
    calls['n'] += 1
    if calls['n'] < 3:
        return TIMEOUT
    return pd.DataFrame([
        {'symbol': 'NIFTY01SEP2624500CE', 'lotsize': 65, 'strike': 24500, 'expiry': '01-SEP-26'},
        {'symbol': 'NIFTY01SEP2624500PE', 'lotsize': 65, 'strike': 24500, 'expiry': '01-SEP-26'},
    ])

Survivor.client = types.SimpleNamespace(instruments=flaky_instruments)
Survivor.time = types.SimpleNamespace(sleep=lambda s: None)  # don't actually wait

strat = Survivor.SurvivorStrategy.__new__(Survivor.SurvivorStrategy)
strat.config = dict(Survivor.CONFIG); strat.config['symbol_initials'] = 'NIFTY01SEP26'
strat.instruments_df = None
strat._compute_current_prefix = lambda: 'NIFTY01SEP26'

ok = Survivor.SurvivorStrategy._load_instruments(strat)
print(f"attempts made      : {calls['n']}")
print(f"returned           : {ok}")
print(f"instruments loaded : {0 if strat.instruments_df is None else len(strat.instruments_df)}")
assert calls['n'] == 3, "should have retried twice before succeeding"
assert ok is True, "should succeed on the third attempt"
assert strat.instruments_df is not None and len(strat.instruments_df) == 2
print("PASS: a transient timeout no longer kills the strategy")

# And a permanent failure must still fail, not loop forever.
perm = {'n': 0}
def always_timeout(exchange=None):
    perm['n'] += 1
    return TIMEOUT
Survivor.client = types.SimpleNamespace(instruments=always_timeout)
strat2 = Survivor.SurvivorStrategy.__new__(Survivor.SurvivorStrategy)
strat2.config = dict(Survivor.CONFIG); strat2.config['symbol_initials'] = 'NIFTY01SEP26'
strat2.instruments_df = None
strat2._compute_current_prefix = lambda: 'NIFTY01SEP26'
ok2 = Survivor.SurvivorStrategy._load_instruments(strat2)
print(f"permanent failure  : attempts={perm['n']} returned={ok2}")
assert perm['n'] == 3 and ok2 is False, "must give up after 3, not loop"
print("PASS: a permanent failure still fails, bounded at 3 attempts")
