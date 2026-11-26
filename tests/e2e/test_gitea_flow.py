"""End to end against a real Gitea (PRD-Gitea GT4) — opt-in.

A Gitea container delivers real webhooks to the service running in this
process (the generic ``/webhooks/{instance_id}`` router behind uvicorn); the
worker's ``run_task`` drives the agent with a fake model; the harness pushes
the branch, opens the ``WIP:`` Pull Request with ``Closes #N`` and comments
on the issue, all through the Gitea plugin and a minimal-scope bot token. A
follow-up comment then reuses the same branch and Pull Request.

    E2E_GITEA=1 uv run pytest tests/e2e/test_gitea_flow.py -v

Needs Docker and git. ``E2E_GITEA_IMAGE`` picks the image (default
``gitea/gitea:28.4.0``); ``E2E_GITEA_KEEP=1`` leaves the container up
(``http://localhost:3310``, ``root`` / the shared test password).
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import threading
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response
from pydantic_settings import SettingsConfigDict

from issue_agent.config import Settings
from issue_agent.providers.registry import ProviderRegistry
from issue_agent.queue.models import TaskStatus
from issue_agent.queue.store import SQLiteStore
from issue_agent.runner.executor import MiniSWERunner
from issue_agent.webhook.routes import router as webhook_router
from issue_agent.worker import run_task
from tests.helpers.fake_model import FakeModel, _make_bash_response
from tests.helpers.gitea_server import GiteaServer, ok

if TYPE_CHECKING:
    from collections.abc import Iterator

    import httpx

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("E2E_GITEA") != "1", reason="opt-in: set E2E_GITEA=1 (needs Docker)"
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker"),
]

IMAGE = os.environ.get("E2E_GITEA_IMAGE", "gitea/gitea:28.4.0")
INSTANCE = "gitea_e2e"
ORG, REPO = "team", "team/app"
BOT, MAINTAINER = "issue-agent", "maintainer"
SECRET = "e2e-webhook-secret"  # throwaway container only
MINIMAL_SCOPES = ["write:issue", "write:repository", "read:organization"]
README = "# App\n\nWelcome to Statewa.\n"
SUBMIT = "printf 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\\ndone\\n'"


class SettingsNoEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, env_prefix="", extra="ignore", yaml_file=None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# --- Gitea -------------------------------------------------------------------


@pytest.fixture(scope="module")
def gitea() -> Iterator[GiteaServer]:
    server = GiteaServer(name="fly-swe-gitea-e2e", port=3310)
    server.start(IMAGE)
    try:
        for user in (BOT, MAINTAINER):
            server.create_user(user)
        with server.root() as root:
            ok(root.post("/orgs", json={"username": ORG, "visibility": "public"}))
            ok(root.post(f"/orgs/{ORG}/repos", json={"name": "app", "default_branch": "main"}))
            ok(root.post(f"/repos/{REPO}/contents/README.md", json={
                "content": base64.b64encode(README.encode()).decode(),
                "message": "init", "branch": "main",
            }))  # fmt: skip
            for user in (BOT, MAINTAINER):
                ok(root.put(f"/repos/{REPO}/collaborators/{user}", json={"permission": "write"}))
            for name in ("docs", "wontfix"):
                ok(root.post(f"/repos/{REPO}/labels", json={"name": name, "color": "#0e8a16"}))
            ok(
                root.post(
                    f"/orgs/{ORG}/labels", json={"name": "agent-generated", "color": "#5319e7"}
                )
            )
        yield server
    finally:
        if os.environ.get("E2E_GITEA_KEEP") != "1":
            server.stop()


# --- The service -------------------------------------------------------------


class Service:
    """The webhook app (uvicorn thread) and the worker's view of the queue."""

    def __init__(self, settings: Settings, db: str) -> None:
        self.settings = settings
        self.registry = ProviderRegistry.from_settings(settings)
        self.store = SQLiteStore(db)  # the worker's connection
        self.responses: list[tuple[str, int, str]] = []
        self.port = 0

    def run_next(self, *commands: str, timeout: float = 60) -> Any:
        """Wait for the next queued task and run it with a fake model that
        runs ``commands`` and then submits."""
        deadline = time.monotonic() + timeout
        while (task := self.store.fetch_next()) is None:
            if time.monotonic() > deadline:
                pytest.fail(f"no task queued; webhook responses: {self.responses}")
            time.sleep(0.5)
        fake = FakeModel([*(_make_bash_response(c) for c in commands), _make_bash_response(SUBMIT)])
        runner = MiniSWERunner(model_override=fake)
        with patch("issue_agent.runner.executor._resolve_agent_runner", return_value=runner):
            run_task(self.store, task, self.settings, self.registry)
        return self.store.get_task(task.id)

    def quiet(self, seconds: float = 6) -> list[tuple[str, int, str]]:
        """Let Gitea's queued deliveries land; return the ones received
        meanwhile. Fails if any of them queued a task."""
        start = len(self.responses)
        time.sleep(seconds)
        assert self.store.get_queue_depth() == 0, self.responses[start:]
        return self.responses[start:]


@pytest.fixture(scope="module")
def service(gitea: GiteaServer, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    data = tmp_path_factory.mktemp("gitea-e2e")
    token = gitea.make_token(BOT, "e2e", MINIMAL_SCOPES)
    settings = SettingsNoEnv(
        PROVIDERS={
            INSTANCE: {
                "type": "gitea",
                "host": gitea.url,
                "bot_login": BOT,
                "token": token,
                "webhook_secret": SECRET,
            }
        },
        # draft: the core prefixes the title with capabilities.draft_prefix.
        REPO_ROUTES=[{"issues": REPO, "provider": INSTANCE, "target": REPO, "draft": True}],
        LLM_API_KEY="unused-fake-model",
        DATA_DIR=str(data),
        TOOLCHAINS_ENABLED=False,
    )
    db = str(data / "issue_agent.db")
    web_store = SQLiteStore(db)
    web_store.init()
    service = Service(settings, db)

    app = FastAPI()
    app.state.store = web_store
    app.state.registry = ProviderRegistry.from_settings(settings)
    app.include_router(webhook_router)

    @app.middleware("http")
    async def record(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        event = request.headers.get("x-gitea-event-type", "?")
        service.responses.append((event, response.status_code, body.decode()))
        return Response(body, response.status_code, dict(response.headers))

    port = service.port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    with patch("issue_agent.webhook.routes.get_settings", return_value=settings):
        thread.start()
        while not server.started:
            time.sleep(0.1)
        yield service
        server.should_exit = True
        thread.join(timeout=10)
    service.store.close()
    web_store.close()


# --- Helpers -----------------------------------------------------------------


def _as(gitea: GiteaServer, user: str) -> httpx.Client:
    return gitea.api(user=user)


def _readme(gitea: GiteaServer, ref: str) -> str:
    with gitea.root() as root:
        raw = ok(root.get(f"/repos/{REPO}/contents/README.md", params={"ref": ref}))
    return base64.b64decode(raw["content"]).decode()


def _open_prs(gitea: GiteaServer) -> list[dict[str, Any]]:
    with gitea.root() as root:
        return ok(root.get(f"/repos/{REPO}/pulls", params={"state": "open"}))


def _comments(gitea: GiteaServer, number: int) -> list[dict[str, Any]]:
    with gitea.root() as root:
        return ok(root.get(f"/repos/{REPO}/issues/{number}/comments"))


def _bot_comments(gitea: GiteaServer, number: int) -> list[dict[str, Any]]:
    return [c for c in _comments(gitea, number) if c["user"]["login"] == BOT]


# --- The flow ----------------------------------------------------------------


def test_label_to_pr_then_follow_up(gitea: GiteaServer, service: Service) -> None:
    # The issue exists before the hook: an unlabeled `opened` would only be
    # skipped by policy (with a comment), which isn't what this flow tests.
    with _as(gitea, MAINTAINER) as maint:
        issue = ok(maint.post(f"/repos/{REPO}/issues", json={
            "title": "Typo in the README", "body": "It says 'Statewa'; should be 'Stateway'.",
        }))  # fmt: skip
    number = issue["number"]
    gitea.add_hook(
        REPO,
        url=f"http://host.docker.internal:{service.port}/webhooks/{INSTANCE}",
        secret=SECRET,
        # The runbook's three toggles; Pull Request Comment stays off.
        events=["issues", "issue_label", "issue_comment"],
    )

    # 1. A maintainer labels it `docs` → one task → the agent fixes the typo.
    with gitea.root() as root:
        docs = next(lb for lb in ok(root.get(f"/repos/{REPO}/labels")) if lb["name"] == "docs")
    with _as(gitea, MAINTAINER) as maint:
        ok(maint.post(f"/repos/{REPO}/issues/{number}/labels", json={"labels": [docs["id"]]}))
    task = service.run_next("sed -i 's/Statewa\\b/Stateway/' README.md")

    assert task.status is TaskStatus.SUCCEEDED, (task.status, task.error)
    prs = _open_prs(gitea)
    assert len(prs) == 1, prs
    pr = prs[0]
    branch = pr["head"]["ref"]
    assert branch.startswith(f"agent/issue-{number}-")
    assert pr["base"]["ref"] == "main"
    assert pr["title"].startswith("WIP: ")
    assert not pr["title"].startswith("WIP: WIP")
    assert f"Closes #{number}" in pr["body"]
    assert "agent-generated" in {lb["name"] for lb in pr["labels"]}
    assert "Stateway" in _readme(gitea, branch)
    assert "Statewa." not in _readme(gitea, branch)
    assert any(pr["html_url"] in c["body"] or f"#{pr['number']}" in c["body"]
               for c in _bot_comments(gitea, number))  # fmt: skip

    # 2. The bot's own comments and label change must not start another run.
    service.quiet()

    # 3. A follow-up comment from the issue author reuses the branch and PR.
    head_before = pr["head"]["sha"]
    with _as(gitea, MAINTAINER) as maint:
        ok(maint.post(f"/repos/{REPO}/issues/{number}/comments", json={
            "body": "Thanks! Please also add a line saying 'Have fun.'",
        }))  # fmt: skip
    task = service.run_next("printf 'Have fun.\\n' >> README.md")

    assert task.status is TaskStatus.SUCCEEDED, (task.status, task.error)
    prs = _open_prs(gitea)
    assert [p["number"] for p in prs] == [pr["number"]]
    assert prs[0]["head"]["ref"] == branch
    assert prs[0]["head"]["sha"] != head_before
    assert "Have fun." in _readme(gitea, branch)
    assert _bot_comments(gitea, pr["number"]), "the follow-up is reported on the PR"

    service.quiet()
