"""GitHub pull-request publisher. Ships in core, credential from the environment
only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import ForgeError, Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://api.github.com", "token_env": "GITHUB_TOKEN"}

# PATCH on a missing ref answers 422; 404 is accepted too so a future API
# tightening does not turn "branch not created yet" into a hard failure.
_REF_MISSING = (404, 422)


def _ref_path(ref: str) -> str:
    """Encode a branch/ref for use inside a URL *path*.

    A slash is structural in a ref name — `sync/local-files` is one branch, not
    two path segments — so quoting the whole string with safe="" would break it.
    Each segment is encoded on its own instead. `.` and `..` need explicit
    handling: urllib treats `.` as unreserved and never escapes it, so
    quote("..", safe="") is still "..", which a proxy or the server may collapse
    into a traversal. They are escaped by hand.

    This matters because `branch` defaults to change.branch_hint, which is
    connector-supplied and therefore untrusted, exactly like the file keys
    safe_join() guards.

    Note this is weaker than a full guarantee: an empty segment (from a
    leading/trailing/doubled slash, e.g. "/foo" or "a//b") passes through
    unchanged rather than being rejected. That is fine here — the result is
    simply not a syntactically valid ref, which the forge itself rejects.
    """
    return "/".join(
        seg.replace(".", "%2E") if seg in {".", ".."} else quote(seg, safe="")
        for seg in ref.split("/")
    )


class GitHubClient:
    def __init__(self, cfg: ForgeConfig, transport: Transport = request) -> None:
        self._cfg = cfg
        self._transport = transport
        self._api = cfg.api_base.rstrip("/")
        self._repo = cfg.repo

    def _call(self, method: str, path: str, payload: dict | list | None = None):
        return self._transport(
            method,
            f"{self._api}{path}",
            headers={
                "Authorization": f"Bearer {self._cfg.token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            payload=payload,
        )

    def default_branch(self) -> str:
        return self._call("GET", f"/repos/{self._repo}")["default_branch"]

    def put_files(
        self,
        branch: str,
        base: str,
        files: dict[str, str],
        removed: list[str],
        message: str,
    ) -> None:
        # `removed` is honoured in the adapter-specific delete task; accepting it
        # here keeps the protocol change and the delete mechanics reviewable apart.
        # One call yields both the base commit SHA and its tree SHA, so no
        # separate ref lookup is needed. Contents go inline in the tree entries,
        # so no blob calls are needed either.
        head = self._call("GET", f"/repos/{self._repo}/commits/{_ref_path(base)}")
        tree = self._call(
            "POST",
            f"/repos/{self._repo}/git/trees",
            {
                "base_tree": head["commit"]["tree"]["sha"],
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": body}
                    for path, body in sorted(files.items())  # deterministic
                ],
            },
        )
        commit = self._call(
            "POST",
            f"/repos/{self._repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [head["sha"]]},
        )
        # The PATCH target is a URL *path*, so it is percent-encoded via
        # _ref_path(). The POST body below is JSON, not a path — "ref" there
        # must be the literal, fully-qualified ref name, unencoded. A URL path
        # and a JSON field are different things; only the former needs escaping.
        try:
            self._call(
                "PATCH",
                f"/repos/{self._repo}/git/refs/heads/{_ref_path(branch)}",
                {"sha": commit["sha"], "force": True},
            )
        except ForgeError as exc:
            if exc.status not in _REF_MISSING:
                raise
            self._call(
                "POST",
                f"/repos/{self._repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )

    def find_open_pr(self, branch: str) -> str | None:
        # kbforge always pushes to the target repo itself, never a fork, so the
        # head owner is the first segment of repo by construction.
        owner = self._repo.split("/")[0]
        head = quote(f"{owner}:{branch}", safe="")
        prs = self._call("GET", f"/repos/{self._repo}/pulls?head={head}&state=open")
        return str(prs[0]["number"]) if prs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        pr = self._call(
            "POST",
            f"/repos/{self._repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return pr["html_url"]

    def update_pr(self, pr_id: str, title: str, body: str) -> str:
        pr = self._call(
            "PATCH",
            f"/repos/{self._repo}/pulls/{pr_id}",
            {"title": title, "body": body},
        )
        return pr["html_url"]


class GitHubPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="github", version="0.3.0", source_system="GitHub pull requests"
        )

    @hookimpl
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        try:
            return build_config(config, DEFAULTS).validate_config()
        except ValueError as exc:
            return [str(exc)]

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        cfg = build_config(config, DEFAULTS)
        return publish_to_forge(GitHubClient(cfg), change, cfg)
