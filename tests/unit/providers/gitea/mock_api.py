"""A ``GiteaAPI`` over ``httpx.MockTransport``, for the client/PR tests."""

from __future__ import annotations

from typing import Any

import httpx

from issue_agent.providers.gitea.api import GiteaAPI
from issue_agent.providers.gitea.settings import GiteaProviderSettings

TOKEN = "gitea-token-value"
HOST = "https://git.example.com/sub"
# The path prefix every API request carries (host subpath + /api/v1).
P = "/sub/api/v1"


def settings(**kw: Any) -> GiteaProviderSettings:
    return GiteaProviderSettings(type="gitea", host=HOST, token=TOKEN, **kw)


class Recorder:
    """A MockTransport handler that answers from ``routes`` and records every
    request. A route value is a response, a JSON body (200), or a list of
    responses consumed one per call (the last one repeats)."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"message": "not routed"})
        if isinstance(answer, list) and answer and isinstance(answer[0], httpx.Response):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if (r.method, r.url.path) == (method, path)]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_api(rec: Recorder, clock: Clock | None = None) -> GiteaAPI:
    return GiteaAPI(settings(), transport=httpx.MockTransport(rec), clock=clock or Clock())
