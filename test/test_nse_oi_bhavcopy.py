"""NSE's F&O bhavcopy as the Change-in-OI anchor.

One file carries the previous session's open interest for every contract, so a
symbol switch costs no broker history calls at all. What has to hold: each row
maps onto the symbol the rest of the platform uses, the numbers arrive in the
broker's own units, and a leg the file cannot speak for still falls through to
the per-leg path rather than being silently anchored at zero.

No network - the fixture builds a file in memory.
"""

import csv
import io
import zipfile
from datetime import date

import pytest

import services.nse_oi_bhavcopy as bhav
import services.oi_profile_service as svc

ROWS = [
    # (TckrSymb, XpryDt, StrkPric, OptnTp, OpnIntrst)
    ("NIFTY", "2026-09-22", "23200.00", "PE", "7467135"),
    ("NIFTY", "2026-09-22", "23200.00", "CE", "1000000"),
    ("VEDL", "2026-09-29", "292.50", "CE", "50000"),
    ("TVSMOTOR", "2026-09-29", "3850.00", "CE", "0"),
]


def _zipped_csv(rows=ROWS):
    """A bhavcopy archive with the columns the parser reads."""
    header = ["TckrSymb", "XpryDt", "StrkPric", "OptnTp", "OpnIntrst"]
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    # A futures row carries no OptnTp and must be skipped, not mis-keyed.
    writer.writerow(["NIFTY", "2026-09-29", "0.00", "", "123456"])

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("BhavCopy_NSE_FO_0_0_0_20260916_F_0000.csv", text.getvalue())
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clear_book_cache():
    bhav._book_cache.clear()
    bhav._failed_at.clear()
    yield
    bhav._book_cache.clear()
    bhav._failed_at.clear()


def test_a_row_maps_onto_the_symbol_the_platform_uses():
    oi, underlyings = bhav._parse(_zipped_csv())
    # Whole strikes are written bare and fractional ones keep their decimal,
    # the way the symbol master writes them. Checked against symtoken: all
    # 35,010 option rows in the 16-Sep-2026 file matched a known symbol.
    assert oi["NIFTY22SEP2623200PE"] == 7_467_135
    assert oi["VEDL29SEP26292.5CE"] == 50_000
    assert underlyings == frozenset({"NIFTY", "VEDL", "TVSMOTOR"})
    # The futures row (NIFTY, 29-Sep, strike 0.00) has no option type and
    # belongs to no option symbol. No NIFTY option in the fixture expires on
    # 29-Sep, so any NIFTY29SEP26 key could only have come from that row.
    assert not [s for s in oi if s.startswith("NIFTY29SEP26")]


def test_a_ticker_with_an_unreadable_row_is_not_spoken_for():
    # One malformed OI cell must not turn that contract into a zero anchor:
    # the whole ticker goes back to the broker instead.
    rows = [*ROWS, ("VEDL", "2026-09-29", "300.00", "CE", "n/a")]
    oi, underlyings = bhav._parse(_zipped_csv(rows))
    assert "VEDL" not in underlyings
    assert "NIFTY" in underlyings
    assert "VEDL29SEP26300CE" not in oi


def test_the_anchor_matches_what_the_per_leg_path_reports():
    # The number this whole file exists to replace. NIFTY22SEP2623200PE closed
    # 15-Sep-2026 at 4,552,925 - verified against NSE, and pinned in
    # test_oi_profile_multi_expiry. The 16-Sep file reads 7,467,135 with a
    # day's change of 2,914,210, and 7,467,135 - 2,914,210 is that close, so
    # the file's open interest is in the broker's units with no conversion.
    oi, _ = bhav._parse(_zipped_csv())
    assert oi["NIFTY22SEP2623200PE"] - 2_914_210 == 4_552_925


def test_underlying_is_read_back_off_the_symbol():
    assert bhav.underlying_of("NIFTY22SEP2623200PE") == "NIFTY"
    assert bhav.underlying_of("TVSMOTOR29SEP263850CE") == "TVSMOTOR"
    assert bhav.underlying_of("VEDL29SEP26292.5CE") == "VEDL"
    assert bhav.underlying_of("") is None


def _book():
    oi, underlyings = bhav._parse(_zipped_csv())
    return bhav.Bhavcopy(date(2026, 9, 16), oi, underlyings)


def test_the_file_answers_the_legs_it_covers():
    svc._prev_oi_cache.clear()
    legs = ["NIFTY22SEP2623200PE", "VEDL29SEP26292.5CE"]
    resolved, remaining = svc._resolve_from_nse(legs, _book(), "NFO", "2026-09-17")
    assert resolved == {"NIFTY22SEP2623200PE": 7_467_135, "VEDL29SEP26292.5CE": 50_000}
    assert remaining == []


def test_a_strike_absent_from_a_covered_underlying_is_anchored_at_zero():
    # The file lists every contract that traded or held open interest. A NIFTY
    # strike missing from it held none, so all of today's open interest is a
    # fresh build - unlike a failed broker read, where absence means unknown
    # and a zero anchor would paint the strike as a huge write.
    svc._prev_oi_cache.clear()
    resolved, remaining = svc._resolve_from_nse(
        ["NIFTY22SEP2699999CE"], _book(), "NFO", "2026-09-17"
    )
    assert resolved == {"NIFTY22SEP2699999CE": 0.0}
    assert remaining == []


def test_an_underlying_the_file_does_not_cover_still_goes_to_the_broker():
    # An F&O name listed today is absent from yesterday's file entirely.
    # Anchoring its every strike at zero would report the whole chain as built
    # today, so it has to fall through to the per-leg path instead.
    svc._prev_oi_cache.clear()
    resolved, remaining = svc._resolve_from_nse(
        ["NEWNAME29SEP261000CE"], _book(), "NFO", "2026-09-17"
    )
    assert resolved == {}
    assert remaining == ["NEWNAME29SEP261000CE"]


def test_only_exchanges_with_a_file_are_served_from_one():
    # Crypto has no closing file and keeps the per-leg fallback.
    assert bhav.previous_session_oi("CRYPTO") is None
    assert bhav.cached_previous_session_oi("CRYPTO") is None


def test_bse_plain_csv_reads_like_the_nse_zip():
    # BSE serves the same UDiFF columns unzipped. Without its file, SENSEX max
    # pain was worked out from the fetched strikes alone, and on 9 of 20 days
    # (26-Oct to 23-Nov-2026) it differed from the full chain by 100-500 points.
    raw = (
        b"TckrSymb,XpryDt,StrkPric,OptnTp,OpnIntrst\n"
        b"SENSEX,2026-11-26,83500.00,CE,123460\n"
        b"SENSEX,2026-11-26,81000.00,PE,98760\n"
        b"SENSEX,2026-11-26,0,,555\n"
    )
    oi, names = bhav._parse(raw)
    assert oi == {"SENSEX26NOV2683500CE": 123460.0, "SENSEX26NOV2681000PE": 98760.0}
    assert names == frozenset({"SENSEX"})


def test_each_exchange_keeps_its_own_file(monkeypatch):
    asked = []
    monkeypatch.setattr(bhav, "_displayed_session", lambda today, *_: today)
    monkeypatch.setattr(bhav, "_session_before", lambda day, *_: None)
    monkeypatch.setattr(
        bhav,
        "_newest_before",
        lambda day, exchange="NFO": (
            asked.append(exchange) or bhav.Bhavcopy(day, {exchange: 1.0}, frozenset())
        ),
    )
    bhav._book_cache.clear()
    assert bhav.previous_session_oi("NFO").oi == {"NFO": 1.0}
    assert bhav.previous_session_oi("BFO").oi == {"BFO": 1.0}
    assert bhav.cached_previous_session_oi("NFO").oi == {"NFO": 1.0}
    assert asked == ["NFO", "BFO"]
    bhav._book_cache.clear()


def test_a_failure_is_not_retried_on_every_miss(monkeypatch):
    # NSE being unreachable at 09:15 must not cost the session, but it must
    # also not put a download in front of every leg that misses.
    attempts = []
    monkeypatch.setattr(bhav, "_download", lambda day, *_: attempts.append(day) or None)
    monkeypatch.setattr(bhav, "_displayed_session", lambda today, *_: today)

    assert bhav.previous_session_oi("NFO") is None
    first = len(attempts)
    assert first > 0, "it should have looked for a file"
    assert bhav.previous_session_oi("NFO") is None
    assert len(attempts) == first, "the second miss must not download again"


def test_an_nse_outage_stops_the_search(monkeypatch):
    # An outage is not ten missing dates: one failed request ends the walk, so
    # the per-leg broker fallback starts instead of waiting out ten timeouts.
    attempts = []

    def down(day, *_):
        attempts.append(day)
        raise bhav._NseUnavailable

    monkeypatch.setattr(bhav, "_download", down)
    assert bhav._newest_before(date(2026, 9, 16)) is None
    assert len(attempts) == 1


def test_a_failed_warm_up_leaves_no_cooldown(monkeypatch):
    # The boot warm-up is speculative. If it fails, the first real request must
    # still try NSE rather than sit out the retry window.
    attempts = []
    monkeypatch.setattr(bhav, "_download", lambda day, *_: attempts.append(day) or None)
    monkeypatch.setattr(bhav, "_displayed_session", lambda today, *_: today)
    bhav._failed_at.clear()

    assert bhav.previous_session_oi("NFO", record_failure=False) is None
    first = len(attempts)
    assert bhav.previous_session_oi("NFO") is None
    assert len(attempts) == 2 * first, "the request after a failed warm-up must try again"


def test_an_older_file_is_not_taken_for_the_expected_session(monkeypatch):
    # The expected anchor file (15-Sep) is not up yet, so the walk finds 14-Sep.
    # Caching that would anchor two days of build as one; it must be refused.
    calls = []

    def download(day, *_):
        calls.append(day)
        return _zipped_csv() if day == date(2026, 9, 14) else None

    monkeypatch.setattr(bhav, "_download", download)
    monkeypatch.setattr(bhav, "_displayed_session", lambda today, *_: date(2026, 9, 16))
    monkeypatch.setattr(bhav, "is_market_holiday", lambda day, exchange=None: day.weekday() >= 5)
    bhav._failed_at.clear()

    assert bhav.previous_session_oi("NFO") is None
    # The walk did reach the older file, so the None is the refusal, not a miss.
    assert date(2026, 9, 14) in calls
    assert bhav.cached_previous_session_oi("NFO") is None


def test_the_expected_file_is_cached(monkeypatch):
    monkeypatch.setattr(
        bhav, "_download", lambda day, *_: _zipped_csv() if day == date(2026, 9, 15) else None
    )
    monkeypatch.setattr(bhav, "_displayed_session", lambda today, *_: date(2026, 9, 16))
    monkeypatch.setattr(bhav, "is_market_holiday", lambda day, exchange=None: day.weekday() >= 5)
    bhav._failed_at.clear()

    book = bhav.previous_session_oi("NFO")
    assert book is not None and book.trade_date == date(2026, 9, 15)
    assert bhav.cached_previous_session_oi("NFO") is book


def test_a_file_with_no_options_is_not_a_session(monkeypatch):
    # BSE served a file for the 24-Nov-2026 holiday with no options in it.
    empty = b"TckrSymb,XpryDt,StrkPric,OptnTp,OpnIntrst\n"
    full = b"TckrSymb,XpryDt,StrkPric,OptnTp,OpnIntrst\nSENSEX,2026-11-26,83500,CE,10\n"
    files = {date(2026, 11, 24): empty, date(2026, 11, 23): full}
    monkeypatch.setattr(bhav, "_download", lambda day, *_: files.get(day))
    book = bhav._newest_before(date(2026, 11, 25), "BFO")
    assert book.trade_date == date(2026, 11, 23)
