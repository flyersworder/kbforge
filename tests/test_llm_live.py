import os

import pytest

pytest.importorskip("pydantic_ai")

from datetime import UTC, datetime  # noqa: E402

from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer  # noqa: E402
from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor  # noqa: E402
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
