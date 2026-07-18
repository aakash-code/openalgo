"""
Tests for the /TFJWT TradeFinder token update in strategies/isi_v56_live_strategy.py.

Covers apply_tf_jwt_update() — validate (structure / decodable exp / not expired /
strictly-newer-than-current) → backup existing → atomic replace of every configured
tf_jwt.txt → masked audit → reply. It must NEVER raise and NEVER overwrite on a
validation failure, and must never leak the full token.

A TradeFinder JWT here is any `header.payload.signature` string whose base64url
payload carries an `exp` unix-seconds claim — the decoder only reads the payload,
so tests mint tokens with controlled expiries without any real signing.
"""

import base64
import importlib.util
import json
import site
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "strategies" / "isi_v56_live_strategy.py"


@pytest.fixture(scope="module")
def isi():
    if "openalgo" not in sys.modules or not hasattr(sys.modules["openalgo"], "api"):
        for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
            sdk = Path(site_dir) / "openalgo" / "__init__.py"
            if sdk.exists():
                spec = importlib.util.spec_from_file_location(
                    "openalgo", sdk, submodule_search_locations=[str(sdk.parent)]
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules["openalgo"] = mod
                spec.loader.exec_module(mod)
                break
    spec = importlib.util.spec_from_file_location("isi_tfjwt_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _make_jwt(exp_ist: datetime) -> str:
    """Mint a decodable JWT whose exp (interpreted by the module as UTC->IST)
    yields exp_ist. The module adds +5:30 to a UTC epoch, so we subtract it."""
    exp_utc = exp_ist.replace(tzinfo=UTC)
    exp_epoch = int(exp_utc.timestamp()) - int(5.5 * 3600)
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url({"email": "x@example.com", "exp": exp_epoch, "plan": "DIAMOND"})
    return f"{header}.{payload}.signaturepart"


@pytest.fixture
def env(isi, tmp_path, monkeypatch):
    """Point the module's token-file list at a temp file and pin now_ist()."""
    fixed_now = datetime(2026, 7, 17, 10, 0, 0)
    monkeypatch.setattr(isi, "now_ist", lambda: fixed_now)
    monkeypatch.setattr(isi, "jlog", lambda *a, **k: None)  # don't touch the real audit file
    token_file = tmp_path / "tf_jwt.txt"
    monkeypatch.setattr(isi, "TF_JWT_FILE", str(token_file))
    monkeypatch.setattr(isi, "_ALL_TF_JWT_FILES", [str(token_file)])
    return {"now": fixed_now, "file": token_file, "tmp": tmp_path}


# ── happy path ───────────────────────────────────────────────────────────────


def test_valid_newer_token_is_written_atomically_with_backup(isi, env, monkeypatch):
    env["file"].write_text(_make_jwt(datetime(2026, 7, 17, 11, 0, 0)))  # current: 11:00
    captured = {}
    monkeypatch.setattr(isi, "jlog", lambda ev, **k: captured.update(k, event=ev))

    new = _make_jwt(datetime(2026, 7, 17, 13, 0, 0))  # newer: 13:00
    ok, reply = isi.apply_tf_jwt_update(new)

    assert ok is True
    assert reply.startswith("✅ TradeFinder token updated")
    assert "13:00 IST" in reply
    assert env["file"].read_text().strip() == new           # file replaced
    assert (env["tmp"] / "tf_jwt.txt.previous").exists()      # backup written
    # audit is masked, never the full token
    assert captured.get("source") == "telegram"
    assert new not in json.dumps(captured)
    assert captured.get("masked", "").endswith(new[-3:])


def test_first_ever_token_when_no_file_exists(isi, env):
    assert not env["file"].exists()
    new = _make_jwt(datetime(2026, 7, 17, 13, 0, 0))
    ok, reply = isi.apply_tf_jwt_update(new)
    assert ok is True
    assert env["file"].read_text().strip() == new


# ── rejections (must NOT overwrite) ──────────────────────────────────────────


def test_malformed_token_rejected(isi, env):
    env["file"].write_text("EXISTING")
    ok, reply = isi.apply_tf_jwt_update("not-a-jwt")
    assert ok is False and "not a JWT" in reply
    assert env["file"].read_text() == "EXISTING"             # untouched


def test_undecodable_or_no_exp_rejected(isi, env):
    env["file"].write_text("EXISTING")
    # three segments but payload has no exp
    tok = f"{_b64url({'a': 1})}.{_b64url({'no': 'exp'})}.sig"
    ok, reply = isi.apply_tf_jwt_update(tok)
    assert ok is False and ("decode" in reply or "exp" in reply)
    assert env["file"].read_text() == "EXISTING"


def test_expired_token_rejected(isi, env):
    env["file"].write_text("EXISTING")
    past = _make_jwt(datetime(2026, 7, 17, 9, 0, 0))  # before pinned now (10:00)
    ok, reply = isi.apply_tf_jwt_update(past)
    assert ok is False and "expired" in reply
    assert env["file"].read_text() == "EXISTING"


def test_not_newer_than_current_rejected(isi, env):
    env["file"].write_text(_make_jwt(datetime(2026, 7, 17, 13, 0, 0)))   # current: 13:00
    same_or_older = _make_jwt(datetime(2026, 7, 17, 12, 0, 0))            # 12:00 <= 13:00
    ok, reply = isi.apply_tf_jwt_update(same_or_older)
    assert ok is False and "not newer" in reply
    # file still holds the 13:00 token (unchanged)
    assert isi._jwt_expiry_from_token(env["file"].read_text().strip()) == datetime(2026, 7, 17, 13, 0, 0)


# ── multi-instance fan-out ───────────────────────────────────────────────────


def test_updates_all_configured_files(isi, env, monkeypatch):
    f2 = env["tmp"] / "variant" / "tf_jwt.txt"
    monkeypatch.setattr(isi, "_ALL_TF_JWT_FILES", [str(env["file"]), str(f2)])
    new = _make_jwt(datetime(2026, 7, 17, 14, 0, 0))
    ok, reply = isi.apply_tf_jwt_update(new)
    assert ok is True
    assert env["file"].read_text().strip() == new
    assert f2.read_text().strip() == new                     # sibling instance also updated


# ── error isolation ──────────────────────────────────────────────────────────


def test_never_raises_and_reports_when_no_file_writable(isi, env, monkeypatch):
    env["file"].write_text("EXISTING")
    # Make the atomic replace explode for every target -> 0 written, no raise.
    monkeypatch.setattr(isi.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("ro fs")))
    new = _make_jwt(datetime(2026, 7, 17, 13, 0, 0))
    ok, reply = isi.apply_tf_jwt_update(new)   # must not raise
    assert ok is False and "no file could be written" in reply


def test_mask_never_reveals_full_token(isi):
    tok = _make_jwt(datetime(2026, 7, 17, 13, 0, 0))
    masked = isi._mask_jwt(tok)
    assert tok not in masked
    assert masked.startswith(tok[:7]) and masked.endswith(tok[-3:])


# ── command wiring ───────────────────────────────────────────────────────────


def test_cmd_tfjwt_usage_when_no_arg(isi, env):
    bot = isi.TgQueryBot.__new__(isi.TgQueryBot)  # no thread/network init
    assert bot._cmd_tfjwt([]) == "Usage: /TFJWT <token>"


def test_cmd_tfjwt_applies_token(isi, env):
    bot = isi.TgQueryBot.__new__(isi.TgQueryBot)
    new = _make_jwt(datetime(2026, 7, 17, 13, 30, 0))
    reply = bot._cmd_tfjwt([new])
    assert reply.startswith("✅ TradeFinder token updated")
    assert env["file"].read_text().strip() == new
