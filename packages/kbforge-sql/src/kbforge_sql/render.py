"""Canonical entity -> fixed-format markdown (spec §4.5).

No templating: wording is the synthesizer's job. This only has to present the
row faithfully and byte-stably, because this text is ALL a grounding reader
sees of a row (`llm_synthesizer._grounding_block` passes `text`, not
`structured`)."""

from __future__ import annotations

from collections.abc import Sequence

from kbforge_sql.values import Scalar

_NULL = "—"


def _value(v: Scalar) -> str:
    if v is None:
        return _NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).replace("\n", "<br>")


def _cell(v: Scalar) -> str:
    return _value(v).replace("|", "\\|")


def render_text(
    lead: str | None, attributes: Sequence[Sequence], group: dict | None
) -> str:
    blocks: list[str] = []
    if lead:
        blocks.append(lead)
    blocks.append(
        "\n".join(
            [
                "## Attributes",
                *(f"- **{k}:** {_value(v)}" for k, v in attributes),
            ]
        )
    )
    if group is not None:
        columns = group["columns"]
        lines = [
            f"## {group['heading']}",
            "| " + " | ".join(_cell(c) for c in columns) + " |",
            "|" + "---|" * len(columns),
            *("| " + " | ".join(_cell(v) for v in row) + " |" for row in group["rows"]),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
