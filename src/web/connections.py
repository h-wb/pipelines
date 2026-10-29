"""Connection onboarding and settings; activity and ingestion routes live separately.

The registry describes methods/fields, the service validates and persists them,
and these handlers translate forms and choose the next page. No source-specific
conditionals or deployment definitions belong here.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.core.config import ConfigurationConflict, ConfigurationError
from src.core.operations import OperationError
from src.core.secrets import SecretResolutionError
from src.core.service import MiroirService

Page = Callable[..., HTMLResponse]


def connection_pages(service: MiroirService, page: Page) -> APIRouter:
    router = APIRouter()

    def back(connection_id: str, notice: str) -> RedirectResponse:
        return RedirectResponse(
            f"/connections/{connection_id}/settings?notice={notice}", status_code=303
        )

    def creation_page(
        request: Request,
        integration: str,
        method: str = "",
        values: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        integrations = service.integrations()
        definition = next((i for i in integrations if i["name"] == integration), None)
        methods = definition["delivery_methods"] if definition else []
        if len(methods) == 1 and not method:
            method = methods[0]["id"]
        selected_method = next((m for m in methods if m["id"] == method), None)
        return page(
            request,
            "new_connection.html",
            integrations=integrations,
            integration=definition,
            selected_method=selected_method,
            values=values or {},
            error=error,
        )

    @router.get("/connections/new", response_class=HTMLResponse)
    def new_connection(
        request: Request, integration: str = "", method: str = ""
    ) -> HTMLResponse:
        if integration:
            if method:
                service.setup_method(integration, method)
            elif not any(i["name"] == integration for i in service.integrations()):
                raise OperationError("Unknown integration")
        return creation_page(request, integration, method)

    @router.post("/connections/new")
    async def create_connection(request: Request) -> Response:
        form = await request.form()
        integration = str(form.get("integration", ""))
        connection_id = str(form.get("id", ""))
        method = str(form.get("method", ""))
        settings = {
            key.removeprefix("setting."): str(value)
            for key, value in form.items()
            if key.startswith("setting.") and str(value) != ""
        }
        credentials = {
            key.removeprefix("credential."): str(value)
            for key, value in form.items()
            if key.startswith("credential.")
        }
        folder_path = str(form.get("folder.path", "")).strip() or None
        try:
            selected = service.setup_method(integration, method)
            service.create_connection(
                integration,
                connection_id,
                settings,
                credentials,
                delivery_method=selected["id"],
                folder_path=folder_path,
            )
        except (ConfigurationError, OperationError, SecretResolutionError) as error:
            response = creation_page(
                request,
                integration,
                method,
                {"id": connection_id, "folder.path": folder_path, **settings},
                str(error),
            )
            response.status_code = 400
            return response
        service.check_connection(connection_id)
        return RedirectResponse(
            f"/connections/{connection_id}/setup?method={selected['id']}",
            status_code=303,
        )

    def setup_page(
        request: Request,
        connection_id: str,
        method: str,
        error: str | None = None,
        delivery_token: str | None = None,
    ) -> HTMLResponse:
        view = service.connection_settings(connection_id)
        selected = service.setup_method(view["integration"]["name"], method)
        operations = [
            operation
            for operation in view["connection"]["operations"]
            if selected["id"] != "webhook" or operation["origin"] == "webhook"
        ]
        latest = operations[0] if operations else None
        attempt = latest["attempts"][-1] if latest and latest["attempts"] else None
        response = page(
            request,
            "connection_setup.html",
            c=view["connection"],
            integration=view["integration"],
            method=selected,
            error=error,
            delivery_token=delivery_token,
            setup_operation=latest,
            setup_attempt=attempt,
            receive_path=f"/api/connections/{connection_id}/receive",
            # One-time secrets must never vanish because a page refreshes itself.
            refresh=not delivery_token
            and (
                bool(attempt and attempt["ingestion"] == "running")
                or bool(latest and not attempt)
                or (
                    selected["id"] == "webhook"
                    and view["connection"]["delivery"]["enabled"]
                    and not latest
                )
            ),
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/connections/{connection_id}/setup", response_class=HTMLResponse)
    def setup(request: Request, connection_id: str, method: str = "") -> HTMLResponse:
        return setup_page(request, connection_id, method)

    @router.post("/connections/{connection_id}/setup")
    def start_setup(
        request: Request,
        connection_id: str,
        method: str = Form(),
    ) -> Response:
        view = service.connection_settings(connection_id)
        selected = service.setup_method(view["integration"]["name"], method)
        try:
            if selected["id"] == "webhook":
                if not view["connection"]["delivery"]["enabled"]:
                    token = service.create_delivery_token(connection_id)
                    return setup_page(
                        request, connection_id, method, delivery_token=token
                    )
                service.prepare_connection(connection_id)
                return RedirectResponse(
                    f"/connections/{connection_id}/setup?method=webhook",
                    status_code=303,
                )
            service.request_sync(connection_id)
            return RedirectResponse(
                f"/connections/{connection_id}?notice=sync_requested", status_code=303
            )
        except OperationError as error:
            response = setup_page(request, connection_id, method, error=str(error))
            response.status_code = 400
            return response

    @router.get("/connections/{connection_id}/settings", response_class=HTMLResponse)
    def settings(request: Request, connection_id: str) -> HTMLResponse:
        return page(
            request,
            "connection_settings.html",
            **service.connection_settings(connection_id),
            error=None,
        )

    @router.post("/connections/{connection_id}/settings")
    async def save_settings(request: Request, connection_id: str) -> Response:
        form = await request.form()
        values = {
            key.removeprefix("setting."): str(value) or None
            for key, value in form.items()
            if key.startswith("setting.")
        }
        revision = str(form.get("file_revision", ""))
        try:
            service.update_connection_settings(connection_id, values, revision)
        except (ConfigurationError, OperationError) as error:
            view = service.connection_settings(connection_id)
            view["values"].update(values)
            view["file_revision"] = revision
            response = page(
                request, "connection_settings.html", **view, error=str(error)
            )
            response.status_code = (
                409 if isinstance(error, ConfigurationConflict) else 400
            )
            return response
        service.check_connection(connection_id)
        return back(connection_id, "saved")

    @router.post("/connections/{connection_id}/folder")
    async def save_folder(request: Request, connection_id: str) -> Response:
        form = await request.form()
        revision = str(form.get("file_revision", ""))
        path = str(form.get("folder.path", "")).strip() or None
        try:
            service.update_folder(connection_id, path, revision)
        except (ConfigurationError, OperationError) as error:
            view = service.connection_settings(connection_id)
            view["folder_values"] = {"path": path}
            view["file_revision"] = revision
            response = page(
                request, "connection_settings.html", **view, error=str(error)
            )
            response.status_code = (
                409 if isinstance(error, ConfigurationConflict) else 400
            )
            return response
        service.check_connection(connection_id)
        return back(connection_id, "saved")

    @router.post("/connections/{connection_id}/analysis")
    async def save_analysis(request: Request, connection_id: str) -> Response:
        form = await request.form()
        revision = str(form.get("file_revision", ""))
        values = {"timezone": str(form.get("timezone", ""))}
        try:
            service.update_analysis_settings(connection_id, values, revision)
        except (ConfigurationError, OperationError) as error:
            view = service.connection_settings(connection_id)
            view["analysis_values"].update(values)
            view["file_revision"] = revision
            response = page(
                request, "connection_settings.html", **view, error=str(error)
            )
            response.status_code = (
                409 if isinstance(error, ConfigurationConflict) else 400
            )
            return response
        return back(connection_id, "saved")

    @router.post("/connections/{connection_id}/credentials/{name}")
    def set_credential(connection_id: str, name: str, value: str = Form()) -> Response:
        service.set_credential(connection_id, name, value)
        service.check_connection(connection_id)
        return back(connection_id, "credential_saved")

    @router.post("/connections/{connection_id}/check")
    def check(connection_id: str) -> Response:
        service.check_connection(connection_id)
        return back(connection_id, "checked")

    @router.post("/connections/{connection_id}/remove")
    def remove(connection_id: str, confirm: str = Form("")) -> Response:
        if confirm != connection_id:
            raise OperationError("Type the connection ID to confirm removal")
        service.remove_connection(connection_id)
        return RedirectResponse("/?notice=removed", status_code=303)

    @router.post("/connections/{connection_id}/delivery-token")
    def delivery_token_page(request: Request, connection_id: str) -> Response:
        try:
            token = service.create_delivery_token(connection_id)
            return setup_page(request, connection_id, "webhook", delivery_token=token)
        except OperationError as error:
            response = setup_page(request, connection_id, "webhook", error=str(error))
            response.status_code = 400
            return response

    @router.post("/connections/{connection_id}/delivery-token/revoke")
    def revoke_delivery_token_form(connection_id: str) -> Response:
        service.revoke_delivery_token(connection_id)
        return back(connection_id, "delivery_revoked")

    return router
