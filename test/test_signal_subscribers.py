"""Subscriber credentials and HMAC webhook signing.

These are the access-control tests for a paid feed: a revoked key must stop
working immediately, a wrong key must never authenticate, and a signature must
not be replayable.
"""

import hashlib
import hmac
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from database import signal_db
from services.signal_webhook_service import sign_payload


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'signals.db'}")
    session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    signal_db.Base.metadata.create_all(engine)
    monkeypatch.setattr(signal_db, "db_session", session)
    yield session
    session.remove()


class TestSubscriberAuth:
    def test_created_key_authenticates(self, db):
        api_key, _secret = signal_db.create_subscriber("Acme")
        sub = signal_db.verify_subscriber(api_key)
        assert sub is not None
        assert sub.name == "Acme"

    def test_plaintext_key_is_never_stored(self, db):
        api_key, _ = signal_db.create_subscriber("Acme")
        row = db.query(signal_db.SignalSubscriber).one()
        assert api_key not in row.key_hash
        assert row.key_hash.startswith("$argon2")

    def test_wrong_key_is_rejected(self, db):
        signal_db.create_subscriber("Acme")
        assert signal_db.verify_subscriber("not-the-key") is None
        assert signal_db.verify_subscriber("") is None

    def test_each_subscriber_gets_a_distinct_key(self, db):
        key_a, secret_a = signal_db.create_subscriber("A")
        key_b, secret_b = signal_db.create_subscriber("B")
        assert key_a != key_b
        assert secret_a != secret_b
        assert signal_db.verify_subscriber(key_a).name == "A"
        assert signal_db.verify_subscriber(key_b).name == "B"

    def test_revoked_key_stops_working_immediately(self, db):
        api_key, _ = signal_db.create_subscriber("Acme")
        sub = signal_db.verify_subscriber(api_key)

        assert signal_db.revoke_subscriber(sub.id) is True
        assert signal_db.verify_subscriber(api_key) is None, "a revoked key must not authenticate"

    def test_revoking_an_unknown_subscriber_is_reported(self, db):
        assert signal_db.revoke_subscriber(9999) is False


class TestWebhookRouting:
    def test_only_active_subscribers_with_a_url_receive_pushes(self, db):
        signal_db.create_subscriber("PullOnly")  # no webhook
        signal_db.create_subscriber("Pusher", webhook_url="https://acme.example/hook")
        key_gone, _ = signal_db.create_subscriber("Gone", webhook_url="https://gone.example/hook")

        signal_db.revoke_subscriber(signal_db.verify_subscriber(key_gone).id)

        names = [s.name for s in signal_db.get_webhook_subscribers()]
        assert names == ["Pusher"]

    def test_hmac_secret_round_trips(self, db):
        _key, secret = signal_db.create_subscriber("Acme", webhook_url="https://acme.example/hook")
        sub = signal_db.get_webhook_subscribers()[0]
        assert signal_db.decrypt_hmac_secret(sub) == secret

    def test_secret_is_encrypted_at_rest(self, db):
        _key, secret = signal_db.create_subscriber("Acme", webhook_url="https://acme.example/hook")
        row = db.query(signal_db.SignalSubscriber).one()
        assert secret not in (row.hmac_secret_enc or "")


class TestSigning:
    def test_signature_matches_an_independent_computation(self):
        """A subscriber implementing the documented scheme must get the same MAC."""
        secret = "s3cret"
        body = json.dumps({"symbol": "TATAMOTORS"}, separators=(",", ":"))
        ts = "1785516434"

        expected = hmac.new(
            secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256
        ).hexdigest()
        assert sign_payload(secret, ts, body) == f"sha256={expected}"

    def test_timestamp_is_inside_the_mac_so_it_cannot_be_replayed(self):
        """Changing only the timestamp must invalidate the signature.

        If the timestamp were merely a sibling header, a captured request could
        be replayed indefinitely with a fresh one.
        """
        body = '{"symbol":"TATAMOTORS"}'
        assert sign_payload("s3cret", "1785516434", body) != sign_payload(
            "s3cret", "1785516999", body
        )

    def test_a_different_secret_produces_a_different_signature(self):
        body = '{"symbol":"TATAMOTORS"}'
        assert sign_payload("secret-a", "1", body) != sign_payload("secret-b", "1", body)

    def test_tampering_with_the_body_invalidates_the_signature(self):
        ts = "1785516434"
        good = sign_payload("s3cret", ts, '{"entry":742.3}')
        tampered = sign_payload("s3cret", ts, '{"entry":1.0}')
        assert good != tampered
