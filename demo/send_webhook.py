"""Send one correctly signed and one tampered webhook to a running IntegrateHub server.

    uvicorn app.main:app --reload          # in one terminal
    python demo/send_webhook.py            # in another, after seed_demo.py

Good for the "HMAC webhook verification" moment in a demo recording.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import urllib.error
import urllib.request
from pathlib import Path

STATE_FILE = Path(__file__).resolve().parent / ".demo_state.json"


def post(url: str, payload: dict, signature: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Webhook-Signature": signature})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> None:
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--connector-id", type=int, default=state.get("webhook_connector_id"))
    parser.add_argument("--secret", default=state.get("webhook_secret"))
    args = parser.parse_args()
    if not args.connector_id or not args.secret:
        raise SystemExit("Run demo/seed_demo.py first, or pass --connector-id and --secret.")

    event = {"event_id": "evt_live_001", "type": "demo_request.submitted", "given_name": "Mira",
             "family_name": "Halvorsen", "work_email": "mira.halvorsen@fjord-analytics.example",
             "company_name": "Fjord Analytics AS", "title": "CISO", "interest": "ISO 27001"}
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    signature = "sha256=" + hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
    url = f"{args.base_url}/api/webhooks/{args.connector_id}"

    print(f"POST {url}\n  signed event   → ", *post(url, event, signature))
    tampered = {**event, "work_email": "attacker@evil.example"}
    print("  tampered body  → ", *post(url, tampered, signature))
    print("  replayed event → ", *post(url, event, signature), "(same email, flagged as duplicate)")


if __name__ == "__main__":
    main()
