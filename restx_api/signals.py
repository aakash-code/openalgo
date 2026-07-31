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


events_api = Namespace("events", description="Subscriber signal feed")


def _bearer_subscriber():
    """Resolve the Authorization: Bearer key to an active subscriber, or None.

    Deliberately a header bearer token rather than the body `apikey` used across
    /api/v1: subscribers are third parties and must never hold the owner's key.
    """
    from database.signal_db import verify_subscriber

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return verify_subscriber(header[7:].strip())


@events_api.route("/", strict_slashes=False)
class SignalEvents(Resource):
    @limiter.limit(SIGNAL_RATE_LIMIT)
    def get(self):
        """Signals after `since_id`, oldest first.

        The cursor is the row id: monotonic and immune to clock skew, so a
        subscriber that was offline resumes exactly where it stopped without
        needing timestamps to agree between machines.
        """
        try:
            subscriber = _bearer_subscriber()
            if subscriber is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid or missing bearer token"}),
                    401,
                )

            from database.signal_db import get_events_since

            try:
                since_id = int(request.args.get("since_id", 0))
                limit = int(request.args.get("limit", 200))
            except (TypeError, ValueError):
                return make_response(
                    jsonify({"status": "error", "message": "since_id and limit must be integers"}),
                    400,
                )

            rows = get_events_since(since_id, limit)
            events = [r.to_dict() for r in rows]
            return make_response(
                jsonify(
                    {
                        "status": "success",
                        "events": events,
                        # Echo the cursor back when empty so a caller can keep
                        # polling with the same value rather than restarting.
                        "last_id": events[-1]["id"] if events else since_id,
                    }
                ),
                200,
            )
        except Exception as e:
            logger.exception(f"Unexpected error in signal events endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


signals_api.add_namespace(api, path="/ingest")
signals_api.add_namespace(events_api, path="/events")
