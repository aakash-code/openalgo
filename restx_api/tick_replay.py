import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from limiter import limiter
from services.tick_replay_service import replay_ticks_service
from utils.logging import get_logger

from .data_schemas import TickReplaySchema

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace("tickreplay", description="Replay recently-recorded ticks for gap backfill")

logger = get_logger(__name__)

tick_replay_schema = TickReplaySchema()


@api.route("/", strict_slashes=False)
class TickReplay(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Return ticks recorded for a symbol after a given epoch-ms timestamp.

        Used by the chart client to backfill footprint/tape data missed during a
        feed stall. Returns an empty tick list when the Redis tape is disabled or
        unavailable (safe no-op for the client)."""
        try:
            data = tick_replay_schema.load(request.json)

            success, response_data, status_code = replay_ticks_service(
                api_key=data["apikey"],
                symbol=data["symbol"],
                exchange=data["exchange"],
                since_ms=data.get("since", 0.0),
                limit=data.get("limit", 5000),
            )

            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in tickreplay endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
