"""TradeFinder's background work is switched on or off from the panel, no restart.

On 19-Oct-2026 TradeFinder's enrichment asked Upstox for thousands of history
calls per 30 minutes against an allowance of 2,000 requests for everything,
and charts, quotes, the sandbox position book and the Writer Flow recorder
waited minutes behind it. The only way to stop it was an .env key and a
restart in the middle of the session.
"""

from __future__ import annotations

import pytest
from flask import Flask

import database.tf_boost_db as tdb
import services.tf_settings as tfs


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(tdb, "TF_BOOST_DB_PATH", str(tmp_path / "tf.duckdb"))
    monkeypatch.setattr(tfs, "_enabled", None)
    started = []
    monkeypatch.setattr(tfs, "start_background", lambda: started.append(True))
    return started


def test_an_install_that_never_used_the_switch_follows_env(fresh, monkeypatch, tmp_path):
    monkeypatch.setenv("TF_BOOST_SNAPSHOT_ENABLED", "true")
    assert tfs.background_enabled() is True
    assert not (tmp_path / "tf.duckdb").exists()  # reading creates nothing


def test_switching_off_holds_over_env_and_restarts(fresh, monkeypatch):
    monkeypatch.setenv("TF_BOOST_SNAPSHOT_ENABLED", "true")
    tfs.set_background_enabled(False)
    assert tfs.background_enabled() is False
    monkeypatch.setattr(tfs, "_enabled", None)  # a restart: read from the database
    assert tfs.background_enabled() is False


def test_switching_on_starts_the_scheduler_at_once(fresh, monkeypatch):
    monkeypatch.setenv("TF_BOOST_SNAPSHOT_ENABLED", "false")
    assert tfs.background_enabled() is False
    tfs.set_background_enabled(True)
    assert tfs.background_enabled() is True
    assert fresh == [True]


def test_scheduled_jobs_stand_down_while_off(fresh, monkeypatch):
    import services.tf_boost_snapshot_service as snap

    calls = []
    guarded = snap._only_when_on(lambda: calls.append("ran"))
    tfs.set_background_enabled(False)
    guarded()
    tfs.set_background_enabled(True)
    guarded()
    assert calls == ["ran"]
    for job in (
        snap._run_snapshot_tick,
        snap._run_morning_check,
        snap._run_daily_study,
        snap._run_latency_check,
        snap._startup_catchup,
    ):
        assert hasattr(job, "__wrapped__"), job.__name__


def test_the_panel_serves_cached_enrichment_without_fetching_while_off(fresh, monkeypatch):
    import restx_api.tf_market_pulse as mp

    fetched = []
    for name in (
        "ensure_cpr_cache",
        "ensure_first_candle_cache",
        "ensure_directional_score_cache",
        "ensure_future_liquidity_cache",
        "ensure_momentum_setup_cache",
    ):
        monkeypatch.setattr(mp, name, lambda *a, n=name: fetched.append(n))
    for name in (
        "attach_cpr",
        "attach_first_candle",
        "attach_directional_score",
        "attach_future_liquidity",
        "attach_momentum_setup",
    ):
        monkeypatch.setattr(mp, name, lambda items: None)
    monkeypatch.setattr(mp, "momentum_signals_today", lambda: [])
    monkeypatch.setattr(mp, "fetch_market_pulse", lambda: {"intraday_boost": [{"symbol": "SBIN"}]})
    monkeypatch.setattr(mp, "get_auth_token_broker", lambda *a, **k: ("t", "upstox"))

    app = Flask(__name__)
    resource = mp.TfMarketPulse()
    tfs.set_background_enabled(False)
    with app.test_request_context(json={"apikey": "k"}):
        resource.post()
    assert fetched == []
    tfs.set_background_enabled(True)
    with app.test_request_context(json={"apikey": "k"}):
        resource.post()
    assert len(fetched) == 5


def test_the_settings_route_reads_and_switches(fresh, monkeypatch):
    import blueprints.tradefinder_proxy as bp

    monkeypatch.setattr(bp, "check_session_validity", lambda f: f)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="tf-settings")
    app.add_url_rule(
        "/trading/tradefinder/settings",
        view_func=bp.tradefinder_settings.__wrapped__,
        methods=["GET", "POST"],
    )
    c = app.test_client()
    with c.session_transaction() as s:
        s["user"] = "trader"
    assert (
        c.post("/trading/tradefinder/settings", json={"background_enabled": False}).get_json()[
            "background_enabled"
        ]
        is False
    )
    assert c.get("/trading/tradefinder/settings").get_json()["background_enabled"] is False
    bad = c.post("/trading/tradefinder/settings", json={"background_enabled": "yes"})
    assert bad.status_code == 400
    assert "on or off" in bad.get_json()["message"]
