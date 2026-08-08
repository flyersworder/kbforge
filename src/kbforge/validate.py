"""Agent-facing artifact validators — the §4.4 laws, enforced core.

These run in the pipeline's validate stage (architecture §7) over a
ProposedChange's `concepts` projection. A non-empty result aborts the run; no
MR opens for a non-conformant artifact. kbforge checks synthesis output rather
than trusting it (spec §5), so every law is a runtime check that returns a
report — never a construction-time crash.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime

import yaml

from kbforge.models import ConceptFrontmatter, ProposedChange

_SCALAR = (str, int, float, bool)

# OKF reserved filenames that carry no frontmatter, hence no projection.
_RESERVED = frozenset({"index.md", "log.md"})


def _blank(value: object) -> bool:
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


def _instant(value: object) -> datetime | None:
    """Parse a rendered `at` to an aware instant, or None if it is not one.

    PyYAML hands back a `datetime` for an unquoted stamp and a `str` for a quoted
    one, so comparing serialized text makes the verdict depend on quoting —
    `'...Z'` and `'...+00:00'` are the same moment and OKF's own examples use the
    `Z` spelling. Compare instants."""
    if isinstance(value, datetime):
        return value if value.utcoffset() is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


@dataclass(frozen=True)
class Failure:
    """One law violation, collected into a report rather than raised."""

    concept_path: str
    law: str
    message: str


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _check_projection_coherence(proposal: ProposedChange) -> list[Failure]:
    """The four laws run only over `concepts`, but the publisher writes `files`.
    If the two disagree, a rendered concept file can ship to the bundle with no
    projection — silently unvalidated. Bind the carrier: every non-reserved file
    MUST have a projection, and every projection MUST have a rendered file. Without
    this, `run_artifact_validators() == []` does not entail "the artifact is
    conformant" — a producer defeats the gate by omission, not by emitting
    something wrong."""
    failures: list[Failure] = []
    concept_files = {p for p in proposal.files if _basename(p) not in _RESERVED}
    for path in sorted(concept_files - set(proposal.concepts)):
        failures.append(
            Failure(
                path,
                "projection-coherence",
                "rendered file has no ConceptFrontmatter projection; it would "
                "ship unvalidated (§4.4 gate)",
            )
        )
    for path in sorted(set(proposal.concepts) - set(proposal.files)):
        failures.append(
            Failure(
                path,
                "projection-coherence",
                "concept projection has no rendered file in the proposal (§4.4 gate)",
            )
        )
    return failures


def _check_type(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if _blank(concept.type):
        return [
            Failure(
                path,
                "okf-type",
                "concept type is empty; OKF requires a non-empty type",
            )
        ]
    return []


def _check_anchor_presence(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if not concept.sources:
        return [
            Failure(
                path,
                "anchor-presence",
                "concept carries no source anchor (§4.4 law 3)",
            )
        ]
    return []


def _check_freshness_legible(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if concept.generated_at is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept carries no freshness stamp (§4.4 law 4)",
            )
        ]
    if concept.generated_at.utcoffset() is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept freshness stamp is timezone-naive; whats_stale needs an "
                "aware datetime (§4.4 law 4)",
            )
        ]
    return []


def _is_filterable(value: object) -> bool:
    if isinstance(value, _SCALAR):
        return True
    if isinstance(value, list):
        return all(isinstance(v, _SCALAR) for v in value)
    return False


def _check_facets_wellformed(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    failures: list[Failure] = []
    for key, value in concept.facets.items():
        if value in (None, "", [], {}):
            failures.append(
                Failure(
                    path,
                    "facet-wellformedness",
                    f"facet {key!r} is empty; a filterable facet must carry a "
                    "value (§4.4 law 1)",
                )
            )
        elif not _is_filterable(value):
            failures.append(
                Failure(
                    path,
                    "facet-wellformedness",
                    f"facet {key!r} must be a scalar or flat list to be "
                    "filterable (§4.4 law 1)",
                )
            )
    return failures


def _check_links_resolve(
    proposal: ProposedChange, existing_paths: frozenset[str]
) -> list[Failure]:
    known = set(proposal.files) | set(proposal.concepts) | set(existing_paths)
    failures: list[Failure] = []
    for path, concept in proposal.concepts.items():
        for link in concept.links:
            if link not in known:
                failures.append(
                    Failure(
                        path,
                        "link-resolvability",
                        f"link {link!r} resolves to no concept in the bundle "
                        "(§4.4 law 2)",
                    )
                )
    return failures


def run_artifact_validators(
    proposal: ProposedChange,
    existing_paths: frozenset[str] = frozenset(),
) -> list[Failure]:
    """Check projection↔files coherence, then run the four §4.4 laws over the
    proposal's concept projection.

    Empty result == conformant artifact. The coherence check runs first so that
    `[] == conformant` cannot be defeated by a file that ships without a
    projection. `existing_paths` are bundle-root-relative paths already on `main`,
    so law 2 resolves links to concepts this change does not itself carry."""
    failures: list[Failure] = []
    failures += _check_projection_coherence(proposal)
    for path, concept in proposal.concepts.items():
        failures += _check_type(path, concept)
        failures += _check_facets_wellformed(path, concept)
        failures += _check_anchor_presence(path, concept)
        failures += _check_freshness_legible(path, concept)
    failures += _check_links_resolve(proposal, existing_paths)
    return failures


# Stricter than OKF §11, which requires only a non-empty `type`. kbforge is a
# producer: it holds its own output to `title`/`description`/`generated` so a
# rendered file can never satisfy the §4.4 projection while omitting the stamp
# law 4 depends on. Consumers stay permissive; producers do not have to be.
_STRICT_REQUIRED = ("type", "title", "description", "generated")


def _parse_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    _, _, rest = content.partition("---")
    front_raw, sep, _ = rest.partition("\n---")
    if not sep:
        return {}
    try:
        data = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _check_generated_shape(
    path: str, front: dict, concept: ConceptFrontmatter | None
) -> list[Failure]:
    """`generated` is a mapping, so the presence loop cannot judge it: `{}` and
    `{by: ''}` are both non-None and both useless. OKF §5.2 makes `by` REQUIRED
    within the block, and law 4 is worthless without `at`.

    The last check binds the two carriers. `_check_projection_coherence` binds
    path *sets*; nothing bound field *values*, so law 4 could pass on a
    projection carrying `generated_at` while the file it ships alongside carried
    a different stamp, or none. `whats_stale` reads the file."""
    generated = front.get("generated")
    if not isinstance(generated, dict):
        return [
            Failure(
                path,
                "okf-strict",
                "rendered 'generated' must be a mapping of 'by' and 'at' (OKF §5.2)",
            )
        ]
    failures: list[Failure] = []
    if _blank(generated.get("by")):
        failures.append(
            Failure(
                path,
                "okf-strict",
                "rendered 'generated.by' is empty; OKF §5.2 requires an actor",
            )
        )
    at = generated.get("at")
    if at is None or (isinstance(at, str) and not at.strip()):
        failures.append(
            Failure(
                path,
                "okf-strict",
                "rendered 'generated.at' is missing; whats_stale reads the file, "
                "not the projection (§4.4 law 4)",
            )
        )
        return failures
    instant = _instant(at)
    if instant is None:
        failures.append(
            Failure(
                path,
                "okf-strict",
                f"rendered 'generated.at' {at!r} is not a timezone-aware ISO 8601 "
                "instant; whats_stale cannot compare it (§4.4 law 4)",
            )
        )
    elif concept is not None and concept.generated_at is not None:
        if instant != concept.generated_at:
            failures.append(
                Failure(
                    path,
                    "okf-strict",
                    "rendered 'generated.at' disagrees with the concept "
                    "projection's generated_at; law 4 checks one and "
                    "whats_stale reads the other",
                )
            )
    return failures


def _expected_resources(concept: ConceptFrontmatter) -> set[str]:
    """The `resource` values a conformant render of this projection must carry —
    the same rule `synthesize._source_entry` applies."""
    return {a.url or f"{a.system}:{a.native_id}" for a in concept.sources}


def _check_sources_shape(
    path: str, front: dict, concept: ConceptFrontmatter | None
) -> list[Failure]:
    """Law 3 checks the projection; this checks the file that actually ships.

    Presence alone was not enough: `sources: "see the wiki"` is truthy. OKF §5.1
    requires a list of entries each carrying a REQUIRED `resource`, and the set
    of those must be the set the validated anchors imply — otherwise a concept
    can cite provenance the gate never saw, which is worse than citing none."""
    sources = front.get("sources")
    if not isinstance(sources, list) or not sources:
        return [
            Failure(
                path,
                "okf-strict",
                "rendered 'sources' must be a non-empty list of entries (§4.4 "
                "law 3, OKF §5.1)",
            )
        ]
    failures: list[Failure] = []
    for entry in sources:
        if not isinstance(entry, dict) or _blank(entry.get("resource")):
            failures.append(
                Failure(
                    path,
                    "okf-strict",
                    "every rendered 'sources' entry needs a non-empty 'resource' "
                    "(REQUIRED by OKF §5.1)",
                )
            )
    if failures or concept is None or not concept.sources:
        return failures
    rendered = {e["resource"] for e in sources if isinstance(e, dict)}
    if rendered != _expected_resources(concept):
        failures.append(
            Failure(
                path,
                "okf-strict",
                "rendered 'sources' cite resources the validated anchors do not; "
                "the concept would ship provenance the gate never checked",
            )
        )
    return failures


def _check_strict_okf(proposal: ProposedChange) -> list[Failure]:
    failures: list[Failure] = []
    for path, content in proposal.files.items():
        front = _parse_frontmatter(content)
        # A directory listing carries no frontmatter (OKF §8), which is the whole
        # reason it is exempt. A file *named* index.md that opens a frontmatter
        # fence is claiming to be a concept, and exempting it would skip both the
        # strict checks and projection coherence. Key on the raw fence, not on
        # the parsed result: `_parse_frontmatter` returns {} for unparseable YAML
        # too, so "no frontmatter" and "broken frontmatter" would look alike.
        if _basename(path) in _RESERVED and not content.lstrip().startswith("---"):
            continue
        concept = proposal.concepts.get(path)
        for key in _STRICT_REQUIRED:
            value = front.get(key)
            # `generated` is a mapping; _check_generated_shape judges it below.
            blank = value is None if key == "generated" else _blank(value)
            if blank:
                failures.append(
                    Failure(
                        path,
                        "okf-strict",
                        f"rendered concept is missing required OKF field {key!r}",
                    )
                )
        if front.get("generated") is not None:
            failures += _check_generated_shape(path, front, concept)
        failures += _check_sources_shape(path, front, concept)
        if concept is not None:
            failures += _check_carriers_agree(path, front, concept)
    return failures


def _check_carriers_agree(
    path: str, front: dict, concept: ConceptFrontmatter
) -> list[Failure]:
    """Bind the OKF-owned keys the laws govern to the file that ships them.

    Every §4.4 law runs on the projection, but the bundle receives the file. The
    two are built by the same code today, so binding only `generated.at` looked
    sufficient — it is not. `synthesize._render` merges facets into top-level
    frontmatter, so a facet named `type` or `links` (a plugin connector passing
    an upstream record straight into `structured` is the obvious way) silently
    overwrites the OKF field in the file while the projection keeps the good
    value. Law 1 approves the facet, law 2 approves the empty projection links,
    and a boolean `type` ships. Bind the values, and every law provably governs
    the artifact rather than a parallel copy of it."""
    failures: list[Failure] = []
    if not _blank(front.get("type")) and front.get("type") != concept.type:
        failures.append(
            Failure(
                path,
                "okf-strict",
                f"rendered 'type' {front.get('type')!r} disagrees with the "
                f"projection's {concept.type!r}; the laws checked the projection",
            )
        )
    if front.get("links", []) != concept.links:
        failures.append(
            Failure(
                path,
                "okf-strict",
                "rendered 'links' disagree with the projection's; law 2 resolves "
                "the projection, so a link only in the file is never checked",
            )
        )
    return failures


def run_validators(
    proposal: ProposedChange,
    existing_paths: frozenset[str] = frozenset(),
) -> list[Failure]:
    """The full validate stage (§7): strict-OKF checks over the rendered `files`
    plus the four §4.4 agent-facing laws over the `concepts` projection."""
    return _check_strict_okf(proposal) + run_artifact_validators(
        proposal, existing_paths
    )
