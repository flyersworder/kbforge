"""GitLab merge-request publisher. Ships in core, credential from the
environment only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import ForgeError, Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://gitlab.com/api/v4", "token_env": "GITLAB_TOKEN"}

# Tree listing is paginated by hand: _http.request returns only the parsed body,
# never the response headers, so x-next-page is not available to us. A page
# shorter than _TREE_PAGE_SIZE is the last one; _TREE_MAX_PAGES caps the loop so
# a misbehaving server cannot spin it forever.
_TREE_PAGE_SIZE = 100
_TREE_MAX_PAGES = 1000


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
            headers={"PRIVATE-TOKEN": self._cfg.token()},
            payload=payload,
        )

    def default_branch(self) -> str:
        return self._call("GET", f"/projects/{self._project}")["default_branch"]

    def _existing_paths(self, base: str) -> set[str]:
        """Blob paths already present on `base`, scoped to base_path when set.

        force=true overwrites the target *ref*, not the tree: the commit is
        built from start_branch, so everything on base is still there.
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
        return found

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
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
