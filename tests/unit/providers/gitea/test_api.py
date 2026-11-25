"""``GiteaAPI``: requests, pagination, error mapping, TLS and caches."""

from __future__ import annotations

import json
import ssl
from typing import TYPE_CHECKING

import certifi
import httpx
import pytest
from structlog.testing import capture_logs

from issue_agent.providers.gitea import api as api_module
from issue_agent.providers.gitea.api import (
    GiteaAPI,
    GiteaAPIError,
    GiteaAuthError,
    GiteaNotFoundError,
    ssl_context,
)
from tests.unit.providers.gitea.mock_api import TOKEN, Clock, P, Recorder, make_api, settings

if TYPE_CHECKING:
    from pathlib import Path

REPO = "team/app"


def _api(rec: Recorder, clock: Clock | None = None) -> GiteaAPI:
    return make_api(rec, clock)


class TestRequests:
    def test_sends_token_auth_to_the_api_base(self):
        rec = Recorder({("GET", f"{P}/repos/{REPO}"): {"default_branch": "main"}})

        assert _api(rec).get(f"/repos/{REPO}") == {"default_branch": "main"}
        request = rec.requests[0]
        assert request.url.host == "git.example.com"
        assert request.headers["Authorization"] == f"token {TOKEN}"

    def test_post_and_patch_send_json(self):
        rec = Recorder(
            {
                ("POST", f"{P}/x"): {"ok": 1},
                ("PATCH", f"{P}/y"): httpx.Response(204),
            }
        )
        api = _api(rec)

        assert api.post("/x", json={"body": "hi"}) == {"ok": 1}
        assert api.patch("/y", json={"a": 1}) is None
        assert json.loads(rec.requests[0].content) == {"body": "hi"}

    @pytest.mark.parametrize(
        ("status", "error"),
        [
            (401, GiteaAuthError),
            (403, GiteaAuthError),
            (404, GiteaNotFoundError),
            (422, GiteaAPIError),
            (500, GiteaAPIError),
            (503, GiteaAPIError),
        ],
    )
    def test_error_mapping(self, status: int, error: type[GiteaAPIError]):
        rec = Recorder({("GET", f"{P}/x"): httpx.Response(status)})

        with pytest.raises(error) as exc:
            _api(rec).get("/x")
        assert exc.value.status == status
        assert exc.value.path == "/x"
        assert TOKEN not in str(exc.value)

    def test_redirect_is_not_followed(self):
        rec = Recorder(
            {("GET", f"{P}/x"): httpx.Response(302, headers={"Location": "https://evil.test/"})}
        )

        with pytest.raises(GiteaAPIError) as exc:
            _api(rec).get("/x")
        assert exc.value.status == 302
        assert len(rec.requests) == 1

    def test_network_error_propagates(self):
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out", request=request)

        api = GiteaAPI(settings(), transport=httpx.MockTransport(boom))

        with pytest.raises(httpx.TimeoutException):
            api.get("/x")

    def test_logs_never_carry_the_token(self):
        rec = Recorder({("GET", f"{P}/x"): httpx.Response(403)})
        api = _api(rec)

        with capture_logs() as logs, pytest.raises(GiteaAuthError):
            api.get("/x")
        assert logs
        assert TOKEN not in repr(logs)
        assert logs[0]["path"] == "/x"
        assert logs[0]["status"] == 403

    def test_close(self):
        api = _api(Recorder({}))
        api.close()

        with pytest.raises(RuntimeError):
            api.get("/x")


class TestPaginate:
    def test_walks_pages_until_a_short_one(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(api_module, "PAGE_SIZE", 2)
        pages = {1: [1, 2], 2: [3, 4], 3: [5]}
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            seen.append(params)
            return httpx.Response(200, json=pages[int(params["page"])])

        api = GiteaAPI(settings(), transport=httpx.MockTransport(handler))

        assert api.paginate("/l", params={"state": "open"}) == [1, 2, 3, 4, 5]
        assert seen == [
            {"state": "open", "page": "1", "limit": "2"},
            {"state": "open", "page": "2", "limit": "2"},
            {"state": "open", "page": "3", "limit": "2"},
        ]

    def test_empty_page_stops(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(api_module, "PAGE_SIZE", 2)
        rec = Recorder(
            {("GET", f"{P}/l"): [httpx.Response(200, json=[1, 2]), httpx.Response(200, json=[])]}
        )

        assert _api(rec).paginate("/l") == [1, 2]
        assert len(rec.requests) == 2

    def test_page_cap_bounds_a_server_ignoring_page(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(api_module, "PAGE_SIZE", 1)
        monkeypatch.setattr(api_module, "MAX_PAGES", 3)
        rec = Recorder({("GET", f"{P}/l"): ["same"]})

        assert _api(rec).paginate("/l") == ["same"] * 3

    def test_non_list_page_is_an_error(self):
        rec = Recorder({("GET", f"{P}/l"): {"message": "nope"}})

        with pytest.raises(GiteaAPIError):
            _api(rec).paginate("/l")


class TestSSLContext:
    def test_empty_path_uses_the_default_store(self):
        assert ssl_context("") is True

    def test_bundle_is_added_to_the_default_store(self, tmp_path: Path):
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(open(certifi.where(), "rb").read())  # noqa: PTH123, SIM115

        context = ssl_context(str(bundle))

        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode is ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.cert_store_stats()["x509_ca"] > 0

    def test_missing_bundle_fails_at_construction(self, tmp_path: Path):
        with pytest.raises(OSError):
            GiteaAPI(settings(ca_bundle_path=str(tmp_path / "missing.pem")))

    def test_garbage_bundle_fails_at_construction(self, tmp_path: Path):
        bundle = tmp_path / "ca.pem"
        bundle.write_text("not a certificate")

        with pytest.raises(ssl.SSLError):
            ssl_context(str(bundle))


ORG = f"{P}/orgs/team/labels"
REPO_LABELS = f"{P}/repos/{REPO}/labels"


class TestLabels:
    def test_merges_org_and_repo_labels_repo_wins(self):
        rec = Recorder(
            {
                ("GET", ORG): [{"id": 1, "name": "agent-generated"}, {"id": 2, "name": "bug"}],
                ("GET", REPO_LABELS): [{"id": 10, "name": "bug"}, {"id": 11, "name": "wontfix"}],
            }
        )

        assert _api(rec).labels(REPO) == {"agent-generated": 1, "bug": 10, "wontfix": 11}

    def test_skips_malformed_entries(self):
        rec = Recorder(
            {
                ("GET", ORG): [],
                ("GET", REPO_LABELS): [
                    {"id": 1, "name": "ok"},
                    {"id": "2", "name": "bad-id"},
                    {"name": "no-id"},
                    "junk",
                ],
            }
        )

        assert _api(rec).labels(REPO) == {"ok": 1}

    def test_user_owned_repo_has_no_org_labels(self):
        rec = Recorder({("GET", REPO_LABELS): [{"id": 3, "name": "bug"}]})  # org → 404

        assert _api(rec).labels(REPO) == {"bug": 3}

    def test_org_labels_forbidden_warns_and_uses_repo_labels(self):
        rec = Recorder(
            {
                ("GET", ORG): httpx.Response(403),
                ("GET", REPO_LABELS): [{"id": 3, "name": "bug"}],
            }
        )

        with capture_logs() as logs:
            assert _api(rec).labels(REPO) == {"bug": 3}
        assert any(e["event"] == "gitea_org_labels_forbidden" for e in logs)

    def test_repo_labels_error_propagates(self):
        rec = Recorder({("GET", ORG): [], ("GET", REPO_LABELS): httpx.Response(500)})

        with pytest.raises(GiteaAPIError):
            _api(rec).labels(REPO)

    def test_cached_until_ttl_or_refresh(self):
        rec = Recorder({("GET", ORG): [], ("GET", REPO_LABELS): [{"id": 3, "name": "bug"}]})
        clock = Clock()
        api = _api(rec, clock)

        api.labels(REPO)
        api.labels(REPO)
        assert len(rec.calls("GET", REPO_LABELS)) == 1

        api.labels(REPO, refresh=True)
        assert len(rec.calls("GET", REPO_LABELS)) == 2

        clock.now += api_module.CACHE_TTL_SECONDS
        api.labels(REPO)
        assert len(rec.calls("GET", REPO_LABELS)) == 3


class TestLabelIds:
    def test_in_order_unknown_refetched_once_then_skipped(self):
        rec = Recorder(
            {
                ("GET", ORG): [{"id": 1, "name": "a"}],
                ("GET", REPO_LABELS): [{"id": 10, "name": "b"}],
            }
        )

        with capture_logs() as logs:
            assert _api(rec).label_ids(REPO, ["b", "nope", "a"]) == [10, 1]

        assert len(rec.calls("GET", REPO_LABELS)) == 2
        assert not rec.calls("POST", REPO_LABELS)
        assert [e["label"] for e in logs if e["event"] == "gitea_label_unknown"] == ["nope"]

    def test_label_created_since_the_cache_is_found_on_refetch(self):
        rec = Recorder(
            {
                ("GET", ORG): [],
                ("GET", REPO_LABELS): [
                    httpx.Response(200, json=[]),
                    httpx.Response(200, json=[{"id": 5, "name": "new"}]),
                ],
            }
        )

        assert _api(rec).label_ids(REPO, ["new"]) == [5]

    def test_all_known_uses_the_cache(self):
        rec = Recorder({("GET", ORG): [], ("GET", REPO_LABELS): [{"id": 10, "name": "b"}]})
        api = _api(rec)

        api.label_ids(REPO, ["b"])
        api.label_ids(REPO, ["b"])

        assert len(rec.calls("GET", REPO_LABELS)) == 1


PERM = f"{P}/repos/{REPO}/collaborators/alice/permission"


class TestPermission:
    def test_returns_the_raw_permission_lowercased(self):
        rec = Recorder({("GET", PERM): {"permission": "Write", "role_name": "write"}})

        assert _api(rec).permission(REPO, "alice") == "write"

    def test_forbidden_is_none_with_a_warning(self):
        rec = Recorder({("GET", PERM): httpx.Response(403)})

        with capture_logs() as logs:
            assert _api(rec).permission(REPO, "alice") is None
        assert [e["event"] for e in logs if e["log_level"] == "warning"] == [
            "gitea_permission_forbidden"
        ]

    def test_malformed_body_is_none(self):
        rec = Recorder({("GET", PERM): {"permission": 3}})

        assert _api(rec).permission(REPO, "alice") is None

    def test_other_errors_propagate_and_are_not_cached(self):
        rec = Recorder(
            {("GET", PERM): [httpx.Response(500), httpx.Response(200, json={"permission": "read"})]}
        )
        api = _api(rec)

        with pytest.raises(GiteaAPIError):
            api.permission(REPO, "alice")
        assert api.permission(REPO, "alice") == "read"

    def test_cached_per_repo_and_login_case_insensitively(self):
        rec = Recorder({("GET", PERM): {"permission": "admin"}})
        clock = Clock()
        api = _api(rec, clock)

        assert api.permission(REPO, "alice") == "admin"
        assert api.permission(REPO, "ALICE") == "admin"
        assert len(rec.requests) == 1

        clock.now += api_module.CACHE_TTL_SECONDS
        api.permission(REPO, "alice")
        assert len(rec.requests) == 2

    def test_forbidden_is_cached_too(self):
        rec = Recorder({("GET", PERM): httpx.Response(403)})
        api = _api(rec)

        api.permission(REPO, "alice")
        api.permission(REPO, "alice")
        assert len(rec.requests) == 1
