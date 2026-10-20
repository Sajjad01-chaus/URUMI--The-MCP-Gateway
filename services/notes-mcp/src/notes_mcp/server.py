import asyncio
import hmac
import os
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

READ_ONLY = ToolAnnotations(readOnlyHint=True)


class Settings(BaseSettings):
    mcp_auth_token: str
    app_version: str = "dev"
    notes_db_path: str = "/data/notes.db"
    allowed_hosts: str = "localhost:*,127.0.0.1:*"
    port: int = 8000


class Note(BaseModel):
    id: int
    title: str
    body: str
    created_at: str


class NoteStore:
    """Tiny SQLite store; lives on an emptyDir, so notes reset when the pod is replaced (demo upstream)."""

    def __init__(self, path: str):
        self.path = path
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT, created_at TEXT)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _run(self, sql: str, args: tuple = ()) -> list[Note]:
        with self._connect() as db:
            cursor = db.execute(sql, args)
            if sql.lstrip().upper().startswith("INSERT"):
                rows = db.execute("SELECT id, title, body, created_at FROM notes WHERE id = ?", (cursor.lastrowid,)).fetchall()
            else:
                rows = cursor.fetchall()
        return [Note(id=r[0], title=r[1], body=r[2], created_at=r[3]) for r in rows]

    async def run(self, sql: str, args: tuple = ()) -> list[Note]:
        return await asyncio.to_thread(self._run, sql, args)


async def fault_delay() -> None:
    """FAULT_DELAY_MS > 0 makes every tool slow, to demo the gateway's timeout on a slow upstream."""
    delay_ms = int(os.environ.get("FAULT_DELAY_MS", "0") or 0)
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


def create_server(store: NoteStore, version: str = "dev") -> MCPServer:
    mcp = MCPServer(name="notes-mcp", version=version, instructions="Team notes: create, list, read and search short notes.")

    @mcp.tool()
    async def create_note(title: Annotated[str, Field(min_length=1, max_length=200)], body: Annotated[str, Field(max_length=5000)]) -> Note:
        """Save a new team note."""
        await fault_delay()
        created = datetime.now(UTC).isoformat(timespec="seconds")
        return (await store.run("INSERT INTO notes (title, body, created_at) VALUES (?, ?, ?)", (title, body, created)))[0]

    @mcp.tool(annotations=READ_ONLY)
    async def list_notes(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> list[Note]:
        """List the most recent notes."""
        await fault_delay()
        return await store.run("SELECT id, title, body, created_at FROM notes ORDER BY id DESC LIMIT ?", (limit,))

    @mcp.tool(annotations=READ_ONLY)
    async def get_note(note_id: Annotated[int, Field(ge=1)]) -> Note:
        """Read one note by id."""
        await fault_delay()
        notes = await store.run("SELECT id, title, body, created_at FROM notes WHERE id = ?", (note_id,))
        if not notes:
            raise ToolError(f"note {note_id} not found")
        return notes[0]

    @mcp.tool(annotations=READ_ONLY)
    async def search(query: Annotated[str, Field(min_length=1, max_length=100)]) -> list[Note]:
        """Search notes by text in the title or body."""
        await fault_delay()
        like = f"%{query}%"
        return await store.run("SELECT id, title, body, created_at FROM notes WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT 20", (like, like))

    return mcp


class BearerTokenMiddleware:
    """Only callers presenting the gateway's token reach the MCP endpoint; /healthz stays open."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        presented = dict(scope["headers"]).get(b"authorization", b"") if scope["type"] == "http" else b""
        if scope["type"] == "http" and scope["path"] != "/healthz" and not hmac.compare_digest(presented, self.expected):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app(settings: Settings) -> ASGIApp:
    server = create_server(NoteStore(settings.notes_db_path), settings.app_version)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "version": settings.app_version})

    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[h.strip() for h in settings.allowed_hosts.split(",") if h.strip()],
            allowed_origins=[],
        ),
    )
    return BearerTokenMiddleware(app, settings.mcp_auth_token)


def main() -> None:
    settings = Settings()
    uvicorn.run(build_app(settings), host="0.0.0.0", port=settings.port)
