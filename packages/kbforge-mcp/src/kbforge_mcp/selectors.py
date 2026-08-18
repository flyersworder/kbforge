"""Which documents to read. The reader is fixed; only this half varies."""

from __future__ import annotations

from kbforge_mcp.client import McpClient
from kbforge_mcp.config import McpSourceConfig
from kbforge_mcp.mapping import DocRef, refs_from_select
from kbforge_mcp.slug import SlugError, native_id_for


async def select_refs(
    client: McpClient, cfg: McpSourceConfig
) -> tuple[list[DocRef], bool]:
    """Return the selected refs and whether the selection was complete.

    `static_ids` is complete by construction -- the configured list IS the scope,
    which is what would later license tombstones. A query selector never is: it
    saw whatever the server chose to return.
    """
    if cfg.static_ids is not None:
        refs = []
        for raw in cfg.static_ids:
            try:
                refs.append(
                    DocRef(
                        raw_id=raw,
                        native_id=native_id_for(raw),
                        url=raw if "://" in raw else None,
                        title=None,
                    )
                )
            except SlugError as exc:
                raise RuntimeError(
                    f"config 'static_ids' entry unusable: {exc}"
                ) from exc
        return refs, True

    assert cfg.select is not None, "config validation guarantees one selector"
    result = await client.call(cfg.select.tool, cfg.select.args)
    return refs_from_select(result, cfg.select.ids), False
