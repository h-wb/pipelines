"""HTTP API and web interface over the shared miroir services.

Single-user and self-hosted: one admin bearer token (`MIROIR_API_TOKEN`). Browsers
log in with it once and get a session cookie derived from it; the token itself
is never stored in the cookie. Automatic exports use connection-scoped,
receive-only bearer tokens rather than the administrator token.
"""

import hashlib
import hmac
import os
import re
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
from src.core.secrets import SecretKeyError, SecretResolutionError
from src.core.service import MiroirService
from src.integrations.formats import InvalidArtifact

SESSION_COOKIE = "miroir_session"
DEFAULT_MAX_UPLOAD = 512 * 1024 * 1024
CHUNK = 1024 * 1024
ROLES = ("historical", "ongoing")

# Fixed messages for redirects: nothing user-supplied is reflected from the URL.
NOTICES = {
    "sync_requested": "Sync requested.",
    "models_requested": "Analysis rebuild requested.",
    "import_requested": "Import requested.",
    "submitted": "File stored.",
    "duplicate": "This exact file was already stored; nothing new was saved.",
    "already_imported": "Already imported with the current parser; use replay to load it again.",
    "saved": "Configuration saved. It applies to operations requested from now on.",
    "created": "Connection added.",
    "credential_saved": "Credential saved.",
    "checked": "Connection checked; see the credentials status below.",
    "removed": "Connection removed. Its secrets were deleted; loaded data and files were kept.",
    "applied": "Deployments applied: the configuration and Prefect now agree.",
    "delivery_revoked": "Automatic delivery token revoked. File uploads remain available.",
}

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class UploadTooLarge(ValueError):
    pass


class Unauthorized(Exception):
    pass


def create_app(
    service: MiroirService, token: str, max_upload_bytes: int = DEFAULT_MAX_UPLOAD
) -> FastAPI:
    if len(token) < 16:
        raise ValueError("MIROIR_API_TOKEN must be at least 16 characters")
    session = hmac.new(token.encode(), b"miroir-session", hashlib.sha256).hexdigest()
    app = FastAPI(title="Miroir", docs_url=None, redoc_url=None)

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
        # This route authenticates its connection-scoped bearer before reading bytes.
        if request.method == "POST" and re.fullmatch(
            r"/api/connections/[a-z][a-z0-9_]{0,23}/receive", request.url.path
        ):
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
    for kind in (
        ConfigurationError,
        OperationError,
        InvalidArtifact,
        UploadTooLarge,
        SecretResolutionError,
        SecretKeyError,
    ):
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

    @app.post("/api/connections/{connection_id}/models", status_code=202)
    def api_models(connection_id: str) -> dict:
        return {"operation_id": service.request_models(connection_id)}

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

    @app.post("/api/connections/{connection_id}/receive")
    async def receive(request: Request, connection_id: str) -> JSONResponse:
        header = request.headers.get("authorization", "")
        bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not service.verify_delivery_token(connection_id, bearer):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        path = await save_stream(request.stream(), staging)
        try:
            result = service.submit_file(
                connection_id,
                path,
                "ongoing",
                import_now=True,
                original_name="automatic-export.json",
                origin="webhook",
            )
        finally:
            path.unlink(missing_ok=True)
        # Receive-only clients get a receipt, never internal storage paths/history.
        receipt = {"artifact_id": result["artifact"]["id"], "new": result["new"]}
        for field in ("operation_id", "already_imported"):
            if field in result:
                receipt[field] = result[field]
        return JSONResponse(receipt, status_code=201 if result["new"] else 200)

    @app.post("/api/connections/{connection_id}/delivery-token", status_code=201)
    def create_delivery_token(connection_id: str) -> JSONResponse:
        return JSONResponse(
            {"token": service.create_delivery_token(connection_id)},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/connections/{connection_id}/delivery-token", status_code=204)
    def revoke_delivery_token(connection_id: str) -> Response:
        service.revoke_delivery_token(connection_id)
        return Response(status_code=204)

    @app.post(
        "/api/connections/{connection_id}/artifacts/{artifact_id}/import",
        status_code=202,
    )
    def api_import(connection_id: str, artifact_id: str, replay: bool = False) -> dict:
        return {
            "operation_id": service.request_import(connection_id, artifact_id, replay)
        }

    @app.get("/api/integrations")
    def api_integrations() -> list:
        return service.integrations()

    @app.post("/api/connections", status_code=201)
    async def api_create_connection(request: Request) -> dict:
        """Body: {"integration", "id", "settings": {...}, "credentials": {name: value}}."""
        body = await request.json()
        if not isinstance(body, dict):
            raise OperationError("Expected a JSON object")
        connection_id = service.create_connection(
            str(body.get("integration", "")),
            str(body.get("id", "")),
            body.get("settings") or {},
            body.get("credentials") or {},
            delivery_method=body.get("delivery_method"),
        )
        return {"id": connection_id, "check": service.check_connection(connection_id)}

    @app.put("/api/connections/{connection_id}/settings")
    async def api_save_connection_settings(
        request: Request, connection_id: str
    ) -> dict:
        settings = await request.json()
        if not isinstance(settings, dict):
            raise OperationError("Expected a settings object")
        service.update_connection_settings(
            connection_id, settings, request.headers.get("if-match", "").strip('"')
        )
        return {"saved": True, "check": service.check_connection(connection_id)}

    @app.put("/api/connections/{connection_id}/analysis")
    async def api_save_analysis_settings(request: Request, connection_id: str) -> dict:
        settings = await request.json()
        if not isinstance(settings, dict):
            raise OperationError("Expected an analysis settings object")
        service.update_analysis_settings(
            connection_id, settings, request.headers.get("if-match", "").strip('"')
        )
        return {"saved": True}

    @app.post("/api/connections/{connection_id}/prepare")
    def api_prepare_connection(connection_id: str) -> dict:
        service.prepare_connection(connection_id)
        return {"ready": True}

    @app.put("/api/connections/{connection_id}/credentials/{name}", status_code=204)
    async def api_set_credential(
        request: Request, connection_id: str, name: str
    ) -> Response:
        """Body: {"value": "..."}. The value is never returned by any endpoint."""
        body = await request.json()
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, str):
            raise OperationError('Expected {"value": "..."}')
        service.set_credential(connection_id, name, value)
        return Response(status_code=204)

    @app.post("/api/connections/{connection_id}/check")
    def api_check(connection_id: str) -> dict:
        return service.check_connection(connection_id)

    @app.delete("/api/connections/{connection_id}", status_code=204)
    def api_remove(connection_id: str) -> Response:
        service.remove_connection(connection_id)
        return Response(status_code=204)

    @app.post("/api/refresh")
    def api_refresh() -> list:
        return service.refresh()

    @app.get("/api/deployments/plan")
    def api_deployments_plan() -> list:
        return service.deployments_plan()

    @app.post("/api/deployments/apply")
    def api_deployments_apply() -> list:
        return service.deployments_apply()

    @app.post("/deployments/apply")
    def apply_deployments() -> Response:
        service.deployments_apply()
        return RedirectResponse("/?notice=applied", status_code=303)

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
        miroir = service.save_configuration(text, expected)
        return {"saved": True, "revision": miroir.revision}

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

    from src.web.connections import connection_pages

    app.include_router(connection_pages(service, page))

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
            receive_path=f"/api/connections/{connection_id}/receive",
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

    @app.post("/connections/{connection_id}/models")
    def rebuild_models(connection_id: str) -> Response:
        service.request_models(connection_id)
        return back(connection_id, "models_requested")

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
        status["requested_not_started"]
        or (
            last
            and (
                last["ingestion"] == "running"
                or last["transformation"] in ("pending", "running")
            )
        )
    )
