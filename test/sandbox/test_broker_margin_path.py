import os
import sys
from decimal import Decimal
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

existing_sandbox = sys.modules.get("sandbox")
if existing_sandbox and getattr(existing_sandbox, "__file__", "").startswith(
    os.path.join(PROJECT_ROOT, "test", "sandbox")
):
    del sys.modules["sandbox"]

from database.sandbox_db import SandboxOrders, SandboxPositions, db_session, init_db
from sandbox.order_manager import OrderManager

init_db()


def cleanup_user(user_id: str):
    SandboxOrders.query.filter_by(user_id=user_id).delete()
    SandboxPositions.query.filter_by(user_id=user_id).delete()
    db_session.commit()


def test_precheck_orders_uses_broker_basket_delta(monkeypatch):
    user_id = "TEST_MARGIN_DELTA"
    cleanup_user(user_id)
    order_manager = OrderManager(user_id)

    monkeypatch.setattr(
        order_manager,
        "_get_derivative_margin_positions",
        lambda: [
            {
                "symbol": "NIFTY24APR2522500CE",
                "exchange": "NFO",
                "action": "SELL",
                "quantity": 50,
                "product": "MIS",
                "pricetype": "MARKET",
                "price": 120.0,
            }
        ],
    )

    def fake_broker_margin(positions):
        if len(positions) == 1:
            return Decimal("150000"), "ok"
        return Decimal("90000"), "ok"

    monkeypatch.setattr(order_manager, "_calculate_broker_margin_for_positions", fake_broker_margin)

    success, response, status_code = order_manager.precheck_orders(
        [
            {
                "symbol": "NIFTY24APR2522000PE",
                "exchange": "NFO",
                "action": "SELL",
                "quantity": 50,
                "product": "MIS",
                "pricetype": "MARKET",
                "price": 110.0,
            }
        ]
    )

    assert success is True
    assert status_code == 200
    assert response["margin_source"] == "broker_api"
    assert Decimal(response["required_margin"]) == Decimal("0")


def test_precheck_orders_strict_broker_rejects_on_margin_failure(monkeypatch):
    user_id = "TEST_MARGIN_STRICT"
    cleanup_user(user_id)
    order_manager = OrderManager(user_id)

    monkeypatch.setattr(order_manager, "_get_derivative_margin_positions", lambda: [])
    monkeypatch.setattr(
        order_manager, "_calculate_broker_margin_for_positions", lambda positions: (None, "broker down")
    )
    monkeypatch.setattr(order_manager, "_get_analyzer_broker_margin_mode", lambda: "strict_broker")

    success, response, status_code = order_manager.precheck_orders(
        [
            {
                "symbol": "NIFTY24APR2522500CE",
                "exchange": "NFO",
                "action": "SELL",
                "quantity": 50,
                "product": "MIS",
                "pricetype": "MARKET",
                "price": 120.0,
            }
        ]
    )

    assert success is False
    assert status_code == 400
    assert response["status"] == "error"
    assert "broker down" in response["message"]


def test_precheck_orders_broker_fallback_uses_internal_margin(monkeypatch):
    user_id = "TEST_MARGIN_FALLBACK"
    cleanup_user(user_id)
    order_manager = OrderManager(user_id)

    monkeypatch.setattr(order_manager, "_get_derivative_margin_positions", lambda: [])
    monkeypatch.setattr(
        order_manager, "_calculate_broker_margin_for_positions", lambda positions: (None, "broker timeout")
    )
    monkeypatch.setattr(
        order_manager, "_get_analyzer_broker_margin_mode", lambda: "broker_with_fallback"
    )
    monkeypatch.setattr(
        order_manager.fund_manager,
        "calculate_margin_required",
        lambda *args, **kwargs: (Decimal("12345"), "ok"),
    )

    success, response, status_code = order_manager.precheck_orders(
        [
            {
                "symbol": "NIFTY24APR2522500CE",
                "exchange": "NFO",
                "action": "SELL",
                "quantity": 50,
                "product": "MIS",
                "pricetype": "MARKET",
                "price": 120.0,
            }
        ]
    )

    assert success is True
    assert status_code == 200
    assert response["margin_source"] == "fallback_internal"
    assert Decimal(response["required_margin"]) == Decimal("12345")


def test_place_order_persists_broker_margin_audit_fields(monkeypatch):
    user_id = "TEST_MARGIN_AUDIT"
    cleanup_user(user_id)
    order_manager = OrderManager(user_id)

    monkeypatch.setattr(order_manager, "_validate_order", lambda order_data: (True, "Validation passed"))
    monkeypatch.setattr(
        "sandbox.order_manager.get_symbol_info",
        lambda symbol, exchange: SimpleNamespace(lotsize=50),
    )
    monkeypatch.setattr(
        order_manager, "_get_derivative_margin_positions", lambda: []
    )

    def fake_broker_margin(positions):
        if not positions:
            return Decimal("0"), "ok"
        return Decimal("250000"), "ok"

    monkeypatch.setattr(order_manager, "_calculate_broker_margin_for_positions", fake_broker_margin)
    monkeypatch.setattr(
        order_manager.fund_manager,
        "calculate_margin_required",
        lambda *args, **kwargs: (Decimal("250000"), "ok"),
    )
    monkeypatch.setattr(order_manager.fund_manager, "check_margin_available", lambda amount: (True, "ok"))
    monkeypatch.setattr(order_manager.fund_manager, "block_margin", lambda amount, description: (True, "ok"))

    success, response, status_code = order_manager.place_order(
        {
            "symbol": "NIFTY24APR2522500CE",
            "exchange": "NFO",
            "action": "SELL",
            "quantity": 50,
            "price": 120,
            "price_type": "LIMIT",
            "product": "NRML",
            "strategy": "audit-test",
        }
    )

    assert success is True
    assert status_code == 200

    order = SandboxOrders.query.filter_by(user_id=user_id).order_by(SandboxOrders.id.desc()).first()
    assert order is not None
    assert order.margin_source == "broker_api"
    assert order.margin_snapshot is not None
    assert "projected_margin_required" in order.margin_snapshot
