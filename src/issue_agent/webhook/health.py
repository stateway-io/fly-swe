from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from issue_agent.config import Settings, get_settings
from issue_agent.webhook.function_calling import PENDING

if TYPE_CHECKING:
    from issue_agent.providers.registry import ProviderRegistry
    from issue_agent.queue.store import SQLiteStore

router = APIRouter()


def _resolve_version() -> str:
    try:
        return _pkg_version("fly-swe")
    except PackageNotFoundError:
        return "unknown"


# Resolved once at import time, not per request — /healthz and /readyz are
# polled every 30s by Fly's health check (fly.toml), and the installed
# version never changes without a restart.
_VERSION = _resolve_version()


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _get_store(request: Request) -> SQLiteStore:
    return request.app.state.store


def _get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


def _missing_settings(settings: Settings) -> list[str]:
    """The provider-independent settings; each provider reports its own."""
    missing = []
    if not settings.repo_routes:
        missing.append("REPO_ROUTES (config.yaml: repos)")
    if not settings.llm_api_key:
        missing.append("LLM_API_KEY")
    return missing


def _provider_checks(registry: ProviderRegistry) -> tuple[bool, list[str], dict[str, str]]:
    """Every provider instance's ``health_check()``, flattened into readyz.

    A provider's ``config`` check folds into ``settings`` (its
    ``missing: ...`` list joins the core one); every other check is keyed
    ``<instance_id>_<name>`` — e.g. ``github_auth``.
    """
    ok = True
    missing: list[str] = []
    checks: dict[str, str] = {}
    for plugin in registry.all():
        health = plugin.health_check()
        ok = ok and health.ok
        for name, result in health.checks.items():
            if name == "config":
                if result != "ok":
                    missing.extend(result.removeprefix("missing: ").split(", "))
            else:
                checks[f"{plugin.instance_id}_{name}"] = result
    return ok, missing, checks


def _function_calling_status(request: Request) -> str:
    """Informational only — never flips readiness (LiteLLM's capability map has
    false negatives for models it doesn't know). The check itself runs in the
    background from the app's lifespan (``webhook/function_calling.py``):
    ``pending`` until it finishes, or when the app was started without it."""
    check = getattr(request.app.state, "function_calling", None)
    return check.result if check is not None else PENDING


@router.get("/healthz")
async def healthz() -> JSONResponse:
    # /healthz is a liveness probe — it must always answer 200 even if
    # settings can't be fully validated (that's what /readyz is for), so a
    # missing/invalid config falls back to a generic service name instead of
    # raising.
    try:
        service_name = _slugify(get_settings().agent_name)
    except Exception:
        service_name = "issue-agent"
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": service_name,
            "version": _VERSION,
        },
    )


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    settings = get_settings()
    store = _get_store(request)

    sqlite_ok, sqlite_msg = True, "ok"
    try:
        store.conn.execute("SELECT 1").fetchone()
    except Exception as e:
        sqlite_ok, sqlite_msg = False, str(e)

    providers_ok, provider_missing, provider_checks = _provider_checks(_get_registry(request))
    missing = provider_missing + _missing_settings(settings)

    checks = {
        "sqlite": sqlite_msg,
        "settings": f"missing: {', '.join(missing)}" if missing else "ok",
        **provider_checks,
        "llm_function_calling": _function_calling_status(request),
    }

    all_ok = sqlite_ok and not missing and providers_ok

    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "not_ready",
            "version": _VERSION,
            "checks": checks,
        },
    )
