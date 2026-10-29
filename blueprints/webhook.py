"""
blueprints/webhook.py — Tracksolid Pro webhook receiver.

Tracksolid pushes alarm events here in real time (jimi.push.device.alarm).
We feed them into the live_fleet state caches so the Live map updates
without polling. Polling remains as a slow fallback in live_fleet.

Setup:
  1. Deploy this app so /webhook/jimi is publicly reachable over HTTPS.
  2. In Tracksolid Pro → Help → API Settings → Webhook Configuration,
     register:  https://<your-host>/webhook/jimi?token=<WEBHOOK_TOKEN>
  3. Set WEBHOOK_TOKEN in .env (any random string).
  4. Use the Push Records tab in the portal to audit deliveries.

Security:
  Tracksolid does not sign webhook payloads, so we require the shared
  token as a query-string parameter. Requests without the correct token
  are rejected with 403. Rotate the token by updating .env and the
  portal URL together.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, request, jsonify

from services.live_fleet import ingest_alarm_push


log = logging.getLogger(__name__)
bp = Blueprint("webhook", __name__, url_prefix="/webhook")

_WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")


def _token_ok() -> bool:
    if not _WEBHOOK_TOKEN:
        # If no token is configured, refuse rather than silently accept
        # unauthenticated writes into the live state caches.
        log.error("WEBHOOK_TOKEN is not set — rejecting push")
        return False
    supplied = (request.args.get("token") or "").strip()
    return supplied == _WEBHOOK_TOKEN


"""
blueprints/webhook.py — Tracksolid Pro webhook receiver.

Tracksolid pushes alarm events here in real time (jimi.push.device.alarm).
We feed them into the live_fleet state caches so the Live map updates
without polling. Polling remains as a slow fallback in live_fleet.

Setup:
  1. Deploy this app so /webhook/jimi is publicly reachable over HTTPS.
  2. In Tracksolid Pro → Help → API Settings → Webhook Configuration,
     register:  https://<your-host>/webhook/jimi?token=<WEBHOOK_TOKEN>
  3. Set WEBHOOK_TOKEN in .env (any random string).
  4. Use the Push Records tab in the portal to audit deliveries.

Verification:
  The Tracksolid portal performs a URL-verification probe when you save
  the webhook configuration. That probe may be a GET or a POST with an
  empty body — both are acknowledged with HTTP 200 as long as the token
  is correct, so the portal accepts the URL.

Security:
  Tracksolid does not sign webhook payloads, so we require the shared
  token as a query-string parameter. Requests without the correct token
  are rejected with 403. Rotate the token by updating .env and the
  portal URL together.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, request, jsonify

from services.live_fleet import ingest_alarm_push


log = logging.getLogger(__name__)
bp = Blueprint("webhook", __name__, url_prefix="/webhook")

_WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")


def _token_ok() -> bool:
    if not _WEBHOOK_TOKEN:
        # If no token is configured, refuse rather than silently accept
        # unauthenticated writes into the live state caches.
        log.error("WEBHOOK_TOKEN is not set — rejecting push")
        return False
    supplied = (request.args.get("token") or "").strip()
    return supplied == _WEBHOOK_TOKEN


@bp.route("/jimi", methods=["GET", "POST"])
def jimi():
    if not _token_ok():
        log.warning("webhook: rejected request (bad or missing token)")
        return jsonify({"status": "forbidden"}), 403

    # Tracksolid's URL-verification probe (and some platform health checks)
    # use GET with no body. Acknowledge it so the portal accepts the URL.
    if request.method == "GET":
        log.info("webhook: GET probe acknowledged")
        return jsonify({"status": "ok"}), 200

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # A POST with an empty or non-JSON body can also be a verification
        # probe. Acknowledge it rather than returning 400 so the portal
        # doesn't flag the URL as unreachable.
        if not payload:
            log.info("webhook: empty POST acknowledged (likely a probe)")
            return jsonify({"status": "ok"}), 200
        log.warning("webhook: non-JSON or malformed body")
        return jsonify({"status": "bad_request"}), 400

    msg_type = (payload.get("msgType") or "").strip()
    log.info("webhook: received msgType=%s", msg_type)

    if msg_type == "jimi.push.device.alarm":
        try:
            handled = ingest_alarm_push(payload)
        except Exception:
            log.exception("webhook: ingest_alarm_push raised")
            # Still return 200 — a 5xx would just make Tracksolid retry
            # tomorrow at 9 AM, which is useless for a live map anyway.
            return jsonify({"status": "error"}), 200

        return jsonify({
            "status":  "ok",
            "handled": bool(handled),
        }), 200

    # Unknown push type — acknowledge so Tracksolid doesn't retry.
    return jsonify({"status": "ignored"}), 200