"""Agent-facing artifact validators — the §4.4 laws, enforced core.

These run in the pipeline's validate stage (architecture §7) over a
ProposedChange's `concepts` projection. A non-empty result aborts the run; no
MR opens for a non-conformant artifact. kbforge checks synthesis output rather
than trusting it (spec §5), so every law is a runtime check that returns a
report — never a construction-time crash.
"""

from __future__ import annotations

from dataclasses import dataclass

from kbforge.models import ConceptFrontmatter, ProposedChange

_SCALAR = (str, int, float, bool)

# OKF reserved filenames that carry no frontmatter, hence no projection.
_RESERVED = frozenset({"index.md", "log.md"})


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
    if not concept.type.strip():
        return [
            Failure(
                path,
                "okf-type",
                "concept type is empty; OKF requires a non-empty type",
            )
        ]
    return []


def _check_anchor_presence(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if not concept.resources:
        return [
            Failure(
                path,
                "anchor-presence",
                "concept carries no resource anchor (§4.4 law 3)",
            )
        ]
    return []


def _check_freshness_legible(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if concept.freshness is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept carries no freshness stamp (§4.4 law 4)",
            )
        ]
    if concept.freshness.utcoffset() is None:
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
