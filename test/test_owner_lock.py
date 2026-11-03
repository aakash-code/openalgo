"""Owner passcode lock: path guard and passcode routes (fork feature)."""

import time

import pytest
from flask import Flask, jsonify

import blueprints.owner_lock as bp_mod
import utils.owner_lock as lock


@pytest.fixture
def state(monkeypatch):
    s = {"code": None}
    monkeypatch.setattr(lock, "is_passcode_set", lambda: s["code"] is not None)
    monkeypatch.setattr(bp_mod, "is_passcode_set", lambda: s["code"] is not None)
    monkeypatch.setattr(
        bp_mod, "verify_passcode", lambda c: s["code"] is not None and c == s["code"]
    )
    monkeypatch.setattr(bp_mod, "set_passcode", lambda c: s.update(code=c or None))
    monkeypatch.setattr(bp_mod, "authenticate_user", lambda u, p: p == "loginpw")
    monkeypatch.setattr(bp_mod.limiter, "enabled", False, raising=False)
    import utils.session as us

    monkeypatch.setattr(us, "is_session_valid", lambda: True)
    return s


@pytest.fixture
def client(state):
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(bp_mod.owner_lock_bp)

    @app.route("/python/", methods=["GET"])
    @app.route("/python/api/strategies")
    @app.route("/python/status")
    def open_view():
        return jsonify(ok=True)

    @app.route("/python/api/strategy/<sid>/content")
    @app.route("/python/export/<sid>")
    @app.route("/python/delete/<sid>", methods=["POST"])
    @app.route("/python/stop/<sid>", methods=["POST"])
    @app.route("/python/save/<sid>", methods=["POST"])
    @app.route("/python/api/logs/<sid>")
    @app.route("/python/some-new-upstream-route", methods=["POST"])
    @app.route("/apikey", methods=["GET", "POST"])
    @app.route("/auth/change-password", methods=["POST"])
    def locked_view(sid=None):
        return jsonify(ok=True)

    c = app.test_client()
    with c.session_transaction() as sess:
        sess["logged_in"] = True
        sess["user"] = "owner"
    return c


JSON = {"Accept": "application/json"}
LOCKED = [
    ("get", "/python/api/strategy/s1/content"),
    ("get", "/python/export/s1"),
    ("post", "/python/delete/s1"),
    ("post", "/python/stop/s1"),
    ("post", "/python/save/s1"),
    ("get", "/python/api/logs/s1"),
    ("post", "/python/some-new-upstream-route"),
    ("get", "/apikey"),
    ("post", "/auth/change-password"),
]


def test_no_passcode_means_nothing_locked(client):
    for method, path in LOCKED:
        assert getattr(client, method)(path, headers=JSON).status_code == 200, path


def test_passcode_locks_owner_routes_but_not_watching(client, state):
    state["code"] = "1234"
    for method, path in LOCKED:
        r = getattr(client, method)(path, headers=JSON)
        assert r.status_code == 403, path
        assert r.get_json()["error"] == "owner_locked"
    for path in ("/python/", "/python/api/strategies", "/python/status"):
        assert client.get(path, headers=JSON).status_code == 200, path


def test_page_shell_loads_but_its_data_does_not(client, state):
    state["code"] = "1234"
    assert client.get("/apikey", headers={"Accept": "text/html"}).status_code == 200
    assert client.get("/apikey", headers=JSON).status_code == 403


def test_wrong_then_right_passcode(client, state):
    state["code"] = "1234"
    assert client.post("/owner-lock/unlock", json={"passcode": "9999"}).status_code == 400
    assert client.post("/python/delete/s1", headers=JSON).status_code == 403
    assert client.post("/owner-lock/unlock", json={"passcode": "1234"}).status_code == 200
    assert client.post("/python/delete/s1", headers=JSON).status_code == 200
    client.post("/owner-lock/lock")
    assert client.post("/python/delete/s1", headers=JSON).status_code == 403


def test_unlock_expires(client, state):
    state["code"] = "1234"
    with client.session_transaction() as sess:
        sess[lock.SESSION_KEY] = time.time() - 1
    assert client.post("/python/delete/s1", headers=JSON).status_code == 403


def test_first_passcode_needs_login_password(client, state):
    assert (
        client.post(
            "/owner-lock/passcode", json={"new_passcode": "1234", "login_password": "x"}
        ).status_code
        == 400
    )
    assert state["code"] is None
    r = client.post(
        "/owner-lock/passcode", json={"new_passcode": "1234", "login_password": "loginpw"}
    )
    assert r.status_code == 200 and state["code"] == "1234"


def test_change_or_remove_needs_current_passcode(client, state):
    state["code"] = "1234"
    r = client.post(
        "/owner-lock/passcode", json={"new_passcode": "5678", "login_password": "loginpw"}
    )
    assert r.status_code == 400 and state["code"] == "1234"
    assert (
        client.post(
            "/owner-lock/passcode", json={"remove": True, "current_passcode": "0000"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/owner-lock/passcode", json={"remove": True, "current_passcode": "1234"}
        ).status_code
        == 200
    )
    assert state["code"] is None


def test_short_passcode_refused(client, state):
    r = client.post(
        "/owner-lock/passcode", json={"new_passcode": "12", "login_password": "loginpw"}
    )
    assert r.status_code == 400 and state["code"] is None


def test_status(client, state):
    assert client.get("/owner-lock/status").get_json()["enabled"] is False
    state["code"] = "1234"
    body = client.get("/owner-lock/status").get_json()
    assert body["enabled"] is True and body["unlocked"] is False
