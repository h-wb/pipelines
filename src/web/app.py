"""HTTP API and web interface over the shared workspace services.

Single-user and self-hosted: one bearer token (`WORKSPACE_API_TOKEN`). Browsers
log in with it once and get a session cookie derived from it; the token itself
is never stored in the cookie. API clients, such as a phone pushing exports,
send `Authorization: Bearer <token>`.
"""

import hashlib
import hmac
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.core.config import ConfigurationConflict, ConfigurationError
from src.core.operations import OperationError
from src.core.service import WorkspaceService
from src.integrations.formats import InvalidArtifact

SESSION_COOKIE = "workspace_session"
DEFAULT_MAX_UPLOAD = 512 * 1024 * 1024
CHUNK = 1024 * 1024
ROLES = ("historical", "ongoing")

# Fixed messages for redirects: nothing user-supplied is reflected from the URL.
NOTICES = {
    "sync_requested": "Sync requested.",
    "import_requested": "Import requested.",
    "submitted": "File stored.",
    "duplicate": "This exact file was already stored; nothing new was saved.",
    "already_imported": "Already imported with the current parser; use replay to load it again.",
    "saved": "Configuration saved. It applies to operations requested from now on.",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class UploadTooLarge(ValueError):
    pass


class Unauthorized(Exception):
    pass


def create_app(
    service: WorkspaceService, token: str, max_upload_bytes: int = DEFAULT_MAX_UPLOAD
) -> FastAPI:
    if len(token) < 16:
        raise ValueError("WORKSPACE_API_TOKEN must be at least 16 characters")
    session = hmac.new(token.encode(), b"workspace-session", hashlib.sha256).hexdigest()
    app = FastAPI(title="Personal data workspace", docs_url=None, redoc_url=None)

    def authorize(request: Request) -> None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            if hmac.compare_digest(header[7:].strip().encode(), token.encode()):
                return
            raise Unauthorized
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not hmac.compare_digest(cookie.encode(), session.encode()):
            raise Unauthorized
        # Cookies are SameSite=Strict; also refuse cross-origin form posts outright.
        origin = request.headers.get("origin")
        if (
            request.method != "GET"
            and origin
            and urlsplit(origin).netloc != request.url.netloc
        ):
            raise Unauthorized

    @app.middleware("http")
    async def require_login(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in ("/login", "/healthz"):
            return await call_next(request)
        try:
            authorize(request)
        except Unauthorized:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    async def known_error(request: Request, error: Exception) -> Response:
        from src.core.runner import AlreadyImported

        status = 400
        if isinstance(error, (ConfigurationConflict, AlreadyImported)):
            status = 409
        elif isinstance(error, UploadTooLarge):
            status = 413
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(error)}, status_code=status)
        return templates.TemplateResponse(
            request, "error.html", {"message": str(error)}, status_code=status
        )

    # AlreadyImported and ConfigurationConflict are subclasses of these.
    for kind in (ConfigurationError, OperationError, InvalidArtifact, UploadTooLarge):
        app.add_exception_handler(kind, known_error)

    async def save_stream(chunks: AsyncIterator[bytes], directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=directory, prefix=".upload-")
        size = 0
        try:
            with os.fdopen(handle, "wb") as target:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise UploadTooLarge(f"Upload exceeds {max_upload_bytes} bytes")
                    target.write(chunk)
        except BaseException:
            os.unlink(name)
            raise
        return Path(name)

    async def upload_chunks(upload: UploadFile) -> AsyncIterator[bytes]:
        while chunk := await upload.read(CHUNK):
            yield chunk

    def submit(
        connection_id: str, path: Path, role: str, import_now: bool, name: str
    ) -> dict:
        try:
            if role not in ROLES:
                raise OperationError("role must be historical or ongoing")
            return service.submit_file(
                connection_id,
                path,
                role,  # type: ignore[arg-type]
                import_now=import_now,
                original_name=name,
            )
        finally:
            path.unlink(missing_ok=True)

    staging = service.artifact_root / ".uploads"

    # ---- JSON API -------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/overview")
    def api_overview() -> dict:
        return service.overview()

    @app.get("/api/connections/{connection_id}")
    def api_connection(connection_id: str) -> dict:
        return service.connection(connection_id)

    @app.post("/api/connections/{connection_id}/sync", status_code=202)
    def api_sync(connection_id: str) -> dict:
        return {"operation_id": service.request_sync(connection_id)}

    @app.post("/api/connections/{connection_id}/artifacts")
    async def api_upload(
        request: Request,
        connection_id: str,
        role: str,
        name: str = "upload",
    ) -> JSONResponse:
        """Store the raw request body as one file (e.g. a Health Auto Export POST).

        `?import=true` also requests its import. `name` is only a label.
        """
        import_now = request.query_params.get("import", "").lower() in (
            "1",
            "true",
            "yes",
        )
        path = await save_stream(request.stream(), staging)
        result = submit(connection_id, path, role, import_now, name)
        return JSONResponse(result, status_code=201 if result["new"] else 200)

    @app.post(
        "/api/connections/{connection_id}/artifacts/{artifact_id}/import",
        status_code=202,
    )
    def api_import(connection_id: str, artifact_id: str, replay: bool = False) -> dict:
        return {
            "operation_id": service.request_import(connection_id, artifact_id, replay)
        }

    @app.get("/api/config")
    def api_config() -> dict:
        return service.configuration()

    @app.put("/api/config")
    async def api_save_config(request: Request) -> dict:
        expected = request.headers.get("if-match", "").strip('"')
        if not expected:
            raise OperationError(
                "If-Match with the file_revision you edited is required"
            )
        text = (await request.body()).decode("utf-8")
        workspace = service.save_configuration(text, expected)
        return {"saved": True, "revision": workspace.revision}

    # ---- Web interface ---------------------------------------------------------

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        context["notice"] = NOTICES.get(request.query_params.get("notice", ""))
        return templates.TemplateResponse(request, name, context)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return page(request, "login.html", failed=False)

    @app.post("/login")
    def login(request: Request, token_input: str = Form(alias="token")) -> Response:
        if not hmac.compare_digest(token_input.encode(), token.encode()):
            return templates.TemplateResponse(
                request, "login.html", {"failed": True, "notice": None}, status_code=401
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/logout")
    def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        data = service.overview()
        refresh = any(_in_flight(c["status"]) for c in data["connections"])
        return page(request, "overview.html", overview=data, refresh=refresh)

    @app.get("/connections/{connection_id}", response_class=HTMLResponse)
    def connection(request: Request, connection_id: str) -> HTMLResponse:
        data = service.connection(connection_id)
        return page(
            request,
            "connection.html",
            c=data,
            roles=ROLES,
            refresh=_in_flight(data["status"]),
        )

    def back(connection_id: str, notice: str) -> RedirectResponse:
        return RedirectResponse(
            f"/connections/{connection_id}?notice={notice}", status_code=303
        )

    @app.post("/connections/{connection_id}/sync")
    def sync(connection_id: str) -> Response:
        service.request_sync(connection_id)
        return back(connection_id, "sync_requested")

    @app.post("/connections/{connection_id}/upload")
    async def upload(
        connection_id: str,
        file: UploadFile,
        role: str = Form(),
        import_now: bool = Form(False),
    ) -> Response:
        path = await save_stream(upload_chunks(file), staging)
        result = submit(
            connection_id, path, role, import_now, file.filename or "upload"
        )
        if result.get("already_imported"):
            return back(connection_id, "already_imported")
        if result.get("operation_id"):
            return back(connection_id, "import_requested")
        return back(connection_id, "submitted" if result["new"] else "duplicate")

    @app.post("/connections/{connection_id}/artifacts/{artifact_id}/import")
    def import_(
        connection_id: str, artifact_id: str, replay: bool = Form(False)
    ) -> Response:
        from src.core.runner import AlreadyImported

        try:
            service.request_import(connection_id, artifact_id, replay)
        except AlreadyImported:
            return back(connection_id, "already_imported")
        return back(connection_id, "import_requested")

    @app.get("/config", response_class=HTMLResponse)
    def config_form(request: Request) -> HTMLResponse:
        return page(request, "config.html", config=service.configuration(), error=None)

    @app.post("/config", response_model=None)
    def save_config(
        request: Request, text: str = Form(), file_revision: str = Form()
    ) -> Response:
        try:
            service.save_configuration(text, file_revision)
        except ConfigurationError as error:
            # Keep the edit on screen; a conflict also shows the file's current text.
            current = service.configuration()
            return templates.TemplateResponse(
                request,
                "config.html",
                {
                    "config": {**current, "text": text},
                    "current_text": current["text"]
                    if isinstance(error, ConfigurationConflict)
                    else None,
                    "error": str(error),
                    "notice": None,
                },
                status_code=409 if isinstance(error, ConfigurationConflict) else 400,
            )
        return RedirectResponse("/config?notice=saved", status_code=303)

    return app


def _in_flight(status: dict) -> bool:
    """Refresh the page while something is requested or running."""
    last = status["last_attempt"]
    return bool(
        status["requested_not_started"] or (last and last["ingestion"] == "running")
    )
