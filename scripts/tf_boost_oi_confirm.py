"""Does open interest confirm a RUN badge?

    uv run python scripts/tf_boost_oi_confirm.py                 # today
    uv run python scripts/tf_boost_oi_confirm.py 2026-09-18
    uv run python scripts/tf_boost_oi_confirm.py --all           # every recorded day
    uv run python scripts/tf_boost_oi_confirm.py --limit 20      # quick iteration

The badge finds movement; measured over 290 badges on 16-21 Sep 2026 it does not
predict that the move continues (up badges +0.060 median at +30m, down badges
-0.070). Every filter tried from the same rank-and-price data failed. This asks
whether OPEN INTEREST -- a genuinely different input -- separates the runs that
carried on from the ones that died.

At the badge minute, for each badged stock:

  futures   price and OI together give the standard buildup reading
              price up + OI up    long buildup     new money, backing the move
              price up + OI down  short covering   shorts fleeing, often fades
              price dn + OI up    short buildup
              price dn + OI down  long unwinding
  options   the signature Aakash described, on the ATM pair
              up badge:   PE OI rising = put writing, CE OI falling = call unwinding
              down badge: CE OI rising = call writing, PE OI falling = put unwinding

Nothing here changes the badge. This is evidence, and it is meant to be capable
of returning "no" -- which is why the summary reports the unconfirmed bucket
beside the confirmed one rather than only the cases that worked.

Borrowed wholesale from tf_boost_option_audit.py, including the four checks it
already encodes: the option side must match the badge direction, travel from the
turn is never reported as the day move, snapshot prices are checked against the
broker's traded range, and liquidity is reported rather than assumed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tf_boost_option_audit import (  # noqa: E402
    EXPIRY,
    atm_contract,
    clock,
    fetch_bars,
    first_badge,
    first_recorded_minute,
    outcome,
)

from database.auth_db import get_auth_token_broker, get_first_available_api_key  # noqa: E402
from database.symbol import SymToken, db_session  # noqa: E402
from database.tf_boost_db import (  # noqa: E402
    get_boost_change_timeline,
    get_boost_rank_timeline,
)
from services.tf_symbol_alias import tradable_symbol  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")

# OI is read over two windows because they answer different questions. The
# lookback says what positioning did just before the badge fired; the session
# window says what has been built all day. A move can be backed by one and not
# the other, and which matters is exactly what this script is here to find out.
LOOKBACK_MIN = 30

# Contracts thinner than this are reported, never trusted -- the same rule the
# option audit already applies to premium. An OI change on a contract nobody
# trades is noise dressed as positioning.
MIN_OI_VOLUME = 10_000


def expiry_date() -> datetime:
    """The configured expiry as a date. `29SEP26` -> 2026-09-29."""
    return datetime.strptime(EXPIRY, "%d%b%y")


# One expiry cycle is a month, so a day more than this far before the expiry
# belonged to an EARLIER contract.
CYCLE_DAYS = 35


def in_expiry_cycle(day: str) -> bool:
    """Was `EXPIRY` the near month on `day`?

    The symbol master carries only live contracts -- on 21-Sep-2026 it held
    29-SEP, 27-OCT and 23-NOV and nothing older -- so a July day cannot resolve
    its own near-month future at all. Reading July against the September
    contract does not fail loudly: it silently measures a far-month contract
    that barely traded, and reports its noise as positioning. The recorder has
    41 days going back to 15-Jul, and every one before the current cycle would
    have been wrong this way.

    Expired contracts ARE reachable for this broker via
    services/expired_fno_service.py (`EXPIRED_FNO_CAPABLE_BROKERS` includes
    upstox), so extending past one cycle is possible -- it is simply not done
    here, and the days are skipped rather than guessed at.
    """
    expiry = expiry_date()
    stamp = datetime.strptime(day, "%Y-%m-%d")
    return (expiry - stamp).days >= 0 and (expiry - stamp).days <= CYCLE_DAYS


def near_future(symbol: str) -> str | None:
    """The near-month future for a stock, or None if it has no F&O.

    Only the configured expiry is considered. Stock F&O in India is monthly, so
    for a run of days inside one expiry cycle this is the contract that was
    actually liquid on every one of them.
    """
    name = f"{symbol}{EXPIRY}FUT"
    hit = (
        db_session.query(SymToken.symbol)
        .filter(SymToken.exchange == "NFO", SymToken.symbol == name)
        .first()
    )
    return hit[0] if hit else None


def at_minute(bars, minute):
    """The last bar at or before `minute`, or None if the series starts later."""
    before = [b for t, b in bars if t.hour * 60 + t.minute <= minute]
    return before[-1] if before else None


def oi_reading(bars, badge_min):
    """OI and price at the badge, over the lookback, and over the session.

    Returns None when the contract has no bar at the badge minute -- an absent
    reading is reported as absent, never as a zero change, which would land in
    the "OI flat" bucket and quietly dilute whatever signal exists.
    """
    now = at_minute(bars, badge_min)
    if not now or not now.get("oi"):
        return None
    back = at_minute(bars, badge_min - LOOKBACK_MIN)
    first = bars[0][1] if bars else None
    if not first or not first.get("oi"):
        return None

    def delta(then):
        if not then or not then.get("oi"):
            return None, None
        d_oi = (now["oi"] - then["oi"]) / then["oi"] * 100
        d_px = (now["close"] - then["close"]) / then["close"] * 100 if then["close"] else 0.0
        return d_oi, d_px

    look_oi, look_px = delta(back)
    sess_oi, sess_px = delta(first)
    return {
        "oi": now["oi"],
        "volume": sum(b.get("volume", 0) for _, b in bars),
        "look_oi_pct": look_oi,
        "look_px_pct": look_px,
        "sess_oi_pct": sess_oi,
        "sess_px_pct": sess_px,
    }


# Below this an OI move is not positioning, it is rounding. Chosen so a contract
# whose OI barely moved does not get sorted into a directional bucket.
OI_FLAT_PCT = 0.5


def buildup(price_pct: float | None, oi_pct: float | None) -> str:
    """The four-quadrant reading, or 'flat' when nothing moved enough to say."""
    if price_pct is None or oi_pct is None:
        return "unknown"
    if abs(oi_pct) < OI_FLAT_PCT:
        return "oi flat"
    if price_pct >= 0:
        return "long buildup" if oi_pct > 0 else "short covering"
    return "short buildup" if oi_pct > 0 else "long unwinding"


def writer_signature(direction: str, ce, pe) -> str:
    """Aakash's reading of the ATM pair, from the WRITER's point of view.

    Up badge: puts being written and calls being unwound both say the people who
    sell options expect the level to hold. Down badge is the mirror.

    The two sides are judged against EACH OTHER, not each against zero. Both
    sides build on most active days, so "PE rose" alone proves nothing:
    ADANIGREEN, 18-Sep-2026, had PE OI +25.93% and CE OI +27.44% at its badge,
    which an absolute test calls put writing when calls in fact built harder.
    It then fell 1.38 in thirty minutes. The net is what carries the meaning.
    """
    if ce is None or pe is None:
        return "unknown"
    ce_oi, pe_oi = ce["look_oi_pct"], pe["look_oi_pct"]
    if ce_oi is None or pe_oi is None:
        return "unknown"
    # Positive = the side that supports the badge built more than the side
    # against it. Signed so up and down read the same way.
    net = (pe_oi - ce_oi) if direction == "up" else (ce_oi - pe_oi)
    supporting, opposing = (pe_oi, ce_oi) if direction == "up" else (ce_oi, pe_oi)
    writing = "put writing" if direction == "up" else "call writing"
    unwind = "call unwind" if direction == "up" else "put unwind"

    if abs(net) < OI_FLAT_PCT:
        return "balanced"
    if net < 0:
        # The side betting AGAINST the badge is the one building.
        return "written against"
    if supporting > OI_FLAT_PCT and opposing < -OI_FLAT_PCT:
        return f"{writing} + {unwind}"
    if supporting > OI_FLAT_PCT:
        return writing
    if opposing < -OI_FLAT_PCT:
        return unwind
    return "net supportive"


def forward(bars, badge_min, direction, minutes):
    """Signed move in the badge's direction `minutes` after it fired.

    Deliberately the same measure used on the rank-and-price filters already
    tested, so a result here can be compared against those directly instead of
    being a new number on a new scale.
    """
    entry = at_minute(bars, badge_min)
    later = at_minute(bars, badge_min + minutes)
    if not entry or not later or later is entry:
        return None
    sign = 1 if direction == "up" else -1
    return (later["close"] - entry["close"]) / entry["close"] * 100 * sign


def audit_day(day: str, limit: int | None, morning: bool) -> list[dict]:
    changes, ranks = get_boost_change_timeline(day), get_boost_rank_timeline(day)
    if not changes:
        print(f"  no snapshots for {day}")
        return []

    api_key = get_first_available_api_key()
    auth_token, broker = get_auth_token_broker(api_key, include_feed_token=False)
    recording_began = first_recorded_minute(day)

    symbols = sorted(changes)
    if morning:
        early = {
            sym
            for sym, days in ranks.items()
            if any(minute <= 10 * 60 for minute, _ in days.get(day, []))
        }
        symbols = [s for s in symbols if s in early]
    if limit:
        symbols = symbols[:limit]

    rows, skipped = [], {"no badge": 0, "already running": 0, "no fno": 0, "no bars": 0}
    for n, symbol in enumerate(symbols, start=1):
        cps, rps = changes[symbol].get(day, []), ranks.get(symbol, {}).get(day, [])
        if len(cps) < 12 or not rps:
            continue
        badge_min, row, _day_change = first_badge(symbol, cps, rps)
        if badge_min is None:
            skipped["no badge"] += 1
            continue
        # Already mid-run when the recorder woke: not an entry anyone could take.
        if recording_began is not None and badge_min <= recording_began + 1:
            skipped["already running"] += 1
            continue

        tradable = tradable_symbol(symbol)
        fut_name = near_future(tradable)
        if not fut_name:
            skipped["no fno"] += 1
            continue

        stock_bars = fetch_bars(tradable, "NSE", day, auth_token, broker)
        stock = outcome(stock_bars, badge_min, row["run_direction"]) if stock_bars else None
        if not stock:
            skipped["no bars"] += 1
            continue

        fut = oi_reading(fetch_bars(fut_name, "NFO", day, auth_token, broker), badge_min)

        legs = {}
        for side in ("CE", "PE"):
            contract = atm_contract(tradable, stock["entry"], side)
            legs[side] = (
                oi_reading(fetch_bars(contract[0], "NFO", day, auth_token, broker), badge_min)
                if contract
                else None
            )

        direction = row["run_direction"]
        rows.append(
            {
                "day": day,
                "symbol": symbol,
                "badge_min": badge_min,
                "direction": direction,
                "rank": row["current_rank"],
                "fut_class": buildup(
                    fut["look_px_pct"] if fut else None, fut["look_oi_pct"] if fut else None
                ),
                # Futures OI barely moves over 30 minutes -- on 18-Sep-2026, 29 of
                # 32 up badges read "oi flat" over the lookback, which says the
                # window is too short to carry a reading rather than that nothing
                # was built. The session window is the one that can answer for
                # futures; both are reported so the difference stays visible.
                "fut_sess_class": buildup(
                    fut["sess_px_pct"] if fut else None, fut["sess_oi_pct"] if fut else None
                ),
                "fut_oi_pct": fut["look_oi_pct"] if fut else None,
                "fut_sess_oi_pct": fut["sess_oi_pct"] if fut else None,
                "writers": writer_signature(direction, legs["CE"], legs["PE"]),
                "ce_oi_pct": legs["CE"]["look_oi_pct"] if legs["CE"] else None,
                "pe_oi_pct": legs["PE"]["look_oi_pct"] if legs["PE"] else None,
                "thin": any(
                    leg is not None and leg["volume"] < MIN_OI_VOLUME for leg in legs.values()
                ),
                "f30": forward(stock_bars, badge_min, direction, 30),
                "f60": forward(stock_bars, badge_min, direction, 60),
                "with_pct": stock["with_pct"],
                "against_pct": stock["against_pct"],
                "close_pct": stock["close_pct"],
            }
        )
        if n % 40 == 0:
            print(f"    ... {n}/{len(symbols)}", flush=True)

    print(f"  {day}: {len(rows)} badges measured   skipped {skipped}")
    return rows


def med(values):
    vals = sorted(v for v in values if v is not None)
    n = len(vals)
    return None if not n else (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)


def bucket_table(title: str, rows: list[dict], key: str) -> list[str]:
    out = [
        f"\n{title}",
        f"  {'bucket':<26}{'n':>4}{'+30m':>9}{'win%':>6}{'+60m':>9}{'MFE':>8}{'EOD':>8}",
    ]
    out.append("  " + "-" * 61)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    for name, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        f30 = [r["f30"] for r in group if r["f30"] is not None]
        wins = sum(1 for v in f30 if v > 0) / len(f30) * 100 if f30 else 0
        out.append(
            f"  {name:<26}{len(group):>4}"
            f"{(med(f30) or 0):>+9.3f}{wins:>6.0f}"
            f"{(med([r['f60'] for r in group]) or 0):>+9.3f}"
            f"{(med([r['with_pct'] for r in group]) or 0):>+8.2f}"
            f"{(med([r['close_pct'] for r in group]) or 0):>+8.2f}"
        )
    return out


def report(rows: list[dict], days: list[str]) -> str:
    lines = [
        f"OI confirmation for RUN badges -- {', '.join(days)}",
        f"{len(rows)} badges with an F&O contract, expiry {EXPIRY}",
        "",
        "+30m/+60m are the stock's move in the badge's direction after it fired.",
        "MFE is the best it ever went that way; EOD is where it closed.",
        "A bucket only means something if it beats the others by more than ~0.07,",
        "which is where every rank-and-price filter already tested landed.",
    ]
    for direction in ("up", "down"):
        group = [r for r in rows if r["direction"] == direction]
        if not group:
            continue
        lines.append("")
        lines.append("=" * 63)
        lines.append(f"{direction.upper()} BADGES  (n={len(group)})")
        lines.append("=" * 63)
        lines += bucket_table("BY FUTURES BUILDUP (30m before the badge)", group, "fut_class")
        lines += bucket_table(
            "BY FUTURES BUILDUP (since the session open)", group, "fut_sess_class"
        )
        lines += bucket_table("BY ATM WRITER SIGNATURE", group, "writers")

    thin = [r for r in rows if r["thin"]]
    if thin:
        lines.append(
            f"\n{len(thin)} badges had an ATM leg under {MIN_OI_VOLUME:,} volume; "
            "their OI change is reported but should not be relied on."
        )
    lines.append("\nPER BADGE")
    hdr = (
        f"  {'day':<11}{'symbol':<12}{'badge':>9}{'dir':>5}{'futOI%':>9}  "
        f"{'buildup':<16}{'CE%':>8}{'PE%':>8}  {'writers':<26}{'+30m':>8}"
    )
    lines.append(hdr)

    def num(value):
        """A missing reading prints as '-', never as 0.00 -- a contract with no
        bar is not a contract whose OI held still."""
        return "-" if value is None else f"{value:+.2f}"

    for r in sorted(rows, key=lambda x: (x["day"], x["badge_min"])):
        lines.append(
            f"  {r['day']:<11}{r['symbol']:<12}{clock(r['badge_min']):>9}{r['direction']:>5}"
            f"{num(r['fut_oi_pct']):>9}  "
            f"{r['fut_class']:<16}"
            f"{num(r['ce_oi_pct']):>8}{num(r['pe_oi_pct']):>8}  "
            f"{r['writers']:<26}"
            f"{num(r['f30']):>8}" + ("  thin" if r["thin"] else "")
        )
    return "\n".join(lines)


def main() -> None:
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = None
    for flag in flags:
        if flag.startswith("--limit"):
            limit = int(flag.split("=", 1)[1]) if "=" in flag else int(args.pop())
    morning = "--morning" in flags

    if "--all" in flags:
        from database.tf_boost_db import get_connection

        with get_connection() as conn:
            days = [
                str(d[0])
                for d in conn.execute(
                    "SELECT DISTINCT snapshot_date FROM tf_boost_snapshots "
                    "WHERE list_type = 'intraday_boost' ORDER BY snapshot_date"
                ).fetchall()
            ]
    else:
        days = [args[0] if args else datetime.now(IST).strftime("%Y-%m-%d")]

    usable = [d for d in days if in_expiry_cycle(d)]
    outside = [d for d in days if d not in usable]
    if outside:
        print(
            f"skipping {len(outside)} day(s) outside the {EXPIRY} cycle "
            f"({outside[0]} to {outside[-1]}) -- their near-month contract has "
            "expired and is no longer in the symbol master, so it cannot be "
            "read without services/expired_fno_service.py"
        )
    if not usable:
        print("no day falls inside the configured expiry cycle; set TF_AUDIT_EXPIRY")
        return
    print(f"days: {', '.join(usable)}")
    rows: list[dict] = []
    for day in usable:
        rows += audit_day(day, limit, morning)
    days = usable
    db_session.remove()

    if not rows:
        print("nothing measured")
        return
    text = report(rows, days)
    print(text)
    stamp = days[0] if len(days) == 1 else f"{days[0]}_to_{days[-1]}"
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "log",
        f"tf_boost_oi_confirm_{stamp}.txt",
    )
    with open(path, "w") as handle:
        handle.write(text + "\n")
    print(f"\nwritten to {path}")


if __name__ == "__main__":
    main()
