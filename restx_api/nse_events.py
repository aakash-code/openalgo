import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import get_auth_token_broker
from limiter import limiter
from services.nse_events_service import events_for, fetch_events
from utils.logging import get_logger

from .data_schemas import NseEventsSchema

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace(
    "nseevents",
    description="NSE board meetings / results / corporate actions by symbol, "
                "cached for the trading day",
)

logger = get_logger(__name__)

nse_events_schema = NseEventsSchema()


@api.route("/", strict_slashes=False)
class NseEvents(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Returns {symbol: {kind, date, purpose, daysAway}} for symbols with an
        event inside the window. A stock in a results/board-meeting window trades
        on news rather than structure, so a scanner can warn before entry.

        Degrades quietly: NSE rate-limits and intermittently 401s, and an empty
        map must never break the caller's own flow."""
        try:
            payload = nse_events_schema.load(request.json or {})
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)

        auth_token, _broker = get_auth_token_broker(
            payload.get("apikey", ""), include_feed_token=False
        )
        if auth_token is None:
            return make_response(
                jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
            )

        try:
            symbols = payload.get("symbols") or []
            if symbols:
                data = events_for(symbols, within_days=payload.get("within_days", 2))
            else:
                data = fetch_events()
            return make_response(jsonify({"status": "success", "data": data}), 200)
        except Exception as err:
            logger.error("NSE events request failed: %s", err)
            return make_response(
                jsonify({"status": "error", "message": "NSE events unavailable"}), 200
            )
