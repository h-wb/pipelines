"""Analyses (decision 022): packages with explicitly linked connections.

The catalog describes packages and their input roles, the service validates and
persists analyses, and these handlers translate forms and choose the next page.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.core.config import ConfigurationConflict, ConfigurationError
from src.core.operations import OperationError
from src.core.service import MiroirService

Page = Callable[..., HTMLResponse]


def _inputs(form: Any) -> dict[str, str]:
    return {
        key.removeprefix("input."): str(value)
        for key, value in form.items()
        if key.startswith("input.")
    }


def _parameters(form: Any) -> dict[str, Any]:
    """Text parameters; list parameters are entered one item per line."""
    values: dict[str, Any] = {}
    for key, value in form.items():
        if key.startswith("parameter."):
            name = key.removeprefix("parameter.")
            values[name] = str(value)
        elif key.startswith("parameter_list."):
            name = key.removeprefix("parameter_list.")
            values[name] = [
                line.strip() for line in str(value).splitlines() if line.strip()
            ]
    return values


def analysis_pages(service: MiroirService, page: Page) -> APIRouter:
    router = APIRouter()

    def failed(response: HTMLResponse, error: Exception) -> HTMLResponse:
        response.status_code = 409 if isinstance(error, ConfigurationConflict) else 400
        return response

    def new_page(
        request: Request,
        package: str = "",
        values: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        packages = service.packages()
        selected = next((p for p in packages if p["id"] == package), None)
        if package and selected is None:
            raise OperationError("Unknown package")
        overview = service.overview()
        _, revision = service._raw_config()
        return page(
            request,
            "new_analysis.html",
            packages=packages,
            package=selected,
            connections=overview["connections"],
            values=values or {},
            file_revision=revision,
            error=error,
        )

    @router.get("/analyses/new", response_class=HTMLResponse)
    def new_analysis(request: Request, package: str = "") -> HTMLResponse:
        return new_page(request, package)

    @router.post("/analyses/new")
    async def create_analysis(request: Request) -> Response:
        form = await request.form()
        package = str(form.get("package", ""))
        analysis_id = str(form.get("id", ""))
        timezone = str(form.get("timezone", "") or "UTC")
        inputs = _inputs(form)
        try:
            service.create_analysis(
                analysis_id,
                package,
                inputs,
                str(form.get("file_revision", "")),
                timezone=timezone,
            )
        except (ConfigurationError, OperationError) as error:
            values = {"id": analysis_id, "timezone": timezone, "inputs": inputs}
            return failed(new_page(request, package, values, str(error)), error)
        return RedirectResponse(
            f"/analyses/{analysis_id}?notice=analysis_created", status_code=303
        )

    def detail_page(
        request: Request, analysis_id: str, error: str | None = None
    ) -> HTMLResponse:
        view = service.analysis(analysis_id)
        return page(
            request,
            "analysis.html",
            a=view,
            error=error,
            refresh=view["status"]["state"] == "running",
        )

    @router.get("/analyses/{analysis_id}", response_class=HTMLResponse)
    def analysis(request: Request, analysis_id: str) -> HTMLResponse:
        return detail_page(request, analysis_id)

    @router.post("/analyses/{analysis_id}/settings")
    async def save(request: Request, analysis_id: str) -> Response:
        form = await request.form()
        values: dict[str, Any] = {
            "timezone": str(form.get("timezone", "")),
            "inputs": _inputs(form),
        }
        parameters = _parameters(form)
        if parameters:
            values["parameters"] = parameters
        try:
            service.update_analysis(
                analysis_id, values, str(form.get("file_revision", ""))
            )
        except (ConfigurationError, OperationError) as error:
            return failed(detail_page(request, analysis_id, str(error)), error)
        return RedirectResponse(
            f"/analyses/{analysis_id}?notice=saved", status_code=303
        )

    @router.post("/analyses/{analysis_id}/models")
    def rebuild(analysis_id: str) -> Response:
        service.request_models(analysis_id)
        return RedirectResponse(
            f"/analyses/{analysis_id}?notice=models_requested", status_code=303
        )

    @router.post("/analyses/{analysis_id}/remove")
    async def remove(request: Request, analysis_id: str) -> Response:
        form = await request.form()
        if str(form.get("confirm", "")) != analysis_id:
            raise OperationError("Type the analysis ID to confirm removal")
        service.remove_analysis(analysis_id, str(form.get("file_revision", "")))
        return RedirectResponse("/?notice=analysis_removed", status_code=303)

    return router
