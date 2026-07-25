# Branch Accumulation and Deletion Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the sync branch accumulate across runs instead of being rebuilt from base each time, and give the publisher a machine-readable removal list so deleted concepts are actually deleted.

**Architecture:** `publish_to_forge` resolves the base *after* asking whether a review request is open, so an open request makes the branch build on itself. `ProposedChange` gains `files_removed`, which the pipeline assigns deterministically after synthesis — deletion is structure, not prose. Each adapter intersects the removal set with the base tree before emitting deletes, because both forges reject deleting an absent path.

**Tech Stack:** Python 3.12+, pydantic v2, pluggy, stdlib `urllib`. Tests: pytest. Lint: ruff + ty via `prek`.

## Global Constraints

- **No new runtime dependencies.** `pyproject.toml`'s `dependencies` stays exactly `pluggy>=1.5`, `pydantic>=2.0`, `pyyaml>=6`. No httpx, requests, PyGithub, or python-gitlab.
- **Never merge.** No merge method may be added to `ForgeClient` or any adapter. This is enforced structurally, not by convention.
- **Credentials from environment variables only.** Never a CLI flag, never a config value.
- **No test may make a real network call unless marked `@pytest.mark.live`.** Live tests are skipped unless `--run-live` is passed.
- **Deletion authority is the pipeline's.** A synthesizer's `files_removed` output is always overwritten, never trusted or merely validated.
- **Deletions come only from explicit tombstones.** Absence never implies deletion.
- Every command below runs from the repo root with `uv run`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/kbforge/mirror.py` | canonical mirror + read-only diff | add `load_all()` |
| `src/kbforge/models.py` | pydantic data model | add `ProposedChange.files_removed` |
| `src/kbforge/synthesize.py` | concept assembly | consistent `claims_removed`; system-safe `branch_hint` |
| `src/kbforge/pipeline.py` | the fixed pipeline | assign `files_removed`, exclude tombstones from `existing`, expand referrers |
| `src/kbforge/publishers/forge.py` | forge-agnostic orchestration | reorder sequence, resolve base, `safe_join` removals |
| `src/kbforge/publishers/gitlab.py` | GitLab adapter | delete actions |
| `src/kbforge/publishers/github.py` | GitHub adapter | base-tree listing + `sha: None` |
| `src/kbforge/publishers/dry_run.py` | local publisher | unlink removed paths |
| `tests/test_forge_live.py` | live forge suite | accumulation + no-resurrection scenarios |

---

### Task 1: `mirror.load_all()`

Referrer expansion needs every document the mirror knows, not just the ones this run fetched — an incremental connector's fetch may not contain the concept that links to a deleted one.

**Files:**
- Modify: `src/kbforge/mirror.py`
- Test: `tests/test_mirror.py`

**Interfaces:**
- Consumes: nothing
- Produces: `load_all(mirror: Path) -> list[CanonicalDocument]` — every document currently stored, in sorted `doc_id` order. Returns `[]` when the mirror directory does not exist.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mirror.py`:

```python
def test_load_all_returns_every_stored_document(tmp_path):
    mirror = tmp_path / "mirror"
    commit(mirror, [_doc("a", "A"), _doc("b", "B")])

    docs = load_all(mirror)

    assert [d.doc_id for d in docs] == ["sys:a", "sys:b"]


def test_load_all_is_empty_for_a_missing_mirror(tmp_path):
    assert load_all(tmp_path / "does-not-exist") == []


def test_load_all_omits_documents_retired_by_a_tombstone(tmp_path):
    mirror = tmp_path / "mirror"
    commit(mirror, [_doc("a", "A"), _doc("b", "B")])
    commit(mirror, [_doc("a", "A", deleted=True)])

    assert [d.doc_id for d in load_all(mirror)] == ["sys:b"]
```

Check the existing helper in `tests/test_mirror.py` that builds a `CanonicalDocument`. If it is not named `_doc` or does not accept `deleted`, adapt these tests to the local helper rather than renaming it; the assertions are what matter.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mirror.py -k load_all -v`
Expected: FAIL with `ImportError` / `NameError: name 'load_all' is not defined`

- [ ] **Step 3: Write minimal implementation**

Add to `src/kbforge/mirror.py`, after `_load`:

```python
def load_all(mirror: Path) -> list[CanonicalDocument]:
    """Every document the mirror currently holds, sorted by doc_id.

    Tombstoned documents are absent by construction: commit() unlinks their
    slot, so the mirror only ever stores live documents.
    """
    if not mirror.is_dir():
        return []
    docs = [
        CanonicalDocument.model_validate_json(slot.read_text("utf-8"))
        for slot in sorted(mirror.glob("*.json"))
    ]
    return sorted(docs, key=lambda d: d.doc_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mirror.py -v`
Expected: PASS, including the pre-existing tests

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/mirror.py tests/test_mirror.py
git commit -m "feat(mirror): add load_all() for referrer lookup"
```

---

### Task 2: `files_removed` on the model, and two `assemble` defects

`ProposedChange` gains the removal channel. Two existing defects in `assemble()` surface once deletion-only runs become possible and are fixed here:

1. `claims_removed` is set to raw `doc_id`s while `claims_added`/`claims_modified` are converted to bundle paths, so the review body mixes two identifier formats.
2. `branch_hint` derives its system from `items[0]`, falling back to the literal `"source"` when `items` is empty. A deletion-only run has no items, so it would publish to `sync/source` — a *different* branch from the established one — and open a second review request.

**Files:**
- Modify: `src/kbforge/models.py`
- Modify: `src/kbforge/synthesize.py:88-100` (the tail of `assemble`)
- Test: `tests/test_synthesize.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ProposedChange.files_removed: list[str]` — bundle-relative paths to delete, default `[]`. `assemble()` keeps its existing signature `assemble(items, changeset, existing_paths=frozenset()) -> ProposedChange`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_synthesize.py`:

```python
def test_proposed_change_defaults_to_no_removals():
    change = assemble([], ChangeSet(added=[], modified=[], removed=[], unchanged_count=0))

    assert change.files_removed == []


def test_claims_removed_are_bundle_paths_like_added_and_modified():
    """The review body must not mix doc_ids with paths."""
    changeset = ChangeSet(
        added=[], modified=[], removed=["sys:gone"], unchanged_count=0
    )

    change = assemble([], changeset)

    assert change.summary.claims_removed == ["concepts/gone/overview.md"]


def test_branch_hint_survives_a_deletion_only_run():
    """No items means no doc to read the system from; the removed doc_ids carry
    it. Falling back to 'source' would publish to a different branch and open a
    second review request."""
    changeset = ChangeSet(
        added=[], modified=[], removed=["local_files:gone.md"], unchanged_count=0
    )

    change = assemble([], changeset)

    assert change.branch_hint == "sync/local_files"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_synthesize.py -k "removals or claims_removed or deletion_only" -v`
Expected: FAIL — `AttributeError: 'ProposedChange' object has no attribute 'files_removed'`, then `assert ['sys:gone'] == ['concepts/gone/overview.md']`, then `assert 'sync/source' == 'sync/local_files'`

- [ ] **Step 3: Add the model field**

In `src/kbforge/models.py`, inside `class ProposedChange`, directly after the `files` field:

```python
    files_removed: list[str] = Field(default_factory=list)
    """Bundle-relative paths to delete. Assigned by the pipeline, never by a
    synthesizer — deletion is structure, not prose (§4.4 posture)."""
```

- [ ] **Step 4: Fix both `assemble` defects**

In `src/kbforge/synthesize.py`, replace the tail of `assemble` (from `summary.claims_added = ...` to the `return`) with:

```python
    summary.claims_added = sorted(concept_path(x) for x in changeset.added)
    summary.claims_modified = sorted(concept_path(x) for x in changeset.modified)
    # Paths, not doc_ids: the review body must speak one identifier format.
    summary.claims_removed = sorted(concept_path(x) for x in changeset.removed)
    # A deletion-only run has no items, so the system has to come from the
    # removed doc_ids ("system:native_id"). Falling back to a literal would
    # publish to a different branch and open a second review request.
    if items:
        system = items[0][0].anchor.system
    elif changeset.removed:
        system = changeset.removed[0].partition(":")[0]
    else:
        system = "source"
    return ProposedChange(
        branch_hint=f"sync/{system}",
        files=files,
        concepts=concepts,
        summary=summary,
    )
```

Keep any trailing arguments the current `return` already passes.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_synthesize.py tests/test_models.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. If an existing test asserted `claims_removed` as doc_ids, update it to the path form — that is the defect being fixed, not a regression.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/models.py src/kbforge/synthesize.py tests/
git commit -m "feat(models): add ProposedChange.files_removed; fix claims_removed and deletion-only branch_hint"
```

---

### Task 3: Accumulate onto the open review request's branch

This is the 0.3.0 data-loss fix and is independently valuable. `find_open_pr` moves ahead of `put_files` so the base can be the branch itself. `put_files` grows a `removed` parameter that adapters accept but do not yet act on; Tasks 4-6 implement the behaviour.

**Files:**
- Modify: `src/kbforge/publishers/forge.py` (the `ForgeClient` protocol and `publish_to_forge`)
- Modify: `src/kbforge/publishers/github.py` (`put_files` signature only)
- Modify: `src/kbforge/publishers/gitlab.py` (`put_files` signature only)
- Test: `tests/test_forge.py`

**Interfaces:**
- Consumes: `ProposedChange.files_removed` (Task 2)
- Produces: `ForgeClient.put_files(self, branch: str, base: str, files: dict[str, str], removed: list[str], message: str) -> None`. `publish_to_forge(client, change, cfg) -> str` keeps its signature.

- [ ] **Step 1: Write the failing tests**

In `tests/test_forge.py`, update `FakeForgeClient.put_files` to the new signature:

```python
    def put_files(self, branch, base, files, removed, message) -> None:
        self.calls.append(("put_files", branch, base, files, removed, message))
```

Then append:

```python
def test_an_open_review_request_makes_the_branch_build_on_itself():
    """The 0.3.0 data-loss bug: resetting to base rebuilt away anything an
    earlier run put on a branch whose review request had not merged."""
    client = FakeForgeClient(open_pr="7")
    change = ProposedChange(branch_hint="sync/x", files={"a.md": "A"})

    publish_to_forge(client, change, _cfg())

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[2] == "sync/x", "base must be the branch when a review request is open"
    assert client.calls[0][0] == "find_open_pr", "must be asked before put_files"


def test_no_open_review_request_rebuilds_from_the_default_branch():
    client = FakeForgeClient(open_pr=None, default="main")
    change = ProposedChange(branch_hint="sync/x", files={"a.md": "A"})

    publish_to_forge(client, change, _cfg())

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[2] == "main"


def test_a_new_review_request_targets_the_default_branch_not_the_sync_branch():
    client = FakeForgeClient(open_pr=None, default="main")
    change = ProposedChange(branch_hint="sync/x", files={"a.md": "A"})

    publish_to_forge(client, change, _cfg())

    create = next(c for c in client.calls if c[0] == "create_pr")
    assert create[2] == "main"


def test_removed_paths_are_prefixed_and_path_checked_like_files():
    client = FakeForgeClient(open_pr=None)
    change = ProposedChange(
        branch_hint="sync/x", files={"a.md": "A"}, files_removed=["old.md"]
    )

    publish_to_forge(client, change, _cfg(base_path="kb"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[4] == ["kb/old.md"]


def test_a_traversing_removal_path_is_rejected_before_any_network_call():
    client = FakeForgeClient(open_pr=None)
    change = ProposedChange(
        branch_hint="sync/x", files={}, files_removed=["../../.github/workflows/x.yml"]
    )

    with pytest.raises(PathError):
        publish_to_forge(client, change, _cfg())

    assert client.calls == [], "no call may be made before paths are validated"
```

Use the existing `_cfg(...)` helper in this file; if it takes different keyword arguments, adapt the calls rather than the helper.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_forge.py -v`
Expected: FAIL — `TypeError: put_files() takes 5 positional arguments but 6 were given`, plus base assertions comparing `"main"` to `"sync/x"`

- [ ] **Step 3: Update the protocol and orchestration**

In `src/kbforge/publishers/forge.py`, replace the `put_files` entry in `ForgeClient` with:

```python
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
        """
        ...
```

Replace the body of `publish_to_forge` with:

```python
    files = {safe_join(cfg.base_path, rel): body for rel, body in change.files.items()}
    removed = sorted(safe_join(cfg.base_path, rel) for rel in change.files_removed)
    branch = cfg.branch or change.branch_hint
    body = summary_md(change.summary)

    # Asked before put_files, not after: an open review request means the branch
    # must build on itself, or work from earlier runs is rebuilt away.
    pr_id = client.find_open_pr(branch)
    target = cfg.base or client.default_branch()
    base = branch if pr_id is not None else target

    client.put_files(branch, base, files, removed, cfg.title)
    if pr_id is not None:
        return client.update_pr(pr_id, cfg.title, body)
    return client.create_pr(branch, target, cfg.title, body)
```

Note both `files` and `removed` are computed before any client call, preserving the existing guarantee that a traversing path is rejected before it reaches the network.

- [ ] **Step 4: Update both adapter signatures**

In `src/kbforge/publishers/github.py` and `src/kbforge/publishers/gitlab.py`, change each `put_files` signature to accept `removed: list[str]` between `files` and `message`. Do not use it yet. Add this comment above each body:

```python
        # `removed` is honoured in the adapter-specific delete task; accepting it
        # here keeps the protocol change and the delete mechanics reviewable apart.
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Adapter tests calling `put_files` positionally need `removed` inserted; update them.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/publishers/ tests/
git commit -m "fix(publishers): accumulate onto the open review request's branch"
```

---

### Task 4: GitLab deletes

**Files:**
- Modify: `src/kbforge/publishers/gitlab.py` (`put_files`)
- Test: `tests/test_gitlab_publisher.py`

**Interfaces:**
- Consumes: `put_files(..., removed, ...)` (Task 3), `_existing_paths(base) -> set[str]` (existing)
- Produces: no new public names

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_gitlab_publisher.py`:

```python
def test_removed_paths_become_delete_actions(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {
            ("GET", _TREE): [{"type": "blob", "path": "old.md"}],
            ("POST", f"/projects/{PROJECT}/repository/commits"): {},
        }
    )
    client.put_files("b", "main", {}, ["old.md"], "msg")

    actions = transport.calls[-1]["payload"]["actions"]
    assert actions == [{"action": "delete", "file_path": "old.md"}]


def test_a_removal_absent_from_base_is_not_sent(monkeypatch):
    """GitLab answers 400 'A file with this name doesn't exist'. Filtering also
    makes a retry after a partial failure idempotent."""
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {
            ("GET", _TREE): [],
            ("POST", f"/projects/{PROJECT}/repository/commits"): {},
        }
    )
    client.put_files("b", "main", {"a.md": "A"}, ["never-there.md"], "msg")

    actions = transport.calls[-1]["payload"]["actions"]
    assert all(a["action"] != "delete" for a in actions)
```

Reuse this file's existing `_client` helper and its tree-listing URL constant; if the listing key is spelled differently, match the local convention and define `_TREE` accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gitlab_publisher.py -k removed -v`
Expected: FAIL — `assert [] == [{'action': 'delete', ...}]`

- [ ] **Step 3: Implement**

In `src/kbforge/publishers/gitlab.py`, replace the `actions` construction inside `put_files` with:

```python
        existing = self._existing_paths(base)
        actions = [
            {
                "action": "update" if path in existing else "create",
                "file_path": path,
                "content": body,
            }
            for path, body in sorted(files.items())  # deterministic
        ]
        # Only delete what is actually on base: GitLab answers 400 "A file with
        # this name doesn't exist" otherwise, which would also make any retry
        # after a partial failure fail outright.
        actions += [
            {"action": "delete", "file_path": path}
            for path in sorted(removed)
            if path in existing
        ]
```

then pass `"actions": actions` in the payload.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_gitlab_publisher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/gitlab.py tests/test_gitlab_publisher.py
git commit -m "feat(gitlab): delete removed paths, filtered against base"
```

---

### Task 5: GitHub deletes

GitHub removes a path by including a tree entry whose `sha` is `None`, alongside `mode` and `type`. Verified live: it answers **422** for a path absent from `base_tree`, so the same filtering GitLab needs is required here. GitHub's `put_files` has the base tree SHA but no path listing, so one recursive tree read is added.

**Files:**
- Modify: `src/kbforge/publishers/github.py` (`put_files`)
- Test: `tests/test_github_publisher.py`

**Interfaces:**
- Consumes: `put_files(..., removed, ...)` (Task 3)
- Produces: `GitHubClient._existing_paths(tree_sha: str) -> set[str]` — blob paths in that tree, recursively

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_github_publisher.py`:

```python
def test_removed_paths_become_null_sha_tree_entries(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {
            ("GET", f"/repos/{REPO}/commits/main"): {
                "sha": "c1",
                "commit": {"tree": {"sha": "t1"}},
            },
            ("GET", f"/repos/{REPO}/git/trees/t1?recursive=1"): {
                "tree": [{"type": "blob", "path": "old.md"}],
                "truncated": False,
            },
            ("POST", f"/repos/{REPO}/git/trees"): {"sha": "t2"},
            ("POST", f"/repos/{REPO}/git/commits"): {"sha": "c2"},
            ("PATCH", f"/repos/{REPO}/git/refs/heads/b"): {},
        }
    )
    client.put_files("b", "main", {}, ["old.md"], "msg")

    tree = next(
        c["payload"]["tree"] for c in transport.calls if c["method"] == "POST"
        and c["url"].endswith("/git/trees")
    )
    assert {"path": "old.md", "mode": "100644", "type": "blob", "sha": None} in tree


def test_a_removal_absent_from_base_is_not_sent(monkeypatch):
    """GitHub answers 422 for a path absent from base_tree."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {
            ("GET", f"/repos/{REPO}/commits/main"): {
                "sha": "c1",
                "commit": {"tree": {"sha": "t1"}},
            },
            ("GET", f"/repos/{REPO}/git/trees/t1?recursive=1"): {
                "tree": [],
                "truncated": False,
            },
            ("POST", f"/repos/{REPO}/git/trees"): {"sha": "t2"},
            ("POST", f"/repos/{REPO}/git/commits"): {"sha": "c2"},
            ("PATCH", f"/repos/{REPO}/git/refs/heads/b"): {},
        }
    )
    client.put_files("b", "main", {"a.md": "A"}, ["never-there.md"], "msg")

    tree = next(
        c["payload"]["tree"] for c in transport.calls if c["method"] == "POST"
        and c["url"].endswith("/git/trees")
    )
    assert all(e["path"] != "never-there.md" for e in tree)


def test_a_truncated_base_tree_listing_raises(monkeypatch):
    """Mirrors gitlab's TreeListingTruncatedError: a partial listing would make
    a real deletion look absent from base and be silently skipped."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, _ = _client(
        {
            ("GET", f"/repos/{REPO}/commits/main"): {
                "sha": "c1",
                "commit": {"tree": {"sha": "t1"}},
            },
            ("GET", f"/repos/{REPO}/git/trees/t1?recursive=1"): {
                "tree": [],
                "truncated": True,
            },
        }
    )
    with pytest.raises(TreeListingTruncatedError):
        client.put_files("b", "main", {}, ["old.md"], "msg")
```

Import `TreeListingTruncatedError` from `kbforge.publishers.gitlab` — it is a `PublishError` subclass and reusing it keeps the CLI's single catch site correct. If that import reads oddly across adapters, move the class to `kbforge/publishers/_http.py` and update the GitLab import in the same commit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_github_publisher.py -k "removed or truncated" -v`
Expected: FAIL — the tree payload contains no `old.md` entry; `TreeListingTruncatedError` is not raised

- [ ] **Step 3: Implement**

In `src/kbforge/publishers/github.py`, add above `put_files`:

```python
    def _existing_paths(self, tree_sha: str) -> set[str]:
        """Blob paths in `tree_sha`, recursively.

        GitHub answers 422 when a tree entry deletes a path absent from
        base_tree, so removals must be filtered. A truncated listing would make
        a present path look absent and silently skip a real deletion, so it
        raises rather than returning a partial set.
        """
        tree = self._call("GET", f"/repos/{self._repo}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise TreeListingTruncatedError(
                f"base tree {tree_sha} exceeds GitHub's recursive listing limit; "
                "refusing to return a partial listing. Scope the publish "
                "config's 'base_path' to a narrower subtree."
            )
        return {e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"}
```

Then, inside `put_files`, replace the tree-entry construction with:

```python
        entries: list[dict] = [
            {"path": path, "mode": "100644", "type": "blob", "content": body}
            for path, body in sorted(files.items())  # deterministic
        ]
        if removed:
            existing = self._existing_paths(head["commit"]["tree"]["sha"])
            entries += [
                {"path": path, "mode": "100644", "type": "blob", "sha": None}
                for path in sorted(removed)
                if path in existing
            ]
        tree = self._call(
            "POST",
            f"/repos/{self._repo}/git/trees",
            {"base_tree": head["commit"]["tree"]["sha"], "tree": entries},
        )
```

The listing call is made only when there is something to remove, so ordinary publishes cost no extra request.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_github_publisher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/github.py tests/test_github_publisher.py
git commit -m "feat(github): delete removed paths via null-sha tree entries"
```

---

### Task 6: dry-run publisher deletes

**Files:**
- Modify: `src/kbforge/publishers/dry_run.py`
- Test: `tests/test_dry_run_publisher.py`

**Interfaces:**
- Consumes: `ProposedChange.files_removed` (Task 2)
- Produces: no new public names

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dry_run_publisher.py`:

```python
def test_removed_paths_are_deleted_from_the_output_directory(tmp_path):
    publisher = DryRunPublisher()
    config = {"out_dir": str(tmp_path)}
    publisher.kbforge_publish(
        ProposedChange(branch_hint="sync/x", files={"a.md": "A", "b.md": "B"}), config
    )

    publisher.kbforge_publish(
        ProposedChange(branch_hint="sync/x", files={"b.md": "B2"}, files_removed=["a.md"]),
        config,
    )

    out = tmp_path / "sync-x"
    assert not (out / "a.md").exists()
    assert (out / "b.md").read_text() == "B2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_dry_run_publisher.py -k removed -v`
Expected: FAIL — `assert not True` (the file is still present)

- [ ] **Step 3: Implement**

In `src/kbforge/publishers/dry_run.py`, inside `kbforge_publish`, after the write loop and before the `MR_BODY.md` write:

```python
        for rel in change.files_removed:
            # missing_ok: the dry-run directory may be fresh, and deletion must
            # be idempotent across re-runs exactly as it is on a forge.
            (out_dir / rel).unlink(missing_ok=True)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_dry_run_publisher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/dry_run.py tests/test_dry_run_publisher.py
git commit -m "feat(dry-run): delete removed paths from the output directory"
```

---

### Task 7: Pipeline wiring — removals, honest `existing`, referrer expansion

Every publisher can now honour removals, so the pipeline starts emitting them. Three changes land together because each is unsafe without the others: emitting removals without excluding tombstones from `existing` would let law 2 resolve links to deleted concepts, and without referrer expansion an unchanged concept would keep a link to a file this run deletes.

**Files:**
- Modify: `src/kbforge/pipeline.py` (the body of `run`, around lines 113-125)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `load_all()` (Task 1), `ProposedChange.files_removed` (Task 2)
- Produces: no new public names

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py`, following this file's existing fake-connector and recording-publisher helpers:

```python
def test_a_tombstone_reaches_the_publisher_as_a_removal(tmp_path):
    """The review body already advertised '## Removed'; the change must honour it."""
    # First run establishes the concept in the mirror.
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])

    change = publisher.last_change
    assert change.files_removed == ["concepts/gone/overview.md"]


def test_a_synthesizer_cannot_decide_deletions(tmp_path):
    """Deletion is structure, not prose: whatever a synthesizer returns in
    files_removed is discarded, so an LLM cannot delete a file it dislikes."""

    class Meddling:
        def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
            change = StubSynthesizer().synthesize(changed_docs, changeset, existing_paths)
            change.files_removed = ["concepts/victim/overview.md"]
            return change

    _run_once(tmp_path, [_doc("a.md", "A")])
    publisher = _run_once(tmp_path, [_doc("a.md", "A2")], synthesizer=Meddling())

    assert "concepts/victim/overview.md" not in publisher.last_change.files_removed


def test_a_tombstoned_concept_is_not_treated_as_an_existing_link_target(tmp_path):
    """existing feeds law 2; counting a concept this run deletes would let a
    dangling link ship."""
    _run_once(tmp_path, [_doc("gone.md", "Gone"), _doc("keep.md", "Keep")])
    publisher = _run_once(
        tmp_path,
        [_doc("gone.md", "Gone", deleted=True), _doc("keep.md", "Keep2")],
    )

    concept = publisher.last_change.concepts["concepts/keep/overview.md"]
    assert "concepts/gone/overview.md" not in concept.links


def test_a_concept_linking_to_a_deleted_one_is_pulled_into_scope(tmp_path):
    """referrer is unchanged, so nothing would otherwise re-synthesize it and its
    link to the deleted concept would survive in the bundle."""
    _run_once(
        tmp_path,
        [_doc("gone.md", "Gone"), _doc("referrer.md", "Ref", relations=["sys:gone.md"])],
    )
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])

    change = publisher.last_change
    assert "concepts/referrer/overview.md" in change.files
    assert change.concepts["concepts/referrer/overview.md"].links == []
```

Add `_run_once(tmp_path, docs, synthesizer=None)` to this file if no equivalent exists: it builds a fake connector returning `docs`, runs `run(...)` against a recording publisher that stores `last_change`, and returns that publisher. Reuse the mirror and state directories across calls within a test so the second run diffs against the first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -k "removal or deletions or tombstoned or linking" -v`
Expected: FAIL — `assert [] == ['concepts/gone/overview.md']`, the meddled path present, the dangling link present, and `KeyError: 'concepts/referrer/overview.md'`

- [ ] **Step 3: Implement**

In `src/kbforge/pipeline.py`, add to the imports:

```python
from kbforge.mirror import commit, diff, load_all
```

(keeping whatever `mirror` names are already imported), then replace the scope-and-synthesize block with:

```python
    changed = set(changeset.added) | set(changeset.modified)
    changed_docs = [d for d in docs if d.doc_id in changed]  # "scope"

    # A concept linking to a deleted one must be re-synthesized, or its link
    # survives as a dangling reference (§4.4 law 2) that nothing checks: the
    # validators only inspect concepts carried by this proposal. The mirror, not
    # `docs`, is the source — an incremental connector's fetch need not contain
    # the referrer.
    removed_ids = set(changeset.removed)
    if removed_ids:
        referrers = [
            d
            for d in load_all(mirror_path)
            if d.doc_id not in changed
            and d.doc_id not in removed_ids
            and removed_ids.intersection(d.relations)
        ]
        changed_docs += referrers
        changed |= {d.doc_id for d in referrers}

    # Tombstoned docs are excluded: existing feeds law 2, and a concept this run
    # deletes must not count as a resolvable link target.
    existing = frozenset(concept_path(d.doc_id) for d in docs if not d.deleted)
    proposal = synthesizer.synthesize(changed_docs, changeset, existing)

    # Assigned here, never taken from the synthesizer: deletion is structure,
    # not prose, so an LLM synthesizer cannot delete a file it dislikes.
    proposal.files_removed = sorted(concept_path(d) for d in changeset.removed)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): emit removals, exclude tombstones from existing, expand referrers"
```

---

### Task 8: Live tests

Both defects live in the steady state, so neither is visible offline or in a single run. These are the tests that decide whether the feature works.

**Files:**
- Modify: `tests/test_forge_live.py`

**Interfaces:**
- Consumes: everything above
- Produces: no new public names

- [ ] **Step 1: Write the live tests**

Append to `tests/test_forge_live.py`, reusing this file's `_require`, `_cli`, `RUN_ID`, and the GitLab helpers:

```python
def _change(branch: str, files: dict[str, str], removed: list[str] | None = None):
    return ProposedChange(
        branch_hint=branch,
        files=files,
        files_removed=removed or [],
        summary=ChangeSummary(claims_added=sorted(files)),
    )


@pytest.mark.live
def test_gitlab_accumulates_across_runs_and_deletes_without_resurrection():
    repo = _require("KBFORGE_LIVE_GITLAB_REPO")
    _require("GITLAB_TOKEN")
    branch = f"sync/accum-{RUN_ID}"
    base_path = f"live/{RUN_ID}-accum"
    config = {"repo": repo, "base_path": base_path, "branch": branch}
    publisher = GitLabPublisher()
    alpha, beta = "concepts/alpha.md", "concepts/beta.md"

    # Run 1 — two concepts.
    publisher.kbforge_publish(_change(branch, {alpha: "A1", beta: "B1"}), config)
    assert _gl_file(repo, branch, f"{base_path}/{alpha}") == "A1"

    # Run 2 — touch only beta, do not merge. Alpha must survive: rebuilding the
    # branch from base here is the 0.3.0 data-loss bug.
    publisher.kbforge_publish(_change(branch, {beta: "B2"}), config)
    assert _gl_file(repo, branch, f"{base_path}/{alpha}") == "A1", "alpha was rebuilt away"
    assert _gl_file(repo, branch, f"{base_path}/{beta}") == "B2"

    # Run 3 — delete alpha.
    publisher.kbforge_publish(_change(branch, {beta: "B3"}, removed=[alpha]), config)
    with pytest.raises(AssertionError):
        _gl_file(repo, branch, f"{base_path}/{alpha}")

    # Run 4 — an unrelated change must not resurrect alpha. Under the 0.3.0
    # model this reset the branch to base, where alpha still exists unmerged.
    publisher.kbforge_publish(_change(branch, {beta: "B4"}), config)
    with pytest.raises(AssertionError):
        _gl_file(repo, branch, f"{base_path}/{alpha}")
    assert _gl_file(repo, branch, f"{base_path}/{beta}") == "B4"

    mrs = _gl_open_mrs(repo, branch)
    assert len(mrs) == 1, f"four runs must share one review request, got {len(mrs)}"
```

`_gl_file` raises `AssertionError` via `_cli` when the file is absent (the CLI exits non-zero on 404), which is what `pytest.raises(AssertionError)` asserts. Verify that against the helper before relying on it; if it raises something else, match the real exception.

Add the GitHub twin of this test using `GitHubPublisher`, `_gh_file`, and `_gh_open_prs`, with `branch = f"sync/accum-gh-{RUN_ID}"` and its own `base_path`. Repeat the body rather than parametrizing — the two forges' helpers differ and an explicit pair reads better than a fixture indirection.

- [ ] **Step 2: Run the live tests**

```bash
GITHUB_TOKEN=$(gh auth token) \
GITLAB_TOKEN=$(glab config get token --host gitlab.com) \
KBFORGE_LIVE_GITHUB_REPO=flyersworder/kbforge-live-test \
KBFORGE_LIVE_GITLAB_REPO=yeqi519/kbforge-live-test \
uv run pytest tests/test_forge_live.py --run-live -v
```

Expected: PASS, all four tests.

- [ ] **Step 3: Prove the accumulation test has teeth**

Temporarily revert `publish_to_forge` so `base = target` unconditionally, re-run the GitLab accumulation test, and confirm it fails at the "alpha was rebuilt away" assertion. Restore the fix and re-run to green. Report both outcomes — a live test that cannot fail proves nothing.

- [ ] **Step 4: Verify the default suite still skips them**

Run: `uv run pytest -q`
Expected: PASS with the live tests skipped.

- [ ] **Step 5: Commit**

```bash
git add tests/test_forge_live.py
git commit -m "test(live): accumulation across runs and deletion without resurrection"
```

---

### Task 9: Docs and 0.4.0 release prep

Two README bullets stop being true and must go, and one new sharp edge takes their place.

**Files:**
- Modify: `README.md` (the consequences list under "Publishing to GitHub or GitLab")
- Modify: `docs/architecture.md` (§5.2)
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml`
- Delete: `docs/design/2026-07-25-deletion-propagation-design.md`

**Interfaces:**
- Consumes: everything above
- Produces: nothing

- [ ] **Step 1: Fix the README**

Replace the two-bullet consequences list with:

```markdown
- Concepts deleted from the source are deleted from the target repo, provided the
  connector emits an explicit tombstone. Absence never implies a deletion.
- Manual commits on the sync branch are preserved — a later run builds on the
  branch while its review request is open. A hand edit to a concept kbforge
  later regenerates is overwritten by that regeneration.
```

- [ ] **Step 2: Update architecture.md §5.2**

Add after the existing publishers paragraph:

```markdown
While a review request is open, a run sets the sync branch from the branch
itself rather than from the default branch, so successive runs accumulate into
one review request. When none is open the branch is rebuilt from the default
branch, which is how a merged or abandoned request self-heals. Deletions travel
as `ProposedChange.files_removed`, assigned by the pipeline rather than by a
synthesizer: deletion is structure, not prose, so a model cannot delete a
concept it dislikes.
```

- [ ] **Step 3: Fold the spec into architecture.md and delete it**

`docs/design/` holds specs for unbuilt work only. Preserve any non-derivable rationale — the declined non-goals in §6 — then delete the spec file.

- [ ] **Step 4: Cut the CHANGELOG and bump the version**

Add a `## [0.4.0] - <today>` section above `[0.2.0]`, with `### Added` for deletion propagation and `### Fixed` for the accumulation data-loss bug, stating plainly that an unmerged review request previously lost work from earlier runs. Add the compare links. Set `version = "0.4.0"` in `pyproject.toml`; `__init__.py` needs no edit — it derives from package metadata.

- [ ] **Step 5: Verify the release artifacts**

```bash
uv run pytest -q
uv build
uv run --with twine twine check dist/kbforge-0.4.0*
```

Expected: suite passes; both artifacts PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: release 0.4.0 — docs, changelog, version bump"
```

---

## Self-Review

**Spec coverage:** §1.1 accumulation → Task 3 + Task 8. §1.2 deletions → Tasks 2, 4, 5, 6, 7. §2 decisions → Task 3 (base), Task 7 (authority), Task 4/5 (intersection). §3.1 sequence → Task 3. §3.2 authority → Task 7 (with a discriminating test that a meddling synthesizer is overruled). §3.3 referrers → Task 7. §3.4 `existing` → Task 7. §4 adapters → Tasks 4, 5, 6. §5 deletion-only runs → Task 2 (`branch_hint`). §6 non-goals → Task 9 step 3. §7 testing → Task 8. §8 build sequence → reordered so publishers honour removals before the pipeline emits them, closing the window where deletions would be silently dropped.

**Placeholder scan:** none. Every code step carries its code; every command carries its expected result.

**Type consistency:** `put_files(branch, base, files, removed, message)` is used identically in the protocol, both adapters, `FakeForgeClient`, and every test. `load_all(mirror: Path) -> list[CanonicalDocument]` matches its one caller. `_existing_paths` takes a *ref* on GitLab and a *tree SHA* on GitHub — deliberately different inputs for the same question, noted here because the shared name could otherwise mislead.
