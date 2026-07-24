"""Forge-agnostic publish orchestration.

Knows the *sequence* — reset a branch to base, put the files on it, open or
update exactly one review request — and nothing about GitHub or GitLab. The two
forges decompose "commit these files" completely differently (GitLab in one
call, GitHub in four), so the ForgeClient protocol is pitched at intentions,
not at REST endpoints.

MUST NOT merge (§5.2). There is no merge method here or on any adapter, so the
guarantee cannot be violated without deliberately widening the interface.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, fields
from typing import Protocol

from kbforge.models import ProposedChange
from kbforge.publishers.summary import summary_md


class PathError(ValueError):
    """A configured or generated path escapes the target repository."""


# `repo` is interpolated into API URL paths (GitHub) and URL-encoded into one
# (GitLab), so it is held to the characters a forge namespace can actually
# contain. Slash is allowed because it separates owner/name and GitLab
# subgroups; `..` is rejected separately, since it is made of allowed
# characters.
_REPO_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")


def safe_join(base_path: str, rel: str) -> str:
    """Join a config prefix to a bundle-relative path, refusing anything that
    escapes the repo. Applied to *both* base_path (user config) and every key of
    change.files (connector/synthesizer output) — file keys are produced
    downstream and are equally capable of naming ../../.github/workflows/x.yml.
    """
    if not rel:
        raise PathError("empty file path")
    for part in (base_path, rel):
        if part.startswith("/"):
            raise PathError(f"absolute path not allowed: {part!r}")
        if ".." in part.split("/"):
            raise PathError(f"'..' not allowed in path: {part!r}")
    joined = posixpath.join(base_path, rel) if base_path else rel
    normalized = posixpath.normpath(joined)
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise PathError(f"path escapes the repository: {joined!r}")
    return normalized


@dataclass
class ForgeConfig:
    """Publisher config. Mirrors LLMConfig's shape: plain dataclass, a
    validate_*() returning human-readable problems, and the credential named by
    an env var rather than carried as a value."""

    repo: str = ""
    base: str = ""
    base_path: str = ""
    branch: str = ""
    title: str = "kbforge: knowledge base sync"
    api_base: str = ""
    token_env: str = ""

    def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.repo:
            problems.append("'repo' is required (owner/name)")
        else:
            segments = self.repo.split("/")
            # At least two, not exactly two: GitLab projects nest in subgroups
            # (group/subgroup/project); GitHub is always owner/name.
            if len(segments) < 2 or not all(segments):
                problems.append(f"'repo' must be owner/name, got {self.repo!r}")
            elif ".." in segments:
                # Every segment is non-empty in 'acme/../../x', so the shape
                # check above accepts it; it still climbs out of the namespace.
                problems.append(f"'repo' must not contain '..', got {self.repo!r}")
            elif not _REPO_CHARS.fullmatch(self.repo):
                problems.append(
                    "'repo' may only contain letters, digits, '.', '_', '-' and "
                    f"'/', got {self.repo!r}"
                )
        if self.base_path:
            try:
                safe_join(self.base_path, "probe.md")
            except PathError as exc:
                problems.append(f"'base_path' invalid: {exc}")
        if not self.token_env:
            problems.append("'token_env' must be non-empty")
        elif not os.environ.get(self.token_env):
            problems.append(f"env var {self.token_env} is not set")
        return problems

    def token(self) -> str:
        return os.environ.get(self.token_env, "")


def build_config(config: dict, defaults: dict) -> ForgeConfig:
    """Merge per-forge defaults under the user's --publish-set values."""
    merged = {**defaults, **config}
    known = {f.name for f in fields(ForgeConfig)}
    unknown = sorted(set(merged) - known)
    if unknown:
        raise ValueError(
            f"unknown publisher config key(s): {', '.join(unknown)}; "
            f"known keys: {', '.join(sorted(known))}"
        )
    return ForgeConfig(**merged)


class ForgeClient(Protocol):
    """Every method names an intention, never a REST endpoint."""

    def default_branch(self) -> str: ...

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
        """Reset `branch` to `base`, then apply exactly `files` as one commit.

        Files present in `base` but absent from `files` are inherited, not
        deleted — concept deletions do not propagate (spec §8).
        """
        ...

    def find_open_pr(self, branch: str) -> str | None:
        """The open PR/MR id for `branch` as an opaque string, or None."""
        ...

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str: ...

    def update_pr(self, pr_id: str, title: str, body: str) -> str: ...


def publish_to_forge(
    client: ForgeClient, change: ProposedChange, cfg: ForgeConfig
) -> str:
    """Open or update one review request; return its URL. Never merges.

    Validates every file path with safe_join() before making any client call
    (including client.default_branch(), used when cfg.base is unset) so a
    traversing file key is rejected before it can reach the network.
    """
    files = {safe_join(cfg.base_path, rel): body for rel, body in change.files.items()}
    base = cfg.base or client.default_branch()
    branch = cfg.branch or change.branch_hint
    body = summary_md(change.summary)

    client.put_files(branch, base, files, cfg.title)
    pr_id = client.find_open_pr(branch)
    if pr_id is not None:
        return client.update_pr(pr_id, cfg.title, body)
    return client.create_pr(branch, base, cfg.title, body)
