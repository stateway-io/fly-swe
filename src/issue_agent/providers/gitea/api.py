"""HTTP access to one Gitea instance's v1 API (PRD-Gitea §3.1, §4.3).

One ``GiteaAPI`` per provider instance, shared by every ``ForgeClient`` the
plugin hands out: the core asks ``plugin.client(repo)`` for a fresh client
on each use, so the caches the PRD calls for (label name → ID per repo,
commenter permission per ``(repo, login)``) live here, not on the client.

Errors: 401/403 raise ``GiteaAuthError``, 404 ``GiteaNotFoundError``, any
other non-2xx (3xx included — redirects are never followed, so the token
can't be carried to another host) ``GiteaAPIError``. Network failures
propagate as httpx exceptions. The ports only promise "may raise"; the
worker's existing retry policy decides what to do with them. Logs carry the
method, path and status — never a header, so never the token.
"""

from __future__ import annotations

import ssl
import threading
import time
from typing import TYPE_CHECKING, Any

import certifi
import httpx

from issue_agent.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from issue_agent.providers.gitea.settings import GiteaProviderSettings

logger = get_logger(__name__)

PAGE_SIZE = 50  # Gitea's default MAX_RESPONSE_ITEMS
MAX_PAGES = 100
CACHE_TTL_SECONDS = 300.0


class GiteaAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, detail: str = "") -> None:
        self.status = status
        self.method = method
        self.path = path
        message = f"Gitea API {method} {path} -> {status}"
        super().__init__(f"{message}: {detail}" if detail else message)


class GiteaAuthError(GiteaAPIError):
    """401/403: bad, expired or under-scoped token, or missing repo rights."""


class GiteaNotFoundError(GiteaAPIError):
    """404: no such repo/issue/label — or one the token can't see."""


def ssl_context(ca_bundle_path: str) -> ssl.SSLContext | bool:
    """httpx's ``verify``: the default store, plus ``ca_bundle_path`` when set.

    Additive on purpose — a bundle holding just a private root CA must not
    drop the public ones. A missing or unreadable bundle raises here, at
    construction, rather than on the first request.
    """
    if not ca_bundle_path:
        return True
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=ca_bundle_path)
    return context


class _TTLCache:
    _MISS = object()

    def __init__(self, ttl: float, clock: Callable[[], float]) -> None:
        self._ttl = ttl
        self._clock = clock
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None or self._clock() - hit[0] >= self._ttl:
                return self._MISS
            return hit[1]

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)

    def drop(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)


class GiteaAPI:
    def __init__(
        self,
        settings: GiteaProviderSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self._http = httpx.Client(
            base_url=settings.api_url,
            headers={"Authorization": f"token {settings.token}", "Accept": "application/json"},
            timeout=settings.request_timeout_seconds,
            verify=ssl_context(settings.ca_bundle_path),
            follow_redirects=False,
            transport=transport,
        )
        self._labels = _TTLCache(cache_ttl_seconds, clock)
        self._permissions = _TTLCache(cache_ttl_seconds, clock)

    def close(self) -> None:
        self._http.close()

    # --- Requests -----------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return _json(self._request("GET", path, params=params))

    def post(self, path: str, json: Any) -> Any:
        return _json(self._request("POST", path, json=json))

    def patch(self, path: str, json: Any) -> Any:
        return _json(self._request("PATCH", path, json=json))

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Every item of a ``page``/``limit`` list endpoint.

        A short page is not the end: Gitea clamps ``limit`` to the admin's
        ``[api] MAX_RESPONSE_ITEMS``, which may be below ``PAGE_SIZE``. So it
        stops once it holds the ``X-Total-Count`` Gitea sends on list
        endpoints, or, without that header, on an empty page. ``MAX_PAGES``
        bounds a server that ignores ``page``."""
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            response = self._request(
                "GET", path, params={**(params or {}), "page": page, "limit": PAGE_SIZE}
            )
            batch = _json(response)
            if not isinstance(batch, list):
                raise GiteaAPIError(200, "GET", path, "expected a list")
            items.extend(batch)
            total = _total_count(response)
            if not batch or (total is not None and len(items) >= total):
                break
        return items

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        status = response.status_code
        logger.debug("gitea_api_request", method=method, path=path, status=status)
        if 200 <= status < 300:
            return response
        if status in (401, 403):
            raise GiteaAuthError(status, method, path)
        if status == 404:
            raise GiteaNotFoundError(status, method, path)
        raise GiteaAPIError(status, method, path)

    # --- Cached lookups -----------------------------------------------------

    def labels(self, repo: str, *, refresh: bool = False) -> dict[str, int]:
        """Label name → ID for ``repo``: its own labels plus its org's
        (Gitea applies org labels to org repos). A repo label wins a name
        clash. The owner being a user (404) or a token without
        ``read:organization`` (403) means repo labels only."""
        if refresh:
            self._labels.drop(repo)
        cached = self._labels.get(repo)
        if cached is not _TTLCache._MISS:
            return cached
        owner = repo.split("/", 1)[0]
        org: list[Any] = []
        try:
            org = self.paginate(f"/orgs/{owner}/labels")
        except GiteaNotFoundError:
            pass
        except GiteaAuthError as exc:
            logger.warning("gitea_org_labels_forbidden", repo=repo, status=exc.status)
        names: dict[str, int] = {}
        for label in [*org, *self.paginate(f"/repos/{repo}/labels")]:
            if (
                isinstance(label, dict)
                and isinstance(label.get("name"), str)
                and isinstance(label.get("id"), int)
            ):
                names[label["name"]] = label["id"]
        self._labels.put(repo, names)
        return names

    def label_ids(self, repo: str, names: list[str]) -> list[int]:
        """IDs for ``names`` on ``repo``, in order, re-reading the label list
        once if a name is missing (it may have been created since the cache
        was filled). A name that still isn't there is logged and skipped —
        the service never creates labels in someone's repo."""
        known = self.labels(repo)
        if any(name not in known for name in names):
            known = self.labels(repo, refresh=True)
        ids: list[int] = []
        for name in names:
            if name in known:
                ids.append(known[name])
            else:
                logger.warning("gitea_label_unknown", repo=repo, label=name)
        return ids

    def permission(self, repo: str, login: str) -> str | None:
        """``login``'s raw Gitea permission on ``repo`` (``owner``,
        ``admin``, ``write``, ``read``, ``none``), or None when the token may
        not ask (403 — the bot isn't repo admin). Both are cached."""
        key = (repo, login.lower())
        cached = self._permissions.get(key)
        if cached is not _TTLCache._MISS:
            return cached
        try:
            body = self.get(f"/repos/{repo}/collaborators/{login}/permission")
        except GiteaAuthError as exc:
            logger.warning("gitea_permission_forbidden", repo=repo, status=exc.status)
            value = None
        else:
            raw = body.get("permission") if isinstance(body, dict) else None
            value = raw.lower() if isinstance(raw, str) else None
        self._permissions.put(key, value)
        return value


def _total_count(response: httpx.Response) -> int | None:
    raw = response.headers.get("X-Total-Count", "")
    return int(raw) if raw.isdigit() else None


def _json(response: httpx.Response) -> Any:
    return response.json() if response.content else None
