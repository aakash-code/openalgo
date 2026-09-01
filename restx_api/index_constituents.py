import os
from collections import defaultdict

from flask import jsonify, make_response, request
from flask_restx import Namespace, Resource
from marshmallow import ValidationError

from database.auth_db import get_auth_token_broker
from database.index_constituents_db import (
    get_current_constituents,
    get_sync_status,
)
from limiter import limiter
from services.index_constituents_service import (
    apply_manual_override,
    sync_index_constituents,
)
from utils.logging import get_logger

from .data_schemas import IndexConstituentsRefreshSchema, IndexConstituentsSchema

API_RATE_LIMIT = os.getenv("API_RATE_LIMIT", "10 per second")
api = Namespace(
    "indexconstituents",
    description="NSE sectoral index membership + weights for the Sector "
                 "Contribution / Index Driver engine",
)

logger = get_logger(__name__)

index_constituents_schema = IndexConstituentsSchema()
index_constituents_refresh_schema = IndexConstituentsRefreshSchema()


def _serialize(rows):
    by_index = defaultdict(list)
    index_exchange = {}
    for row in rows:
        by_index[row.index_symbol].append(
            {"symbol": row.symbol, "exchange": row.exchange, "weight": row.weight,
             "weight_source": row.weight_source}
        )
        index_exchange[row.index_symbol] = row.index_exchange
    return [
        {
            "index_symbol": index_symbol,
            "index_exchange": index_exchange[index_symbol],
            "constituents": sorted(constituents, key=lambda c: -(c["weight"] or 0)),
        }
        for index_symbol, constituents in by_index.items()
    ]


@api.route("/", strict_slashes=False)
class IndexConstituents(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Return current index membership + weights, optionally filtered to
        one index via `index`. Public reference data (no broker auth needed),
        but kept on the same apikey-in-body convention as the rest of the API."""
        try:
            data = index_constituents_schema.load(request.json)
            rows = get_current_constituents(index_symbol=data.get("index"))
            status = get_sync_status() or {}

            return make_response(
                jsonify({
                    "status": "success",
                    "weights_as_of": status.get("weights_as_of"),
                    "last_sync": status.get("last_updated"),
                    "data": _serialize(rows),
                }),
                200,
            )
        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in indexconstituents endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )


@api.route("/refresh", strict_slashes=False)
class IndexConstituentsRefresh(Resource):
    @limiter.limit(API_RATE_LIMIT)
    def post(self):
        """Trigger a sync. With `index` + `constituents` in the body, bypasses
        the niftyindices.com scrape and writes a manual-override version for
        just that index -- the escape hatch if the site is blocked mid-cycle.
        Without them, runs the normal CSV+seed sync for every index."""
        try:
            data = index_constituents_refresh_schema.load(request.json)

            auth_token, _broker = get_auth_token_broker(
                data.get("apikey", ""), include_feed_token=False
            )
            if auth_token is None:
                return make_response(
                    jsonify({"status": "error", "message": "Invalid openalgo apikey"}), 403
                )

            if data.get("index") and data.get("constituents"):
                ok = apply_manual_override(data["index"], data["constituents"])
                return make_response(
                    jsonify({"status": "success" if ok else "error", "index": data["index"]}),
                    200,
                )

            stats = sync_index_constituents(force=True)
            return make_response(jsonify({"status": "success", "stats": stats}), 200)

        except ValidationError as err:
            return make_response(jsonify({"status": "error", "message": err.messages}), 400)
        except Exception as e:
            logger.exception(f"Unexpected error in indexconstituents/refresh endpoint: {e}")
            return make_response(
                jsonify({"status": "error", "message": "An unexpected error occurred"}), 500
            )
