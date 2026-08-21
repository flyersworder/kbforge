import os
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from datetime import UTC, datetime  # noqa: E402

from kbforge.grounding import GroundingConfig  # noqa: E402
from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer  # noqa: E402
from kbforge.models import (  # noqa: E402  # noqa: E402
    CanonicalDocument,
    ChangeSet,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ResourceAnchor,
)
from kbforge.pipeline import Published, run  # noqa: E402
from kbforge.publishers.dry_run import DryRunPublisher  # noqa: E402
from kbforge.synthesize import concept_path  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402


@pytest.mark.live
def test_live_deepseek_produces_conformant_concept():
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    anchor = ResourceAnchor(
        system="local_files",
        native_id="apps/checkout.md",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="h",
    )
    doc = CanonicalDocument(
        anchor=anchor,
        doc_id="local_files:apps/checkout.md",
        title="Checkout",
        text="The checkout service accepts a cart and returns an order.",
    )
    synth = LLMSynthesizer(LLMConfig())  # default deepseek/deepseek-v4-flash
    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))
    path = concept_path(doc.doc_id)
    assert proposal.files[path].strip()
    assert run_validators(proposal, frozenset({path})) == []
    # The gate checks `generated.by` is non-blank, never its OKF §7 shape, so a
    # malformed actor passes run_validators. Assert it here, where the config is
    # the real one and the model id is provider-qualified.
    assert proposal.concepts[path].generated_by == "kbforge/deepseek-v4-flash"


def _live_doc(system: str, native_id: str, title: str, text: str):
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash=f"h-{system}-{native_id}",
        ),
        doc_id=f"{system}:{native_id}",
        title=title,
        text=text,
    )


class _LiveConnector:
    """Fixed documents, no I/O — the source under test here is the model and the
    shared mirror, not a connector."""

    def __init__(self, name: str, docs: list[CanonicalDocument]):
        self._name, self._docs = name, docs

    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(name=self._name, version="0.1.0", source_system="live")

    def kbforge_validate_config(self, config: dict) -> list[str]:
        return []

    def kbforge_fetch(self, config: dict, cursor) -> FetchResult:
        return FetchResult(records=[], cursor=Cursor(connector=self._name))

    def kbforge_normalize(self, records) -> list[CanonicalDocument]:
        return self._docs


@pytest.mark.live
def test_live_grounding_cites_both_systems_through_a_shared_mirror(tmp_path):
    """The one end-to-end proof that grounding works outside a fake.

    Every offline grounding test drives `_GroundingSynth`, which records what it
    was handed and renders a fixed body, so nothing has checked that a REAL model
    given the grounding block produces a concept that passes the §4.4 laws while
    citing both systems. It also runs two connectors against one shared mirror —
    the layout §7.1 requires and the offline tests only simulate.

    `Published` is itself an assertion: the pipeline runs `run_validators` before
    publishing and returns `Aborted` on any law violation, so reaching a URL
    means a real model's multi-source output passed the emit-side gate."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")

    ticket = _live_doc(
        "servicenow",
        "SVC0042",
        "Checkout timeouts",
        "Customers report the checkout API timing out at 30s under load. The "
        "team raised the gateway timeout to 60s as a mitigation.",
    )
    page = _live_doc(
        "confluence",
        "apps/checkout.md",
        "Checkout",
        "The checkout service accepts a cart and returns an order. It is "
        "fronted by the public API gateway.",
    )
    cfg = GroundingConfig(
        grounding={"confluence:apps/checkout.md": ["servicenow:SVC0042"]}
    )

    def _run(connector):
        return run(
            connector,
            DryRunPublisher(),
            config={"system": connector.kbforge_connector_info().name},
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={"out_dir": str(tmp_path / "out")},
            synthesizer=LLMSynthesizer(LLMConfig()),
            grounding_config=cfg,
        )

    # The grounding target syncs first, into the SAME mirror.
    first = _run(_LiveConnector("servicenow", [ticket]))
    assert isinstance(first, Published), first
    second = _run(_LiveConnector("confluence", [page]))
    assert isinstance(second, Published), second

    path = concept_path(page.doc_id)
    rendered = (Path(second.url) / path).read_text("utf-8")
    front = yaml.safe_load(rendered.split("---")[1])

    # Provenance: both systems cited, owning anchor first (§5.1, §7.1).
    assert [s["resource"] for s in front["sources"]] == [
        "confluence:apps/checkout.md",
        "servicenow:SVC0042",
    ]
    assert front["generated"]["by"] == "kbforge/deepseek-v4-flash"
    # The body must be informed by the grounding document, not merely cite it.
    assert "60" in rendered or "timeout" in rendered.lower(), rendered
