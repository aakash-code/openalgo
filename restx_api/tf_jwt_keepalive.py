import os

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import get_auth_token_broker
from limiter import limiter
from services.tf_jwt_keepalive_service import trigger_refresh_if_needed
from utils.logging import get_logger

from .data_schemas import TfJwtKeepAliveSchema

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace(
    "tfjwtkeepalive",
    description="Keep the server-side TradeFinder JWT fresh — pinged by an active openalgo-chart session",
)

logger = get_logger(__name__)

tf_jwt_keepalive_schema = TfJwtKeepAliveSchema()


@api.route("/", strict_slashes=False)
class TfJwtKeepAlive(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Cheap expiry check on the server-side TF JWT (strategies/tf_jwt.txt).
        Only when it's missing/expiring soon does this kick off the (slow,
        browser-based) refresh, in a background thread — the response always
        returns immediately. Safe to call every few minutes from an active
        client."""
        try:
            data = tf_jwt_keepalive_schema.load(request.json)

            auth_token, _broker = get_auth_token_broker(data["apikey"], include_feed_token=False)
            if auth_token is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            status = trigger_refresh_if_needed(min_seconds=data["minSeconds"])
            return make_response(jsonify({"status": "success", **status}), 200)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in tfjwtkeepalive endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
