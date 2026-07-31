# restx_api/signal_schemas.py
"""Validation for the signal distribution surface.

Field names mirror the producer's PublicSignal exactly. Notably absent, and
intentionally rejected if sent: quantity, and any upstream-provider scoring or
vocabulary — see src/headless/publicSignal.ts in the chart repo for why the
payload is built by explicit construction rather than by spreading the internal
signal object.
"""

from marshmallow import EXCLUDE, Schema, fields, validate


class SignalIngestSchema(Schema):
    """One signal edge posted by the headless producer."""

    class Meta:
        # Unknown keys are dropped rather than rejected: a future producer field
        # must not 500 an older server mid-session. Anything we actually store
        # is declared below.
        unknown = EXCLUDE

    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    eventId = fields.Str(required=True, validate=validate.Length(min=1, max=128))
    event = fields.Str(required=True, validate=validate.OneOf(["open", "invalidated"]))
    symbol = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    exchange = fields.Str(required=True, validate=validate.Length(min=1, max=16))
    sector = fields.Str(required=True, validate=validate.Length(min=1, max=64))
    side = fields.Str(required=True, validate=validate.OneOf(["LONG", "SHORT"]))
    entry = fields.Float(required=True)
    stop = fields.Float(required=True)
    target = fields.Float(required=True)
    stopPct = fields.Float(required=True)
    signalTimeIst = fields.Str(required=True, validate=validate.Length(min=1, max=32))
