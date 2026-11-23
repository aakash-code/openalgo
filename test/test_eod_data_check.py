"""The end-of-day data check's pure parts: comparing against the exchange's
close, the two records agreeing, and the sentences an alert carries."""

from services.eod_data_check import WILD_MIN_OI, agree, compare, problems

EX = {
    "NIFTY24NOV2625000CE": 1_000_000.0,
    "NIFTY24NOV2625000PE": 2_000_000.0,
    "NIFTY24NOV2626350PE": 65.0,
}


def report(rec, tic, **extra):
    return {
        "day": "2026-11-24",
        "underlying": "NIFTY",
        "records_agree": agree(rec, tic),
        "recorder_minutes_share": 1.0,
        "index_minutes_share": 1.0,
        "longest_gap_seconds": 2.0,
        "longest_gap_from": "10:00:00",
        "sources": [compare("recorder", rec, EX), compare("ticks", tic, EX)],
        **extra,
    }


def test_distance_from_the_close_is_measured_not_demanded():
    ours = {"NIFTY24NOV2625000CE": 1_030_000, "NIFTY24NOV2625000PE": 2_000_000}
    r = compare("recorder", ours, EX)
    assert (r.compared, r.exact) == (2, 1)
    assert r.median_share == 0.015  # 3% and 0%
    assert r.wild == [] and r.unknown == []
    assert problems(report(ours, dict(ours))) == []


def test_a_contract_far_off_the_close_is_a_problem_but_a_tiny_one_is_not():
    ours = {"NIFTY24NOV2625000CE": 1_400_000, "NIFTY24NOV2626350PE": 130}
    r = compare("recorder", ours, EX)
    assert [w["symbol"] for w in r.wild] == ["NIFTY24NOV2625000CE"]
    assert 130 < WILD_MIN_OI
    lines = problems(report(ours, dict(ours)))
    assert any("more than 25%" in line and "NIFTY24NOV2625000CE" in line for line in lines)


def test_a_symbol_the_exchange_does_not_know_is_a_mapping_fault():
    ours = {"NIFTY24NOV26250000CE": 10}
    r = compare("recorder", ours, EX)
    assert r.unknown == ["NIFTY24NOV26250000CE"] and r.compared == 0
    assert any("not in the exchange's file" in line for line in problems(report(ours, dict(ours))))


def test_our_two_records_must_agree_exactly():
    rec = {"NIFTY24NOV2625000CE": 1_000_000, "NIFTY24NOV2625000PE": 2_000_000}
    tic = {"NIFTY24NOV2625000CE": 1_000_000, "NIFTY24NOV2625000PE": 1_999_935}
    a = agree(rec, tic)
    assert (a["compared"], a["exact"]) == (2, 1)
    assert any("disagree on 1 of 2" in line for line in problems(report(rec, tic)))


def test_coverage_shortfalls_and_feed_gaps_are_named_with_their_time():
    ours = {"NIFTY24NOV2625000CE": 1_000_000}
    lines = problems(
        report(
            ours,
            dict(ours),
            recorder_minutes_share=0.95,
            longest_gap_seconds=124.3,
            longest_gap_from="09:22:34",
        )
    )
    assert any("95.0% of the session's minutes" in line for line in lines)
    assert any("124 seconds from 09:22:34" in line for line in lines)
