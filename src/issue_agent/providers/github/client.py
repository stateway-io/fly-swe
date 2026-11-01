from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from issue_agent.core.models import Comment
from issue_agent.providers.base import ForgeClient
from issue_agent.providers.github.normalize import author_from_github

if TYPE_CHECKING:
    from github import Github
    from github.Issue import Issue
    from github.IssueComment import IssueComment
    from github.PullRequest import PullRequest
    from github.Repository import Repository

    from issue_agent.providers.github.auth import GitHubAppAuth
    from issue_agent.providers.github.normalize import BotIdentity


def _canonical_comments(comments: list[IssueComment], identity: BotIdentity) -> list[Comment]:
    normalized: list[Comment] = []
    for c in comments:
        user = c.user
        raw = c.raw_data or {}
        normalized.append(
            Comment(
                id=str(c.id),
                author=author_from_github(
                    {
                        "login": getattr(user, "login", None),
                        "type": getattr(user, "type", None),
                    },
                    association=raw.get("author_association"),
                    performed_via_github_app_id=(raw.get("performed_via_github_app") or {}).get(
                        "id"
                    ),
                    identity=identity,
                ),
                body=c.body or "",
                created_at=c.created_at,
            )
        )
    return normalized


class GitHubClient:
    def __init__(self, auth: GitHubAppAuth, installation_id: int) -> None:
        self._auth = auth
        self._installation_id = installation_id

    def get_github(self) -> Github:
        return self._auth.get_client(self._installation_id)

    def get_repo(self, full_name: str) -> Repository:
        return self.get_github().get_repo(full_name)

    def get_issue(self, full_name: str, issue_number: int) -> Issue:
        return self.get_repo(full_name).get_issue(issue_number)

    def get_issue_comments(self, full_name: str, issue_number: int) -> list[IssueComment]:
        return list(self.get_issue(full_name, issue_number).get_comments())

    def list_issue_comments(
        self, full_name: str, issue_number: int, limit: int, identity: BotIdentity
    ) -> list[Comment]:
        """The issue's ``limit`` most recent comments as canonical
        ``Comment``s.

        Returned in chronological order. Plain models — not PyGithub objects —
        so the run-context assembly downstream stays a pure, testable function
        over data it can't accidentally re-fetch from the network.
        ``identity`` sets ``author.is_self`` on the agent's own comments.
        """
        comments = list(self.get_issue(full_name, issue_number).get_comments())
        return _canonical_comments(comments[-limit:] if limit > 0 else [], identity)

    def find_pr_by_branch(self, full_name: str, branch_name: str) -> PullRequest | None:
        """Return the open PR whose head is ``branch_name``, or ``None``.

        ``head`` MUST be qualified as ``owner:ref``. GitHub does not reject a
        bare ref — it silently drops the filter and returns every open PR in
        the repository, so the caller gets an unrelated PR and reports it as
        its own (issue #89). The ``head.ref`` check below is a cheap backstop
        against that whole class of error: whatever the filter does, only a PR
        that really is on this branch comes back.
        """
        owner, _ = full_name.split("/", 1)
        repo = self.get_repo(full_name)
        for pr in repo.get_pulls(state="open", head=f"{owner}:{branch_name}"):
            if pr.head.ref == branch_name:
                return pr
        return None

    def get_pull_request(self, full_name: str, pr_number: int) -> PullRequest:
        return self.get_repo(full_name).get_pull(pr_number)

    def create_comment(self, full_name: str, issue_number: int, body: str) -> IssueComment:
        return self.get_issue(full_name, issue_number).create_comment(body)

    def create_pr(
        self,
        full_name: str,
        title: str,
        body: str,
        head: str,
        base: str,
        **kwargs: Any,
    ) -> PullRequest:
        return self.get_repo(full_name).create_pull(
            title=title,
            body=body,
            head=head,
            base=base,
            **kwargs,
        )


class GitHubForgeClient(ForgeClient):
    """``ForgeClient`` for one repo, authenticated as the App installation
    that covers it."""

    def __init__(self, auth: GitHubAppAuth, repo: str, identity: BotIdentity) -> None:
        self._auth = auth
        self._repo_name = repo
        self._identity = identity

    def _client(self) -> GitHubClient:
        return GitHubClient(self._auth, self._auth.installation_for(self._repo_name))

    def get_issue_state(self, number: int) -> Literal["open", "closed"]:
        state = self._client().get_issue(self._repo_name, number).state
        return "closed" if (state or "").lower() == "closed" else "open"

    def list_comments(self, number: int, limit: int) -> list[Comment]:
        return self._client().list_issue_comments(self._repo_name, number, limit, self._identity)

    def post_comment(self, number: int, body: str) -> str:
        return self._client().create_comment(self._repo_name, number, body).html_url

    def get_default_branch(self) -> str:
        return self._client().get_repo(self._repo_name).default_branch

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._client().get_issue(self._repo_name, number).add_to_labels(*labels)
