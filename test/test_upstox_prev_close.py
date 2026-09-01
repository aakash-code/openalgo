"""_previous_close: prev close must be the previous session's, never today's running close."""
import sys
sys.path.insert(0, '/Users/Shared/Project/openalgo')
import importlib.util, types

# Load the module's helper without importing the whole broker package.
src = open('/Users/Shared/Project/openalgo/broker/upstox/api/data.py').read()
start = src.index('def _previous_close')
end = src.index('def get_api_response')
ns: dict = {}
exec(src[start:end], ns)
_previous_close = ns['_previous_close']

# Real ANGELONE payload shape: v2 ohlc.close tracks LTP intraday, net_change is truth.
v2 = {"last_price": 293.5, "net_change": 7.5, "ohlc": {"open": 286.8, "close": 293.5}}
assert _previous_close(v2, None) == 286.0, _previous_close(v2, None)
assert _previous_close(v2, {}) == 286.0

# v3 prev_ohlc wins when present.
assert _previous_close(v2, {"close": 285.4}) == 285.4

# Unchanged stock: net_change 0 is a real value, not "missing".
flat = {"last_price": 100.0, "net_change": 0.0, "ohlc": {"close": 100.0}}
assert _previous_close(flat, None) == 100.0

# Pre-market / no net_change: ohlc.close is the previous close then.
pre = {"last_price": 0, "ohlc": {"close": 286.0}}
assert _previous_close(pre, None) == 286.0

# Nothing usable -> 0, so callers keep their own fallback.
assert _previous_close({}, {}) == 0.0
assert _previous_close(None, None) == 0.0

# Junk types must not raise.
assert _previous_close({"last_price": "abc", "net_change": "x", "ohlc": {"close": 12}}, None) == 12.0
print("all prev-close cases pass")
