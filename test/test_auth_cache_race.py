"""Guard against the TTL-expiry race in database/auth_db.py caches.

The caches are TTLCaches, so a key can expire between an `in` test and the
subsequent read or `del`. Both raise KeyError, which escaped
get_auth_token_broker() and 500'd /api/v1/history. Reads must go through
.get() and removals through .pop(key, None).
"""

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "database" / "auth_db.py"
CACHES = ("auth_cache", "feed_token_cache", "broker_cache", "user_id_cache")


def test_no_unguarded_cache_access():
    source = SOURCE.read_text()
    offenders = []
    for cache in CACHES:
        offenders += [
            f"del {cache}[...] on line {source[:m.start()].count(chr(10)) + 1}"
            for m in re.finditer(rf"\bdel\s+{cache}\[", source)
        ]
        offenders += [
            f"`in {cache}` membership test on line {source[:m.start()].count(chr(10)) + 1}"
            for m in re.finditer(rf"\bin\s+{cache}\b", source)
        ]
    assert not offenders, "KeyError race reintroduced: " + "; ".join(offenders)
