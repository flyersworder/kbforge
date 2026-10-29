"""`kbforge reset SOURCE` (architecture.md §7.5): forget one source's local
state, and nothing of any other source's."""

import builtins
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from kbforge.__main__ import main
from kbforge.chunking import ChunkRecord, write_record
from kbforge.mirror import slot_key
from kbforge.models import Cursor
from kbforge.pipeline import instance_key
from kbforge.reset import plan_reset, systems_for
from kbforge.sources_record import SourceEntry

DOC = "---\ntype: application\ntitle: {title}\n---\n{title} does things.\n"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}

A_CONFIG = {"path": "./handbook"}
A = {"connector": "local_files", "config": A_CONFIG}
C = {"connector": "git_commits", "config": {"repo": "./repo"}}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={"PATH": os.environ.get("PATH", ""), **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("hello\n", "utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "Add the file")
    return repo


def _docs(root: Path, *stems: str) -> None:
    d = root / "handbook"
    d.mkdir()
    for stem in stems:
        (d / f"{stem}.md").write_text(DOC.format(title=stem), "utf-8")


def _project(root: Path, sources: dict, **top) -> Path:
    yml = root / "kbforge.yaml"
    yml.write_text(yaml.safe_dump({**top, "sources": sources}, sort_keys=False))
    return yml


def _entries(root: Path) -> dict:
    path = root / ".kbforge" / "state" / "sources.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "run.lock"
    }


def _mirror_ids(root: Path) -> list[str]:
    mirror = root / ".kbforge" / "mirror"
    return sorted(
        json.loads(p.read_text("utf-8"))["doc_id"] for p in mirror.glob("*.json")
    )


@pytest.fixture
def at(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_reset_then_run_reproposes_only_that_source(at, capsys):
    _docs(at, "x", "y")
    _repo(at)
    yml = _project(at, {"a": A, "c": C})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()

    assert main(["reset", "a", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["a: removed 2 mirror document(s) and 1 state file(s)"]
    assert list(_entries(at)) == ["c"]
    assert [i for i in _mirror_ids(at) if i.startswith("local_files:")] == []

    assert main(["run", "--project", str(yml)]) == 0
    a_line, c_line = _lines(capsys)
    assert a_line.startswith("a  Published: "), a_line
    assert c_line == "c  NoOp: no change detected; no MR opened."
    assert list(_entries(at)) == ["c", "a"]


def test_reset_leaves_other_sources_mirror_slots_byte_identical(at, capsys):
    _docs(at, "x", "y")
    _repo(at)
    yml = _project(
        at,
        {"a": A, "c": C},
        links={"links": {"local_files:x.md": ["local_files:y.md"]}},
    )
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    root = at / ".kbforge"
    # The fixture must keep exercising a sidecar of the reset source.
    assert list((root / "mirror" / "_links").glob("*.json"))

    a_keys = {slot_key("local_files:x.md"), slot_key("local_files:y.md")}
    a_cursor = f"cursor-local_files-{instance_key(A_CONFIG)}.json"

    def is_a(path: str) -> bool:
        p = Path(path)
        return p.stem in a_keys or p.name == a_cursor

    before = _snapshot(root)
    assert any(is_a(p) for p in before)
    assert main(["reset", "a", "--yes", "--project", str(yml)]) == 0
    capsys.readouterr()
    after = _snapshot(root)

    sources = "state/sources.json"
    expected = {p: b for p, b in before.items() if not is_a(p) and p != sources}
    assert {p: b for p, b in after.items() if p != sources} == expected
    assert list(json.loads(after[sources])) == ["c"]


def test_reset_mid_chunking_removes_the_chunk_slot_and_admitted_docs(at, capsys):
    _docs(at, "x", "y", "z")
    yml = _project(at, {"a": {**A, "chunking": {"max_concepts": 1}}})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    state = at / ".kbforge" / "state"
    digest = instance_key(A_CONFIG)
    assert sorted(p.name for p in state.glob("c*.json")) == [
        f"chunk-local_files-{digest}.json"
    ]
    assert _mirror_ids(at) == ["local_files:x.md"]

    assert main(["reset", "a", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["a: removed 1 mirror document(s) and 1 state file(s)"]
    assert list(state.glob("c*.json")) == []
    assert _mirror_ids(at) == []
    assert _entries(at) == {}

    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[0].startswith("a  Published: ")
    assert _mirror_ids(at) == ["local_files:x.md"]


def test_reset_a_stale_name_works_from_its_entry(at, capsys):
    _docs(at, "x")
    _repo(at)
    yml = _project(at, {"a": A, "gone": C})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    _project(at, {"a": A})
    state = at / ".kbforge" / "state"
    assert len(list(state.glob("cursor-git_commits-*.json"))) == 1

    assert main(["reset", "gone", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["gone: removed 1 mirror document(s) and 1 state file(s)"]
    assert list(state.glob("cursor-git_commits-*.json")) == []
    assert _mirror_ids(at) == ["local_files:x.md"]
    assert list(_entries(at)) == ["a"]

    assert main(["reset", "gone", "--yes", "--project", str(yml)]) == 2
    assert _lines(capsys) == ["unknown source 'gone'; known: a"]


@pytest.mark.parametrize(
    ("answer", "printed"),
    [
        (EOFError, ["", "Reset cancelled."]),
        ("n", ["Reset cancelled."]),
        ("", ["Reset cancelled."]),
        ("yess", ["Reset cancelled."]),
    ],
)
def test_reset_without_yes_on_eof_cancels(at, monkeypatch, capsys, answer, printed):
    _docs(at, "x")
    yml = _project(at, {"a": A})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr(builtins, "input", fake_input)
    before = _snapshot(at / ".kbforge")
    assert main(["reset", "a", "--project", str(yml)]) == 0
    assert prompts == [
        "Reset a: remove 1 mirror document(s) and 1 state file(s)? [y/N] "
    ]
    assert _lines(capsys) == printed
    assert _snapshot(at / ".kbforge") == before


@pytest.mark.parametrize("answer", ["y", " YES ", "Y\n"])
def test_reset_proceeds_on_yes(at, monkeypatch, capsys, answer):
    _docs(at, "x")
    yml = _project(at, {"a": A})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    assert main(["reset", "a", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["a: removed 1 mirror document(s) and 1 state file(s)"]
    assert _mirror_ids(at) == []


def test_reset_with_nothing_to_reset(at, capsys):
    _docs(at, "x")
    yml = _project(at, {"a": A})
    assert main(["reset", "a", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["a: nothing to reset"]
    assert main(["reset", "nope", "--yes", "--project", str(yml)]) == 2
    assert _lines(capsys) == ["unknown source 'nope'; known: a"]


def test_reset_with_unknown_systems_removes_state_and_leaves_the_mirror(at, capsys):
    """State from before sources.json, whose cursor's systems were dropped on
    the legacy read-through: nothing says which mirror slots are its."""
    _docs(at, "x")
    yml = _project(at, {"a": A})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    state = at / ".kbforge" / "state"
    (state / "sources.json").unlink()
    [cursor] = state.glob("cursor-*.json")
    data = json.loads(cursor.read_text("utf-8"))
    cursor.write_text(json.dumps({**data, "systems": []}), "utf-8")

    assert main(["reset", "a", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "a: systems unknown: mirror left as is",
        "a: removed 0 mirror document(s) and 1 state file(s)",
    ]
    assert not cursor.exists()
    assert _mirror_ids(at) == ["local_files:x.md"]


def test_reset_never_touches_the_legacy_shared_cursor(at, capsys):
    _docs(at, "x")
    yml = _project(at, {"a": A})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    legacy = at / ".kbforge" / "state" / "cursor-local_files.json"
    legacy.write_text(Cursor(connector="local_files").model_dump_json(), "utf-8")
    assert main(["reset", "a", "--yes", "--project", str(yml)]) == 0
    assert legacy.exists()


def test_reset_help_states_the_closed_deletion_limit(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["reset", "-h"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "closed unmerged" in out
    assert "never re-proposes it" in out


def test_systems_for_prefers_the_entry_then_the_slots(tmp_path):
    cursor_slot = tmp_path / "cursor.json"
    chunk_slot = tmp_path / "chunk.json"
    assert systems_for(None, cursor_slot, chunk_slot) == frozenset()

    cursor_slot.write_text(
        Cursor(connector="mcp", systems=["wiki"]).model_dump_json(), "utf-8"
    )
    write_record(
        chunk_slot,
        ChunkRecord(
            branch_hints=[],
            pending=True,
            admitted=["notes:a:b", "wiki:x"],
            mirror={},
            cursor=None,
        ),
    )
    assert systems_for(None, cursor_slot, chunk_slot) == frozenset({"wiki", "notes"})

    entry = SourceEntry(connector="mcp", digest="d", systems=["only"])
    assert systems_for(entry, cursor_slot, chunk_slot) == frozenset({"only"})

    # Unreadable state contributes nothing; reset still removes the slot.
    chunk_slot.write_text("{torn", "utf-8")
    assert systems_for(None, cursor_slot, chunk_slot) == frozenset({"wiki"})


def test_plan_reset_with_no_systems_selects_no_mirror_doc(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "cursor-mcp-d1.json").write_text("{}", "utf-8")
    (state / "chunk-mcp-d2.json").write_text("{}", "utf-8")
    (state / "cursor-mcp-other.json").write_text("{}", "utf-8")
    (state / "cursor-mcp.json").write_text("{}", "utf-8")
    rp = plan_reset(
        tmp_path / "mirror",
        state,
        connector="mcp",
        digests={"d1", "d2"},
        systems=frozenset(),
    )
    assert [p.name for p in rp.state_files] == [
        "cursor-mcp-d1.json",
        "chunk-mcp-d2.json",
    ]
    assert rp.doc_ids == []


@pytest.mark.parametrize("argv", [["reset", "a", "--yes"], ["redo", "a"]])
def test_reset_and_redo_exit_3_while_the_mirror_is_locked(at, capsys, argv):
    from kbforge.lock import lock_path, mirror_lock

    _docs(at, "x")
    yml = _project(at, {"a": {**A, "chunking": {"max_concepts": 1}}})
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    mirror = at / ".kbforge" / "mirror"
    before = _snapshot(at / ".kbforge")
    with mirror_lock(mirror):
        assert main([*argv, "--project", str(yml)]) == 3
    assert _lines(capsys) == [
        f"Locked: another kbforge run holds {lock_path(mirror)}; "
        "nothing was read or written."
    ]
    assert _snapshot(at / ".kbforge") == before


def _without(snapshot: dict[str, bytes], *names: str) -> dict[str, bytes]:
    return {p: b for p, b in snapshot.items() if Path(p).name not in names}


def test_resetting_a_renamed_sources_old_name_keeps_the_new_ones_state(at, capsys):
    """`old` renamed to `new`, same config: both entries name one digest and
    one system, and both belong to `new` now (stale names own nothing)."""
    _docs(at, "x")
    chunked = {**A, "chunking": {"max_concepts": 1}}
    yml = _project(at, {"old": chunked})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": chunked})
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[-1] == "new  NoOp: no change detected; no MR opened."
    assert list(_entries(at)) == ["old", "new"]
    root = at / ".kbforge"
    before = _snapshot(root)
    assert [p for p in before if Path(p).name.startswith("cursor-")]
    assert [p for p in before if Path(p).name.startswith("chunk-")]

    assert main(["reset", "old", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "old: system 'local_files' belongs to source 'new'; "
        "its documents were left in place",
        "old: state slots shared with source 'new' were left in place",
        "old: removed 0 mirror document(s) and 0 state file(s)",
    ]
    after = _snapshot(root)
    assert _without(after, "sources.json") == _without(before, "sources.json")
    assert list(_entries(at)) == ["new"]

    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["new  NoOp: no change detected; no MR opened."]


def test_resetting_a_stale_name_spares_the_system_it_passed_on(at, capsys):
    """`old` on ./handbook leaves the file and `new` takes system local_files
    on ./other: old's slots are its own, its system's documents are new's."""
    _docs(at, "x")
    other = at / "other"
    other.mkdir()
    (other / "y.md").write_text(DOC.format(title="y"), "utf-8")
    new = {
        "connector": "local_files",
        "config": {"path": "./other"},
        "chunking": {"max_concepts": 1},
    }
    yml = _project(at, {"old": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": new})
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[-1].startswith("new  Published: ")
    root = at / ".kbforge"
    old_cursor = f"cursor-local_files-{instance_key(A_CONFIG)}.json"
    before = _snapshot(root)
    assert f"state/{old_cursor}" in before
    new_chunk = f"chunk-local_files-{instance_key({'path': './other'})}.json"
    assert f"state/{new_chunk}" in before

    assert main(["reset", "old", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "old: system 'local_files' belongs to source 'new'; "
        "its documents were left in place",
        "old: removed 0 mirror document(s) and 1 state file(s)",
    ]
    after = _snapshot(root)
    # new's cursor and mirror slots, and everything else, byte-identical.
    assert _without(after, "sources.json") == _without(
        before, "sources.json", old_cursor
    )
    assert "local_files:y.md" in _mirror_ids(at)
    assert list(_entries(at)) == ["new"]

    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["new  NoOp: no change detected; no MR opened."]


def test_resetting_an_old_name_keeps_the_slot_a_source_last_ran_with(at, capsys):
    """`new` has changed config since its last run: the slot its entry records
    is still its own (`kbforge reset new` clears it), not the old name's."""
    _docs(at, "x")
    yml = _project(at, {"old": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": {"connector": "local_files", "config": {"path": "./y"}}})
    capsys.readouterr()
    root = at / ".kbforge"
    before = _snapshot(root)

    assert main(["reset", "old", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "old: system 'local_files' belongs to source 'new'; "
        "its documents were left in place",
        "old: state slots shared with source 'new' were left in place",
        "old: removed 0 mirror document(s) and 0 state file(s)",
    ]
    assert _without(_snapshot(root), "sources.json") == _without(before, "sources.json")
    assert list(_entries(at)) == ["new"]


def test_resetting_an_old_name_before_the_renamed_source_has_run(at, capsys):
    """`old` renamed to `new` with no run since: `new` has no entry yet, but
    its config resolves to old's slots, which it inherits on its next run."""
    _docs(at, "x")
    yml = _project(at, {"old": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": A})
    capsys.readouterr()
    root = at / ".kbforge"
    before = _snapshot(root)

    assert main(["reset", "old", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "old: system 'local_files' belongs to source 'new'; "
        "its documents were left in place",
        "old: state slots shared with source 'new' were left in place",
        "old: removed 0 mirror document(s) and 0 state file(s)",
    ]
    assert _without(_snapshot(root), "sources.json") == _without(before, "sources.json")
    assert _entries(at) == {}
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["new  NoOp: no change detected; no MR opened."]


@pytest.mark.parametrize("order", [["older", "old"], ["old", "older"]])
def test_resetting_two_stale_names_spares_an_unrun_renamed_source(at, capsys, order):
    """`older` on ./h1 runs, is renamed `old` on ./handbook and runs, then is
    renamed `new` (old's config) and has not run. Neither stale name's reset,
    in either order, may take new's slot's documents: nothing but the slot
    itself ties local_files to new."""
    _docs(at, "x")
    h1 = at / "h1"
    h1.mkdir()
    (h1 / "z.md").write_text(DOC.format(title="z"), "utf-8")
    yml = _project(
        at, {"older": {"connector": "local_files", "config": {"path": "./h1"}}}
    )
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"old": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": A})
    capsys.readouterr()
    root = at / ".kbforge"
    older_cursor = f"cursor-local_files-{instance_key({'path': './h1'})}.json"
    before = _snapshot(root)
    assert f"state/{older_cursor}" in before
    assert _mirror_ids(at) == ["local_files:x.md", "local_files:z.md"]

    expected = {
        "older": [
            "older: system 'local_files' belongs to source 'new'; "
            "its documents were left in place",
            "older: removed 0 mirror document(s) and 1 state file(s)",
        ],
        "old": [
            "old: system 'local_files' belongs to source 'new'; "
            "its documents were left in place",
            "old: state slots shared with source 'new' were left in place",
            "old: removed 0 mirror document(s) and 0 state file(s)",
        ],
    }
    for name in order:
        assert main(["reset", name, "--yes", "--project", str(yml)]) == 0
        assert _lines(capsys) == expected[name]

    # new's cursor and every mirror slot byte-identical; only older's own
    # cursor is gone.
    assert _without(_snapshot(root), "sources.json") == _without(
        before, "sources.json", older_cursor
    )
    assert _entries(at) == {}
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["new  NoOp: no change detected; no MR opened."]


def test_a_slot_with_unstamped_systems_is_still_its_takers(at, capsys):
    """A cursor whose systems are empty (a legacy slot read through) says
    nothing; the entry that records the slot still ties its system to it."""
    _docs(at, "x")
    yml = _project(at, {"old": A})
    assert main(["run", "--project", str(yml)]) == 0
    _project(at, {"new": A})
    capsys.readouterr()
    [cursor] = (at / ".kbforge" / "state").glob("cursor-*.json")
    data = json.loads(cursor.read_text("utf-8"))
    cursor.write_text(json.dumps({**data, "systems": []}), "utf-8")
    root = at / ".kbforge"
    before = _snapshot(root)

    assert main(["reset", "old", "--yes", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "old: system 'local_files' belongs to source 'new'; "
        "its documents were left in place",
        "old: state slots shared with source 'new' were left in place",
        "old: removed 0 mirror document(s) and 0 state file(s)",
    ]
    assert _without(_snapshot(root), "sources.json") == _without(before, "sources.json")
