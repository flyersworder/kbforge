"""Which documents to read. The reader is fixed; only this half varies."""

from __future__ import annotations

from kbforge_mcp.client import McpClient
from kbforge_mcp.config import McpSourceConfig
from kbforge_mcp.mapping import DocRef, ref_for, refs_from_select


async def select_refs(
    client: McpClient, cfg: McpSourceConfig
) -> tuple[list[DocRef], bool]:
    """Return the selected refs and whether the selection was complete.

    `static_ids` is complete by construction -- the configured list IS the scope,
    which is what would later license tombstones. A query selector never is: it
    saw whatever the server chose to return.
    """
    if cfg.static_ids is not None:
        # `ref_for` owns both the slugging and the "does this id look like a
        # url" predicate that decides `.url`; building a DocRef by hand here
        # would be a second copy of a predicate mapping.py explicitly warns
        # against duplicating, and would substitute a bare RuntimeError for
        # its SlugError -> MappingError conversion.
        return [ref_for(raw, None) for raw in cfg.static_ids], True

    assert cfg.select is not None, "config validation guarantees one selector"
    result = await client.call(cfg.select.tool, cfg.select.args)
    return refs_from_select(result, cfg.select.ids), False
