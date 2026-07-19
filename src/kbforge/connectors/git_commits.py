"""A git-history connector: each commit reachable from a ref becomes one canonical
document. No credentials, no network — reads a local repository with `git log`.

Unlike the feed-less local_files connector, this one uses the cursor for real: the
watermark is the last-synced commit SHA, so an incremental fetch returns only
`<last_sha>..<ref>` — the first live exercise of the pipeline's incremental path.
A commit is immutable, so its content hash never changes and a re-seen commit is a
clean no-op."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from kbforge.canonical import content_hash
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)

_SYSTEM = "git_commits"
_UNIT = "\x1f"  # field separator inside one commit's formatted record
# sha, author name, author email, author date (ISO), committer date (ISO), subject,
# body — body is last because it may contain newlines (records are NUL-delimited).
_FORMAT = _UNIT.join(["%H", "%an", "%ae", "%aI", "%cI", "%s", "%b"])
_FIELDS = 7


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout


class GitCommitsConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_SYSTEM,
            version="0.1.0",
            source_system="git history (local repository)",
            info_types=["commit"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        problems: list[str] = []
        repo = config.get("repo")
        if not repo or not Path(repo).is_dir():
            problems.append(f"config 'repo' is not a readable directory: {repo!r}")
        elif not _git(Path(repo), "rev-parse", "--git-dir", check=False).strip():
            problems.append(f"config 'repo' is not a git repository: {repo!r}")
        max_commits = config.get("max_commits")
        if max_commits is not None and (
            not isinstance(max_commits, int) or isinstance(max_commits, bool)
        ):
            problems.append(f"config 'max_commits' must be an int: {max_commits!r}")
        return problems

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        repo = Path(config["repo"])
        ref = config.get("ref", "HEAD")
        tip = _git(repo, "rev-parse", ref, check=False).strip()
        if not tip:
            # Empty repo / unknown ref: nothing to sync, watermark stays put.
            return FetchResult(
                records=[], cursor=Cursor(connector=_SYSTEM, payload={"ref": ref})
            )

        # cursor=None → backfill everything reachable from ref (bounded by
        # max_commits); an existing watermark → only commits since it (`last..ref`).
        last_sha = cursor.payload.get("last_sha") if cursor else None
        rev_range = f"{last_sha}..{ref}" if last_sha else ref
        log_args = ["log", rev_range, f"--format={_FORMAT}", "-z"]
        max_commits = config.get("max_commits")
        if not last_sha and max_commits is not None:
            log_args.append(f"--max-count={max_commits}")

        raw = _git(repo, *log_args)
        records: list[RawRecord] = []
        for chunk in raw.split("\x00"):
            if not chunk.strip():
                continue
            parts = chunk.split(_UNIT)
            if len(parts) < _FIELDS:
                continue
            sha, author, email, adate, cdate, subject, body = parts[:_FIELDS]
            records.append(
                RawRecord(
                    anchor_hint={
                        "native_id": sha,
                        "url": None,
                        "retrieved_at": cdate,
                        "author": author,
                        "author_email": email,
                        "author_date": adate,
                        "subject": subject,
                    },
                    media_type="text/x-git-commit",
                    payload=body.encode("utf-8"),
                )
            )
        return FetchResult(
            records=records,
            cursor=Cursor(connector=_SYSTEM, payload={"last_sha": tip, "ref": ref}),
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            hint = rec.anchor_hint
            sha = hint["native_id"]
            anchor = ResourceAnchor(
                system=_SYSTEM,
                native_id=sha,
                url=hint.get("url"),
                retrieved_at=datetime.fromisoformat(hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=f"{_SYSTEM}:{sha}",
                title=hint.get("subject") or sha[:12],
                text=rec.payload.decode("utf-8").strip(),
                structured={
                    "author": hint.get("author"),
                    "author_email": hint.get("author_email"),
                    "author_date": hint.get("author_date"),
                },
                relations=[],
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs
