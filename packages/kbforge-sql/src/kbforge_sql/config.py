"""Config model for a SQL source, and the offline validation the CLI runs
before any connection (spec §3.1). `check_columns` is the one check that needs
the query result (§3.2)."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kbforge.canonical import is_blank
from kbforge.synthesize import OKF_OWNED
from kbforge_sql.errors import SqlSourceError

# Same shapes as kbforge-mcp's config: an ALL_CAPS env var name, and a `system`
# that is safe inside a doc_id and a `sync/<system>` branch name.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*\Z")
_SYSTEM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class _Strict(BaseModel):
    # A typo in a source config must be an error, not a silently ignored key.
    model_config = ConfigDict(extra="forbid")


class GroupSpec(_Strict):
    children: list[str] = Field(min_length=1)
    order_by: list[str] = Field(default_factory=list)
    heading: str | None = None


class SqlSourceConfig(_Strict):
    system: str
    url_env: str
    password_env: str | None = None
    query: str
    id: list[str] = Field(min_length=1)
    title: str
    text: str | None = None
    facets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    type: str = "concept"
    url_template: str | None = None
    group: GroupSpec | None = None
    retries: int = Field(default=2, ge=0)
    max_removed_fraction: float = Field(default=0.5, gt=0, le=1)

    def configured_columns(self) -> list[str]:
        """Every column the config names, first mention first, no repeats."""
        cols = [*self.id, self.title]
        if self.text:
            cols.append(self.text)
        cols += [*self.facets, *self.exclude]
        if self.group is not None:
            cols += [*self.group.children, *self.group.order_by]
        return list(dict.fromkeys(cols))


def _template_fields(template: str) -> list[str] | None:
    """Field names in a str.format template, or None if it does not parse."""
    try:
        return [f for _, f, _, _ in Formatter().parse(template) if f is not None]
    except ValueError:
        return None


def problems_for(config: dict) -> list[str]:
    """Human-readable problems; `[]` means the config is usable. No connection."""
    try:
        cfg = SqlSourceConfig.model_validate(config)
    except ValidationError as exc:
        return [
            f"config {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()
        ]

    problems: list[str] = []
    if not _SYSTEM_NAME.match(cfg.system):
        problems.append(
            "config 'system' must be a short identifier -- letters, digits, '_' "
            "and '-', starting with a letter or digit -- because it is part of "
            f"every doc_id and the publish branch: {cfg.system!r}"
        )
    for key, name in (("url_env", cfg.url_env), ("password_env", cfg.password_env)):
        if name is None:
            continue
        if not _ENV_NAME.match(name):
            # The value is NOT echoed: a name that fails this shape is most
            # likely a pasted URL or password.
            problems.append(
                f"config '{key}' must name an environment variable (ALL_CAPS), "
                "not hold its value"
            )
        elif name not in os.environ:
            problems.append(f"config '{key}': environment variable {name} is not set")
    if is_blank(cfg.query):
        problems.append("config 'query' is blank")
    if is_blank(cfg.type):
        problems.append("config 'type' is blank; OKF requires a non-empty type")
    if is_blank(cfg.title):
        problems.append("config 'title' is blank")
    if any(is_blank(c) for c in cfg.id):
        problems.append("config 'id' has a blank column name")

    used = {*cfg.id, cfg.title, *cfg.facets}
    if cfg.text:
        used.add(cfg.text)
    if clash := used & set(cfg.exclude):
        problems.append(
            f"config 'exclude' names column(s) the source also uses: {sorted(clash)}"
        )
    if owned := sorted(set(cfg.facets) & OKF_OWNED):
        problems.append(
            f"config 'facets' must not use OKF-owned key(s) {owned}: the emitter "
            "drops them, so the facet would silently never appear"
        )

    if cfg.group is not None:
        children = set(cfg.group.children)
        if ids := sorted(children & set(cfg.id)):
            problems.append(
                f"config 'group.children' must not include id column(s) {ids}"
            )
        entity = {cfg.title, *cfg.facets} | ({cfg.text} if cfg.text else set())
        if both := sorted(children & entity):
            problems.append(
                "config 'group.children' must not include title, text or facet "
                f"column(s) {both}: they describe the entity, not a child"
            )
        if extra := [c for c in cfg.group.order_by if c not in children]:
            problems.append(
                f"config 'group.order_by' must be a subset of 'group.children': {extra}"
            )

    if cfg.url_template is not None:
        fields = _template_fields(cfg.url_template)
        if fields is None:
            problems.append("config 'url_template' is not a valid format string")
        elif bad := sorted({f for f in fields if f not in cfg.id}):
            problems.append(
                f"config 'url_template' may only reference id column(s): {bad}"
            )
    return problems


def check_columns(cfg: SqlSourceConfig, columns: Sequence[str]) -> None:
    """Spec §3.2: every configured column must be in the result."""
    missing = [c for c in cfg.configured_columns() if c not in columns]
    if missing:
        raise SqlSourceError(
            f"configured column(s) {missing} are not in the query result; "
            f"the query returned: {list(columns)}"
        )
