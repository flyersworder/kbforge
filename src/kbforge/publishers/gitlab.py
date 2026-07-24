"""GitLab merge-request publisher. Ships in core, credential from the
environment only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://gitlab.com/api/v4", "token_env": "GITLAB_TOKEN"}


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

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
        # force=true bases the commit on start_branch and overwrites the target
        # branch, which is why action="create" is always right: the branch is
        # reset first, so no file exists to collide with.
        self._call(
            "POST",
            f"/projects/{self._project}/repository/commits",
            {
                "branch": branch,
                "start_branch": base,
                "force": True,
                "commit_message": message,
                "actions": [
                    {"action": "create", "file_path": path, "content": body}
                    for path, body in sorted(files.items())  # deterministic
                ],
            },
        )

    def find_open_pr(self, branch: str) -> str | None:
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
