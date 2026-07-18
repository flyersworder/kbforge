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


@dataclass(frozen=True)
class Failure:
    """One law violation, collected into a report rather than raised."""

    concept_path: str
    law: str
    message: str


def _check_type(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if not concept.type or not concept.type.strip():
        return [
            Failure(
                path,
                "okf-type",
                "concept type is empty; OKF requires a non-empty type",
            )
        ]
    return []


def _check_anchor_presence(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if len(concept.resources) < 1:
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
    return []


def run_artifact_validators(proposal: ProposedChange) -> list[Failure]:
    """Run the §4.4 laws over the proposal's concept projection.

    Empty result == conformant artifact."""
    failures: list[Failure] = []
    for path, concept in proposal.concepts.items():
        failures += _check_type(path, concept)
        failures += _check_anchor_presence(path, concept)
        failures += _check_freshness_legible(path, concept)
    return failures
