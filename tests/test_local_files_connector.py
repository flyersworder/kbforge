from pathlib import Path

from kbforge.canonical import assert_stability
from kbforge.connectors.local_files import LocalFilesConnector


def _write(dirp: Path, rel: str, body: str) -> None:
    dest = dirp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, "utf-8")


DOC = """---
type: application
title: App X
owner: team-a
relations:
  - apps/y
---
App X does things.
"""


def test_fetch_then_normalize_builds_canonical_doc(tmp_path: Path):
    _write(tmp_path, "apps/x.md", DOC)
    conn = LocalFilesConnector()
    cfg = {"path": str(tmp_path)}
    assert conn.kbforge_validate_config(cfg) == []
    result = conn.kbforge_fetch(cfg, None)
    docs = conn.kbforge_normalize(result.records)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == "local_files:apps/x.md"
    assert doc.title == "App X"
    assert doc.structured == {"owner": "team-a"}  # type/title/relations reserved
    assert doc.relations == ["local_files:apps/y"]
    assert "App X does things." in doc.text
    assert doc.anchor.retrieved_at.utcoffset() is not None  # tz-aware
    assert doc.anchor.content_hash  # set during normalize


def test_normalize_is_stable(tmp_path: Path):
    _write(tmp_path, "apps/x.md", DOC)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    assert_stability(conn.kbforge_normalize, result.records)  # must not raise


def test_validate_config_reports_missing_path(tmp_path: Path):
    conn = LocalFilesConnector()
    problems = conn.kbforge_validate_config({"path": str(tmp_path / "nope")})
    assert problems and "path" in problems[0]


DATED = """---
type: application
title: X
released: 2024-05-01
---
body
"""


def test_frontmatter_with_bare_date_does_not_crash(tmp_path: Path):
    # PyYAML parses an unquoted ISO date into a Python date; content_hash must
    # tolerate it (it would otherwise crash json.dumps in normalize).
    _write(tmp_path, "apps/x.md", DATED)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    docs = conn.kbforge_normalize(result.records)
    assert docs[0].anchor.content_hash  # computed without crashing
    assert "released" in docs[0].structured
    assert_stability(conn.kbforge_normalize, result.records)  # date hashes stably


MALFORMED = """---
title: Notes: Q3 Planning
---
Body text.
"""


def test_malformed_frontmatter_does_not_crash(tmp_path: Path):
    # An unquoted colon is the most common YAML mistake; one bad file must not
    # crash the whole folder's sync, and its raw YAML must not leak into the body.
    _write(tmp_path, "bad.md", MALFORMED)
    _write(tmp_path, "good.md", DOC)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    docs = conn.kbforge_normalize(result.records)  # must not raise
    assert len(docs) == 2
    bad = next(d for d in docs if d.doc_id == "local_files:bad.md")
    assert "Body text." in bad.text
    assert "Q3 Planning" not in bad.text  # unparsed frontmatter not leaked into body


def test_bom_prefixed_frontmatter_is_parsed(tmp_path: Path):
    # A UTF-8 BOM (Windows editors) must not defeat frontmatter parsing.
    raw = b"\xef\xbb\xbf---\ntitle: BOMTitle\n---\n\nBody.\n"
    (tmp_path / "b.md").write_bytes(raw)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    docs = conn.kbforge_normalize(result.records)
    assert docs[0].title == "BOMTitle"  # parsed, not filename fallback
    assert "---" not in docs[0].text  # raw frontmatter not leaked into body


def test_reserved_okf_keys_never_become_facets(tmp_path: Path):
    # Source frontmatter named like emit-side OKF fields must not leak into facets
    # and corrupt the rendered frontmatter (projection↔files divergence).
    _write(tmp_path, "a.md", "---\ntitle: A\nlinks: nope\nresource: nope\n---\nbody\n")
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    docs = conn.kbforge_normalize(result.records)
    assert "links" not in docs[0].structured
    assert "resource" not in docs[0].structured


def _ids(records) -> set[str]:
    return {r.anchor_hint["native_id"] for r in records}


def test_default_ignores_exclude_vendored_and_cache_dirs(tmp_path: Path):
    # Pointed at a repo root, rglob must not sweep dependency/cache dirs into the KB:
    # .venv site-packages docs were 74% of a real live test. Defaults apply with no
    # config at all.
    _write(tmp_path, "docs/real.md", DOC)
    _write(tmp_path, ".venv/lib/site-packages/pkg/README.md", DOC)
    _write(tmp_path, "node_modules/dep/README.md", DOC)
    _write(tmp_path, ".pytest_cache/README.md", DOC)
    _write(tmp_path, "__pycache__/notes.md", DOC)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    assert _ids(result.records) == {"docs/real.md"}  # only the real doc survives


def test_ignore_globs_is_additive_to_defaults(tmp_path: Path):
    # A user-supplied pattern ADDS to the defaults; it must not silently re-enable
    # .venv by replacing the default set (the failure mode for a repo with both).
    _write(tmp_path, "keep.md", DOC)
    _write(tmp_path, "drafts/wip.md", DOC)
    _write(tmp_path, ".venv/pkg/README.md", DOC)
    conn = LocalFilesConnector()
    cfg = {"path": str(tmp_path), "ignore_globs": ["drafts"]}
    result = conn.kbforge_fetch(cfg, None)
    assert _ids(result.records) == {"keep.md"}  # drafts AND default .venv both gone


def test_ignore_glob_matches_filename_pattern(tmp_path: Path):
    # A glob (not just a bare dir name) filters by filename too.
    _write(tmp_path, "notes.md", DOC)
    _write(tmp_path, "_draft-notes.md", DOC)
    conn = LocalFilesConnector()
    cfg = {"path": str(tmp_path), "ignore_globs": ["_draft*"]}
    result = conn.kbforge_fetch(cfg, None)
    assert _ids(result.records) == {"notes.md"}


def test_validate_config_rejects_non_list_ignore_globs(tmp_path: Path):
    conn = LocalFilesConnector()
    problems = conn.kbforge_validate_config(
        {"path": str(tmp_path), "ignore_globs": ".venv"}  # str, not list
    )
    assert problems and "ignore_globs" in problems[0]
