"""Seed a local IntegrateHub database with a realistic, fictional GTM demo workspace.

Run from the repository root (the folder that contains app/ and demo/):

    python demo/seed_demo.py

It drives the real API in-process (FastAPI TestClient), so every record goes through
the same CSV / REST / signed-webhook ingestion, mapping and email deduplication code
the app uses. Afterwards it spreads record timestamps over the last 14 days so the
dashboard charts have shape. Nothing is deleted.

All people, companies and email domains (.example) are fictional.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent
DATA = DEMO_DIR / "data"
DOCS = DEMO_DIR / "docs"
STATE_FILE = DEMO_DIR / ".demo_state.json"

# Local demo values only. They exist in your local SQLite file and nowhere else.
DEMO_PASSWORD = "integratehub-demo"
WEBHOOK_SECRET_INITIAL = "initial-demo-secret-0001"
WEBHOOK_SECRET = "demo-webhook-secret-2026"
MOCK_PORT = 8765

TEAM = [
    ("Lena Fischer", "lena.fischer@integratehub.example", "admin"),
    ("Aarav Mehta", "aarav.mehta@integratehub.example", "member"),
]

sys.path.insert(0, str(REPO_ROOT))
os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY"), "127.0.0.1", "localhost"]))
os.environ["no_proxy"] = os.environ["NO_PROXY"]


def step(message: str) -> None:
    print(f"\n▶ {message}")


def ok(message: str) -> None:
    print(f"  ✓ {message}")


def canonical(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign(payload: dict, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), canonical(payload), hashlib.sha256).hexdigest()


def account(client, name: str, email: str) -> dict:
    response = client.post("/api/auth/register", json={"name": name, "email": email, "password": DEMO_PASSWORD})
    if response.status_code == 409:
        response = client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="Veer Bajpai", help="workspace owner name")
    parser.add_argument("--email", default="veer@integratehub.example", help="workspace owner login email")
    parser.add_argument("--force", action="store_true", help="seed again even if the owner already has records")
    parser.add_argument("--no-spread", action="store_true", help="keep real timestamps (everything lands today)")
    args = parser.parse_args()

    try:
        from fastapi.testclient import TestClient
        from app import main as hub
    except ImportError as exc:
        sys.exit(f"Could not import the app ({exc}). Run from the repo root after `pip install -r requirements.txt`.")
    from mock_partner_api import start_in_background

    with TestClient(hub.app) as client:
        step("Accounts and workspace")
        owner = account(client, args.name, args.email)
        H = {"Authorization": f"Bearer {owner['token']}"}
        dashboard = client.get("/api/dashboard", headers=H).json()
        if dashboard["metrics"]["records"] and not args.force:
            sys.exit(f"  {args.email} already has {dashboard['metrics']['records']} records. "
                     "Use --force to add another batch, or move data/integratehub.db aside for a clean start.")
        workspace = client.get("/api/workspaces", headers=H).json()[0]
        WID = workspace["id"]
        H["X-Workspace-ID"] = str(WID)
        ok(f"owner {args.name} <{args.email}> · workspace '{workspace['name']}' (id {WID})")
        for name, email, role in TEAM:
            account(client, name, email)
            r = client.post(f"/api/workspaces/{WID}/members", headers=H, json={"email": email, "role": "admin" if role == "member" else role})
            if r.status_code not in (200, 409):
                r.raise_for_status()
            ok(f"teammate {name} added")
        aarav_id = next(m["id"] for m in client.get(f"/api/workspaces/{WID}/members", headers=H).json() if m["email"] == TEAM[1][1])
        client.patch(f"/api/workspaces/{WID}/members/{aarav_id}", headers=H, json={"role": "member"}).raise_for_status()
        ok("Aarav's role changed admin → member (shows up in the audit log)")

        batches: list[tuple[str, int]] = []  # (label, last record id) in ingestion order

        def mark(label: str) -> None:
            with sqlite3.connect(hub.DB_PATH) as conn:
                batches.append((label, conn.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]))

        with sqlite3.connect(hub.DB_PATH) as conn:
            start_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]

        step("CSV uploads")
        for filename in ("crm_export_contacts.csv", "event_leads_berlin.csv"):
            with (DATA / filename).open("rb") as handle:
                r = client.post("/api/ingest/csv", headers=H, files={"file": (filename, handle, "text/csv")})
            r.raise_for_status()
            res = r.json()
            ok(f"{filename}: {res['total']} rows → {res['imported']} new, {res['duplicates']} duplicate(s)")
            mark(filename)

        step("REST connector (partner API with different field names)")
        try:
            mock = start_in_background(MOCK_PORT)
            ok(f"mock partner API started on http://127.0.0.1:{MOCK_PORT}")
        except OSError:
            mock = None
            ok(f"port {MOCK_PORT} already in use, assuming mock_partner_api.py is running")
        partner = client.post("/api/connectors", headers=H, json={
            "name": "Partner API · contacts",
            "kind": "rest",
            "endpoint_url": f"http://127.0.0.1:{MOCK_PORT}/contacts",
            "mapping": {"first_name": "firstName", "last_name": "lastName", "email": "emailAddress",
                        "company": "organization", "role": "jobTitle", "phone": "mobile"},
        }).json()
        r = client.post(f"/api/connectors/{partner['id']}/sync", headers=H)
        r.raise_for_status()
        res = r.json()
        ok(f"sync: {res['received']} received → {res['imported']} new, {res['duplicates']} duplicate(s)")
        mark("partner api")
        legacy = client.post("/api/connectors", headers=H, json={
            "name": "Legacy reseller feed",
            "kind": "rest",
            "endpoint_url": f"http://127.0.0.1:{MOCK_PORT}/flaky",
        }).json()
        r = client.post(f"/api/connectors/{legacy['id']}/sync", headers=H)
        ok(f"legacy feed returned 503 → API answered {r.status_code}, connector marked degraded with an error log")
        if mock:
            mock.shutdown()

        step("Signed webhooks (website demo-request form)")
        webhook = client.post("/api/connectors", headers=H, json={
            "name": "Website demo requests",
            "kind": "webhook",
            "secret": WEBHOOK_SECRET_INITIAL,
            "mapping": {"first_name": "given_name", "last_name": "family_name", "email": "work_email",
                        "company": "company_name", "role": "title"},
        }).json()
        client.post(f"/api/workspaces/{WID}/connectors/{webhook['id']}/rotate-secret", headers=H,
                    json={"secret": WEBHOOK_SECRET}).raise_for_status()
        ok("webhook connector created and its signing secret rotated (audit event)")
        added = dupes = 0
        for event in json.loads((DATA / "webhook_events.json").read_text(encoding="utf-8")):
            r = client.post(f"/api/webhooks/{webhook['id']}", json=event,
                            headers={"X-Webhook-Signature": sign(event, WEBHOOK_SECRET)})
            r.raise_for_status()
            added += r.json()["imported"]
            dupes += r.json()["duplicates"]
        ok(f"8 signed events → {added} new, {dupes} duplicate(s)")
        tampered = {"event_id": "evt_tampered", "work_email": "attacker@evil.example", "company_name": "Unknown"}
        r = client.post(f"/api/webhooks/{webhook['id']}", json=tampered,
                        headers={"X-Webhook-Signature": sign({"event_id": "evt_tampered"}, WEBHOOK_SECRET)})
        ok(f"tampered event with a mismatched signature → {r.status_code} {r.json().get('detail')}")
        client.post(f"/api/workspaces/{WID}/audit-log/webhook-replay", headers=H,
                    json={"connector_id": webhook["id"], "event_id": "evt_9003", "already_idempotent": True})
        mark("webhooks")

        step("Review layer: comments, saved views, documents")
        lena = account(client, *TEAM[0][:2])
        LH = {"Authorization": f"Bearer {lena['token']}", "X-Workspace-ID": str(WID)}
        records = client.get("/api/records", headers=H).json()  # newest first
        first_dup = {}
        for rec in records:
            if rec["status"] == "duplicate":
                first_dup.setdefault(rec["source"], rec["id"])
        notes = [
            ("csv", H, "Berlin event list re-sent a contact we already have from the CRM export, with different email "
                       "casing. Dedup still caught it. @Lena please merge before the sequence starts."),
            ("csv", LH, "Merged in CRM and kept the event as the latest touchpoint. @Aarav they asked about SOC 2 timelines."),
            ("rest", H, "Partner API sent an existing contact under its own field names. Mapping normalized it, "
                        "so the email matched and it was flagged instead of creating a second contact."),
            ("webhook", LH, "Demo request from a contact we already know. @Aarav routing to you as the account owner."),
        ]
        posted = 0
        for source, headers, body in notes:
            if source in first_dup:
                client.post(f"/api/records/{first_dup[source]}/comments", headers=headers, json={"body": body}).raise_for_status()
                posted += 1
        ok(f"{posted} comments with @mentions on duplicate records")
        for name, filters in (("Duplicates to review", {"status": "duplicate"}), ("Inbound webhook leads", {"source": "webhook"})):
            client.post("/api/records/saved-views", headers=H, json={"name": name, "filters": filters})
        ok("saved views: 'Duplicates to review', 'Inbound webhook leads'")
        for path, kind in ((DOCS / "gtm_data_dictionary.md", "text/markdown"),
                           (DOCS / "webhook_payload_example.json", "application/json"),
                           (DATA / "partner_api_contacts.json", "application/json")):
            with path.open("rb") as handle:
                client.post("/api/documents", headers=H, files={"file": (path.name, handle, kind)}).raise_for_status()
        ok("3 documents uploaded to the library")

        if not args.no_spread:
            step("Spreading timestamps over the last 14 days")
            spread(hub.DB_PATH, WID, start_id, batches)
            ok("records, ingestion logs and last-sync times backdated by batch")

        final = client.get("/api/dashboard", headers=H).json()["metrics"]

    STATE_FILE.write_text(json.dumps({"webhook_connector_id": webhook["id"], "webhook_secret": WEBHOOK_SECRET,
                                      "partner_connector_id": partner["id"], "workspace_id": WID}, indent=2))
    print("\n" + "─" * 64)
    print(f"Demo workspace ready: {final['records']} records · {final['duplicates']} duplicates flagged · "
          f"{final['connectors']} connectors · {final['documents']} documents")
    print(f"Sign in at http://127.0.0.1:8000  →  {args.email}  /  {DEMO_PASSWORD}")
    print(f"Teammates use the same demo password: {', '.join(e for _, e, _ in TEAM)}")
    print("Live demo helpers:  python demo/mock_partner_api.py   (then click Sync on 'Partner API · contacts')")
    print("                    python demo/send_webhook.py       (signed + tampered webhook)")


def spread(db_path: Path, workspace_id: int, start_id: int, batches: list[tuple[str, int]]) -> None:
    """Give each ingestion batch its own day window, oldest first, ending today."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    windows = [(13, 10), (9, 6), (5, 3), (2, 0)]  # days ago (from, to) per batch
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Before moving anything, remember which record each log entry was written right after.
    log_anchor = {
        row["id"]: conn.execute(
            "SELECT MAX(id) FROM records WHERE connector_id = ? AND id > ? AND created_at <= ?",
            (row["connector_id"], start_id, row["created_at"]),
        ).fetchone()[0]
        for row in conn.execute(
            "SELECT l.id, l.connector_id, l.created_at FROM ingestion_logs l JOIN connectors c ON c.id = l.connector_id "
            "WHERE c.workspace_id = ?", (workspace_id,))
    }
    prev = start_id
    for (label, last_id), (old, new) in zip(batches, windows):
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM records WHERE workspace_id = ? AND id > ? AND id <= ? ORDER BY id", (workspace_id, prev, last_id))]
        span_hours = max((old - new) * 24 + 8, 1)
        begin = now - timedelta(days=old) - timedelta(hours=now.hour) + timedelta(hours=9)
        for index, record_id in enumerate(ids):
            stamp = begin + timedelta(hours=span_hours * index / max(len(ids), 1))
            stamp = min(stamp, now)
            conn.execute("UPDATE records SET created_at = ? WHERE id = ?", (stamp.isoformat(), record_id))
        prev = last_id
    # Ingestion logs and last_sync follow the records they describe.
    for log_id, record_id in log_anchor.items():
        if record_id:
            conn.execute("UPDATE ingestion_logs SET created_at = (SELECT created_at FROM records WHERE id = ?) WHERE id = ?",
                         (record_id, log_id))
    failed_at = (now - timedelta(days=1, hours=3)).isoformat()  # the failing feed: recent, not "just now"
    for connector in conn.execute("SELECT id FROM connectors WHERE workspace_id = ?", (workspace_id,)).fetchall():
        last = conn.execute("SELECT MAX(created_at) FROM records WHERE connector_id = ?", (connector["id"],)).fetchone()[0]
        conn.execute("UPDATE connectors SET last_sync = ? WHERE id = ?", (last or failed_at, connector["id"]))
        if not last:
            conn.execute("UPDATE ingestion_logs SET created_at = ? WHERE connector_id = ?", (failed_at, connector["id"]))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
