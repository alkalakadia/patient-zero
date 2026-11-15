"""Incident notifications.

The app's incident table always updates live. Set PZ_WEBHOOK_URL (a Slack-compatible incoming webhook) to also post
each incident to a channel; PZ_PUBLIC_URL is the link back to the app (default http://localhost:8100).
"""
import json
import os
import threading
import urllib.request

from . import databricks


def send(text, incident_id=None):
    """Post one message to the webhook in the background. Returns False when no webhook is configured."""
    databricks._load_env()
    url = os.environ.get("PZ_WEBHOOK_URL")
    if not url:
        return False
    link = os.environ.get("PZ_PUBLIC_URL", "http://localhost:8100").rstrip("/") + (f"/?incident={incident_id}" if incident_id else "")
    body = json.dumps({"text": f"{text}\n{link}"}).encode()

    def post():
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"content-type": "application/json"}), timeout=5)
        except Exception:
            pass  # a notification must never break an investigation

    threading.Thread(target=post, daemon=True).start()
    return True
