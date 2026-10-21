"""
Writer Flow API: what option writers did at each strike through the day.

    GET /writerflow/api/sessions   underlyings and expiries recorded, newest day first
    GET /writerflow/api/flow       the day read strike by strike, for the page and the chart
    GET /writerflow/api/read       the market read: who leads, futures, levels, volatility
    GET/POST /writerflow/api/read/settings   its switches (read, Telegram, sound)

Reads only db/oi_flow.duckdb, which the OI Flow recorder fills; it makes no
broker call of its own, so opening the page costs the broker nothing. The chart
also sends its own exchange, and an underlying it shows that is not being
recorded is handed to the recorder to start (request_recording), which does its
broker work on the recorder's thread, never in this request. Answers
are built once per recorded write and shared (services/writer_flow_service.py),
and sent gzipped to a browser that accepts it.
"""

from datetime import date, datetime

from flask import Blueprint, Response, jsonify, request

from database.oi_flow_db import EXPIRY_RE, UNDERLYING_RE, available_sessions
from services.oi_flow_recorder_service import on_demand_state, request_recording
from services.writer_flow_service import STEPS, FlowView, flow_answer, recorder_enabled
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

writer_flow_bp = Blueprint("writer_flow_bp", __name__, url_prefix="/")

_TRUE = ("1", "true", "yes")
_CHART_EXCHANGES = ("NFO", "BFO", "MCX", "NSE", "BSE", "NSE_INDEX", "BSE_INDEX")


def _recording_message(underlying: str, state: str | None) -> str | None:
    """What to tell a trader whose chart shows an underlying with nothing recorded."""
    return {
        "started": f"Started recording {underlying}. Today's earlier minutes are being "
        "filled from history; the chart fills in by itself within a few minutes.",
        "recording": f"Recording {underlying}. It shows once its market is open and the "
        "first minutes are in; earlier minutes are filled from history.",
        "full": f"Writer Flow is already recording the most charted underlyings it is "
        f"allowed to. Close another chart's Writer Flow, or raise OI_FLOW_ON_DEMAND_MAX "
        f"in .env and restart, to record {underlying}.",
        "no_options": f"{underlying} has no options listed, so there are no writers to show.",
        "unknown": f"{underlying} was not found in the master contract, so it cannot be "
        "recorded. Check the symbol, or download the master contract again.",
    }.get(state or "")


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M")


@writer_flow_bp.route("/writerflow/api/sessions", methods=["GET"])
@check_session_validity
def sessions():
    try:
        rows = available_sessions()
        return jsonify(
            {
                "status": "success",
                "recorder_enabled": recorder_enabled(),
                "sessions": [
                    {
                        **r,
                        "trade_date": r["trade_date"].isoformat(),
                        "last_sample": _hhmm(r["last_sample"]),
                    }
                    for r in rows
                ],
            }
        )
    except Exception:
        logger.exception("Writer Flow: listing recorded sessions failed")
        return jsonify(
            {
                "status": "error",
                "message": "Could not read the Writer Flow record. Try again in a moment.",
            }
        ), 500


@writer_flow_bp.route("/writerflow/api/flow", methods=["GET"])
@check_session_validity
def flow():
    underlying = (request.args.get("underlying") or "").strip().upper()
    expiry = (request.args.get("expiry") or "").strip().upper()
    day_text = (request.args.get("date") or "").strip()
    step_text = (request.args.get("step") or "auto").strip().lower()
    if not UNDERLYING_RE.match(underlying):
        return jsonify({"status": "error", "message": "Choose an underlying, such as NIFTY."}), 400
    if expiry and not EXPIRY_RE.match(expiry):
        return jsonify({"status": "error", "message": "The expiry should look like 19OCT26."}), 400
    if step_text not in STEPS:
        return jsonify(
            {"status": "error", "message": "The step can be Auto, 1, 3 or 5 minutes."}
        ), 400
    try:
        day = date.fromisoformat(day_text) if day_text else None
        noise_pct = float(request.args.get("noise_pct", 0.5))
        noise_lots = int(request.args.get("noise_lots", 5))
        quiet = float(request.args.get("quiet_minutes", 5.0))
    except ValueError:
        return jsonify({"status": "error", "message": "One of the settings is not a number."}), 400
    if not (0 <= noise_pct <= 50 and 0 <= noise_lots <= 10000 and 1 <= quiet <= 60):
        return jsonify({"status": "error", "message": "One of the settings is out of range."}), 400

    view = FlowView(
        underlying=underlying,
        expiry=expiry,
        day=day,
        step=step_text,
        noise_pct=noise_pct,
        noise_lots=noise_lots,
        quiet_minutes=quiet,
        all_strikes=(request.args.get("all_strikes") or "").strip().lower() in _TRUE,
        lite=(request.args.get("lite") or "").strip().lower() in _TRUE,
    )
    try:
        built = flow_answer(view)
    except Exception:
        logger.exception(f"Writer Flow: reading {underlying} failed")
        return jsonify(
            {
                "status": "error",
                "message": "Could not read the Writer Flow record. Try again in a moment.",
            }
        ), 500

    # Only the chart sends its exchange, and only for today: the page and a
    # past day read what is recorded and start nothing.
    chart_exchange = (request.args.get("exchange") or "").strip().upper()
    state = None
    if chart_exchange in _CHART_EXCHANGES and not day and not expiry:
        state = request_recording(underlying, chart_exchange)
    elif built.empty:
        state = on_demand_state(underlying)
    message = _recording_message(underlying, state) if built.empty else None
    if message:
        return jsonify(
            {
                "status": "empty",
                "message": message,
                "recording": state in ("started", "recording"),
                "recorder_enabled": recorder_enabled(),
            }
        )

    zipped = "gzip" in (request.headers.get("Accept-Encoding") or "").lower()
    response = Response(
        built.gzipped if zipped else built.body, status=built.status, mimetype="application/json"
    )
    if zipped:
        response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


@writer_flow_bp.route("/writerflow/api/read", methods=["GET"])
@check_session_validity
def market_read():
    from services import market_read_service as mr
    from services.writer_flow_service import _on_builder

    day_text = (request.args.get("date") or "").strip()
    try:
        day = date.fromisoformat(day_text) if day_text else datetime.now(mr.IST).date()
    except ValueError:
        return jsonify({"status": "error", "message": "The date should look like 2026-10-21."}), 400
    try:
        read = mr.latest(day)
        if read is None:
            # Built on the Writer Flow builder thread: a whole day takes tens of
            # milliseconds of CPU, which must not hold a request worker.
            read = _on_builder(mr.compute, day)
    except Exception:
        logger.exception("Market read: reading the day failed")
        return jsonify(
            {"status": "error", "message": "Could not read the market. Try again in a moment."}
        ), 500
    if read is None:
        return jsonify(
            {
                "status": "empty",
                "message": f"Nothing recorded for {mr.main_underlying()} on this day yet. "
                "The read starts with the first minutes the Writer Flow recorder stores.",
                "recorder_enabled": recorder_enabled(),
                "settings": mr.settings(),
            }
        )
    return jsonify({"status": "success", **mr.read_json(read), "settings": mr.settings()})


@writer_flow_bp.route("/writerflow/api/read/settings", methods=["GET", "POST"])
@check_session_validity
def market_read_settings():
    from services import market_read_service as mr

    if request.method == "GET":
        return jsonify({"status": "success", "settings": mr.settings()})
    body = request.get_json(silent=True) or {}
    changes = {k: v for k, v in body.items() if k in mr.SETTINGS}
    if not changes or any(not isinstance(v, bool) for v in changes.values()):
        return jsonify(
            {"status": "error", "message": "Turn the read, Telegram or sound on or off."}
        ), 400
    try:
        return jsonify({"status": "success", "settings": mr.update_settings(changes)})
    except Exception:
        logger.exception("Market read: saving the switches failed")
        return jsonify(
            {"status": "error", "message": "Could not save the setting. Try again in a moment."}
        ), 500
