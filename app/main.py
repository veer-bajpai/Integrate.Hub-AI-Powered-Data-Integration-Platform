from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi import Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

try:
    from google import genai
except ImportError:
    genai = None

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "integratehub.db"
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")
JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-change-me")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "http://127.0.0.1:8000")
CANONICAL_FIELDS = ["first_name", "last_name", "email", "company", "phone", "role"]
security = HTTPBearer(auto_error=False)

app = FastAPI(title="IntegrateHub API", version="2.0.0", description="AI-assisted, config-driven data integration platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

OAUTH_PROVIDERS = {
    "google": {
        "client_id": "GOOGLE_CLIENT_ID",
        "client_secret": "GOOGLE_CLIENT_SECRET",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": "openid email profile https://www.googleapis.com/auth/spreadsheets",
    },
    "slack": {
        "client_id": "SLACK_CLIENT_ID",
        "client_secret": "SLACK_CLIENT_SECRET",
        "authorize": "https://slack.com/oauth/v2/authorize",
        "token": "https://slack.com/api/oauth.v2.access",
        "scopes": "chat:write,commands,channels:read",
    },
    "hubspot": {
        "client_id": "HUBSPOT_CLIENT_ID",
        "client_secret": "HUBSPOT_CLIENT_SECRET",
        "authorize": "https://app.hubspot.com/oauth/authorize",
        "token": "https://api.hubapi.com/oauth/v1/token",
        "scopes": "crm.objects.contacts.read crm.objects.contacts.write",
    },
}

OAUTH_INTEGRATIONS = {"Google Sheets": "google", "Slack": "slack", "HubSpot": "hubspot"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    salt, expected = stored.split("$", 1)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(actual, expected)


def secret_box() -> Fernet | None:
    if Fernet is None:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(JWT_SECRET.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    box = secret_box()
    return box.encrypt(value.encode()).decode() if box else None


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    box = secret_box()
    if not box:
        return None
    try:
        return box.decrypt(value.encode()).decode()
    except InvalidToken:
        return None


def token_for(user: sqlite3.Row) -> str:
    payload = {"sub": str(user["id"]), "email": user["email"], "exp": datetime.now(timezone.utc) + timedelta(hours=12)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> sqlite3.Row:
    if not credentials:
        raise HTTPException(401, "Authentication required")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Invalid or expired token") from exc
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = ?", (int(payload["sub"]),)).fetchone()
    if not user:
        raise HTTPException(401, "User not found")
    return user


def init_db() -> None:
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS connectors (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, records INTEGER DEFAULT 0, last_sync TEXT, success_rate REAL DEFAULT 100, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, connector_id INTEGER, data TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, content_type TEXT, path TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS integrations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL, description TEXT NOT NULL, connected_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS ingestion_logs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, connector_id INTEGER, status TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        """)
        connector_columns = {row[1] for row in connection.execute("PRAGMA table_info(connectors)").fetchall()}
        for name, definition in {
            "endpoint_url": "TEXT",
            "secret_encrypted": "TEXT",
            "mapping": "TEXT NOT NULL DEFAULT '{}'",
            "last_error": "TEXT",
        }.items():
            if name not in connector_columns:
                connection.execute(f"ALTER TABLE connectors ADD COLUMN {name} {definition}")
        integration_columns = {row[1] for row in connection.execute("PRAGMA table_info(integrations)").fetchall()}
        for name, definition in {
            "provider": "TEXT",
            "access_token_encrypted": "TEXT",
            "refresh_token_encrypted": "TEXT",
            "scopes": "TEXT",
            "external_account": "TEXT",
        }.items():
            if name not in integration_columns:
                connection.execute(f"ALTER TABLE integrations ADD COLUMN {name} {definition}")


class Credentials(BaseModel):
    email: str
    password: str = Field(min_length=6)


class Registration(Credentials):
    name: str = Field(min_length=2, max_length=80)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class MappingRequest(BaseModel):
    sample: str = Field(min_length=1, max_length=12000)


class ConnectorRequest(BaseModel):
    name: str
    kind: str
    endpoint_url: str | None = None
    secret: str | None = Field(default=None, min_length=8)
    mapping: dict[str, str] = Field(default_factory=dict)


class IntegrationRequest(BaseModel):
    action: str = "connect"


def oauth_redirect_uri(provider: str) -> str:
    return f"{OAUTH_BASE_URL.rstrip('/')}/api/integrations/oauth/{provider}/callback"


def oauth_state(user_id: int, integration_id: int, provider: str) -> str:
    payload = {"sub": str(user_id), "integration_id": integration_id, "provider": provider, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def oauth_credentials(provider: str) -> tuple[dict[str, str], str, str]:
    config = OAUTH_PROVIDERS[provider]
    client_id = os.getenv(config["client_id"])
    client_secret = os.getenv(config["client_secret"])
    if not client_id or not client_secret:
        raise HTTPException(503, f"{provider.title()} OAuth is not configured. Add its client ID and secret to .env")
    return config, client_id, client_secret


def connector_view(row: sqlite3.Row) -> dict[str, Any]:
    connector = dict(row)
    connector.pop("secret_encrypted", None)
    return connector


def mapped_record(raw: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    if not mapping:
        return raw
    return {canonical: raw.get(source) for canonical, source in mapping.items() if source in raw}


def store_records(connection: sqlite3.Connection, connector: sqlite3.Row, rows: list[dict[str, Any]], source: str) -> tuple[int, int]:
    existing = {row[0] for row in connection.execute("SELECT COALESCE(json_extract(data, '$.email'), external_id) FROM records WHERE user_id = ?", (connector["user_id"],)).fetchall()}
    added = 0
    duplicates = 0
    mapping = json.loads(connector["mapping"] or "{}")
    for raw in rows:
        record = mapped_record(raw, mapping)
        email = str(record.get("email") or raw.get("email") or "").strip().lower()
        external_id = str(raw.get("id") or raw.get("external_id") or email or secrets.token_hex(8))
        dedupe_key = email or f"{connector['id']}:{external_id}"
        status = "duplicate" if dedupe_key in existing else "valid"
        if status == "duplicate":
            duplicates += 1
        else:
            added += 1
            existing.add(dedupe_key)
        connection.execute("INSERT INTO records (user_id,connector_id,data,source,status,external_id,created_at) VALUES (?,?,?,?,?,?,?)", (connector["user_id"], connector["id"], json.dumps(record), source, status, external_id, now()))
    return added, duplicates


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict[str, str]:
    with db() as connection:
        connection.execute("SELECT 1")
    return {"status": "ok", "database": "connected"}


@app.post("/api/auth/register")
def register(payload: Registration) -> dict[str, Any]:
    with db() as connection:
        try:
            user_id = connection.execute("INSERT INTO users (name,email,password,created_at) VALUES (?,?,?,?)", (payload.name, payload.email.lower(), hash_password(payload.password), now())).lastrowid
            for name, category, description in [("Slack", "Communication", "Send ingestion alerts and daily summaries."), ("HubSpot", "CRM", "Sync normalized contacts to a CRM."), ("Google Sheets", "Workspace", "Publish clean records to a sheet."), ("REST API", "Developer tools", "Connect a client-owned HTTP endpoint.")]:
                connection.execute("INSERT INTO integrations (user_id,name,category,status,description) VALUES (?,?,?,?,?)", (user_id, name, category, "available", description))
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "An account with that email already exists") from exc
    return {"token": token_for(user), "user": {"id": user["id"], "name": user["name"], "email": user["email"]}}


@app.post("/api/auth/login")
def login(payload: Credentials) -> dict[str, Any]:
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (payload.email.lower(),)).fetchone()
    if not user or not verify_password(payload.password, user["password"]):
        raise HTTPException(401, "Email or password is incorrect")
    return {"token": token_for(user), "user": {"id": user["id"], "name": user["name"], "email": user["email"]}}


@app.get("/api/dashboard")
def dashboard(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        connectors = [dict(row) for row in connection.execute("SELECT * FROM connectors WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()]
        records = connection.execute("SELECT COUNT(*) FROM records WHERE user_id = ?", (user["id"],)).fetchone()[0]
        duplicates = connection.execute("SELECT COUNT(*) FROM records WHERE user_id = ? AND status = 'duplicate'", (user["id"],)).fetchone()[0]
        documents = connection.execute("SELECT COUNT(*) FROM documents WHERE user_id = ?", (user["id"],)).fetchone()[0]
        logs = [dict(row) for row in connection.execute("SELECT * FROM ingestion_logs WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user["id"],)).fetchall()]
    return {"user": {"name": user["name"], "email": user["email"]}, "metrics": {"records": records, "connectors": len(connectors), "duplicates": duplicates, "documents": documents}, "connectors": connectors, "logs": logs}


@app.get("/api/connectors")
def connectors(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        return [connector_view(row) for row in connection.execute("SELECT * FROM connectors WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall()]


@app.get("/api/records")
def records(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM records WHERE user_id = ? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()]


@app.post("/api/connectors")
def create_connector(payload: ConnectorRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    if payload.kind not in {"csv", "webhook", "rest"}:
        raise HTTPException(422, "Connector kind must be csv, webhook, or rest")
    if payload.kind == "rest" and not payload.endpoint_url:
        raise HTTPException(422, "REST connectors require an endpoint_url")
    if payload.kind == "webhook" and not payload.secret:
        raise HTTPException(422, "Webhook connectors require a signing secret")
    with db() as connection:
        connector_id = connection.execute("INSERT INTO connectors (user_id,name,kind,status,endpoint_url,secret_encrypted,mapping,last_sync,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (user["id"], payload.name, payload.kind, "configured", payload.endpoint_url, encrypt_secret(payload.secret), json.dumps(payload.mapping), now(), now())).lastrowid
        return connector_view(connection.execute("SELECT * FROM connectors WHERE id = ?", (connector_id,)).fetchone())


@app.get("/api/connectors/{connector_id}/health")
def connector_health(connector_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        connector = connection.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user["id"])).fetchone()
        if not connector:
            raise HTTPException(404, "Connector not found")
        logs = [dict(row) for row in connection.execute("SELECT status,message,created_at FROM ingestion_logs WHERE connector_id = ? ORDER BY id DESC LIMIT 10", (connector_id,)).fetchall()]
    return {"connector": connector_view(connector), "recent_runs": logs}


@app.post("/api/connectors/{connector_id}/sync")
async def sync_connector(connector_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    with db() as connection:
        connector = connection.execute("SELECT * FROM connectors WHERE id = ? AND user_id = ?", (connector_id, user["id"])).fetchone()
    if not connector:
        raise HTTPException(404, "Connector not found")
    if connector["kind"] != "rest" or not connector["endpoint_url"]:
        raise HTTPException(422, "Only configured REST connectors can be synced")
    headers = {}
    secret = decrypt_secret(connector["secret_encrypted"])
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(connector["endpoint_url"], headers=headers)
            response.raise_for_status()
            body = response.json()
        rows = body if isinstance(body, list) else body.get("data", body.get("results", [])) if isinstance(body, dict) else []
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Expected a JSON array or an object containing data/results")
        with db() as connection:
            added, duplicates = store_records(connection, connector, rows, "rest")
            connection.execute("UPDATE connectors SET records = records + ?, last_sync = ?, status = 'healthy', success_rate = MIN(100, success_rate + 0.5), last_error = NULL WHERE id = ?", (len(rows), now(), connector_id))
            connection.execute("INSERT INTO ingestion_logs (user_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?)", (user["id"], connector_id, "success", f"REST sync imported {added} records and found {duplicates} duplicate(s)", now()))
        return {"status": "success", "received": len(rows), "imported": added, "duplicates": duplicates}
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        with db() as connection:
            connection.execute("UPDATE connectors SET status = 'degraded', last_error = ?, success_rate = MAX(0, success_rate - 1) WHERE id = ?", (str(exc)[:500], connector_id))
            connection.execute("INSERT INTO ingestion_logs (user_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?)", (user["id"], connector_id, "error", f"REST sync failed: {str(exc)[:400]}", now()))
        raise HTTPException(502, "Connector sync failed; inspect connector health for details") from exc


@app.post("/api/connectors/suggest-mapping")
def suggest_mapping(payload: MappingRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    prompt = f"Return ONLY valid JSON object mapping each canonical field to an object with source and confidence (0-1). Canonical fields: {CANONICAL_FIELDS}. Sample: {payload.sample}"
    if genai and os.getenv("GEMINI_API_KEY"):
        try:
            response = genai.Client(api_key=os.getenv("GEMINI_API_KEY")).models.generate_content(model=GEMINI_MODEL, contents=prompt)
            parsed = json.loads(response.text.replace("```json", "").replace("```", "").strip())
            return {"mapping": parsed, "provider": "gemini"}
        except (ValueError, json.JSONDecodeError):
            pass
    lowered = payload.sample.lower()
    guesses = {}
    for field in CANONICAL_FIELDS:
        aliases = {"first_name": ["first", "given"], "last_name": ["last", "surname", "family"], "email": ["email", "mail"], "company": ["company", "organization", "account"], "phone": ["phone", "mobile", "tel"], "role": ["role", "title", "job"]}[field]
        source = next((part.strip(' \"\\n,:') for part in payload.sample.split(',') if any(alias in part.lower() for alias in aliases)), None)
        guesses[field] = {"source": source, "confidence": 0.92 if source else 0.0}
    return {"mapping": guesses, "provider": "local-fallback"}


@app.post("/api/clients/assistant/chat")
def assistant_chat(payload: ChatRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, str]:
    with db() as connection:
        stats = {"records": connection.execute("SELECT COUNT(*) FROM records WHERE user_id = ?", (user["id"],)).fetchone()[0], "duplicates": connection.execute("SELECT COUNT(*) FROM records WHERE user_id = ? AND status = 'duplicate'", (user["id"],)).fetchone()[0], "connectors": connection.execute("SELECT COUNT(*) FROM connectors WHERE user_id = ?", (user["id"],)).fetchone()[0]}
    context = f"Workspace facts: {stats}. User question: {payload.message}"
    if genai and os.getenv("GEMINI_API_KEY"):
        try:
            response = genai.Client(api_key=os.getenv("GEMINI_API_KEY")).models.generate_content(model=GEMINI_MODEL, contents="Answer concisely using only these workspace facts. If the facts do not answer it, say what is missing. " + context)
            return {"answer": response.text, "provider": "gemini"}
        except Exception:
            pass
    message = payload.message.lower()
    if "duplicate" in message:
        answer = f"I found {stats['duplicates']} duplicate record(s) flagged for review. The current matching key is email."
    elif "connector" in message or "source" in message:
        answer = f"This workspace has {stats['connectors']} connectors and {stats['records']} normalized records across them."
    elif "record" in message or "how many" in message:
        answer = f"There are {stats['records']} records in the workspace. I can break that down by connector once you ask for a specific source."
    else:
        answer = "I can answer questions about record volume, connectors, duplicates, validation, and ingestion health. Add GEMINI_API_KEY for broader grounded analysis."
    return {"answer": answer, "provider": "local-fallback"}


@app.post("/api/ingest/csv")
async def ingest_csv(file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload a CSV file")
    content = (await file.read()).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(content)))
    if not rows:
        raise HTTPException(400, "The CSV has no data rows")
    with db() as connection:
        connector = connection.execute("SELECT id FROM connectors WHERE user_id = ? AND kind = 'csv' ORDER BY id LIMIT 1", (user["id"],)).fetchone()
        connector_id = connector["id"] if connector else connection.execute("INSERT INTO connectors (user_id,name,kind,status,last_sync,created_at) VALUES (?,?,?,?,?,?)", (user["id"], file.filename, "csv", "healthy", now(), now())).lastrowid
        existing = {row[0] for row in connection.execute("SELECT json_extract(data, '$.email') FROM records WHERE user_id = ?", (user["id"],)).fetchall()}
        added = 0
        duplicates = 0
        for row in rows:
            email = next((row[key] for key in row if key.lower() in ("email", "e-mail", "contact_email")), "").strip().lower()
            status = "duplicate" if email and email in existing else "valid"
            if status == "duplicate":
                duplicates += 1
            else:
                added += 1
                if email:
                    existing.add(email)
            connection.execute("INSERT INTO records (user_id,connector_id,data,source,status,external_id,created_at) VALUES (?,?,?,?,?,?,?)", (user["id"], connector_id, json.dumps(row), "csv", status, email or secrets.token_hex(8), now()))
        connection.execute("UPDATE connectors SET records = records + ?, last_sync = ?, status = 'healthy' WHERE id = ?", (len(rows), now(), connector_id))
        connection.execute("INSERT INTO ingestion_logs (user_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?)", (user["id"], connector_id, "success", f"Imported {len(rows)} rows, {duplicates} duplicate(s)", now()))
    return {"imported": added, "duplicates": duplicates, "total": len(rows)}


@app.post("/api/webhooks/{connector_id}")
async def webhook(connector_id: int, payload: dict[str, Any], x_signature: str = Header(default="", alias="X-Webhook-Signature")) -> dict[str, Any]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    with db() as connection:
        connector = connection.execute("SELECT * FROM connectors WHERE id = ? AND kind = 'webhook'", (connector_id,)).fetchone()
        if not connector:
            raise HTTPException(404, "Connector not found")
        secret = decrypt_secret(connector["secret_encrypted"])
        if not secret:
            raise HTTPException(503, "Webhook connector is not configured with a signing secret")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(x_signature.removeprefix("sha256="), expected):
            raise HTTPException(401, "Invalid webhook signature")
        added, duplicates = store_records(connection, connector, [payload], "webhook")
        connection.execute("UPDATE connectors SET records = records + 1, last_sync = ?, status = 'healthy', last_error = NULL WHERE id = ?", (now(), connector_id))
        connection.execute("INSERT INTO ingestion_logs (user_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?)", (connector["user_id"], connector_id, "success", f"Webhook accepted; {duplicates} duplicate(s)", now()))
    return {"status": "accepted", "imported": added, "duplicates": duplicates}


@app.get("/api/documents")
def documents(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT id,name,size,content_type,created_at FROM documents WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()]


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    content = await file.read()
    safe_name = f"{user['id']}_{secrets.token_hex(6)}_{Path(file.filename or 'document').name}"
    path = UPLOADS / safe_name
    path.write_bytes(content)
    with db() as connection:
        document_id = connection.execute("INSERT INTO documents (user_id,name,size,content_type,path,created_at) VALUES (?,?,?,?,?,?)", (user["id"], file.filename or "document", len(content), file.content_type or "application/octet-stream", str(path), now())).lastrowid
    return {"id": document_id, "name": file.filename, "size": len(content)}


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: int, user: sqlite3.Row = Depends(current_user)) -> FileResponse:
    with db() as connection:
        document = connection.execute("SELECT * FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"])).fetchone()
    if not document or not Path(document["path"]).exists():
        raise HTTPException(404, "Document not found")
    return FileResponse(document["path"], filename=document["name"], media_type=document["content_type"])


@app.get("/api/integrations")
def integrations(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        result = []
        for row in connection.execute("SELECT * FROM integrations WHERE user_id = ? ORDER BY id", (user["id"],)).fetchall():
            item = dict(row)
            item.pop("access_token_encrypted", None)
            item.pop("refresh_token_encrypted", None)
            result.append(item)
        return result


@app.get("/api/integrations/{integration_id}/oauth/start")
def start_oauth(integration_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, str]:
    with db() as connection:
        integration = connection.execute("SELECT * FROM integrations WHERE id = ? AND user_id = ?", (integration_id, user["id"])).fetchone()
    if not integration:
        raise HTTPException(404, "Integration not found")
    provider = OAUTH_INTEGRATIONS.get(integration["name"])
    if not provider:
        raise HTTPException(422, "This integration uses endpoint credentials rather than OAuth")
    config, client_id, _ = oauth_credentials(provider)
    params = {
        "client_id": client_id,
        "redirect_uri": oauth_redirect_uri(provider),
        "response_type": "code",
        "scope": config["scopes"],
        "state": oauth_state(user["id"], integration_id, provider),
    }
    if provider == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    query = urlencode(params)
    return {"url": f"{config['authorize']}?{query}"}


@app.get("/api/integrations/oauth/{provider}/callback")
async def oauth_callback(provider: str, code: str | None = None, state: str | None = None, error: str | None = None) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?integration_error={error}")
    if provider not in OAUTH_PROVIDERS or not code or not state:
        return RedirectResponse("/?integration_error=invalid_oauth_response")
    try:
        payload = jwt.decode(state, JWT_SECRET, algorithms=["HS256"])
        if payload.get("provider") != provider:
            raise jwt.InvalidTokenError("Provider mismatch")
        user_id = int(payload["sub"])
        integration_id = int(payload["integration_id"])
        config, client_id, client_secret = oauth_credentials(provider)
    except (jwt.PyJWTError, KeyError, ValueError, HTTPException):
        return RedirectResponse("/?integration_error=invalid_oauth_state")
    token_payload = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": oauth_redirect_uri(provider)}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(config["token"], data=token_payload, headers={"Accept": "application/json"})
            response.raise_for_status()
            token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Provider returned no access token")
        external_account = token_data.get("team", {}).get("name") if provider == "slack" else token_data.get("hub_id") if provider == "hubspot" else token_data.get("id_token")
        with db() as connection:
            integration = connection.execute("SELECT id FROM integrations WHERE id = ? AND user_id = ?", (integration_id, user_id)).fetchone()
            if not integration:
                return RedirectResponse("/?integration_error=integration_not_found")
            connection.execute("UPDATE integrations SET provider = ?, status = 'connected', connected_at = ?, access_token_encrypted = ?, refresh_token_encrypted = ?, scopes = ?, external_account = ? WHERE id = ? AND user_id = ?", (provider, now(), encrypt_secret(access_token), encrypt_secret(token_data.get("refresh_token")), token_data.get("scope") or config["scopes"], str(external_account or ""), integration_id, user_id))
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return RedirectResponse("/?integration_error=token_exchange_failed")
    return RedirectResponse("/?integration=connected")


@app.post("/api/integrations/{integration_id}")
def update_integration(integration_id: int, payload: IntegrationRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, str]:
    status = "connected" if payload.action == "connect" else "available"
    with db() as connection:
        connection.execute("UPDATE integrations SET status = ?, connected_at = ?, access_token_encrypted = CASE WHEN ? = 'available' THEN NULL ELSE access_token_encrypted END, refresh_token_encrypted = CASE WHEN ? = 'available' THEN NULL ELSE refresh_token_encrypted END WHERE id = ? AND user_id = ?", (status, now() if status == "connected" else None, status, status, integration_id, user["id"]))
    return {"status": status}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/{path:path}")
def static_files(path: str) -> FileResponse:
    file = ROOT / "static" / path
    if file.exists() and file.is_file():
        return FileResponse(file)
    return FileResponse(ROOT / "static" / "index.html")


