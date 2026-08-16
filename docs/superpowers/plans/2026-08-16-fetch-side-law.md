# Fetch-Side Law Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a core fetch-side validator that makes `FetchResult.complete` load-bearing, rejects colliding `doc_id`s, and refuses records that cannot be cited — plus the emit-side check that catches the same defect's worst symptom.

**Architecture:** One new law (`assert_fetch_contract`) in `canonical.py` beside `assert_stability`, called from `pipeline.run` between `normalize` and `diff`. One new check inside the existing `_check_projection_coherence` binding `files` and `files_removed` as disjoint. Both are core and unconditional — an opt-in trust guarantee is not a guarantee. No new pipeline stage, no new plugin family.

**Tech Stack:** Python 3.12+, Pydantic v2, pluggy, pytest, uv, ruff + ty via prek.

**Spec:** `docs/design/2026-08-16-fetch-side-law-design.md`

## Global Constraints

- **Pipeline order is fixed.** The law is a validator between two existing stages, in the same position and of the same kind as `assert_stability`. Do not add a stage.
- **The law is core and unconditional.** No flag, no per-connector opt-out.
- **`normalize` stays pure** — no network, no clock, no randomness. Nothing in this plan may introduce one.
- **The default suite never touches the network.** Every test here is offline.
- **Verify a gate by breaking what it guards.** Mutate **in place**, restore with `git checkout --`. Never `cp -R` the repo — the copied `.venv` resolves `kbforge` to the original source, so nothing is actually mutated and everything passes spuriously.
- **Assert on failure *messages*, not just exception types.** A test asserting only `pytest.raises(FetchContractError)` passes on whichever of three checks fired.
- **Exact messages** (copied verbatim from spec §2.1):
  - `duplicate doc_id in fetch output: {doc_id}`
  - `record has no native_id: doc_id={doc_id}`
  - `incomplete fetch cannot emit a tombstone: {doc_id}`
- **Commands:** `uv run pytest` (full suite), `uvx prek run --all-files` (ruff + ty).
- **Baseline:** 275 passed, 6 skipped. The suite must never end below this.

---

### Task 1: `assert_fetch_contract` and its blankness helper

`_blank` currently lives in `validate.py`, but the new law needs the same
Unicode-aware semantics. Move it down to `canonical.py` (which imports only
`models` + stdlib, so there is no cycle) and have `validate.py` import it.

**Files:**
- Modify: `src/kbforge/canonical.py` (add `is_blank`, `FetchContractError`, `assert_fetch_contract`)
- Modify: `src/kbforge/validate.py:12,26-37` (remove `_blank` and the now-unused `unicodedata` import; import `is_blank`)
- Test: `tests/test_canonical.py`

**Interfaces:**
- Consumes: `CanonicalDocument` from `kbforge.models`
- Produces:
  - `kbforge.canonical.is_blank(value: object) -> bool`
  - `kbforge.canonical.FetchContractError(RuntimeError)`
  - `kbforge.canonical.assert_fetch_contract(docs: Sequence[CanonicalDocument], *, complete: bool) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_canonical.py`:

```python
def _fdoc(doc_id="sys:a.md", native_id="a.md", deleted=False):
    """A minimal CanonicalDocument for fetch-contract tests."""
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system="sys",
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="h",
        ),
        doc_id=doc_id,
        title="A",
        text="A",
        deleted=deleted,
    )


def test_fetch_contract_accepts_a_well_formed_complete_fetch():
    assert_fetch_contract(
        [_fdoc("sys:a.md", "a.md"), _fdoc("sys:b.md", "b.md")], complete=True
    )


def test_fetch_contract_rejects_a_duplicate_doc_id():
    """Two records sharing an id both land in ChangeSet.added, then assemble
    collapses them onto one concept_path with last-write-wins: one document is
    silently absent from the KB and nothing downstream looks broken."""
    docs = [_fdoc("sys:a.md", "a.md"), _fdoc("sys:a.md", "a.md")]
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract(docs, complete=True)
    assert str(exc.value) == "duplicate doc_id in fetch output: sys:a.md"


def test_fetch_contract_rejects_a_blank_native_id():
    """The fetch-side mirror of the §4.4 anchor-presence law: a record with no
    native_id cannot be cited, so a reviewer cannot follow it to its source."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "")], complete=True)
    assert str(exc.value) == "record has no native_id: doc_id=sys:a.md"


def test_fetch_contract_rejects_a_zero_width_native_id():
    """Blankness is judged on visible content, not str.strip(): U+200B is `Cf`,
    survives strip(), and is no more citable than an empty string."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "​")], complete=True)
    assert str(exc.value) == "record has no native_id: doc_id=sys:a.md"


def test_fetch_contract_rejects_a_tombstone_from_an_incomplete_fetch():
    """complete=False means the connector saw a partial slice of the source, so
    absence is not evidence of deletion. This is the check that makes
    FetchResult.complete load-bearing rather than decorative."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "a.md", deleted=True)], complete=False)
    assert str(exc.value) == "incomplete fetch cannot emit a tombstone: sys:a.md"


def test_fetch_contract_allows_a_tombstone_from_a_complete_fetch():
    assert_fetch_contract([_fdoc("sys:a.md", "a.md", deleted=True)], complete=True)
```

Add whatever of these imports `tests/test_canonical.py` is missing at the top:

```python
from datetime import UTC, datetime

import pytest

from kbforge.canonical import (
    FetchContractError,
    assert_fetch_contract,
)
from kbforge.models import CanonicalDocument, ResourceAnchor
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_canonical.py -v`
Expected: FAIL — `ImportError: cannot import name 'FetchContractError' from 'kbforge.canonical'`

- [ ] **Step 3: Move `is_blank` into `canonical.py`**

In `src/kbforge/canonical.py`, add `import unicodedata` to the stdlib imports and insert after the `StabilityError` class:

```python
def is_blank(value: object) -> bool:
    """True when `value` is not a string, or is a string carrying no content.

    `str.strip()` removes NBSP (it is `Zs`) but not U+200B and friends, which are
    `Cf` — invisible, zero-width, and routinely present in text pasted out of a
    browser. A concept whose `type` is a zero-width space is untyped in every way
    that matters, so blankness has to be judged on visible content."""
    if not isinstance(value, str):
        return True
    return not "".join(
        ch for ch in value if unicodedata.category(ch) not in ("Cf", "Zs", "Cc")
    ).strip()
```

In `src/kbforge/validate.py`: delete the `_blank` definition (lines 26-37) and the
`import unicodedata` line, then add to the kbforge imports:

```python
from kbforge.canonical import is_blank as _blank
```

Keeping the local alias means the five existing call sites (lines 102, 261, 331,
371, 403) do not change — one definition of blankness, zero call-site churn.

- [ ] **Step 4: Add the error and the law**

Append to `src/kbforge/canonical.py`:

```python
class FetchContractError(RuntimeError):
    """A connector's fetch output violates the fetch-side contract."""


def assert_fetch_contract(
    docs: Sequence[CanonicalDocument], *, complete: bool
) -> None:
    """Fetch-side law: what a connector hands the mirror must be identifiable,
    and honest about its own coverage.

    Runs on normalize output rather than on RawRecords for two reasons: `doc_id`
    is what the mirror keys on, and tombstones only exist post-normalize —
    RawRecord has no `deleted` field.

    - **Unique `doc_id`.** Two records sharing an id both land in
      `ChangeSet.added` (diff never mutates, so `prev is None` twice), and
      `synthesize.assemble` then collapses them onto one `concept_path` with
      last-write-wins. Mirror and bundle agree afterwards, so nothing looks
      broken: one document is simply absent from the knowledge base.
    - **Non-blank `native_id`,** or the document cannot be cited — the fetch-side
      mirror of the §4.4 anchor-presence law.
    - **No tombstone from an incomplete fetch.** `complete=False` means the
      connector saw a partial slice, so absence is not evidence of deletion.

    Deliberately NOT checked: that content is verbatim. Core has no independent
    access to the source, so it cannot tell a returned document from an agent's
    summary of one. This closes the identity half of retriever-not-extractor;
    the verbatim half stays contract.

    Also not checked: that `normalize` is clock-free. `assert_stability` compares
    `content_hash`, which excludes the anchor by design, so a `datetime.now()`
    inside normalize hashes identically on both passes and passes that gate."""
    seen: set[str] = set()
    for doc in docs:
        if doc.doc_id in seen:
            raise FetchContractError(f"duplicate doc_id in fetch output: {doc.doc_id}")
        seen.add(doc.doc_id)
        if is_blank(doc.anchor.native_id):
            raise FetchContractError(f"record has no native_id: doc_id={doc.doc_id}")
        if not complete and doc.deleted:
            raise FetchContractError(
                f"incomplete fetch cannot emit a tombstone: {doc.doc_id}"
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_canonical.py tests/test_validate.py tests/test_strict_okf.py -v`
Expected: PASS — the validate suites confirm the `_blank` move changed no behavior.

- [ ] **Step 6: Run the full suite and linters**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: 281 passed, 6 skipped (275 + 6 new). All three hooks pass.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/canonical.py src/kbforge/validate.py tests/test_canonical.py
git commit -m "feat: add the fetch-side contract law

Three checks over normalize output: unique doc_id, non-blank native_id,
and no tombstone from an incomplete fetch. Not yet wired into the
pipeline. Moves _blank down to canonical as is_blank so one definition
of blankness serves both the fetch-side and emit-side laws."
```

---

### Task 2: Wire the law into the pipeline

**Files:**
- Modify: `src/kbforge/pipeline.py:106-109`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `assert_fetch_contract`, `FetchContractError` from Task 1
- Produces: no new symbols; `pipeline.run` now raises `FetchContractError` on a contract violation

- [ ] **Step 1: Give `_FakeConnector` a `complete` flag**

In `tests/test_pipeline.py`, modify `_FakeConnector` (around line 215) so a test
can produce an incomplete fetch:

```python
class _FakeConnector:
    """Returns a fixed list of CanonicalDocuments, deterministically — satisfies
    assert_stability without a clock or any real I/O."""

    def __init__(self, docs: list[CanonicalDocument], complete: bool = True):
        self._docs = docs
        self._complete = complete

    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="fake", version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config: dict) -> list[str]:
        return []

    def kbforge_fetch(self, config: dict, cursor) -> FetchResult:
        return FetchResult(
            records=[], cursor=Cursor(connector="fake"), complete=self._complete
        )

    def kbforge_normalize(self, records) -> list[CanonicalDocument]:
        return self._docs
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_pipeline.py`:

```python
def test_pipeline_rejects_a_duplicate_doc_id_before_it_reaches_the_mirror(tmp_path):
    """Without the law this run publishes happily and silently drops a document:
    diff appends the id to `added` twice, assemble collapses both onto one
    concept_path, and mirror and bundle agree afterwards."""
    docs = [_doc("a.md", "First"), _doc("a.md", "Second")]
    with pytest.raises(FetchContractError) as exc:
        run(
            _FakeConnector(docs),
            _RecordingPublisher(),
            config={},
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={},
        )
    assert str(exc.value) == "duplicate doc_id in fetch output: sys:a.md"


def test_pipeline_rejects_a_tombstone_from_an_incomplete_fetch(tmp_path):
    """The invariant CLAUDE.md states but nothing enforced: a rate-limited
    partial fetch must not be able to manufacture a removal."""
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    with pytest.raises(FetchContractError) as exc:
        run(
            _FakeConnector([_doc("gone.md", "Gone", deleted=True)], complete=False),
            _RecordingPublisher(),
            config={},
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={},
        )
    assert str(exc.value) == "incomplete fetch cannot emit a tombstone: sys:gone.md"


def test_pipeline_still_accepts_a_tombstone_from_a_complete_fetch(tmp_path):
    """The guard must not break ordinary deletion propagation."""
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])
    assert publisher.last_change is not None
    assert publisher.last_change.files_removed == ["concepts/gone/overview.md"]
```

Add to the imports at the top of `tests/test_pipeline.py`:

```python
from kbforge.canonical import FetchContractError
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -k "duplicate_doc_id or incomplete_fetch" -v`
Expected: FAIL — `DID NOT RAISE <class 'kbforge.canonical.FetchContractError'>`. The
duplicate case publishes successfully, which *is* the defect.

- [ ] **Step 4: Add the call site**

In `src/kbforge/pipeline.py`, add `assert_fetch_contract` to the `kbforge.canonical`
import, then insert one line after the existing stability law:

```python
    result = connector.kbforge_fetch(config, _load_cursor(state_path, info.name))
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
    assert_fetch_contract(docs, complete=result.complete)  # §4.3, fetch side

    changeset = diff(mirror_path, docs)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS, including every pre-existing tombstone test.

- [ ] **Step 6: Verify the shipped connectors still pass**

Run: `uv run pytest tests/test_local_files_connector.py tests/test_git_commits_connector.py tests/test_cli.py -v`
Expected: PASS. `local_files` keys `doc_id` on `rel` from `sorted(root.rglob("*.md"))`
(injective) and `git_commits` on `%H` within one `git log` range (likewise); both
return the default `complete=True`; neither emits a tombstone.

- [ ] **Step 7: Mutation-verify the gate has teeth**

Delete the call site **in place**, confirm a test fails, then restore:

```bash
# Comment out the assert_fetch_contract line in src/kbforge/pipeline.py, then:
uv run pytest tests/test_pipeline.py -k "duplicate_doc_id or incomplete_fetch" -q
# EXPECT: 2 failed. If 0 failed, the tests do not exercise the call site.
git checkout -- src/kbforge/pipeline.py
uv run pytest -q  # back to green
```

Record the observed failure count in the commit message. Do **not** perform this
in a `cp -R` of the repo: the copied `.venv` resolves `kbforge` to the original
source, so the mutation would not apply and the check would pass spuriously.

- [ ] **Step 8: Run the full suite and linters**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: 284 passed, 6 skipped. All hooks pass.

- [ ] **Step 9: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline.py
git commit -m "feat: enforce the fetch-side contract in the pipeline

Called between normalize and diff, in the same position and of the same
kind as assert_stability -- a validator between existing stages, not a
new stage.

This makes FetchResult.complete load-bearing for the first time. It was
defined but unconsumed, and nothing derived deletions from absence, so
the documented 'a partial fetch cannot manufacture removals' invariant
held only vacuously.

Mutation-verified: commenting out the call site fails 2 tests."
```

---

### Task 3: Bind `files` and `files_removed` as disjoint

The law in Task 1 prevents the connector-side cause. This catches the emit-side
symptom regardless of cause: a proposal that both writes and deletes one path is
self-contradictory, and no validator currently inspects `files_removed` at all.

**Files:**
- Modify: `src/kbforge/validate.py:71-98` (`_check_projection_coherence`)
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `Failure`, `ProposedChange` (both already in `validate.py`)
- Produces: no new symbols; `_check_projection_coherence` gains a third failure mode under the existing `projection-coherence` slug

- [ ] **Step 1: Write the failing test**

Append to `tests/test_validate.py`:

```python
def test_a_path_both_written_and_removed_is_a_failure():
    """A proposal that adds and deletes one path is self-contradictory, and
    nothing caught it: _check_projection_coherence bound files<->concepts and
    never inspected files_removed, so it returned [] on this proposal."""
    path = "apps/x/overview.md"
    concept = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
    change = ProposedChange(
        branch_hint="b",
        files={path: "# X"},
        files_removed=[path],
        concepts={path: concept},
    )
    failures = run_artifact_validators(change)
    coherence = [f for f in failures if f.law == "projection-coherence"]
    assert len(coherence) == 1
    assert coherence[0].concept_path == path
    assert (
        coherence[0].message
        == "path is both written and removed in one proposal (§4.4 gate)"
    )
```

This uses only fixtures `tests/test_validate.py` already defines at module level
(`ANCHOR` line 7, `NOW` line 6) and needs no new imports.

Two things to get right, both of which a naive version gets wrong:

- Call **`run_artifact_validators`**, not `run_validators`. `_check_projection_coherence`
  runs inside the former; `run_validators` additionally runs `_check_strict_okf`
  over the *rendered file text*, and `"# X"` carries no frontmatter, so that
  route would bury the finding under unrelated strict-OKF failures. Every
  existing test in this file uses `run_artifact_validators` for the same reason.
- Assert `len(coherence) == 1`, not `>= 1`. The path appears in `files`, in
  `concepts`, and in `files_removed`, so the two pre-existing bindings are
  satisfied and only the new one should fire. If this is 2 or 3, the new check
  is double-counting against the existing loops.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_validate.py -k "both_written_and_removed" -v`
Expected: FAIL — `assert 0 == 1`, because no coherence failure is produced.

- [ ] **Step 3: Add the check**

In `src/kbforge/validate.py`, inside `_check_projection_coherence`, insert before
`return failures`:

```python
    # Nothing else inspects files_removed. A path in both sets is a proposal
    # that adds and deletes the same concept: the publisher's behaviour then
    # depends on the order it applies them, which is not a decision a proposal
    # gets to leave open. Reachable from a duplicate doc_id where one copy is
    # tombstoned -- the fetch-side law now rejects that at source, but this
    # binds the symptom regardless of cause.
    for path in sorted(set(proposal.files) & set(proposal.files_removed)):
        failures.append(
            Failure(
                path,
                "projection-coherence",
                "path is both written and removed in one proposal (§4.4 gate)",
            )
        )
```

Also extend the function's docstring with a sentence naming the third binding, so
the docstring still describes everything the function does.

Note: the spec's §3 writes this message as `path is both written and removed:
{path}`. The `{path}` interpolation is dropped because `Failure.concept_path`
already carries it and no sibling message repeats it.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_validate.py -v`
Expected: PASS, with no pre-existing coherence test disturbed.

- [ ] **Step 5: Mutation-verify the check has teeth**

```bash
# Comment out the new `for path in sorted(set(proposal.files) & ...)` loop
# in src/kbforge/validate.py, then:
uv run pytest tests/test_validate.py -k "both_written_and_removed" -q
# EXPECT: 1 failed.
git checkout -- src/kbforge/validate.py
uv run pytest -q  # back to green
```

- [ ] **Step 6: Run the full suite and linters**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: 285 passed, 6 skipped. All hooks pass.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/validate.py tests/test_validate.py
git commit -m "fix: reject a proposal that both writes and removes a path

_check_projection_coherence bound files<->concepts and never looked at
files_removed, so a proposal carrying one path in both sets passed with
run_validators() == []. Reachable from a duplicate doc_id where one copy
is tombstoned: the id lands in changeset.removed and in added, so the
same concept_path reaches the publisher as both a write and a delete.

The fetch-side law now rejects that at source; this binds the symptom
regardless of cause.

Mutation-verified: removing the check fails 1 test."
```

---

### Task 4: Surface both fetch-side errors as messages, not tracebacks

`StabilityError` already escapes `main()` as a traceback. Since this release adds
a second error of the same kind — and a new failure mode for existing third-party
connectors — both get caught.

**Files:**
- Modify: `src/kbforge/__main__.py:191-203`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `FetchContractError` (Task 1), `StabilityError` (existing)
- Produces: no new symbols; `main()` returns exit code 2 with a message on either error

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`, following that file's existing pattern for invoking
`main()` with argv and capturing output:

```python
def test_cli_reports_a_fetch_contract_violation_as_a_message(tmp_path, capsys):
    """A third-party connector tripping the new law is the only thing most
    plugin authors will see of this release; a traceback is the wrong first
    impression."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# A\n\nbody\n", "utf-8")

    with mock.patch(
        "kbforge.pipeline.assert_fetch_contract",
        side_effect=FetchContractError("duplicate doc_id in fetch output: sys:a.md"),
    ):
        code = main(
            [
                "run",
                "--connector", "local_files",
                "--set", f"path={src}",
                "--mirror", str(tmp_path / "mirror"),
                "--out", str(tmp_path / "out"),
                "--state", str(tmp_path / "state"),
            ]
        )

    assert code == 2
    assert "duplicate doc_id in fetch output: sys:a.md" in capsys.readouterr().out
```

Add to the imports at the top of `tests/test_cli.py`:

```python
from unittest import mock

from kbforge.canonical import FetchContractError
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_cli.py -k "fetch_contract_violation" -v`
Expected: FAIL — `FetchContractError` propagates out of `main()` instead of
returning 2.

- [ ] **Step 3: Catch both errors**

In `src/kbforge/__main__.py`, add to the kbforge imports:

```python
from kbforge.canonical import FetchContractError, StabilityError
```

and insert a new handler after the existing `ConfigError` clause:

```python
    except (FetchContractError, StabilityError) as exc:
        # A connector-contract violation, not an operator mistake — but the
        # operator is who sees it, and a traceback tells them nothing about
        # which plugin to report it against. StabilityError is caught here too:
        # it has always escaped as a traceback, and fixing the surfacing only
        # for the newer law would leave the older one worse for no reason.
        print(f"Connector contract violation: {exc}")
        return 2
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and linters**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: 286 passed, 6 skipped. All hooks pass.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/__main__.py tests/test_cli.py
git commit -m "fix: report connector-contract violations as messages

main() caught ConfigError, PublishError and PathError; StabilityError has
always escaped as a traceback. The new fetch-side law is a new failure
mode for existing third-party connectors, so both are now reported with
exit 2 -- a traceback does not tell an operator which plugin to report."
```

---

### Task 5: Release housekeeping and doc fold

**Files:**
- Modify: `pyproject.toml:3`
- Modify: `CHANGELOG.md`
- Modify: `docs/architecture.md` (§4.2, §7 line 689, §4.4)
- Delete: `docs/design/2026-08-16-fetch-side-law-design.md`
- Modify: `docs/design/2026-08-16-mcp-source-connector-design.md` (its "Depends on" link)

- [ ] **Step 1: Bump the version**

In `pyproject.toml`, set `version = "0.6.0"`. Minor, not patch: a third-party
connector emitting duplicate `doc_id`s now fails loudly where it previously
dropped a document silently. Desirable direction, still a new failure mode.

- [ ] **Step 2: Cut the CHANGELOG entry**

Move the `[Unreleased]` content into a `## [0.6.0] - 2026-08-16` section and add
the compare links at the bottom, matching the file's existing format. Content:

```markdown
### Added
- A fetch-side law (`assert_fetch_contract`) run between `normalize` and `diff`:
  `doc_id` must be unique, `native_id` must be non-blank, and an incomplete fetch
  (`FetchResult.complete=False`) may not carry a tombstone. This makes `complete`
  load-bearing for the first time — it was defined but unconsumed, so the
  documented "a partial fetch cannot manufacture removals" invariant previously
  held only because nothing derived removals from absence at all.

### Fixed
- A proposal carrying one path in both `files` and `files_removed` passed
  validation. `_check_projection_coherence` bound `files`↔`concepts` and never
  inspected `files_removed`, so a duplicate `doc_id` where one copy was
  tombstoned reached the publisher as both a write and a delete with
  `run_validators() == []`.
- `StabilityError` and the new `FetchContractError` are reported as messages with
  exit 2 rather than escaping `main()` as a traceback.

### Changed
- **Breaking for connector plugins:** a connector emitting duplicate `doc_id`s,
  a blank `native_id`, or a tombstone on an incomplete fetch now fails the run.
  Both in-tree connectors are unaffected.
```

- [ ] **Step 3: Fold the spec into `architecture.md`**

Three edits, per spec §6:

1. **§4.2** — add: `FetchResult.complete` is enforced; an incomplete fetch may not
   carry a tombstone.
2. **§7, line 689** — the pseudocode currently reads
   `changeset = mirror_and_diff(mirror, docs, result.complete)`, describing a
   consumer the code does not have. Replace with the real sequence:

   ```
   docs = normalize(records)
   assert_stability(normalize, records)          # §4.3 law 1
   assert_fetch_contract(docs, complete=result.complete)
   changeset = diff(mirror, docs)
   ```

   so the document names one consumer of `complete` rather than two.
3. **§4.4** — note the third `projection-coherence` binding (`files` and
   `files_removed` are disjoint) alongside the existing path-set binding.

Carry across only the *rationale* worth keeping — why the law exists, and what it
deliberately does not check (verbatim-ness; `normalize` clock-purity, which
`assert_stability` cannot catch because `content_hash` excludes the anchor). Do
not restate the code.

- [ ] **Step 4: Delete the shipped spec and fix the inbound link**

```bash
git rm docs/design/2026-08-16-fetch-side-law-design.md
```

`docs/design/` holds specs for **unbuilt** work only. In
`docs/design/2026-08-16-mcp-source-connector-design.md`, change the **Depends on**
line and the §8 phasing table so the 0.6.0 row points at `architecture.md`
instead of the deleted file.

- [ ] **Step 5: Verify nothing else links to the deleted spec**

Run: `grep -rn "2026-08-16-fetch-side-law-design" . --include='*.md'`
Expected: no output.

- [ ] **Step 6: Run the full suite and linters**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: 286 passed, 6 skipped. All hooks pass.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore: release 0.6.0

Bumps the version, cuts the CHANGELOG entry, and folds the fetch-side law
spec into architecture.md now that it has shipped -- docs/design/ holds
specs for unbuilt work only.

Corrects the §7 pseudocode, which described mirror_and_diff consuming
result.complete: the code has never done that, and now a validator does
it instead."
```

---

## Self-Review

**Spec coverage** — every section of `2026-08-16-fetch-side-law-design.md` maps to a task:

| Spec section | Task |
|---|---|
| §2 the law, placement | 1 (function), 2 (call site) |
| §2.1 three checks + fixed messages | 1 |
| §2.2 what it does not check | 1 (docstring), 5 (folded rationale) |
| §2.3 failure surfacing | 4 |
| §3 projection-coherence gap | 3 |
| §4 testing, mutation tests | 2 (step 7), 3 (step 5) |
| §5 scope, version | 5 |
| §6 architecture.md amendments | 5 |

**Type consistency** — `assert_fetch_contract(docs, *, complete)` is defined in Task 1
and called with that exact signature in Task 2 and mocked with it in Task 4.
`FetchContractError` is imported from `kbforge.canonical` in all three.
`is_blank` is public in `canonical`, aliased to `_blank` in `validate` so the five
existing call sites are untouched.

**Known deviation from spec** — the disjointness message drops the spec's `{path}`
interpolation because `Failure.concept_path` already carries it; noted inline in
Task 3 Step 3.

**Test count arithmetic** — baseline 275 → +6 (Task 1) → +3 (Task 2) → +1 (Task 3)
→ +1 (Task 4) = **286 passed, 6 skipped**. If a task's actual count differs, stop
and reconcile before continuing: it means a test did not run or a pre-existing one
broke.
