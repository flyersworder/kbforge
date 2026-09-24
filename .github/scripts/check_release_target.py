"""Fail a release before it uploads nothing.

`skip-existing: true` on the publish step is a safety net: one job publishes
three independently-versioned distributions, and without it a duplicate file
400s *after* other files have uploaded — a half-published release. But that same
flag makes two outcomes indistinguishable from the outside: a release that
correctly had nothing new to upload, and a release whose version bump was
forgotten. Both are a green job that published nothing.

This closes that gap by asserting, before the upload, that the distribution the
tag names is actually about to publish something. Two checks, failing for
different reasons:

1. `dist/` holds artifacts at the tag's version — otherwise the pyproject was
   never bumped, or the tag is wrong.
2. PyPI does not already serve that version — otherwise this is a re-release.

When both pass, every OTHER distribution's artifacts are moved out of the dist
directory (into `<dist-dir>-held/`), so the publish step uploads what the tag
names and nothing else. `uv build --all-packages` builds every workspace member;
publishing all of them let a kbforge tag release kbforge-sql 0.1.1 as a side
effect, and would have shipped any companion bumped for a later release early.

Usage:  check_release_target.py <tag> <dist-dir>

Tags name what is being released: `vX.Y.Z` is kbforge, `<distribution>-vX.Y.Z`
is a companion (`kbforge-okfquery-v0.1.0`). See CLAUDE.md's "Releasing" section.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

ROOT_DIST = "kbforge"
TAG_RE = re.compile(
    r"(?:(?P<dist>[A-Za-z0-9][A-Za-z0-9._-]*?)-)?v(?P<version>[0-9][^-]*)$"
)


def fail(message: str) -> NoReturn:
    """`NoReturn`, not `None`: callers rely on control stopping here to narrow a
    match object, and a `-> None` annotation makes the checker assume otherwise."""
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def parse_tag(tag: str) -> tuple[str, str]:
    """`v0.9.0` -> (kbforge, 0.9.0);
    `kbforge-okfquery-v0.1.0` -> (kbforge-okfquery, 0.1.0)."""
    m = TAG_RE.fullmatch(tag)
    if not m:
        fail(
            f"tag {tag!r} is not a release tag. Expected 'vX.Y.Z' for {ROOT_DIST}, "
            f"or '<distribution>-vX.Y.Z' for a companion."
        )
    return (m["dist"] or ROOT_DIST), m["version"]


def _artifact(name: str) -> tuple[str, str] | None:
    """(normalised distribution stem, version) for a wheel or sdist file name,
    or None for anything else in dist/ (uv writes a .gitignore there).

    Wheel and sdist names normalise `-` to `_`, so `kbforge-okfquery` appears as
    `kbforge_okfquery`, and the first `-` always ends the stem."""
    if name.endswith(".whl"):
        parts = name[: -len(".whl")].split("-")
        return (parts[0], parts[1]) if len(parts) >= 2 else None
    if name.endswith(".tar.gz"):
        stem, sep, version = name[: -len(".tar.gz")].partition("-")
        return (stem, version) if sep else None
    return None


def built_versions(dist_dir: Path, distribution: str) -> set[str]:
    """Versions present in dist/ for one distribution."""
    stem = distribution.replace("-", "_")
    return {
        art[1]
        for path in dist_dir.iterdir()
        if (art := _artifact(path.name)) is not None and art[0] == stem
    }


def keep_only(dist_dir: Path, distribution: str, held: Path) -> None:
    """Move every other distribution's artifacts from `dist_dir` to `held`.

    Moved, not deleted, so a failed publish can still be inspected. Files that
    are not artifacts stay where they are."""
    stem = distribution.replace("-", "_")
    held.mkdir(parents=True, exist_ok=True)
    for path in sorted(dist_dir.iterdir()):
        art = _artifact(path.name)
        if art is not None and art[0] != stem:
            path.rename(held / path.name)


def pypi_versions(distribution: str) -> set[str] | None:
    """Versions already on PyPI, or None when the project does not exist yet
    (a first release through a pending publisher — not an error)."""
    url = f"https://pypi.org/pypi/{distribution}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            return set(json.load(response).get("releases", {}))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        fail(f"could not reach PyPI for {distribution}: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        fail(f"could not reach PyPI for {distribution}: {exc.reason}")
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        fail(f"usage: {Path(argv[0]).name} <tag> <dist-dir>")
    tag, dist_dir = argv[1], Path(argv[2])
    if not dist_dir.is_dir():
        fail(f"{dist_dir} is not a directory; run `uv build` first")

    distribution, version = parse_tag(tag)
    print(f"release tag {tag} -> distribution {distribution}, version {version}")

    built = built_versions(dist_dir, distribution)
    if not built:
        fail(
            f"{dist_dir} holds no artifacts for {distribution}. `uv build "
            f"--all-packages` builds every workspace member, so this means the "
            f"distribution name in the tag is wrong."
        )
    if version not in built:
        fail(
            f"tag {tag} releases {distribution} {version}, but the build produced "
            f"{', '.join(sorted(built))}. Bump the version in that package's "
            f"pyproject.toml, or retag. Publishing now would upload nothing and "
            f"still report success."
        )

    published = pypi_versions(distribution)
    if published is None:
        print(f"{distribution} is not yet on PyPI — first release, nothing to compare")
    elif version in published:
        fail(
            f"{distribution} {version} is already on PyPI. skip-existing would "
            f"swallow every file and the job would report success having published "
            f"nothing. Bump the version, or drop this distribution from the release."
        )
    else:
        print(f"{distribution} {version} is new to PyPI — release will publish it")

    held = dist_dir.parent / f"{dist_dir.name}-held"
    keep_only(dist_dir, distribution, held)
    print(f"publishing only {distribution}; other distributions held in {held}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
