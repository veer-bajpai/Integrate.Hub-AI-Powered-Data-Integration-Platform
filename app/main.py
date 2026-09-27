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
from time import monotonic
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
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
DASHBOARD_STATS_CACHE_TTL = 30
dashboard_stats_cache: dict[int, tuple[float, dict[str, Any]]] = {}


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


def current_workspace(
    user: sqlite3.Row = Depends(current_user),
    x_workspace_id: int | None = Header(default=None, alias="X-Workspace-ID"),
) -> sqlite3.Row:
    with db() as connection:
        if x_workspace_id is None:
            membership = connection.execute(
                "SELECT w.*, m.role FROM workspaces w JOIN workspace_memberships m ON m.workspace_id = w.id WHERE m.user_id = ? ORDER BY m.id LIMIT 1",
                (user["id"],),
            ).fetchone()
        else:
            membership = connection.execute(
                "SELECT w.*, m.role FROM workspaces w JOIN workspace_memberships m ON m.workspace_id = w.id WHERE m.user_id = ? AND w.id = ?",
                (user["id"], x_workspace_id),
            ).fetchone()
    if not membership:
        raise HTTPException(404, "Workspace not found")
    return membership


def workspace_for_user(workspace_id: int, user_id: int) -> sqlite3.Row:
    with db() as connection:
        workspace = connection.execute(
            "SELECT w.*, m.role FROM workspaces w JOIN workspace_memberships m ON m.workspace_id = w.id WHERE w.id = ? AND m.user_id = ?",
            (workspace_id, user_id),
        ).fetchone()
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    return workspace


def require_workspace_admin(workspace_id: int, user_id: int) -> sqlite3.Row:
    workspace = workspace_for_user(workspace_id, user_id)
    if workspace["role"] not in {"owner", "admin"}:
        raise HTTPException(404, "Workspace not found")
    return workspace


def write_audit(
    connection: sqlite3.Connection,
    workspace_id: int,
    actor_user_id: int,
    action: str,
    target_type: str,
    target_id: str | int | None,
    details: dict[str, Any],
    already_idempotent: bool | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_log (workspace_id,actor_user_id,action,target_type,target_id,details,already_idempotent,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (workspace_id, actor_user_id, action, target_type, str(target_id) if target_id is not None else None, json.dumps(details, sort_keys=True), None if already_idempotent is None else int(already_idempotent), now()),
    )


def gemini_rollout_bucket(workspace_id: int) -> int:
    digest = hashlib.sha256(f"integratehub-gemini-workspace:{workspace_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def gemini_mapping_enabled(workspace: sqlite3.Row) -> bool:
    return bool(workspace["gemini_enabled"]) and gemini_rollout_bucket(workspace["id"]) < workspace["gemini_rollout_percent"]


def init_db() -> None:
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY, name TEXT NOT NULL, gemini_enabled INTEGER NOT NULL DEFAULT 1, gemini_rollout_percent INTEGER NOT NULL DEFAULT 100, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_memberships (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','admin','member')), created_at TEXT NOT NULL, UNIQUE(workspace_id,user_id), FOREIGN KEY(workspace_id) REFERENCES workspaces(id), FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS connectors (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, records INTEGER DEFAULT 0, last_sync TEXT, success_rate REAL DEFAULT 100, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER, connector_id INTEGER, data TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL, external_id TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER, name TEXT NOT NULL, size INTEGER NOT NULL, content_type TEXT, path TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS integrations (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER, name TEXT NOT NULL, category TEXT NOT NULL, status TEXT NOT NULL, description TEXT NOT NULL, connected_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS ingestion_logs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, workspace_id INTEGER, connector_id INTEGER, status TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, actor_user_id INTEGER NOT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT, details TEXT NOT NULL, already_idempotent INTEGER, created_at TEXT NOT NULL, FOREIGN KEY(workspace_id) REFERENCES workspaces(id), FOREIGN KEY(actor_user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS comments (id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL, author_id INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(record_id) REFERENCES records(id), FOREIGN KEY(author_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS saved_views (id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL, filters TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace_id,user_id,name), FOREIGN KEY(workspace_id) REFERENCES workspaces(id), FOREIGN KEY(user_id) REFERENCES users(id));
        """)
        for table in ("connectors", "records", "documents", "integrations", "ingestion_logs"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if "workspace_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id INTEGER")
        users = connection.execute("SELECT id,name FROM users ORDER BY id").fetchall()
        for user in users:
            membership = connection.execute("SELECT workspace_id FROM workspace_memberships WHERE user_id = ? ORDER BY id LIMIT 1", (user["id"],)).fetchone()
            if not membership:
                workspace_id = connection.execute(
                    "INSERT INTO workspaces (name,created_at) VALUES (?,?)",
                    (f"{user['name']}'s workspace", now()),
                ).lastrowid
                connection.execute(
                    "INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)",
                    (workspace_id, user["id"], "owner", now()),
                )
            else:
                workspace_id = membership["workspace_id"]
            for table in ("connectors", "records", "documents", "integrations", "ingestion_logs"):
                connection.execute(
                    f"UPDATE {table} SET workspace_id = ? WHERE user_id = ? AND workspace_id IS NULL",
                    (workspace_id, user["id"]),
                )
        connection.executescript("""
        CREATE INDEX IF NOT EXISTS idx_connectors_workspace ON connectors(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_records_workspace ON records(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_documents_workspace ON documents(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_integrations_workspace ON integrations(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_ingestion_logs_workspace ON ingestion_logs(workspace_id);
        CREATE INDEX IF NOT EXISTS idx_audit_log_workspace ON audit_log(workspace_id,id DESC);
        CREATE INDEX IF NOT EXISTS idx_comments_record ON comments(record_id,id);
        CREATE INDEX IF NOT EXISTS idx_saved_views_user_workspace ON saved_views(workspace_id,user_id,id DESC);
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


class WorkspaceRequest(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class MemberRequest(BaseModel):
    email: str
    role: str = "member"


class MemberRoleRequest(BaseModel):
    role: str


class WorkspaceAISettings(BaseModel):
    gemini_enabled: bool
    gemini_rollout_percent: int = Field(ge=0, le=100)


class SecretRotationRequest(BaseModel):
    secret: str = Field(min_length=8)


class WebhookReplayAuditRequest(BaseModel):
    connector_id: int
    event_id: str = Field(min_length=1, max_length=200)
    already_idempotent: bool


class RecordCommentRequest(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class SavedViewRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    filters: dict[str, str] = Field(default_factory=dict)


def oauth_redirect_uri(provider: str) -> str:
    return f"{OAUTH_BASE_URL.rstrip('/')}/api/integrations/oauth/{provider}/callback"


def oauth_state(user_id: int, workspace_id: int, integration_id: int, provider: str) -> str:
    payload = {"sub": str(user_id), "workspace_id": workspace_id, "integration_id": integration_id, "provider": provider, "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
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
    connector.pop("workspace_id", None)
    return connector


def mapped_record(raw: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    if not mapping:
        return raw
    return {canonical: raw.get(source) for canonical, source in mapping.items() if source in raw}


def store_records(connection: sqlite3.Connection, connector: sqlite3.Row, rows: list[dict[str, Any]], source: str) -> tuple[int, int]:
    existing = {row[0] for row in connection.execute("SELECT COALESCE(json_extract(data, '$.email'), external_id) FROM records WHERE workspace_id = ?", (connector["workspace_id"],)).fetchall()}
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
        connection.execute("INSERT INTO records (user_id,workspace_id,connector_id,data,source,status,external_id,created_at) VALUES (?,?,?,?,?,?,?,?)", (connector["user_id"], connector["workspace_id"], connector["id"], json.dumps(record), source, status, external_id, now()))
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
            workspace_id = connection.execute("INSERT INTO workspaces (name,created_at) VALUES (?,?)", (f"{payload.name}'s workspace", now())).lastrowid
            connection.execute("INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)", (workspace_id, user_id, "owner", now()))
            for name, category, description in [("Slack", "Communication", "Send ingestion alerts and daily summaries."), ("HubSpot", "CRM", "Sync normalized contacts to a CRM."), ("Google Sheets", "Workspace", "Publish clean records to a sheet."), ("REST API", "Developer tools", "Connect a client-owned HTTP endpoint.")]:
                connection.execute("INSERT INTO integrations (user_id,workspace_id,name,category,status,description) VALUES (?,?,?,?,?,?)", (user_id, workspace_id, name, category, "available", description))
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


@app.get("/api/workspaces")
def list_workspaces(user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            "SELECT w.id,w.name,m.role,w.gemini_enabled,w.gemini_rollout_percent FROM workspaces w JOIN workspace_memberships m ON m.workspace_id = w.id WHERE m.user_id = ? ORDER BY w.id",
            (user["id"],),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/workspaces")
def create_workspace(payload: WorkspaceRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    name = payload.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Workspace name must contain at least two characters")
    with db() as connection:
        workspace_id = connection.execute("INSERT INTO workspaces (name,created_at) VALUES (?,?)", (name, now())).lastrowid
        connection.execute("INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)", (workspace_id, user["id"], "owner", now()))
        for name, category, description in [("Slack", "Communication", "Send ingestion alerts and daily summaries."), ("HubSpot", "CRM", "Sync normalized contacts to a CRM."), ("Google Sheets", "Workspace", "Publish clean records to a sheet."), ("REST API", "Developer tools", "Connect a client-owned HTTP endpoint.")]:
            connection.execute("INSERT INTO integrations (user_id,workspace_id,name,category,status,description) VALUES (?,?,?,?,?,?)", (user["id"], workspace_id, name, category, "available", description))
        workspace = connection.execute("SELECT id,name,gemini_enabled,gemini_rollout_percent FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        write_audit(connection, workspace_id, user["id"], "workspace_created", "workspace", workspace_id, {"name": name})
    return {**dict(workspace), "role": "owner"}


@app.get("/api/workspaces/{workspace_id}/members")
def list_workspace_members(workspace_id: int, user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    workspace_for_user(workspace_id, user["id"])
    with db() as connection:
        rows = connection.execute(
            "SELECT u.id,u.name,u.email,m.role,m.created_at FROM workspace_memberships m JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,u.name",
            (workspace_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/workspaces/{workspace_id}/members")
def add_workspace_member(workspace_id: int, payload: MemberRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    workspace = require_workspace_admin(workspace_id, user["id"])
    role = payload.role.strip().lower()
    if role not in {"member", "admin"} or (role == "admin" and workspace["role"] != "owner"):
        raise HTTPException(404, "Workspace not found")
    with db() as connection:
        member = connection.execute("SELECT id,name,email FROM users WHERE email = ?", (payload.email.lower(),)).fetchone()
        if not member:
            raise HTTPException(404, "User not found")
        try:
            connection.execute("INSERT INTO workspace_memberships (workspace_id,user_id,role,created_at) VALUES (?,?,?,?)", (workspace_id, member["id"], role, now()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "User is already a workspace member") from exc
        write_audit(connection, workspace_id, user["id"], "member_added", "user", member["id"], {"email": member["email"], "role": role})
    return {**dict(member), "role": role}


@app.patch("/api/workspaces/{workspace_id}/members/{member_user_id}")
def change_member_role(workspace_id: int, member_user_id: int, payload: MemberRoleRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, str]:
    workspace = require_workspace_admin(workspace_id, user["id"])
    role = payload.role.strip().lower()
    if role not in {"admin", "member"} or (role == "admin" and workspace["role"] != "owner"):
        raise HTTPException(404, "Workspace not found")
    with db() as connection:
        member = connection.execute("SELECT role FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, member_user_id)).fetchone()
        if not member:
            raise HTTPException(404, "Member not found")
        if member["role"] == "owner":
            raise HTTPException(422, "The workspace owner role cannot be changed here")
        connection.execute("UPDATE workspace_memberships SET role = ? WHERE workspace_id = ? AND user_id = ?", (role, workspace_id, member_user_id))
        write_audit(connection, workspace_id, user["id"], "role_changed", "user", member_user_id, {"from": member["role"], "to": role})
    return {"status": "updated", "role": role}


@app.get("/api/workspaces/{workspace_id}/audit-log")
def get_audit_log(workspace_id: int, user: sqlite3.Row = Depends(current_user)) -> list[dict[str, Any]]:
    require_workspace_admin(workspace_id, user["id"])
    with db() as connection:
        rows = connection.execute(
            "SELECT a.*,u.name AS actor_name FROM audit_log a JOIN users u ON u.id = a.actor_user_id WHERE a.workspace_id = ? ORDER BY a.id DESC LIMIT 200",
            (workspace_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        item["already_idempotent"] = None if item["already_idempotent"] is None else bool(item["already_idempotent"])
        result.append(item)
    return result


@app.post("/api/workspaces/{workspace_id}/audit-log/webhook-replay")
def record_webhook_replay(workspace_id: int, payload: WebhookReplayAuditRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    require_workspace_admin(workspace_id, user["id"])
    with db() as connection:
        connector = connection.execute("SELECT id FROM connectors WHERE id = ? AND workspace_id = ? AND kind = 'webhook'", (payload.connector_id, workspace_id)).fetchone()
        if not connector:
            raise HTTPException(404, "Connector not found")
        write_audit(connection, workspace_id, user["id"], "webhook_replay_recorded", "webhook_event", payload.event_id, {"connector_id": payload.connector_id}, payload.already_idempotent)
    return {"status": "recorded", "already_idempotent": payload.already_idempotent}


@app.post("/api/workspaces/{workspace_id}/connectors/{connector_id}/rotate-secret")
def rotate_connector_secret(workspace_id: int, connector_id: int, payload: SecretRotationRequest, user: sqlite3.Row = Depends(current_user)) -> dict[str, str]:
    require_workspace_admin(workspace_id, user["id"])
    with db() as connection:
        connector = connection.execute("SELECT id FROM connectors WHERE id = ? AND workspace_id = ? AND kind IN ('webhook','rest')", (connector_id, workspace_id)).fetchone()
        if not connector:
            raise HTTPException(404, "Connector not found")
        connection.execute("UPDATE connectors SET secret_encrypted = ? WHERE id = ? AND workspace_id = ?", (encrypt_secret(payload.secret), connector_id, workspace_id))
        write_audit(connection, workspace_id, user["id"], "connector_secret_rotated", "connector", connector_id, {})
    return {"status": "rotated"}


@app.get("/api/workspaces/{workspace_id}/ai-settings")
def get_workspace_ai_settings(workspace_id: int, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    workspace = workspace_for_user(workspace_id, user["id"])
    return {"gemini_enabled": bool(workspace["gemini_enabled"]), "gemini_rollout_percent": workspace["gemini_rollout_percent"], "workspace_bucket": gemini_rollout_bucket(workspace_id)}


@app.patch("/api/workspaces/{workspace_id}/ai-settings")
def update_workspace_ai_settings(workspace_id: int, payload: WorkspaceAISettings, user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    require_workspace_admin(workspace_id, user["id"])
    with db() as connection:
        connection.execute("UPDATE workspaces SET gemini_enabled = ?, gemini_rollout_percent = ? WHERE id = ?", (int(payload.gemini_enabled), payload.gemini_rollout_percent, workspace_id))
        write_audit(connection, workspace_id, user["id"], "gemini_rollout_changed", "workspace", workspace_id, payload.model_dump())
    return {"gemini_enabled": payload.gemini_enabled, "gemini_rollout_percent": payload.gemini_rollout_percent, "workspace_bucket": gemini_rollout_bucket(workspace_id)}


@app.get("/api/dashboard")
def dashboard(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    with db() as connection:
        connectors = [dict(row) for row in connection.execute("SELECT * FROM connectors WHERE workspace_id = ? ORDER BY id", (workspace["id"],)).fetchall()]
        records = connection.execute("SELECT COUNT(*) FROM records WHERE workspace_id = ?", (workspace["id"],)).fetchone()[0]
        duplicates = connection.execute("SELECT COUNT(*) FROM records WHERE workspace_id = ? AND status = 'duplicate'", (workspace["id"],)).fetchone()[0]
        documents = connection.execute("SELECT COUNT(*) FROM documents WHERE workspace_id = ?", (workspace["id"],)).fetchone()[0]
        logs = [dict(row) for row in connection.execute("SELECT * FROM ingestion_logs WHERE workspace_id = ? ORDER BY id DESC LIMIT 5", (workspace["id"],)).fetchall()]
    for connector in connectors:
        connector.pop("workspace_id", None)
    for log in logs:
        log.pop("workspace_id", None)
    return {"user": {"name": user["name"], "email": user["email"]}, "metrics": {"records": records, "connectors": len(connectors), "duplicates": duplicates, "documents": documents}, "connectors": connectors, "logs": logs}


@app.get("/api/stats/dashboard")
def dashboard_stats(user: sqlite3.Row = Depends(current_user)) -> dict[str, Any]:
    user_id = user["id"]
    request_time = monotonic()
    cached = dashboard_stats_cache.get(user_id)
    if cached and cached[0] > request_time:
        return cached[1]
    for cached_user_id, (expires_at, _) in list(dashboard_stats_cache.items()):
        if expires_at <= request_time:
            dashboard_stats_cache.pop(cached_user_id, None)

    today = datetime.now(timezone.utc).date()
    records_start = (today - timedelta(days=13)).isoformat()
    duplicates_start = (today - timedelta(days=6)).isoformat()
    records_end = today.isoformat()
    with db() as connection:
        records_rows = connection.execute(
            "SELECT date(created_at) AS day, COUNT(*) AS count FROM records WHERE user_id = ? AND date(created_at) BETWEEN ? AND ? GROUP BY date(created_at) ORDER BY date(created_at)",
            (user_id, records_start, records_end),
        ).fetchall()
        source_rows = connection.execute(
            "SELECT source, COUNT(*) AS count FROM records WHERE user_id = ? AND date(created_at) BETWEEN ? AND ? GROUP BY source ORDER BY source",
            (user_id, records_start, records_end),
        ).fetchall()
        duplicate_rows = connection.execute(
            "SELECT date(created_at) AS day, COUNT(*) AS count FROM records WHERE user_id = ? AND status = 'duplicate' AND date(created_at) BETWEEN ? AND ? GROUP BY date(created_at) ORDER BY date(created_at)",
            (user_id, duplicates_start, records_end),
        ).fetchall()
        sync_rows = connection.execute(
            "SELECT connector_id, COUNT(*) AS count FROM ingestion_logs WHERE user_id = ? AND connector_id IS NOT NULL AND date(created_at) BETWEEN ? AND ? GROUP BY connector_id",
            (user_id, duplicates_start, records_end),
        ).fetchall()
        connector_rows = connection.execute(
            "SELECT id, name, COALESCE(records, 0) AS records FROM connectors WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()

    record_counts = {row["day"]: row["count"] for row in records_rows}
    duplicate_counts = {row["day"]: row["count"] for row in duplicate_rows}
    records_by_day = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": int(record_counts.get((today - timedelta(days=offset)).isoformat(), 0)),
        }
        for offset in range(13, -1, -1)
    ]
    duplicates_by_day = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": int(duplicate_counts.get((today - timedelta(days=offset)).isoformat(), 0)),
        }
        for offset in range(6, -1, -1)
    ]

    source_counts = {row["source"]: int(row["count"]) for row in source_rows}
    source_order = ["csv", "webhook", "rest"]
    records_by_source = [
        {"source": source, "count": source_counts.pop(source, 0)}
        for source in source_order
    ]
    records_by_source.extend(
        {"source": source, "count": count}
        for source, count in sorted(source_counts.items())
    )

    sync_counts = {row["connector_id"]: int(row["count"]) for row in sync_rows}
    sync_volume_by_connector = []
    for connector in connector_rows:
        connector_id = connector["id"]
        current_records = int(connector["records"])
        has_recent_activity = connector_id in sync_counts
        volume = sync_counts.get(connector_id, current_records)
        sync_volume_by_connector.append(
            {
                "name": connector["name"],
                "records": current_records,
                "last_7_days": volume,
                "basis": "ingestion_logs" if has_recent_activity else "connector_records",
            }
        )

    result = {
        "records_by_day": records_by_day,
        "records_by_source": records_by_source,
        "duplicates_by_day": duplicates_by_day,
        "sync_volume_by_connector": sync_volume_by_connector,
    }
    dashboard_stats_cache[user_id] = (request_time + DASHBOARD_STATS_CACHE_TTL, result)
    return result


@app.get("/api/connectors")
def connectors(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        return [connector_view(row) for row in connection.execute("SELECT * FROM connectors WHERE workspace_id = ? ORDER BY id", (workspace["id"],)).fetchall()]


@app.get("/api/records")
def records(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        result = [dict(row) for row in connection.execute("SELECT * FROM records WHERE workspace_id = ? ORDER BY id DESC LIMIT 100", (workspace["id"],)).fetchall()]
    for record in result:
        record.pop("workspace_id", None)
    return result


@app.get("/api/records/saved-views")
def saved_views(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            "SELECT id,name,filters,created_at,updated_at FROM saved_views WHERE workspace_id = ? AND user_id = ? ORDER BY id DESC",
            (workspace["id"], user["id"]),
        ).fetchall()
    return [{**dict(row), "filters": json.loads(row["filters"])} for row in rows]


@app.get("/api/records/{record_id}")
def record_detail(record_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    with db() as connection:
        record = connection.execute(
            "SELECT * FROM records WHERE id = ? AND workspace_id = ?",
            (record_id, workspace["id"]),
        ).fetchone()
    if not record:
        raise HTTPException(404, "Record not found")
    result = dict(record)
    result.pop("workspace_id", None)
    return result


@app.get("/api/records/{record_id}/comments")
def record_comments(record_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        record = connection.execute("SELECT id FROM records WHERE id = ? AND workspace_id = ?", (record_id, workspace["id"])).fetchone()
        if not record:
            raise HTTPException(404, "Record not found")
        rows = connection.execute(
            "SELECT c.id,c.record_id,c.author_id,c.body,c.created_at,u.name AS author_name FROM comments c JOIN users u ON u.id = c.author_id WHERE c.record_id = ? ORDER BY c.id",
            (record_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/records/{record_id}/comments", status_code=201)
def add_record_comment(record_id: int, payload: RecordCommentRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    body = payload.body.strip()
    if not body:
        raise HTTPException(422, "Comment cannot be empty")
    created_at = now()
    with db() as connection:
        record = connection.execute("SELECT id FROM records WHERE id = ? AND workspace_id = ?", (record_id, workspace["id"])).fetchone()
        if not record:
            raise HTTPException(404, "Record not found")
        comment_id = connection.execute(
            "INSERT INTO comments (record_id,author_id,body,created_at) VALUES (?,?,?,?)",
            (record_id, user["id"], body, created_at),
        ).lastrowid
        write_audit(connection, workspace["id"], user["id"], "record_comment_created", "record", record_id, {"comment_id": comment_id})
    return {"id": comment_id, "record_id": record_id, "author_id": user["id"], "author_name": user["name"], "body": body, "created_at": created_at}


@app.post("/api/records/saved-views", status_code=201)
def create_saved_view(payload: SavedViewRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "View name cannot be empty")
    allowed_filters = {"query", "source", "status", "created_after", "created_before"}
    if set(payload.filters) - allowed_filters:
        raise HTTPException(422, "Saved view contains unsupported filters")
    filters = {key: value.strip() for key, value in payload.filters.items() if value.strip()}
    created_at = now()
    with db() as connection:
        try:
            view_id = connection.execute(
                "INSERT INTO saved_views (workspace_id,user_id,name,filters,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (workspace["id"], user["id"], name, json.dumps(filters, sort_keys=True), created_at, created_at),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "A saved view with that name already exists") from exc
    return {"id": view_id, "name": name, "filters": filters, "created_at": created_at, "updated_at": created_at}


@app.delete("/api/records/saved-views/{view_id}")
def delete_saved_view(view_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, str]:
    with db() as connection:
        result = connection.execute(
            "DELETE FROM saved_views WHERE id = ? AND workspace_id = ? AND user_id = ?",
            (view_id, workspace["id"], user["id"]),
        )
        if not result.rowcount:
            raise HTTPException(404, "Saved view not found")
    return {"status": "deleted"}


@app.get("/api/search")
def global_search(q: str = "", user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    term = q.strip()[:100]
    if len(term) < 2:
        return []
    pattern = f"%{term}%"
    with db() as connection:
        record_rows = connection.execute(
            "SELECT id,data,source,status,created_at FROM records WHERE workspace_id = ? AND (data LIKE ? COLLATE NOCASE OR source LIKE ? COLLATE NOCASE OR status LIKE ? COLLATE NOCASE) ORDER BY id DESC LIMIT 5",
            (workspace["id"], pattern, pattern, pattern),
        ).fetchall()
        document_rows = connection.execute(
            "SELECT id,name,content_type,path,created_at FROM documents WHERE workspace_id = ? ORDER BY id DESC LIMIT 100",
            (workspace["id"],),
        ).fetchall()
        comment_rows = connection.execute(
            "SELECT c.id,c.record_id,c.body,c.created_at,u.name AS author_name FROM comments c JOIN records r ON r.id = c.record_id JOIN users u ON u.id = c.author_id WHERE r.workspace_id = ? AND c.body LIKE ? COLLATE NOCASE ORDER BY c.id DESC LIMIT 5",
            (workspace["id"], pattern),
        ).fetchall()
    results = []
    for row in record_rows:
        data = json.loads(row["data"])
        label = data.get("name") or " ".join(filter(None, (data.get("first_name"), data.get("last_name")))) or data.get("email") or f"Record #{row['id']}"
        results.append({"type": "record", "id": row["id"], "record_id": row["id"], "title": str(label), "detail": f"{row['source']} · {row['status']}", "created_at": row["created_at"]})
    document_results = []
    searchable_extensions = {".txt", ".md", ".csv", ".json", ".xml", ".log"}
    for row in document_rows:
        matched_text = term.lower() in row["name"].lower()
        if not matched_text and Path(row["name"]).suffix.lower() in searchable_extensions:
            try:
                with Path(row["path"]).open("rb") as document_file:
                    content = document_file.read(128_000).decode("utf-8", errors="ignore")
                matched_text = term.lower() in content.lower()
            except OSError:
                matched_text = False
        if matched_text:
            document_results.append({"type": "document", "id": row["id"], "title": row["name"], "detail": row["content_type"] or "Document", "created_at": row["created_at"]})
            if len(document_results) == 5:
                break
    results.extend(document_results)
    for row in comment_rows:
        results.append({"type": "comment", "id": row["id"], "record_id": row["record_id"], "title": row["body"][:90], "detail": f"Comment by {row['author_name']} · Record #{row['record_id']}", "created_at": row["created_at"]})
    return sorted(results, key=lambda item: item["created_at"], reverse=True)[:15]


@app.get("/api/activity")
def workspace_activity(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    workspace_id = workspace["id"]
    with db() as connection:
        audit_rows = connection.execute(
            "SELECT a.id,a.action,a.target_type,a.target_id,a.created_at,u.name AS actor_name FROM audit_log a JOIN users u ON u.id = a.actor_user_id WHERE a.workspace_id = ? ORDER BY a.id DESC LIMIT 100",
            (workspace_id,),
        ).fetchall()
        sync_rows = connection.execute(
            "SELECT l.id,l.status,l.message,l.created_at,u.name AS actor_name,c.name AS connector_name FROM ingestion_logs l JOIN users u ON u.id = l.user_id LEFT JOIN connectors c ON c.id = l.connector_id WHERE l.workspace_id = ? ORDER BY l.id DESC LIMIT 100",
            (workspace_id,),
        ).fetchall()
        record_rows = connection.execute(
            "SELECT r.id,r.data,r.source,r.created_at,u.name AS actor_name FROM records r JOIN users u ON u.id = r.user_id WHERE r.workspace_id = ? ORDER BY r.id DESC LIMIT 100",
            (workspace_id,),
        ).fetchall()
        comment_rows = connection.execute(
            "SELECT c.id,c.record_id,c.body,c.created_at,u.name AS actor_name FROM comments c JOIN records r ON r.id = c.record_id JOIN users u ON u.id = c.author_id WHERE r.workspace_id = ? ORDER BY c.id DESC LIMIT 100",
            (workspace_id,),
        ).fetchall()
    activity = []
    for row in audit_rows:
        if row["action"] == "record_comment_created":
            continue
        activity.append({"id": f"audit-{row['id']}", "type": "audit", "actor_name": row["actor_name"], "summary": row["action"].replace("_", " "), "created_at": row["created_at"], "record_id": int(row["target_id"]) if row["target_type"] == "record" and row["target_id"] else None})
    for row in sync_rows:
        connector = f" · {row['connector_name']}" if row["connector_name"] else ""
        activity.append({"id": f"sync-{row['id']}", "type": "connector_sync", "status": row["status"], "actor_name": row["actor_name"], "summary": f"{row['message']}{connector}", "created_at": row["created_at"]})
    for row in record_rows:
        data = json.loads(row["data"])
        label = data.get("name") or data.get("email") or f"Record #{row['id']}"
        activity.append({"id": f"record-{row['id']}", "type": "record_created", "actor_name": row["actor_name"], "summary": f"Record added: {label} via {row['source']}", "created_at": row["created_at"], "record_id": row["id"]})
    for row in comment_rows:
        activity.append({"id": f"comment-{row['id']}", "type": "comment", "actor_name": row["actor_name"], "summary": f"Commented on record #{row['record_id']}: {row['body'][:120]}", "created_at": row["created_at"], "record_id": row["record_id"]})
    return sorted(activity, key=lambda item: item["created_at"], reverse=True)[:100]


@app.post("/api/connectors")
def create_connector(payload: ConnectorRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    if payload.kind not in {"csv", "webhook", "rest"}:
        raise HTTPException(422, "Connector kind must be csv, webhook, or rest")
    if payload.kind == "rest" and not payload.endpoint_url:
        raise HTTPException(422, "REST connectors require an endpoint_url")
    if payload.kind == "webhook" and not payload.secret:
        raise HTTPException(422, "Webhook connectors require a signing secret")
    with db() as connection:
        connector_id = connection.execute("INSERT INTO connectors (user_id,workspace_id,name,kind,status,endpoint_url,secret_encrypted,mapping,last_sync,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (user["id"], workspace["id"], payload.name, payload.kind, "configured", payload.endpoint_url, encrypt_secret(payload.secret), json.dumps(payload.mapping), now(), now())).lastrowid
        return connector_view(connection.execute("SELECT * FROM connectors WHERE id = ? AND workspace_id = ?", (connector_id, workspace["id"])).fetchone())


@app.get("/api/connectors/{connector_id}/health")
def connector_health(connector_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    with db() as connection:
        connector = connection.execute("SELECT * FROM connectors WHERE id = ? AND workspace_id = ?", (connector_id, workspace["id"])).fetchone()
        if not connector:
            raise HTTPException(404, "Connector not found")
        logs = [dict(row) for row in connection.execute("SELECT status,message,created_at FROM ingestion_logs WHERE connector_id = ? AND workspace_id = ? ORDER BY id DESC LIMIT 10", (connector_id, workspace["id"])).fetchall()]
    return {"connector": connector_view(connector), "recent_runs": logs}


@app.post("/api/connectors/{connector_id}/sync")
async def sync_connector(connector_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    with db() as connection:
        connector = connection.execute("SELECT * FROM connectors WHERE id = ? AND workspace_id = ?", (connector_id, workspace["id"])).fetchone()
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
            connection.execute("UPDATE connectors SET records = records + ?, last_sync = ?, status = 'healthy', success_rate = MIN(100, success_rate + 0.5), last_error = NULL WHERE id = ? AND workspace_id = ?", (len(rows), now(), connector_id, workspace["id"]))
            connection.execute("INSERT INTO ingestion_logs (user_id,workspace_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?,?)", (user["id"], workspace["id"], connector_id, "success", f"REST sync imported {added} records and found {duplicates} duplicate(s)", now()))
        return {"status": "success", "received": len(rows), "imported": added, "duplicates": duplicates}
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        with db() as connection:
            connection.execute("UPDATE connectors SET status = 'degraded', last_error = ?, success_rate = MAX(0, success_rate - 1) WHERE id = ? AND workspace_id = ?", (str(exc)[:500], connector_id, workspace["id"]))
            connection.execute("INSERT INTO ingestion_logs (user_id,workspace_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?,?)", (user["id"], workspace["id"], connector_id, "error", f"REST sync failed: {str(exc)[:400]}", now()))
        raise HTTPException(502, "Connector sync failed; inspect connector health for details") from exc


@app.post("/api/connectors/suggest-mapping")
def suggest_mapping(payload: MappingRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    prompt = f"Return ONLY valid JSON object mapping each canonical field to an object with source and confidence (0-1). Canonical fields: {CANONICAL_FIELDS}. Sample: {payload.sample}"
    if gemini_mapping_enabled(workspace) and genai and os.getenv("GEMINI_API_KEY"):
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
def assistant_chat(payload: ChatRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, str]:
    with db() as connection:
        stats = {"records": connection.execute("SELECT COUNT(*) FROM records WHERE workspace_id = ?", (workspace["id"],)).fetchone()[0], "duplicates": connection.execute("SELECT COUNT(*) FROM records WHERE workspace_id = ? AND status = 'duplicate'", (workspace["id"],)).fetchone()[0], "connectors": connection.execute("SELECT COUNT(*) FROM connectors WHERE workspace_id = ?", (workspace["id"],)).fetchone()[0]}
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
async def ingest_csv(file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload a CSV file")
    content = (await file.read()).decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(content)))
    if not rows:
        raise HTTPException(400, "The CSV has no data rows")
    with db() as connection:
        connector = connection.execute("SELECT id FROM connectors WHERE workspace_id = ? AND kind = 'csv' ORDER BY id LIMIT 1", (workspace["id"],)).fetchone()
        connector_id = connector["id"] if connector else connection.execute("INSERT INTO connectors (user_id,workspace_id,name,kind,status,last_sync,created_at) VALUES (?,?,?,?,?,?,?)", (user["id"], workspace["id"], file.filename, "csv", "healthy", now(), now())).lastrowid
        existing = {row[0] for row in connection.execute("SELECT json_extract(data, '$.email') FROM records WHERE workspace_id = ?", (workspace["id"],)).fetchall()}
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
            connection.execute("INSERT INTO records (user_id,workspace_id,connector_id,data,source,status,external_id,created_at) VALUES (?,?,?,?,?,?,?,?)", (user["id"], workspace["id"], connector_id, json.dumps(row), "csv", status, email or secrets.token_hex(8), now()))
        connection.execute("UPDATE connectors SET records = records + ?, last_sync = ?, status = 'healthy' WHERE id = ? AND workspace_id = ?", (len(rows), now(), connector_id, workspace["id"]))
        connection.execute("INSERT INTO ingestion_logs (user_id,workspace_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?,?)", (user["id"], workspace["id"], connector_id, "success", f"Imported {len(rows)} rows, {duplicates} duplicate(s)", now()))
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
        connection.execute("UPDATE connectors SET records = records + 1, last_sync = ?, status = 'healthy', last_error = NULL WHERE id = ? AND workspace_id = ?", (now(), connector_id, connector["workspace_id"]))
        connection.execute("INSERT INTO ingestion_logs (user_id,workspace_id,connector_id,status,message,created_at) VALUES (?,?,?,?,?,?)", (connector["user_id"], connector["workspace_id"], connector_id, "success", f"Webhook accepted; {duplicates} duplicate(s)", now()))
    return {"status": "accepted", "imported": added, "duplicates": duplicates}


@app.get("/api/documents")
def documents(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        return [dict(row) for row in connection.execute("SELECT id,name,size,content_type,created_at FROM documents WHERE workspace_id = ? ORDER BY id DESC", (workspace["id"],)).fetchall()]


@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...), user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, Any]:
    content = await file.read()
    safe_name = f"{workspace['id']}_{user['id']}_{secrets.token_hex(6)}_{Path(file.filename or 'document').name}"
    path = UPLOADS / safe_name
    path.write_bytes(content)
    with db() as connection:
        document_id = connection.execute("INSERT INTO documents (user_id,workspace_id,name,size,content_type,path,created_at) VALUES (?,?,?,?,?,?,?)", (user["id"], workspace["id"], file.filename or "document", len(content), file.content_type or "application/octet-stream", str(path), now())).lastrowid
    return {"id": document_id, "name": file.filename, "size": len(content)}


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> FileResponse:
    with db() as connection:
        document = connection.execute("SELECT * FROM documents WHERE id = ? AND workspace_id = ?", (document_id, workspace["id"])).fetchone()
    if not document or not Path(document["path"]).exists():
        raise HTTPException(404, "Document not found")
    return FileResponse(document["path"], filename=document["name"], media_type=document["content_type"])


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, str]:
    with db() as connection:
        document = connection.execute(
            "SELECT path FROM documents WHERE id = ? AND workspace_id = ?",
            (document_id, workspace["id"]),
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        try:
            Path(document["path"]).unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(500, "Unable to delete document file") from exc
        connection.execute(
            "DELETE FROM documents WHERE id = ? AND workspace_id = ?",
            (document_id, workspace["id"]),
        )
    return {"status": "deleted"}


@app.get("/api/integrations")
def integrations(user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> list[dict[str, Any]]:
    with db() as connection:
        result = []
        for row in connection.execute("SELECT * FROM integrations WHERE workspace_id = ? ORDER BY id", (workspace["id"],)).fetchall():
            item = dict(row)
            item.pop("access_token_encrypted", None)
            item.pop("refresh_token_encrypted", None)
            item.pop("workspace_id", None)
            result.append(item)
        return result


@app.get("/api/integrations/{integration_id}/oauth/start")
def start_oauth(integration_id: int, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, str]:
    with db() as connection:
        integration = connection.execute("SELECT * FROM integrations WHERE id = ? AND workspace_id = ?", (integration_id, workspace["id"])).fetchone()
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
        "state": oauth_state(user["id"], workspace["id"], integration_id, provider),
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
        workspace_id = int(payload["workspace_id"])
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
            member = connection.execute("SELECT 1 FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?", (workspace_id, user_id)).fetchone()
            integration = connection.execute("SELECT id FROM integrations WHERE id = ? AND workspace_id = ?", (integration_id, workspace_id)).fetchone() if member else None
            if not integration:
                return RedirectResponse("/?integration_error=integration_not_found")
            connection.execute("UPDATE integrations SET provider = ?, status = 'connected', connected_at = ?, access_token_encrypted = ?, refresh_token_encrypted = ?, scopes = ?, external_account = ? WHERE id = ? AND workspace_id = ?", (provider, now(), encrypt_secret(access_token), encrypt_secret(token_data.get("refresh_token")), token_data.get("scope") or config["scopes"], str(external_account or ""), integration_id, workspace_id))
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return RedirectResponse("/?integration_error=token_exchange_failed")
    return RedirectResponse("/?integration=connected")


@app.post("/api/integrations/{integration_id}")
def update_integration(integration_id: int, payload: IntegrationRequest, user: sqlite3.Row = Depends(current_user), workspace: sqlite3.Row = Depends(current_workspace)) -> dict[str, str]:
    status = "connected" if payload.action == "connect" else "available"
    with db() as connection:
        integration = connection.execute("SELECT id FROM integrations WHERE id = ? AND workspace_id = ?", (integration_id, workspace["id"])).fetchone()
        if not integration:
            raise HTTPException(404, "Integration not found")
        connection.execute("UPDATE integrations SET status = ?, connected_at = ?, access_token_encrypted = CASE WHEN ? = 'available' THEN NULL ELSE access_token_encrypted END, refresh_token_encrypted = CASE WHEN ? = 'available' THEN NULL ELSE refresh_token_encrypted END WHERE id = ? AND workspace_id = ?", (status, now() if status == "connected" else None, status, status, integration_id, workspace["id"]))
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


