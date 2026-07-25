"""GitHub pull-request publisher. Ships in core, credential from the environment
only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import (
    ForgeError,
    Transport,
    TreeListingTruncatedError,
    request,
)
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

    def _existing_paths(self, tree_sha: str) -> set[str]:
        """Blob paths in `tree_sha`, recursively.

        GitHub answers 422 when a tree entry deletes a path absent from
        base_tree, so removals must be filtered. A truncated listing would make
        a present path look absent and silently skip a real deletion, so it
        raises rather than returning a partial set.
        """
        tree = self._call(
            "GET", f"/repos/{self._repo}/git/trees/{tree_sha}?recursive=1"
        )
        if tree.get("truncated"):
            raise TreeListingTruncatedError(
                f"base tree {tree_sha} exceeds GitHub's recursive listing limit; "
                "refusing to return a partial listing. Note this listing is of "
                "the repository root and is NOT narrowed by the publish "
                "config's 'base_path' — GitHub's tree endpoint takes a tree SHA "
                "and offers no path filter, so scoping 'base_path' does not "
                "shrink it. Publish to a repository small enough to list, such "
                "as a dedicated knowledge repository."
            )
        return {e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"}

    def put_files(
        self,
        branch: str,
        base: str,
        files: dict[str, str],
        removed: list[str],
        message: str,
    ) -> None:
        # One call yields both the base commit SHA and its tree SHA, so no
        # separate ref lookup is needed. Contents go inline in the tree entries,
        # so no blob calls are needed either. A removal becomes a tree entry
        # with sha=None (see _existing_paths for why it's filtered first).
        head = self._call("GET", f"/repos/{self._repo}/commits/{_ref_path(base)}")
        entries: list[dict] = [
            {"path": path, "mode": "100644", "type": "blob", "content": body}
            for path, body in sorted(files.items())  # deterministic
        ]
        base_tree = head["commit"]["tree"]["sha"]
        if removed:
            # Only the listing call is conditional on there being something to
            # remove, so an ordinary publish costs no extra request.
            existing = self._existing_paths(base_tree)
            entries += [
                {"path": path, "mode": "100644", "type": "blob", "sha": None}
                for path in sorted(removed)  # deterministic
                if path in existing
            ]
        if not entries:
            # Nothing to commit: no files, and every removal is already gone
            # from base. Reachable — put_files can succeed and update_pr then
            # fail, leaving the mirror un-advanced so the next run re-emits a
            # tombstone for a path the branch no longer has.
            #
            # Observed against a real repo: POST /git/trees with tree=[] answers
            # 422 "Invalid tree info", so the degenerate payload is not merely
            # ugly, it fails. Skipping the tree call and committing base's own
            # tree makes a valid empty commit, which GitHub accepts and will
            # open a PR from (a branch with no commits at all is refused: 422
            # "No commits between main and <branch>").
            if base == branch:
                return  # the branch already is base; nothing to do at all
            self._set_ref(branch, self._commit(message, base_tree, head["sha"]))
            return
        tree = self._call(
            "POST",
            f"/repos/{self._repo}/git/trees",
            {"base_tree": base_tree, "tree": entries},
        )
        self._set_ref(branch, self._commit(message, tree["sha"], head["sha"]))

    def _commit(self, message: str, tree_sha: str, parent_sha: str) -> str:
        commit = self._call(
            "POST",
            f"/repos/{self._repo}/git/commits",
            {"message": message, "tree": tree_sha, "parents": [parent_sha]},
        )
        return commit["sha"]

    def _set_ref(self, branch: str, sha: str) -> None:
        """Point `branch` at `sha`, creating the ref if it does not exist yet."""
        # The PATCH target is a URL *path*, so it is percent-encoded via
        # _ref_path(). The POST body below is JSON, not a path — "ref" there
        # must be the literal, fully-qualified ref name, unencoded. A URL path
        # and a JSON field are different things; only the former needs escaping.
        try:
            self._call(
                "PATCH",
                f"/repos/{self._repo}/git/refs/heads/{_ref_path(branch)}",
                {"sha": sha, "force": True},
            )
        except ForgeError as exc:
            if exc.status not in _REF_MISSING:
                raise
            self._call(
                "POST",
                f"/repos/{self._repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": sha},
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
            name="github", version="0.4.0", source_system="GitHub pull requests"
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
