"""Capture Gitea webhook fixtures and measure the PRD's (verify) items.

Not a test: a reproducible tool. It starts a throwaway Gitea container,
seeds it through the API (users, org, repo, labels, scoped tokens, webhook),
performs each action the fixtures need, records the raw deliveries on a
local receiver, and answers PRD-Gitea §10 Q1–Q5. Everything it creates is
synthetic (``example.com`` addresses, ``git.example.com`` as ROOT_URL), so
the payloads need no scrubbing.

    uv run python tests/unit/providers/gitea/fixtures/capture.py \
        --image gitea/gitea:28.4.0 --out tests/unit/providers/gitea/fixtures

Needs Docker and git. The container is removed at the end (``--keep`` keeps
it for poking around).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

CONTAINER = "fly-swe-gitea-capture"
HTTP_PORT = 3300
RECEIVER_PORT = 3399
ROOT_URL = "https://git.example.com/"
PASSWORD = "Capture-pass-1234"  # throwaway container only
SECRET = "capture-webhook-secret"  # throwaway container only
ORG = "team"
REPO = f"{ORG}/app"
BOT = "issue-agent"
MAINTAINER = "maintainer"
OUTSIDER = "outsider"

FULL_SCOPES = ["write:issue", "write:repository", "read:user", "read:organization"]


# --- Receiver ----------------------------------------------------------------


class _Receiver:
    def __init__(self) -> None:
        self.deliveries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with receiver._lock:
                    receiver.deliveries.append(
                        {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                    )
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("0.0.0.0", RECEIVER_PORT), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def count(self) -> int:
        with self._lock:
            return len(self.deliveries)

    def wait_new(self, since: int, n: int = 1, timeout: float = 30.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.deliveries) >= since + n:
                    return self.deliveries[since:]
            time.sleep(0.2)
        with self._lock:
            got = self.deliveries[since:]
        raise TimeoutError(f"expected {n} deliveries, got {len(got)}: {[_h(d) for d in got]}")

    def close(self) -> None:
        self._server.shutdown()


def _h(delivery: dict[str, Any]) -> tuple[str | None, str | None]:
    headers = {k.lower(): v for k, v in delivery["headers"].items()}
    return headers.get("x-gitea-event"), headers.get("x-gitea-event-type")


# --- Container ---------------------------------------------------------------


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def start_gitea(image: str) -> None:
    _docker("rm", "-f", CONTAINER, check=False)
    gateway = _docker(
        "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"
    ).stdout.strip()
    env = {
        "GITEA__security__INSTALL_LOCK": "true",
        "GITEA__database__DB_TYPE": "sqlite3",
        "GITEA__server__ROOT_URL": ROOT_URL,
        "GITEA__server__HTTP_PORT": "3000",
        "GITEA__service__DISABLE_REGISTRATION": "true",
        # Gitea 28 moved this to [security] and wants an explicit IP for a
        # non-public address; 1.27 reads [webhook]. Set both.
        "GITEA__security__ALLOWED_HOST_LIST": gateway,
        "GITEA__webhook__ALLOWED_HOST_LIST": gateway,
        "GITEA__webhook__DELIVER_TIMEOUT": "10",
        "GITEA__repository__DEFAULT_BRANCH": "main",
        "GITEA__log__LEVEL": "Warn",
        # The web login (for "Replay") runs over plain http on localhost.
        "GITEA__session__COOKIE_SECURE": "false",
    }
    args = ["run", "-d", "--name", CONTAINER, "-p", f"{HTTP_PORT}:3000"]
    args += ["--add-host", "host.docker.internal:host-gateway"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    _docker(*args, image)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://localhost:{HTTP_PORT}/api/healthz", timeout=2).is_success:
                break
        except httpx.HTTPError:
            pass
        time.sleep(1)
    else:
        raise TimeoutError(_docker("logs", CONTAINER, check=False).stderr[-2000:])
    _docker(
        "exec", "-u", "git", CONTAINER, "gitea", "admin", "user", "create", "--admin",
        "--username", "root", "--password", PASSWORD, "--email", "root@example.com",
        "--must-change-password=false",
    )  # fmt: skip


# --- API helpers -------------------------------------------------------------


def api(auth: httpx.Auth | None = None, token: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"token {token}"} if token else {}
    return httpx.Client(
        base_url=f"http://localhost:{HTTP_PORT}/api/v1", auth=auth, headers=headers, timeout=30
    )


def ok(resp: httpx.Response) -> Any:
    if resp.is_error:
        raise RuntimeError(
            f"{resp.request.method} {resp.request.url} -> {resp.status_code} {resp.text}"
        )
    return resp.json() if resp.content else None


def make_token(user: str, name: str, scopes: list[str]) -> str:
    with api(auth=httpx.BasicAuth(user, PASSWORD)) as c:
        return ok(c.post(f"/users/{user}/tokens", json={"name": name, "scopes": scopes}))["sha1"]


def seed() -> dict[str, Any]:
    root = api(auth=httpx.BasicAuth("root", PASSWORD))
    for user in (BOT, MAINTAINER, OUTSIDER):
        ok(root.post("/admin/users", json={
            "username": user, "email": f"{user}@example.com", "password": PASSWORD,
            "must_change_password": False,
        }))  # fmt: skip
    ok(root.post("/orgs", json={"username": ORG, "visibility": "public"}))
    ok(
        root.post(
            f"/orgs/{ORG}/repos", json={"name": "app", "auto_init": True, "default_branch": "main"}
        )
    )
    for user in (BOT, MAINTAINER):
        ok(root.put(f"/repos/{REPO}/collaborators/{user}", json={"permission": "write"}))
    labels = {}
    for name, color in (
        ("bug", "#ee0701"),
        ("agent-fix-please", "#0e8a16"),
        ("wontfix", "#cccccc"),
    ):
        labels[name] = ok(root.post(f"/repos/{REPO}/labels", json={"name": name, "color": color}))[
            "id"
        ]
    org_label = ok(
        root.post(f"/orgs/{ORG}/labels", json={"name": "agent-generated", "color": "#5319e7"})
    )
    labels["agent-generated"] = org_label["id"]
    return {"root": root, "labels": labels}


def add_hook(root: httpx.Client, *, secret: str, path: str) -> int:
    hook = ok(root.post(f"/repos/{REPO}/hooks", json={
        "type": "gitea",
        "active": True,
        "branch_filter": "*",
        "events": ["issues", "issue_label", "issue_comment", "push"],
        "config": {
            "url": f"http://host.docker.internal:{RECEIVER_PORT}{path}",
            "content_type": "json",
            "secret": secret,
        },
    }))  # fmt: skip
    return hook["id"]


# --- Web UI (for "Replay", which has no API) ---------------------------------


def web_replay(hook_id: int, delivery_uuid: str) -> int:
    """Click "Replay" on a hook delivery through the web UI; returns the status.

    1.27 embeds a ``_csrf`` token in the page; 28 dropped it for an
    Origin/Sec-Fetch-Site check, so send both and the token when present."""
    origin = f"http://localhost:{HTTP_PORT}"
    headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    with httpx.Client(base_url=origin, timeout=30, headers=headers) as web:
        form = {"user_name": "root", "password": PASSWORD, **_csrf(web.get("/user/login").text)}
        login = web.post("/user/login", data=form)
        if login.status_code >= 400:
            raise RuntimeError(f"web login failed: {login.status_code}")
        page = web.get(f"/{REPO}/settings/hooks/{hook_id}").text
        resp = web.post(
            f"/{REPO}/settings/hooks/{hook_id}/replay/{delivery_uuid}", data=_csrf(page)
        )
        return resp.status_code


def _csrf(page: str) -> dict[str, str]:
    m = re.search(r'name="_csrf" (?:content|value)="([^"]+)"', page) or re.search(
        r"csrfToken: '([^']+)'", page
    )
    return {"_csrf": m.group(1)} if m else {}


# --- Git ---------------------------------------------------------------------


def git_push(
    token: str, user: str, branch: str, path: str, content: str, *, base: str = "main"
) -> int:
    """Clone, commit one file on ``branch`` (from ``base``) and push with the
    extraHeader technique the transport will use. Returns git's exit code."""
    host = f"http://localhost:{HTTP_PORT}"
    basic = base64.b64encode(f"{user}:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{host}/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": tempfile.gettempdir(),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *a: subprocess.run(a, cwd=tmp, env=env, capture_output=True, text=True)  # noqa: E731
        if run("git", "clone", "-q", "-b", base, f"{host}/{REPO}.git", ".").returncode:
            return 128
        run("git", "checkout", "-q", "-B", branch)
        Path(tmp, path).write_text(content)
        run("git", "add", path)
        run("git", "-c", "user.name=capture", "-c", "user.email=capture@example.com",
            "commit", "-q", "-m", f"capture: {branch}")  # fmt: skip
        return run("git", "push", "-q", "origin", f"HEAD:refs/heads/{branch}").returncode


# --- Capture -----------------------------------------------------------------


def capture(out: Path, image: str, *, fixtures: bool = True) -> dict[str, Any]:
    receiver = _Receiver()
    measurements: dict[str, Any] = {"image": image}
    try:
        start_gitea(image)
        state = seed()
        root, labels = state["root"], state["labels"]
        measurements["version"] = ok(root.get("/version"))["version"]
        hook_id = add_hook(root, secret=SECRET, path="/signed")

        bot_token = make_token(BOT, "full", FULL_SCOPES)
        maint_token = make_token(MAINTAINER, "full", FULL_SCOPES)
        bot, maint = api(token=bot_token), api(token=maint_token)
        saved: dict[str, dict[str, Any]] = {}

        def expect(name: str, action: Any) -> list[dict[str, Any]]:
            since = receiver.count()
            action()
            got = receiver.wait_new(since)
            saved[name] = got[-1]
            return got

        # Issue opened (by the maintainer), then the label changes.
        issue: dict[str, Any] = {}
        expect("issues_opened", lambda: issue.update(ok(maint.post(f"/repos/{REPO}/issues", json={
            "title": "Crash when the config file is empty", "body": "Steps: run with an empty config.yaml.",
        }))))  # fmt: skip
        n = issue["number"]
        expect(
            "issues_label_updated_without_trigger",
            lambda: ok(
                maint.post(f"/repos/{REPO}/issues/{n}/labels", json={"labels": [labels["wontfix"]]})
            ),
        )
        expect(
            "issues_label_updated_with_trigger",
            lambda: ok(
                maint.post(
                    f"/repos/{REPO}/issues/{n}/labels",
                    json={"labels": [labels["agent-fix-please"]]},
                )
            ),
        )
        expect("issues_label_cleared", lambda: maint.delete(f"/repos/{REPO}/issues/{n}/labels"))
        expect(
            "issues_closed",
            lambda: ok(maint.patch(f"/repos/{REPO}/issues/{n}", json={"state": "closed"})),
        )
        expect(
            "issues_reopened",
            lambda: ok(maint.patch(f"/repos/{REPO}/issues/{n}", json={"state": "open"})),
        )

        # Labels by name (Q: does POST .../labels accept names?).
        r = maint.post(f"/repos/{REPO}/issues/{n}/labels", json={"labels": ["bug"]})
        measurements["labels_by_name"] = {"status": r.status_code, "body": r.text[:300]}
        time.sleep(1)  # let its delivery land before the next expect()

        # Comments.
        comment: dict[str, Any] = {}
        expect(
            "issue_comment_created",
            lambda: comment.update(
                ok(
                    maint.post(
                        f"/repos/{REPO}/issues/{n}/comments", json={"body": "/agent fix please"}
                    )
                )
            ),
        )
        expect(
            "issue_comment_own_bot",
            lambda: ok(
                bot.post(f"/repos/{REPO}/issues/{n}/comments", json={"body": "Working on it."})
            ),
        )
        expect(
            "issue_comment_edited",
            lambda: ok(
                maint.patch(
                    f"/repos/{REPO}/issues/comments/{comment['id']}", json={"body": "/agent fix"}
                )
            ),
        )

        # Comment list paging (§3.1: no limit/page?).
        for i in range(3):
            ok(maint.post(f"/repos/{REPO}/issues/{n}/comments", json={"body": f"filler {i}"}))
        full = ok(maint.get(f"/repos/{REPO}/issues/{n}/comments"))
        limited = ok(
            maint.get(f"/repos/{REPO}/issues/{n}/comments", params={"limit": 1, "page": 1})
        )
        measurements["comments_paging"] = {"all": len(full), "limit_1_page_1": len(limited)}
        time.sleep(2)

        # Push (unsupported event) and a PR to comment on.
        expect("push", lambda: git_push(bot_token, BOT, "agent/issue-1-fix", "fix.txt", "fixed\n"))
        pr = ok(bot.post(f"/repos/{REPO}/pulls", json={
            "head": "agent/issue-1-fix", "base": "main", "title": "WIP: Fix crash",
            "body": f"Closes #{n}", "labels": [labels["agent-generated"]],
        }))  # fmt: skip
        measurements["pr_create_labels"] = [lb["name"] for lb in pr.get("labels") or []]
        time.sleep(1)
        # A comment on a PR: does the "Issue Comment" toggle alone deliver it?
        since = receiver.count()
        ok(
            maint.post(
                f"/repos/{REPO}/issues/{pr['number']}/comments", json={"body": "/agent rebase"}
            )
        )
        try:
            got = receiver.wait_new(since, 1, timeout=10)
            measurements["pr_comment_with_issue_comment_toggle"] = [_h(d) for d in got]
        except TimeoutError:
            measurements["pr_comment_with_issue_comment_toggle"] = "no delivery"
        events = ["issues", "issue_label", "issue_comment", "pull_request_comment", "push"]
        ok(root.patch(f"/repos/{REPO}/hooks/{hook_id}", json={"events": events}))
        expect(
            "issue_comment_on_pull_request",
            lambda: ok(
                maint.post(
                    f"/repos/{REPO}/issues/{pr['number']}/comments", json={"body": "/agent rebase"}
                )
            ),
        )

        # Q1: does "Replay" reuse X-Gitea-Delivery?
        original = saved["issues_opened"]
        uuid = {k.lower(): v for k, v in original["headers"].items()}["x-gitea-delivery"]
        since = receiver.count()
        status = web_replay(hook_id, uuid)
        try:
            replayed = receiver.wait_new(since, 1)[-1]
            rh = {k.lower(): v for k, v in replayed["headers"].items()}
            measurements["q1_replay"] = {
                "web_status": status,
                "original_delivery": uuid,
                "replayed_delivery": rh.get("x-gitea-delivery"),
                "same_delivery": rh.get("x-gitea-delivery") == uuid,
                "same_body": replayed["body"] == original["body"],
            }
        except TimeoutError as exc:
            measurements["q1_replay"] = {"web_status": status, "error": str(exc)}

        # Q2: label-change headers/action, recorded from the deliveries.
        measurements["q2_label_events"] = {
            name: {**dict(zip(("event", "event_type"), _h(saved[name]), strict=True)),
                   "action": json.loads(saved[name]["body"]).get("action")}
            for name in ("issues_label_updated_without_trigger", "issues_label_cleared",
                         "issues_opened", "issue_comment_on_pull_request", "push")
        }  # fmt: skip

        measurements["q3_scopes"] = measure_scopes(labels)
        measurements["q4_permission"] = measure_permission(bot_token)
        measurements["q5_mergeable"] = measure_mergeable(bot_token)

        # Signature shape and an unsigned hook.
        sig_headers = {k.lower(): v for k, v in saved["issues_opened"]["headers"].items()}
        measurements["signature_headers"] = {
            k: v for k, v in sig_headers.items() if "signature" in k or k.startswith("x-")
        }
        add_hook(root, secret="", path="/unsigned")
        since = receiver.count()
        ok(maint.post(f"/repos/{REPO}/issues", json={"title": "unsigned probe", "body": ""}))
        got = receiver.wait_new(since, 2)
        unsigned = next(d for d in got if d["path"] == "/unsigned")
        uh = {k.lower(): v for k, v in unsigned["headers"].items()}
        measurements["unsigned_hook_signature"] = {
            "x-gitea-signature": uh.get("x-gitea-signature"),
            "x-hub-signature-256": uh.get("x-hub-signature-256"),
        }

        write(out, saved, measurements, fixtures=fixtures)
        return measurements
    finally:
        receiver.close()


def measure_scopes(labels: dict[str, int]) -> dict[str, dict[str, int]]:
    """Status of each operation the plugin needs, per single-scope token."""
    root = api(auth=httpx.BasicAuth("root", PASSWORD))
    ok(root.post(f"/repos/{REPO}/issues", json={"title": "scope probe", "body": ""}))
    issue = ok(root.get(f"/repos/{REPO}/issues", params={"state": "open", "type": "issues"}))[0][
        "number"
    ]
    candidates = [*FULL_SCOPES, "read:issue", "read:repository"]
    results: dict[str, dict[str, int]] = {}
    for i, scope in enumerate(["<all>", *candidates]):
        scopes = FULL_SCOPES if scope == "<all>" else [scope]
        token = make_token(BOT, f"probe-{i}", scopes)
        c = api(token=token)
        ops = {
            "GET repo": lambda c=c: c.get(f"/repos/{REPO}"),
            "GET issue": lambda c=c: c.get(f"/repos/{REPO}/issues/{issue}"),
            "GET comments": lambda c=c: c.get(f"/repos/{REPO}/issues/{issue}/comments"),
            "POST comment": lambda c=c: c.post(f"/repos/{REPO}/issues/{issue}/comments", json={"body": "probe"}),
            "GET repo labels": lambda c=c: c.get(f"/repos/{REPO}/labels"),
            "GET org labels": lambda c=c: c.get(f"/orgs/{ORG}/labels"),
            "POST issue labels": lambda c=c: c.post(
                f"/repos/{REPO}/issues/{issue}/labels", json={"labels": [labels["bug"]]}),
            "GET pulls": lambda c=c: c.get(f"/repos/{REPO}/pulls", params={"state": "open"}),
            "GET user": lambda c=c: c.get("/user"),
            "GET permission(self)": lambda c=c: c.get(f"/repos/{REPO}/collaborators/{BOT}/permission"),
        }  # fmt: skip
        results[scope] = {name: op().status_code for name, op in ops.items()}
        results[scope]["git push"] = git_push(token, BOT, f"probe/{i}", "probe.txt", scope)
        branch = f"probe/{i}"
        if results[scope]["git push"] != 0:
            ok(
                root.post(
                    f"/repos/{REPO}/branches",
                    json={"new_branch_name": branch, "old_branch_name": "main"},
                )
            )
            ok(
                root.post(
                    f"/repos/{REPO}/contents/probe-{i}.txt",
                    json={
                        "content": base64.b64encode(b"x").decode(),
                        "branch": branch,
                        "message": "probe",
                    },
                )
            )
        results[scope]["POST pull"] = c.post(f"/repos/{REPO}/pulls", json={
            "head": branch, "base": "main", "title": f"probe {i}"}).status_code  # fmt: skip
    return results


def measure_permission(bot_token: str) -> dict[str, int | str]:
    """Q4: can a write (non-admin) collaborator read someone else's permission?"""
    bot = api(token=bot_token)
    out: dict[str, int | str] = {}
    for target in (MAINTAINER, OUTSIDER, BOT):
        r = bot.get(f"/repos/{REPO}/collaborators/{target}/permission")
        out[f"write bot -> {target}"] = r.status_code
        if r.is_success:
            out[f"write bot -> {target} permission"] = r.json().get("permission")
    root = api(auth=httpx.BasicAuth("root", PASSWORD))
    ok(root.put(f"/repos/{REPO}/collaborators/{BOT}", json={"permission": "admin"}))
    for target in (MAINTAINER, OUTSIDER):
        r = bot.get(f"/repos/{REPO}/collaborators/{target}/permission")
        out[f"admin bot -> {target}"] = r.status_code
        if r.is_success:
            out[f"admin bot -> {target} permission"] = r.json().get("permission")
    ok(root.put(f"/repos/{REPO}/collaborators/{BOT}", json={"permission": "write"}))
    return out


def measure_mergeable(bot_token: str) -> dict[str, list[bool | None]]:
    """Q5: ``mergeable`` polled every 0.1 s right after a PR is opened (clean
    and conflicting head) and right after a conflicting push."""
    bot = api(token=bot_token)

    def poll(number: int, first: bool | None = None) -> list[bool | None]:
        samples = [] if first is None else [first]
        for _ in range(20):
            samples.append(ok(bot.get(f"/repos/{REPO}/pulls/{number}")).get("mergeable"))
            time.sleep(0.1)
        return samples

    git_push(bot_token, BOT, "q5/clean", "clean.txt", "clean\n")
    git_push(bot_token, BOT, "q5/conflict", "README.md", "conflicting head\n")
    git_push(bot_token, BOT, "main", "README.md", "conflicting base\n")
    out: dict[str, list[bool | None]] = {}
    numbers = {}
    for branch in ("q5/clean", "q5/conflict"):
        body = {"head": branch, "base": "main", "title": branch}
        pr = ok(bot.post(f"/repos/{REPO}/pulls", json=body))
        numbers[branch] = pr["number"]
        out[branch] = poll(pr["number"], pr.get("mergeable"))
    git_push(bot_token, BOT, "q5/clean", "README.md", "late conflict\n", base="q5/clean")
    out["q5/clean after conflicting push"] = poll(numbers["q5/clean"])
    return out


def write(
    out: Path, saved: dict[str, dict[str, Any]], measurements: dict[str, Any], *, fixtures: bool
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    measured = out / "measurements" / f"{measurements['version']}.json"
    measured.parent.mkdir(exist_ok=True)
    measured.write_text(json.dumps(measurements, indent=2, default=str) + "\n")
    if not fixtures:
        return
    (out / "headers").mkdir(exist_ok=True)
    for name, delivery in saved.items():
        (out / f"{name}.json").write_bytes(delivery["body"])
        headers = {k: v for k, v in delivery["headers"].items() if k.lower().startswith("x-")}
        (out / "headers" / f"{name}.json").write_text(json.dumps(headers, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", default="gitea/gitea:28.4.0")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent)
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument(
        "--measure-only", action="store_true", help="write measurements/<version>.json only"
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                capture(args.out, args.image, fixtures=not args.measure_only), indent=2, default=str
            )
        )
    finally:
        if not args.keep:
            _docker("rm", "-f", CONTAINER, check=False)


if __name__ == "__main__":
    main()
