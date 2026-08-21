# kbforge — working notes for coding agents

kbforge turns a system of record into a reviewed [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
v0.2 bundle: fetch → normalize → mirror → diff → scope → synthesize → validate →
publish. `docs/architecture.md` is the spec; sections headed **not built** are
specification, not shipped code.

```bash
uv sync --all-extras --dev
prek install                 # ruff + ty on every commit
uv run pytest                # never touches the network
uv run pytest --run-live     # opt-in; calls a real forge / LLM (needs credentials)
```

## Invariants — do not weaken these

These are the trust guarantees. Each is currently enforced *structurally*, and
that is the point: a guarantee you can only break by deleting code is worth more
than one held up by convention.

- **The pipeline order is fixed and not pluggable.** Plugins extend stages; they
  cannot reorder or remove them. Same for the two rules below — making them
  configurable would make them optional.
- **The no-op rule.** A run synthesizes only when something a concept is built
  from has changed, and returns `NoOp()` *before* synthesis otherwise. That is
  `ChangeSet.is_noop` **and** no grounding drift (§7.1) — grounding added a
  second thing a concept is built from, so the rule covers both or it stops
  meaning anything. It is still never "open a review request and see": no
  review request is ever opened for a concept nothing changed under. This is
  also what makes `generated.at` honest and the token bill bounded.
- **kbforge never merges.** No publisher defines a merge method — check with
  `grep -rn 'def .*merge' src/kbforge/publishers/`. Keep it that way; don't add
  one "just for tests".
- **The §4.4 emit-side laws are core validators.** The additive extension hook
  they must never move into is specified in architecture.md §5.3 but not built;
  if you build it, the laws stay core. Opt-in trust guarantees aren't guarantees.
- **`normalize` is pure** — no network, no clock, no randomness. `assert_stability`
  runs it twice per run and rejects a connector whose output differs.
- **One bundle path, one owner.** `concept_path` drops the system prefix, so
  `wiki:readme` and `notes:readme` render the same file. The mirror is shared
  across systems (§7.1 requires it), so the pipeline aborts on a collision and
  on a cross-system relation rather than letting one system's concept overwrite
  another's on merge. System-qualified paths would fix this at the root; that
  rewrites every published path, so it is its own release, not a patch.
- **Deletions are explicit tombstones** (`CanonicalDocument.deleted=True`).
  Absence from an incremental fetch never implies deletion; `FetchResult.complete`
  exists so a rate-limited partial fetch can't manufacture removals.

## The dual-carrier rule (where the bugs live)

`ProposedChange` carries the same concept twice: `.files` is the rendered markdown
the publisher writes, `.concepts` is the `ConceptFrontmatter` projection the laws
check. **A consumer reads the file; the gate reads the projection.** If they
disagree, a concept ships unvalidated while `run_validators() == []` says it's fine.

Two mechanisms keep them honest, and both must survive any refactor:

- `_check_projection_coherence` binds the path *sets* — every non-reserved file has
  a projection and vice versa.
- `_check_strict_okf` binds the *values* for the keys with a projection counterpart
  (`type`, `links`, `generated.at`, `sources`).

Facets merge into top-level frontmatter, so a source field named like an OKF key
would shadow it in the file while the projection kept the good value. `_facets`
drops anything in `OKF_OWNED` for exactly this reason. If you touch either side,
re-read `synthesize._render` and `validate._check_carriers_agree` together.

## Emit-side vocabulary is OKF v0.2

Provenance is `sources` (§5.1), freshness is `generated: {by, at}` (§5.2). The v0.1
keys `resource` (as a list) and `timestamp` are retired and must not come back —
`local_files` keeps them reserved so a v0.1-era source document can't reintroduce
one as a facet. `generated.by` follows the §7 actor convention `<producer>/<version>`,
which is two segments: `actor_for()` exists because model ids are often
provider-qualified and interpolating one whole yields three.

## Verifying a gate

A test over a gate is worth what it catches. Break the thing it guards and confirm
it fails, mutating **in place** and restoring with `git checkout --` — a `cp -R` of
the repo keeps a `.venv` that resolves the package to the original source, so
nothing is actually mutated and everything passes. Assert on failure *messages*,
not just law slugs, or a test passes on a different violation than the one it names.

## Docs layout

`docs/architecture.md` is the library spec. `docs/design/` holds specs for
**unbuilt** work only — fold one into `architecture.md` once it ships, keeping just
the rationale. `docs/context/` holds the originating system design; leave it there,
it's a different subject. A doc drifts when it restates something the code owns.
