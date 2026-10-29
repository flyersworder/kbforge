# Project File Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `kbforge.yaml` per knowledge base, run as one process under one mirror lock, with `init` / `plan` / `run` / `status` / `redo` / `reset` commands — the flag CLI unchanged except that it takes the lock.

**Architecture:** A project layer (`project.py` schema, `project_cli.py` commands, `sources_record.py` state) sits above an unchanged pipeline order. `pipeline.run()` gains three narrow things — an ownership check, `systems` on its results, retiring a stale chunk record — and a `plan()` twin that is the *same function body* stopped before synthesis. The flag CLI and project CLI share one assembly module so both build synthesizers, publishers and outcomes identically.

**Tech Stack:** Python 3.12, Pydantic v2, PyYAML, argparse, pytest, `fcntl` / `msvcrt`.

**Spec:** `docs/design/2026-10-29-project-file-design.md` — read it first; this plan argues from it.

## Global Constraints

- Pipeline stage order, the no-op rule, never-merge, §4.4 laws, dual-carrier checks: untouched. No project key may reach them; every project model is `extra="forbid"`.
- Flag mode (`--connector` given): output and exit codes byte-for-byte as today. Only addition: it takes the lock. `uv run pytest` must pass with **no edits to existing tests** after every task.
- Exit codes: 0 ok / 1 a source failed or aborted / 2 config or usage / **3 lock held**.
- Lock message, exact: `Locked: another kbforge run holds <path>; nothing was read or written.`
- Ownership failure, exact: law `source-ownership`, message `system '<system>' is already owned by source '<name>'`.
- Config-change note, exact: `note: config changed since the last run; starting from a fresh cursor (kbforge reset <name> clears the old state).`
- Source names: `^[a-z][a-z0-9_-]*$`.
- Slot naming stays `cursor-<connector>-<digest>.json` / `chunk-<connector>-<digest>.json`, digest over config **as written**.
- `plan` writes nothing and calls no model. Secrets only via `*_env` keys.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Mutation-check every new gate **in place**, restore with `git checkout --` only after the real fix is committed; assert on messages, not just slugs.

## Review Focus

1. **Renaming a source** (same connector + config, new name) must keep its cursor and must not be blocked by its old name's `sources.json` entry — ownership only counts names present in the current file. Test in Task 6.
2. **Removing a source from the file** leaves its `sources.json` entry; `status` lists it as stale and `reset <old-name>` must still work from the entry alone. Tests in Tasks 8 and 9.
3. **`plan` with a forge publisher but no token** must still produce a verdict (open request reported unknown), not exit 2. Test in Task 7.
4. **`--project` pointing elsewhere**: `local_files` `path: ./handbook` resolves against the project file's directory, and the cwd is restored afterwards (tests call `main()` in-process). Test in Task 6.
5. **A flag-mode run and a project run on the same mirror at once**: one exits 3, regardless of which started first. Test in Task 1 (flag) and Task 6 (project).

---

### Task 1: Mirror run lock, taken by flag-mode `run` and `redo`

**Files:**
- Create: `src/kbforge/lock.py`
- Modify: `src/kbforge/__main__.py` (wrap the `redo(...)` and `run(...)` calls)
- Test: `tests/test_lock.py`

**Interfaces:**
- Produces: `class MirrorLocked(RuntimeError)` with `.path: Path`; `lock_path(mirror: Path) -> Path` = `mirror.resolve().parent / "run.lock"`; `@contextmanager mirror_lock(mirror: Path) -> Iterator[None]` — creates the parent dir, opens `run.lock`, takes an exclusive **non-blocking** lock (`fcntl.flock(LOCK_EX | LOCK_NB)`; on Windows `msvcrt.locking(fd, LK_NBLCK, 1)`), raises `MirrorLocked` if held, releases on exit. Never deletes the lock file.

- [ ] **Step 1: Write the failing tests**

```python
def test_second_holder_is_refused(tmp_path):
    m = tmp_path / "mirror"
    with mirror_lock(m):
        with pytest.raises(MirrorLocked) as exc:
            with mirror_lock(m): ...
    assert exc.value.path == tmp_path / "run.lock"

def test_lock_is_released_when_the_holder_dies(tmp_path):
    # subprocess: python -c "...mirror_lock(m) ... print('held'); time.sleep(60)"
    # read 'held', kill -9 it, then mirror_lock(m) succeeds
    ...

def test_flag_run_exits_3_while_the_mirror_is_locked(tmp_path, capsys):
    # hold mirror_lock(tmp/"mirror") in a subprocess (as above), then
    code = main(["run", "--connector", "local_files", "--set", f"path={src}", *plumbing])
    assert code == 3
    out = capsys.readouterr().out
    assert out.strip() == f"Locked: another kbforge run holds {tmp_path / 'run.lock'}; nothing was read or written."
    assert not (tmp_path / "mirror").exists() or not any((tmp_path / "mirror").iterdir())
```

Same-process re-entry with `flock` on one fd is not a conflict on every OS, so the conflict tests use a **subprocess** holder; `test_second_holder_is_refused` opens a second fd and is fine on Linux/macOS.

- [ ] **Step 2: Run** `uv run pytest tests/test_lock.py -v` — Expected: FAIL (`ModuleNotFoundError: kbforge.lock`).
- [ ] **Step 3: Implement `lock.py`; in `main()` wrap both the `redo(...)` and the `run(...)` call in `with mirror_lock(Path(args.mirror)):`, catching `MirrorLocked` → print the exact message, `return 3`.** Taken after config validation (so a typo still exits 2 without touching disk) and before anything reads the mirror.
- [ ] **Step 4: Run** `uv run pytest -q` — Expected: all pass, existing tests unchanged.
- [ ] **Step 5: Mutation-check** — make `mirror_lock` a no-op `yield`; confirm both conflict tests fail; restore.
- [ ] **Step 6: Commit** `feat: lock the mirror for the whole of a run or redo`

---

### Task 2: Pipeline — ownership check and `systems` on results

**Files:**
- Modify: `src/kbforge/pipeline.py:91-106` (result dataclasses), `:277-320` (`run` signature, check after `assert_fetch_contract`), every `return NoOp()` / `return Published(...)` in `run`
- Test: `tests/test_pipeline_ownership.py`

**Interfaces:**
- Produces:
  - `NoOp.systems: frozenset[str] = field(default=frozenset(), compare=False)`; `Published.systems` likewise (after `url`). `compare=False` keeps every existing `== NoOp()` assertion true.
  - `run(..., foreign_systems: frozenset[str] = frozenset(), owners: Mapping[str, str] | None = None)` — `owners` maps a foreign system to the owning source name, for the message; default `{}`.
  - `fetched_systems = frozenset(d.anchor.system for d in docs)`; results carry `fetched_systems or frozenset(prior.systems if prior else ())`.
  - Public alias `instance_key = _instance_key` (the private name stays: kbforge-sql's comments and README cite it, and a companion edit would need its own release).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_fetched_system_owned_by_another_source_aborts_before_diff(tmp_path):
    r = run(local_files, dry_run, config={"path": str(src)}, mirror=..., state_dir=..., publish_config=...,
            foreign_systems=frozenset({"local_files"}), owners={"local_files": "handbook"})
    assert isinstance(r, Aborted)
    [f] = r.failures
    assert f.law == "source-ownership"
    assert f.message == "system 'local_files' is already owned by source 'handbook'"
    assert not any((tmp_path / "mirror").glob("*.json"))      # nothing committed
    assert not any((tmp_path / "state").glob("cursor-*"))     # cursor not saved

def test_noop_and_published_carry_the_fetched_systems(tmp_path):
    first = run(...)                    # Published
    assert first.systems == frozenset({"local_files"})
    second = run(...)                   # NoOp
    assert second == NoOp() and second.systems == frozenset({"local_files"})
```

- [ ] **Step 2: Run** `uv run pytest tests/test_pipeline_ownership.py -v` — Expected: FAIL (`unexpected keyword argument 'foreign_systems'`).
- [ ] **Step 3: Implement.** The check goes **immediately after `assert_fetch_contract`** (pipeline.py:320), before `diff` — not beside `_scope_failures` as the spec draft said: the two NoOp gates at :344 and :511 return before :650, so a check there would let an ownership violation NoOp and get recorded. One `Failure` per offending system (sorted), `concept_path` of that system's first doc_id (sorted) as the path. Update spec §5.3's placement sentence in the same commit.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Mutation-check** — move the check after the first NoOp gate; confirm a test with an identical-content foreign fetch catches it (add `test_ownership_is_checked_even_when_the_fetch_is_a_noop`: publish under no foreign set, then rerun with `foreign_systems={"local_files"}` → `Aborted`, not `NoOp`). Restore.
- [ ] **Step 6: Commit** `feat(pipeline): refuse a fetched system another source owns`

---

### Task 3: Pipeline — an unchunked publish retires the stale chunk record

**Files:**
- Modify: `src/kbforge/pipeline.py:864-877`
- Modify: `docs/architecture.md` §7.2 — delete the sentence starting "Do not mix chunked and unchunked runs"
- Test: `tests/test_pipeline_chunking.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_an_unchunked_publish_retires_the_chunk_record(tmp_path):
    # chunked run (max_concepts=1, 1 doc) → Published, chunk slot exists
    # edit the doc; unchunked run → Published
    assert not list((tmp_path / "state").glob("chunk-*.json"))
    with pytest.raises(RedoRefused, match="no chunk to redo"):
        redo(...)

def test_an_unchunked_noop_keeps_the_chunk_record(tmp_path):
    # chunked run → Published; unchunked rerun, no change → NoOp
    assert list((tmp_path / "state").glob("chunk-*.json"))
```

- [ ] **Step 2: Run** — Expected: first test FAILS (redo succeeds, rolling back).
- [ ] **Step 3: Implement** — in `run()`, after the cursor save: `if chunking is None: _chunk_slot(state_path, info.name, config).unlink(missing_ok=True)`. Only on the `Published` path.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Mutation-check** — delete the unlink; first test fails. Restore.
- [ ] **Step 6: Commit** `fix(pipeline): an unchunked publish retires the stale chunk record`

---

### Task 4: `pipeline.plan()` — the same body, stopped before synthesis

**Files:**
- Modify: `src/kbforge/pipeline.py:277-877`
- Test: `tests/test_pipeline_plan.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Planned:
    added: list[str]; modified: list[str]; removed: list[str]   # this chunk's, sorted
    grounding_drift: list[str]; link_drift: list[str]
    rebuilt: list[str]          # referrers + arrivals, sorted, deduped
    deferred: int               # backlog + deferred drift + deferred link drift
    systems: frozenset[str]

def plan(connector, publisher, *, config, mirror, state_dir, publish_config,
         grounds: bool, grounding_config=None, chunking=None, links_config=None,
         foreign_systems=frozenset(), owners=None,
         check_open_requests: bool = True) -> NoOp | Aborted | Waiting | Planned
```

- [ ] **Step 1: Write the failing tests** (reuse the fake connectors / fixtures in `tests/test_pipeline_links.py`, `tests/test_grounding_rules.py`, `tests/test_pipeline_chunking.py`)

```python
def _tree(root): return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

def test_plan_writes_nothing(tmp_path):
    # world with grounding rules + links.yaml + chunking(max_concepts=1), one prior publish, then a source edit
    before = _tree(tmp_path)
    p = plan(..., grounds=True, ...)
    assert isinstance(p, Planned)
    assert _tree(tmp_path) == before

def test_plan_verdict_matches_run(tmp_path):
    # for each world: unchanged → both NoOp; edit → Planned.modified == [id] and run() Published;
    # open pending chunk → both Waiting with the same request; path collision → both Aborted, same failures
    ...

def test_plan_without_open_request_checks_does_not_wait(tmp_path):
    # pending chunk record + open request → plan(check_open_requests=False) returns Planned, not Waiting
    ...

def test_plan_reports_deferred_backlog(tmp_path):
    # 3 docs, max_concepts=1 → Planned(added=[first], deferred=2)
```

- [ ] **Step 2: Run** — Expected: FAIL (`cannot import name 'plan'`).
- [ ] **Step 3: Implement by moving `run`'s body into `_run(..., synthesizer: Synthesizer, grounds: bool, until_synthesis: bool, check_open_requests: bool)`**, unchanged except: `grounds` comes from the parameter (not `getattr`), the two `Waiting` checks are skipped when `not check_open_requests`, and immediately after the `_scope_failures` return (pipeline.py:652) `if until_synthesis: return Planned(...)`. `run()` becomes a one-line call with `grounds=getattr(synthesizer, "grounds", False)`, `until_synthesis=False`, `check_open_requests=True`; `plan()` passes `StubSynthesizer()` (never called) and `until_synthesis=True`. **Do not reorder, rename or "tidy" any line of the moved body** — the diff of that region should be indentation-free (it stays at function level) and reviewable as a pure move.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass with no existing test edited. That is the behavior-preservation proof the spec requires.
- [ ] **Step 5: Mutation-check** — add a `commit(mirror_path, admitted_docs)` just before the `Planned` return; `test_plan_writes_nothing` fails. Restore.
- [ ] **Step 6: Commit** `feat(pipeline): plan(), the run body stopped before synthesis`

---

### Task 5: Project schema, loader, and the shared assembly

**Files:**
- Create: `src/kbforge/project.py`, `src/kbforge/assembly.py`
- Modify: `src/kbforge/__main__.py` (flag mode calls `assembly`; no output change)
- Test: `tests/test_project.py`

**Interfaces:**
- Produces (`project.py`):

```python
class Component(BaseModel):            # extra="allow"; settings = model_extra
    name: str
class SourceSpec(BaseModel):           # extra="forbid"
    connector: str
    config: dict[str, Any] = {}
    chunking: ChunkingConfig | None = None
    synthesizer: Component | None = None
    publisher: Component | None = None
class Project(BaseModel):              # extra="forbid"
    state_dir: Path = Path(".kbforge")
    synthesizer: Component = Component(name="stub")
    publisher: Component = Component(name="dry-run")
    links: Path | dict | None = None
    grounding: Path | dict | None = None
    sources: dict[str, SourceSpec]     # ≥1; keys match ^[a-z][a-z0-9_-]*$
    root: Path = Field(exclude=True)   # the file's directory, set by the loader
    def mirror(self) -> Path            # root / state_dir / "mirror"
    def state(self) -> Path             # root / state_dir / "state"
    def out(self) -> Path               # root / state_dir / "out"
class ProjectError(Exception):          # .problems: list[str]
DEFAULT_FILE = "kbforge.yaml"
def load_project(path: Path) -> Project
```

- Produces (`assembly.py`) — lifted out of today's `main()`, same messages:

```python
class SetupError(Exception): ...        # str(exc) is the exact line main() prints today
def build_synthesizer(name: str, settings: dict, mirror: Path) -> Synthesizer | None
def synthesizer_grounds(name: str) -> bool        # without importing kbforge[llm]: "llm" → True
def check_publisher(publisher, name: str, config: dict) -> list[str]
def load_links_value(v: Path | dict | None) -> LinksConfig | None       # path → load_links; dict → model_validate
def load_grounding_value(v: Path | dict | None) -> GroundingConfig
@dataclass(frozen=True) class Outcome: code: int; lines: list[str]; result: object | None = None  # the pipeline result on success
def execute(call: Callable[[], NoOp | Aborted | Published | Waiting], connector_name: str) -> Outcome
```

`execute` holds today's `try/except` ladder and result-to-text mapping from `main()` verbatim (every message, every code). Flag `main()` becomes `o = execute(lambda: run(...), args.connector); print(*o.lines, sep="\n"); return o.code`.

- [ ] **Step 1: Write the failing tests**

```python
def test_loads_the_spec_example(tmp_path):   # the §3 YAML, with links/grounding as files
    p = load_project(write(tmp_path, SPEC_EXAMPLE))
    assert list(p.sources) == ["products", "docs", "handbook"]   # file order kept
    assert p.mirror() == tmp_path / ".kbforge" / "mirror"
    assert p.sources["handbook"].synthesizer.name == "describe"

def test_reports_every_problem_with_its_yaml_path(tmp_path):
    with pytest.raises(ProjectError) as exc:
        load_project(write(tmp_path, "sources:\n  Docs: {connector: x}\n  ok: {connector: y, chunking: {max_concepts: 0}, stages: []}\n"))
    msgs = exc.value.problems
    assert any(m.startswith("sources.Docs:") and "^[a-z][a-z0-9_-]*$" in m for m in msgs)
    assert any(m.startswith("sources.ok.chunking.max_concepts:") for m in msgs)
    assert any(m.startswith("sources.ok.stages:") and "Extra inputs" in m for m in msgs)

def test_no_sources_is_an_error(tmp_path): ...
def test_a_missing_file_names_the_path(tmp_path): ...        # ProjectError(["kbforge.yaml: not found in <dir>"])
def test_synthesizer_grounds_needs_no_llm_extra(monkeypatch): # block pydantic_ai import; synthesizer_grounds("llm") is True
```

- [ ] **Step 2: Run** — Expected: FAIL (module missing).
- [ ] **Step 3: Implement `project.py` and `assembly.py`; switch flag `main()` to `assembly`.** Problems are `".".join(map(str, err["loc"])) + ": " + err["msg"]`. YAML parse errors → `ProjectError([f"{path}: {exc}"])`.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass; `tests/test_cli*.py` untouched proves flag output is unchanged.
- [ ] **Step 5: Commit** `feat: the project file schema, and one assembly for both CLI modes`

---

### Task 6: `sources.json` and project-mode `run`

**Files:**
- Create: `src/kbforge/sources_record.py`, `src/kbforge/project_cli.py`
- Modify: `src/kbforge/__main__.py` (mode dispatch, `--project`, positional sources)
- Test: `tests/test_sources_record.py`, `tests/test_cli_project.py`

**Interfaces:**
- Produces (`sources_record.py`):

```python
class SourceEntry(BaseModel):            # extra="forbid"
    connector: str
    digest: str
    systems: list[str]
    last_published: str | None = None    # ISO 8601 with offset, seconds
def read_sources(state: Path) -> dict[str, SourceEntry]       # missing file → {}
def write_sources(state: Path, entries: dict[str, SourceEntry]) -> None  # temp file + os.replace
```

- Produces (`project_cli.py`): `@dataclass ResolvedSource` (name, connector, publisher, config, publish_config, synthesizer, grounds, chunking); `resolve(project, registry, names: list[str], *, for_plan: bool) -> list[ResolvedSource]` raising `ProjectError` with **all** problems (§6.1; `for_plan` skips `validate_env`, the synthesizer import, and returns publisher problems instead of failing on them); `cmd_run(project, names) -> int`.
- CLI shape: `run`, `redo` get `--connector` optional and `sources: nargs="*"`; new `plan`, `status`, `reset`, `init` subparsers; `--project PATH` on all project commands. `--connector` present → flag mode, where `--mirror`/`--state` (and `--out` for run) are required via `parser.error` (exit 2, argparse's own wording). `--connector` absent and any of `--set`/`--publish-set`/`--llm-set`/`--synthesizer`/`--grounding`/`--chunking`/`--links`/`--mirror`/`--state`/`--out` given → exit 2: `--<flag> needs --connector; a project's sources are configured in kbforge.yaml`.

Behaviour of `cmd_run` (spec §6.2): `chdir` to `project.root` (restore in `finally`), take `mirror_lock(project.mirror())` (exit 3 as Task 1), `resolve`, then per source in file order: print the config-change note if `entries[name].digest != instance_key(config)`; `foreign` = systems of entries **whose name is in the current file** and ≠ this name, plus systems returned earlier in this invocation; `execute(lambda: run(..., foreign_systems=..., owners=...))`; prefix every line with `f"{name:<{w}}  "` (w = longest selected name); on `NoOp`/`Published` write the entry (systems = result.systems or old systems; `last_published` = now on `Published`). Exit 0/1 per Global Constraints; 2 only from `resolve`.

- [ ] **Step 1: Write the failing tests**

```python
def test_project_run_syncs_every_source_then_noops(tmp_path, capsys):
    # kbforge.yaml: sources handbook (local_files ./handbook) + commits (git_commits on a tmp repo)
    assert main(["run", "--project", str(yml)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("handbook  Published:") and out[1].startswith("commits   Published:")
    assert main(["run", "--project", str(yml)]) == 0
    assert all("NoOp" in l for l in capsys.readouterr().out.splitlines())

def test_relative_connector_paths_resolve_against_the_project_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path / "elsewhere"); assert main(["run", "--project", str(yml)]) == 0
    assert Path.cwd() == tmp_path / "elsewhere"

def test_one_misconfigured_source_runs_nothing(tmp_path, capsys):   # bad path in source 2 → exit 2, no mirror
def test_a_failing_source_does_not_stop_the_next(tmp_path, capsys): # source 1 path collision → Aborted; source 2 Published; exit 1
def test_two_local_files_sources_second_aborts(tmp_path, capsys):
    # exact line: "b  Aborted: 1 validation failure(s):" then "  [source-ownership] ...: system 'local_files' is already owned by source 'a'"
def test_renaming_a_source_keeps_its_cursor_and_ownership(tmp_path, capsys):   # Review Focus 1: rename → NoOp
def test_config_change_prints_the_note(tmp_path, capsys)
def test_project_run_exits_3_while_a_flag_run_holds_the_lock(tmp_path, capsys)  # Review Focus 5
def test_select_sources_by_name_and_unknown_name_exits_2(tmp_path, capsys)
def test_flag_options_without_connector_exit_2(tmp_path, capsys)
```

- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement** `sources_record.py`, `project_cli.resolve` + `cmd_run`, CLI dispatch.
- [ ] **Step 4: Run** `uv run pytest -q` — all pass.
- [ ] **Step 5: Mutation-check** — count stale names in `foreign`; the rename test fails. Drop the `chdir`; the relative-path test fails. Restore each.
- [ ] **Step 6: Commit** `feat: kbforge run over a project file`

---

### Task 7: `kbforge plan`

**Files:**
- Modify: `src/kbforge/project_cli.py`, `src/kbforge/__main__.py`
- Test: `tests/test_cli_project.py` (append)

**Interfaces:**
- Consumes: `pipeline.plan`, `Planned` (Task 4); `resolve(..., for_plan=True)` (Task 6); `synthesizer_grounds` (Task 5).
- Produces: `cmd_plan(project, names) -> int`. Takes the lock (it reads the mirror). Per source, exact copy:
  - `NoOp` → `no change → NoOp`
  - `Waiting` → `review request <id> open → would wait`
  - `Aborted` → today's Aborted lines
  - `Planned` → `<a> added, <m> modified, <r> removed, <d> drifted, <k> rebuilt → would propose <n> concepts (validation runs at publish)` where d = grounding + link drift and n = a+m+d+k (+ `; <deferred> more follow in later chunks` when > 0), then one line per doc_id, sorted within kind: `  + <id>  added`, `  ~ <id>  modified`, `  - <id>  removed`, `  ↻ <id>  grounding drift`, `  ↻ <id>  link drift`, `  ↻ <id>  rebuilt for links`
  - publisher problems under `for_plan` → `check_open_requests=False` and a line `note: open request unknown (<problems joined by '; '>); a run may wait instead`
- Exit: 0 unless a source errored or `Aborted` (1), or `resolve` failed (2).

- [ ] **Step 1: Write the failing tests**

```python
def test_plan_then_run_agree(tmp_path, capsys):
    # first plan → "1 added, ... → would propose 1 concepts" and "  + local_files:a.md  added"
    # run → Published; plan → "no change → NoOp"
def test_plan_writes_nothing_under_state_dir(tmp_path)             # snapshot .kbforge before/after
def test_plan_needs_no_model_credentials(tmp_path, monkeypatch, capsys):
    # synthesizer: {name: llm, model: x}; OPENROUTER_API_KEY unset; pydantic_ai import blocked → exit 0
def test_plan_with_forge_publisher_and_no_token(tmp_path, monkeypatch, capsys):   # Review Focus 3
    # publisher github, GITHUB_TOKEN unset → exit 0, note line contains "env var GITHUB_TOKEN is not set"
def test_migration_from_flag_mode_plans_noop(tmp_path, capsys):   # spec §8
    # flag-mode run with --mirror X/mirror --state X/state; then a project with state_dir: X and the
    # same connector config → plan prints "no change → NoOp"
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4:** `uv run pytest -q` — pass.
- [ ] **Step 5: Commit** `feat: kbforge plan`

---

### Task 8: `kbforge status`

**Files:**
- Modify: `src/kbforge/project_cli.py`, `src/kbforge/__main__.py`
- Test: `tests/test_cli_project.py` (append)

**Interfaces:**
- Produces: `cmd_status(project) -> int`. No lock, no fetch. Per source in the file, then per stale `sources.json` name, one line of `·`-joined parts:
  - `never run` when there is no entry and no chunk slot; otherwise `last published <YYYY-MM-DD HH:MM> UTC` or `never published`
  - open request: if the publisher has a real `kbforge_open_request` (reuse `pipeline._open_request_hook`, catching `ConfigError` → omit) and `check_publisher` is clean: check `sync/<system>` for each recorded system plus the chunk record's `branch_hints` → `no open request` / `review request <id> open`; publisher problems → `open request unknown (<problems>)`
  - chunk record with `pending`: `chunk of <len(admitted)> open as <id>, more to follow` when open, else `next chunk proposes on the next run`
  - stale name: `not in kbforge.yaml (kbforge reset <name> clears its state)`
- Exit 0; 2 only for a bad project file.

- [ ] **Step 1: Write the failing tests** — `test_status_before_and_after_a_run` (`never run` → `last published … · no open request`), `test_status_shows_a_pending_chunk` (fake publisher reporting `!12` open), `test_status_lists_a_removed_source_as_stale` (Review Focus 2).
- [ ] **Step 2–4:** run (FAIL) → implement → `uv run pytest -q` (PASS).
- [ ] **Step 5: Commit** `feat: kbforge status`

---

### Task 9: `kbforge reset` and project-mode `redo`

**Files:**
- Create: `src/kbforge/reset.py`
- Modify: `src/kbforge/project_cli.py`, `src/kbforge/__main__.py`
- Test: `tests/test_reset.py`, `tests/test_cli_project.py` (append)

**Interfaces:**
- Produces (`reset.py`):

```python
@dataclass(frozen=True)
class ResetPlan:
    state_files: list[Path]        # existing cursor/chunk slots for every known digest
    doc_ids: list[str]             # mirror docs whose anchor.system ∈ systems, sorted
    systems: frozenset[str]
def plan_reset(mirror: Path, state: Path, *, connector: str, digests: set[str],
               systems: frozenset[str]) -> ResetPlan
def apply_reset(mirror: Path, rp: ResetPlan) -> None
    # commit(mirror, tombstoned copies) to drop slots; delete_sidecar / delete_first_seen /
    # delete_described / delete_links per doc_id; unlink state_files
def systems_for(entry: SourceEntry | None, cursor_slot: Path, chunk_slot: Path) -> frozenset[str]
    # entry.systems, else cursor.systems ∪ {id.partition(":")[0] for id in chunk.admitted}
```

- `cmd_reset(project, name, *, yes: bool) -> int`: works for a name in the file **or** a stale entry (connector + digest from the entry). Digests = current config's (if in file) ∪ entry's. Takes the lock. Prompt `Reset <name>: remove <n> mirror document(s) and <k> state file(s)? [y/N] ` unless `--yes`; EOF or anything but `y`/`yes` → `Reset cancelled.` exit 0. Unknown systems → still remove state files, print `systems unknown: mirror left as is`. Nothing to reset → `<name>: nothing to reset`, exit 0. Removes the `sources.json` entry. Help text carries the closed-deletion limit (spec §6.5).
- `cmd_redo(project, name) -> int`: lock, resolve the one source, `execute(lambda: redo(...))` with today's redo messages prefixed.

- [ ] **Step 1: Write the failing tests**

```python
def test_reset_then_run_reproposes_only_that_source(tmp_path, capsys):
    # sources a (local_files) + c (git_commits); run; reset a --yes; run → a Published, c NoOp
def test_reset_leaves_other_sources_mirror_slots_byte_identical(tmp_path)
def test_reset_mid_chunking_removes_the_chunk_slot_and_admitted_docs(tmp_path)
def test_reset_a_stale_name_works_from_its_entry(tmp_path)           # Review Focus 2
def test_reset_without_yes_on_eof_cancels(tmp_path, monkeypatch, capsys)
def test_project_redo_reproposes_the_last_chunk(tmp_path, capsys)
```

- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Mutation-check** — select mirror docs without the system filter; the byte-identical test fails. Restore.
- [ ] **Step 6: Commit** `feat: kbforge reset, and redo by source name`

---

### Task 10: `kbforge init`

**Files:**
- Modify: `src/kbforge/project_cli.py` (template constant + `cmd_init`), `src/kbforge/__main__.py`
- Test: `tests/test_cli_project.py` (append)

**Interfaces:**
- Produces: `cmd_init(target: Path, docs_path: str) -> int`. Writes the template (one `local_files` source `docs` on `docs_path`, default `./docs`; stub; dry-run; commented llm and github blocks; a comment that sources others ground on go first; a comment that one `local_files` source should point at a common parent). Existing file → `kbforge.yaml already exists; not overwritten` exit 2. Prints exactly:

```
wrote kbforge.yaml
next: kbforge plan
keep .kbforge/ between runs: it holds the mirror that makes re-runs free
```

- [ ] **Step 1: Write the failing tests** — `test_init_writes_a_project_that_plans_and_runs` (init in tmp with `docs/a.md`, then `plan` exit 0 shows `+ local_files:a.md`, `run` exit 0), `test_init_never_overwrites`, `test_init_template_uncommented_blocks_validate` (strip `# ` from the llm/github blocks → `load_project` succeeds).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: kbforge init`

---

### Task 11: README, CHANGELOG, fold the spec

**Files:**
- Modify: `README.md`, `CHANGELOG.md` (`[Unreleased]`), `docs/architecture.md` (new §7.5), delete `docs/design/2026-10-29-project-file-design.md`

- [ ] **Step 1: README** in the spec §7 order: what you get → `init` / `plan` / `run` → the project file (the §3 example) → publishing → why you can trust it → positioning → "Running one source without a project file" (today's flag examples). Delete, don't soften: the shared-mirror rule, same-`--links` rule, always-chunked-or-never, and the hand-reset procedure (now `kbforge reset`). Keep the closed-unmerged-deletion limit. Add the §8 migration note (`state_dir` at the existing dirs, then `plan` should say NoOp).
- [ ] **Step 2: architecture.md §7.5** — the project layer, the lock, ownership, `sources.json`, `plan` = `run` stopped before synthesis; keep only rationale the code doesn't own. Delete the spec file.
- [ ] **Step 3: CHANGELOG** `[Unreleased]` → Added (project file, init/plan/status/reset, redo by name), Changed (flag runs take the mirror lock; exit 3), Fixed (unchunked publish retires a stale chunk record).
- [ ] **Step 4: Verify** `uv run pytest -q` passes; `grep -n "same --links\|always chunked\|delete the mirror directory" README.md` → no matches.
- [ ] **Step 5: Commit** `docs: lead with the project file`

---

### Task 12: Live verification (no code)

Per the working conventions — green offline tests have masked real defects before.

- [ ] **Step 1:** In a scratch dir, `kbforge init`, then edit to: a `kbforge-sql` SQLite source + a `local_files` source, `synthesizer: llm` (model from `.env`), `publisher: github` on the scratch repo.
- [ ] **Step 2:** `kbforge plan` (expect both sources "added"); `kbforge run` (expect two PR URLs); `kbforge run` (expect two NoOp lines); `kbforge status`.
- [ ] **Step 3:** Edit one row; `kbforge plan` shows only that source; `kbforge reset local` `--yes`; `kbforge run` re-proposes it.
- [ ] **Step 4:** With a `kbforge run` in progress, start a flag-mode `kbforge run --mirror <same>` → `Locked:` and exit 3.
- [ ] **Step 5:** Record findings; fix anything found as its own task before release.
