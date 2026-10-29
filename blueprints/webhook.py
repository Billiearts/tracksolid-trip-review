"""
blueprints/webhook.py — Tracksolid Pro webhook receiver.
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
        log.error("WEBHOOK_TOKEN is not set — rejecting push")
        return False
    supplied = (request.args.get("token") or "").strip()
    return supplied == _WEBHOOK_TOKEN


@bp.route("/jimi", methods=["GET", "POST"])
def jimi():
    if not _token_ok():
        log.warning("webhook: rejected request (bad or missing token)")
        return jsonify({"status": "forbidden"}), 403

    # GET probe from the portal
    if request.method == "GET":
        log.info("webhook: GET probe acknowledged")
        return jsonify({"code": 0, "message": "success"}), 200

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # Empty POST probe — acknowledge in the format the portal expects
        log.info("webhook: empty POST acknowledged (likely a probe)")
        return jsonify({"code": 0, "message": "success"}), 200

    msg_type = (payload.get("msgType") or "").strip()
    log.info("webhook: received msgType=%s", msg_type)

    if msg_type == "jimi.push.device.alarm":
        try:
            handled = ingest_alarm_push(payload)
        except Exception:
            log.exception("webhook: ingest_alarm_push raised")
            return jsonify({"code": 0, "message": "success"}), 200

        return jsonify({"code": 0, "message": "success"}), 200

    # Unknown msgType (including verification probes with a non-alarm type)
    # Return the generic success format so the portal accepts the URL.
    log.info("webhook: unknown msgType=%r acknowledged as success", msg_type)
    return jsonify({"code": 0, "message": "success"}), 200