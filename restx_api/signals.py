# restx_api/signals.py
"""Signal distribution surface.

Mounted at /signals/v1/ on its OWN blueprint, deliberately not under /api/v1.
That surface's convention is an `apikey` in the JSON body, and every consumer of
it is the account owner. This surface will also serve third-party subscribers
(stage 4), who must never hold the owner's key — keeping it separate stops the
two auth models from being confused for one another.

Stage 2 exposes ingest only. Subscriber-facing endpoints land in stage 4.
"""

import os

from flask import Blueprint, jsonify, make_response, request
from flask_restx import Api, Namespace, Resource
from marshmallow import ValidationError

from limiter import limiter
from services.signal_ingest_service import ingest_signal_service
from utils.logging import get_logger

from .signal_schemas import SignalIngestSchema

logger = get_logger(__name__)

SIGNAL_RATE_LIMIT = os.getenv("SIGNAL_RATE_LIMIT", "20 per second")

signals_bp = Blueprint("signals_v1", __name__, url_prefix="/signals/v1")
signals_api = Api(
    signals_bp,
    version="1.0",
    title="Signal Distribution API",
    description="Trading signal ingest and subscriber feed",
    doc=False,
)

api = Namespace("ingest", description="Signal ingest from the headless producer")

ingest_schema = SignalIngestSchema()


@api.route("/", strict_slashes=False)
class SignalIngest(Resource):
    @limiter.limit(SIGNAL_RATE_LIMIT)
    def post(self):
        """Store one signal edge and broadcast it.

        Idempotent on `eventId`: a repeat POST stores nothing and broadcasts
        nothing, and still returns 200 so the producer stops retrying.
        """
        try:
            data = ingest_schema.load(request.json)
            api_key = data.pop("apikey")

            success, response_data, status_code = ingest_signal_service(api_key, data)
            return make_response(jsonify(response_data), status_code)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in signal ingest endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


signals_api.add_namespace(api, path="/ingest")
