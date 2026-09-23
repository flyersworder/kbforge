"""`okfquery related`: what one concept connects to, derived when asked.

OKF §3.1 leaves tag views to the consumer ("synthesize one at consumption time
by scanning frontmatter"), so relations implied by a shared tag or facet value
are computed here rather than stored as links a producer must keep fresh.
Backlinks are here for the same reason: no single file shows them.

Lines use `okfquery index`'s format, so an agent reads both the same way."""

from __future__ import annotations

from pathlib import Path

from okfquery.index import entry, facet_values
from okfquery.load import load
from okfquery.parse import OKF_OWNED

NONE = "(none)"
"""An empty section says so: an agent must tell "nothing" from "not checked"."""


def _norm(path: str) -> str:
    """Bundle-relative, as the `concepts` table stores it. OKF §6.1 recommends
    a leading `/` for bundle-absolute links, and a shell user types `./`."""
    return path.removeprefix("./").lstrip("/")


def _section(heading: str, lines: list[str]) -> list[str]:
    return [f"# {heading}", "", *(lines or [NONE])]


def render_related(
    bundle: Path, path: str, by: list[str] | None = None, limit: int = 10
) -> str:
    """Links to, linked from, and up to `limit` concepts sharing a value of any
    `by` facet (default `tags`), ranked by how many values they share, then by
    path. Deterministic: every ordering is total."""
    by = by or ["tags"]
    for key in by:
        if key in OKF_OWNED:
            # Those keys never reach `facets`, so nothing would ever match and
            # the section would read as a real "(none)".
            raise ValueError(f"{key!r} is not a facet: OKF owns it")
    path = _norm(path)
    con = load(bundle)
    rows = {
        p: (title, description, facets)
        for p, title, description, facets in con.execute(
            "select path, title, description, facets from concepts"
        ).fetchall()
    }
    if path not in rows:
        raise ValueError(f"{path} is not a concept in {bundle}")

    def line(p: str) -> str:
        title, description, _ = rows.get(p, (None, None, None))
        return entry(p, title, description)

    # Normalized in Python, not SQL: `_norm` is the one rule for what a link
    # target names, and a bundle may mix `/concepts/...` with `concepts/...`.
    links = con.execute("select path, target from links").fetchall()
    out_ = sorted({_norm(t) for p, t in links if p == path} - {path})
    in_ = sorted({p for p, t in links if _norm(t) == path and p != path})
    seen = {path, *out_, *in_}

    own = {key: set(facet_values(rows[path][2], key)) for key in by}
    ranked: list[tuple[int, str, str]] = []
    for p, (_, _, facets) in rows.items():
        if p in seen:
            continue
        shared = {key: sorted(own[key] & set(facet_values(facets, key))) for key in by}
        count = sum(len(v) for v in shared.values())
        if count:
            what = "; ".join(f"{k}: {', '.join(v)}" for k, v in shared.items() if v)
            ranked.append((count, p, f"{line(p)} (shares {what})"))
    ranked.sort(key=lambda r: (-r[0], r[1]))

    lines = _section("Links to", [line(p) for p in out_])
    lines += ["", *_section("Linked from", [line(p) for p in in_])]
    lines += ["", *_section(f"Shares {', '.join(by)}", [r[2] for r in ranked[:limit]])]
    return "\n".join(lines) + "\n"
