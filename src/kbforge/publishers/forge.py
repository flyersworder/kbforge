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
    # backstop: unreachable given the per-segment rejection above, and kept
    # deliberately so that relaxing that loop cannot silently drop the
    # "never escapes the repo" guarantee.
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
            elif ".." in segments or "." in segments:
                # Every segment is non-empty in 'acme/../../x' or 'acme/./kb',
                # so the shape check above accepts both; '..' climbs out of the
                # namespace, and '.' is rejected alongside it for the same
                # reason (a bare '.' segment is never a meaningful repo path,
                # even though it normalizes to the same repo here).
                problems.append(
                    f"'repo' must not contain '.' or '..' segments, got {self.repo!r}"
                )
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
        self,
        branch: str,
        base: str,
        files: dict[str, str],
        removed: list[str],
        message: str,
    ) -> None:
        """Set `branch` to `base`, apply `files`, delete `removed`, as one commit.

        Paths on `base` in neither list are inherited. `base` is the sync branch
        itself when a review request is open, so successive runs accumulate onto
        one branch instead of rebuilding it from the default branch each time.

        `files` may be empty and every removal may filter out against `base`,
        leaving nothing to commit. An adapter must not send a degenerate commit
        payload in that case: it commits nothing when `base == branch` (the
        branch already holds exactly the intended state) and makes an empty
        commit when `base != branch`, since the commit is also what creates the
        branch the review request will point at.
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

    Validates every path it will send with safe_join() — both the keys of
    change.files and every entry of change.files_removed — before making any
    client call (including client.default_branch(), used when cfg.base is
    unset), so a traversing path is rejected before it can reach the network.
    Removals matter at least as much as writes: a traversing removal names a
    file to delete outside the bundle.
    """
    files = {safe_join(cfg.base_path, rel): body for rel, body in change.files.items()}
    removed = sorted(safe_join(cfg.base_path, rel) for rel in change.files_removed)
    branch = cfg.branch or change.branch_hint
    body = summary_md(change.summary)

    # Asked before put_files, not after: an open review request means the branch
    # must build on itself, or work from earlier runs is rebuilt away.
    pr_id = client.find_open_pr(branch)
    if pr_id is not None:
        client.put_files(branch, branch, files, removed, cfg.title)
        return client.update_pr(pr_id, cfg.title, body)

    # Resolved only here: on the update path `target` is never used, and
    # default_branch() is a network call that would fail an otherwise-viable
    # update to an already-open review request.
    target = cfg.base or client.default_branch()
    client.put_files(branch, target, files, removed, cfg.title)
    return client.create_pr(branch, target, cfg.title, body)
