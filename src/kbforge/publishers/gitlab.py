"""GitLab merge-request publisher. Ships in core, credential from the
environment only. Never merges (§5.2).

Listing cost is asymmetric between the two forges: GitHub's put_files gets
base's tree in a single GET (commits/{ref} returns the tree SHA directly).
GitLab has no equivalent one-shot lookup, so _existing_paths() walks the
project's tree page by page — one GET per _TREE_PAGE_SIZE (100) blobs, on
every publish. On a large repository, an unscoped base_path="" makes that walk
expensive; scoping base_path to the subtree kbforge actually writes to is the
mitigation.
"""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import ForgeError, PublishError, Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://gitlab.com/api/v4", "token_env": "GITLAB_TOKEN"}

# Tree listing is paginated by hand: _http.request returns only the parsed body,
# never the response headers, so x-next-page is not available to us. A page
# shorter than _TREE_PAGE_SIZE is the last one; _TREE_MAX_PAGES caps the loop so
# a misbehaving server cannot spin it forever. Exhausting the cap without ever
# seeing a short page means the listing is incomplete — _existing_paths() raises
# rather than silently returning a partial set (see its docstring).
_TREE_PAGE_SIZE = 100
_TREE_MAX_PAGES = 1000


class TreeListingTruncatedError(PublishError):
    """The base tree has more pages than _TREE_MAX_PAGES covers.

    Returning the partial set gathered so far would be worse than raising: a
    path that exists on base but fell past the cap would be missing from
    `existing`, so put_files() would send action="create" for it and GitLab
    would answer 400 "A file with this name already exists" — the exact bug
    the base/create-vs-update fix addressed, resurfacing silently.
    """


class GitLabClient:
    def __init__(self, cfg: ForgeConfig, transport: Transport = request) -> None:
        self._cfg = cfg
        self._transport = transport
        self._api = cfg.api_base.rstrip("/")
        # Projects are addressed by URL-encoded path, which is also how nested
        # subgroups (group/subgroup/project) are expressed.
        self._project = quote(cfg.repo, safe="")

    def _call(self, method: str, path: str, payload: dict | list | None = None):
        return self._transport(
            method,
            f"{self._api}{path}",
            # Bearer accepts both token families GitLab issues: personal,
            # project and group access tokens, plus OAuth tokens (which
            # PRIVATE-TOKEN rejects with a 401). `glab auth login` stores an
            # OAuth token, so PRIVATE-TOKEN would lock out the official CLI.
            headers={"Authorization": f"Bearer {self._cfg.token()}"},
            payload=payload,
        )

    def default_branch(self) -> str:
        return self._call("GET", f"/projects/{self._project}")["default_branch"]

    def _existing_paths(self, base: str) -> set[str]:
        """Blob paths already present on `base`, scoped to base_path when set.

        force=true overwrites the target *ref*, not the tree: the commit is
        built from start_branch, so everything on base is still there.

        Raises TreeListingTruncatedError instead of returning a partial set if
        the tree has more than _TREE_MAX_PAGES pages — see that class's
        docstring for why a partial set is unsafe to hand back silently.
        """
        ref = quote(base, safe="")
        base_path = self._cfg.base_path
        scope = f"&path={quote(base_path, safe='')}" if base_path else ""
        found: set[str] = set()
        for page in range(1, _TREE_MAX_PAGES + 1):
            try:
                entries = self._call(
                    "GET",
                    f"/projects/{self._project}/repository/tree"
                    f"?ref={ref}&recursive=true&per_page={_TREE_PAGE_SIZE}"
                    f"{scope}&page={page}",
                )
            except ForgeError as exc:
                if exc.status != 404:
                    raise
                # base_path (or the ref) does not exist on base yet: nothing to
                # update, everything gets created.
                break
            entries = entries or []
            found.update(e["path"] for e in entries if e.get("type") == "blob")
            if len(entries) < _TREE_PAGE_SIZE:
                break
        else:
            # The loop ran to completion without a short page or a 404: base's
            # tree has at least _TREE_MAX_PAGES * _TREE_PAGE_SIZE blobs (in
            # scope) and there may be more beyond the cap.
            raise TreeListingTruncatedError(
                f"listing {base!r}"
                + (f" scoped to base_path={base_path!r}" if base_path else "")
                + f" did not finish within _TREE_MAX_PAGES ({_TREE_MAX_PAGES}) "
                "pages; refusing to return a partial listing. Scope the "
                "publish config's 'base_path' to a narrower subtree to keep "
                "the listing within the cap."
            )
        return found

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
        # The commit's tree comes from start_branch, so a path already on base
        # must use action="update" — "create" collides with it ("A file with
        # this name already exists"). GitLab's commits API has no upsert action,
        # so the verb is chosen per path against a listing of base.
        existing = self._existing_paths(base)
        self._call(
            "POST",
            f"/projects/{self._project}/repository/commits",
            {
                "branch": branch,
                "start_branch": base,
                "force": True,
                "commit_message": message,
                "actions": [
                    {
                        "action": "update" if path in existing else "create",
                        "file_path": path,
                        "content": body,
                    }
                    for path, body in sorted(files.items())  # deterministic
                ],
            },
        )

    def find_open_pr(self, branch: str) -> str | None:
        # A branch never reaches a URL *path* here — it is a query value (so
        # safe="" is right, slashes included) or a JSON payload field.
        source = quote(branch, safe="")
        mrs = self._call(
            "GET",
            f"/projects/{self._project}/merge_requests"
            f"?source_branch={source}&state=opened",
        )
        return str(mrs[0]["iid"]) if mrs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        mr = self._call(
            "POST",
            f"/projects/{self._project}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        return mr["web_url"]

    def update_pr(self, pr_id: str, title: str, body: str) -> str:
        mr = self._call(
            "PUT",
            f"/projects/{self._project}/merge_requests/{pr_id}",
            {"title": title, "description": body},
        )
        return mr["web_url"]


class GitLabPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="gitlab", version="0.3.0", source_system="GitLab merge requests"
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
        return publish_to_forge(GitLabClient(cfg), change, cfg)
