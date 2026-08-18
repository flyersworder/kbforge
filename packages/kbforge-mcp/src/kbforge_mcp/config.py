"""Config models for an MCP source, and the offline validation the CLI runs
before any network I/O.

`transport.kind` is an explicit discriminator. v0.2 of the design note carried
`server: https://...  # or a stdio command` -- two incompatible transports in one
string that `kbforge_validate_config` was expected to classify offline. There is
no sniffing here: the kind is declared or the config is invalid.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Enforces the ALL_CAPS environment-variable naming convention, which catches
# the common mistake of pasting a token where a name belongs -- `ghp_...`,
# `sk-...`, and most base64/hex secrets contain lowercase letters or
# punctuation this rejects. It is not a credential detector: an all-uppercase
# alphanumeric credential, such as an AWS access key ID
# (`AKIAIOSFODNN7EXAMPLE`), is a legal variable name under this same rule and
# passes uncaught. No regex can tell the two apart -- they are not different
# shapes. Only checking `os.environ` at call time could, and `problems_for`
# deliberately stays offline (see its docstring), so it does not.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class _Strict(BaseModel):
    # A typo in a source config must be an error, not a silently ignored key.
    model_config = ConfigDict(extra="forbid")


class StdioTransport(_Strict):
    kind: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    """Names of environment variables to pass through. Names, never values."""


class HttpTransport(_Strict):
    kind: Literal["http"]
    url: str
    auth_env: str | None = None
    """Name of the env var holding the bearer token. Never the token itself."""


class IdsMapping(_Strict):
    list: str
    """Key in `structuredContent` holding the array of result records."""
    id: str
    title: str | None = None


class SelectSpec(_Strict):
    tool: str
    args: dict = Field(default_factory=dict)
    ids: IdsMapping | None = None
    """Omitted only when the select tool returns tier-1 resource links."""


class ReadSpec(_Strict):
    tool: str
    id_arg: str
    """The argument name the reader takes the id under. It is not `id`: AWS says
    `url` and GitHub says `path`, which is why no default could be right."""
    static_args: dict = Field(default_factory=dict)
    """Constant arguments alongside the id -- GitHub's reader needs owner+repo."""
    text_key: str | None = None
    """Tier-2 only: the `structuredContent` key holding the document body."""


class McpSourceConfig(_Strict):
    system: str
    transport: StdioTransport | HttpTransport = Field(discriminator="kind")
    read: ReadSpec
    select: SelectSpec | None = None
    static_ids: list[str] | None = None
    media_type: str = "text/markdown"

    @property
    def tool_names(self) -> frozenset[str]:
        """The entire callable set. There is no third entry and no config key
        that could add one (design note §2.4)."""
        names = {self.read.tool}
        if self.select is not None:
            names.add(self.select.tool)
        return frozenset(names)


def problems_for(config: dict) -> list[str]:
    """Human-readable problems; `[]` means the config is usable. No network I/O."""
    try:
        cfg = McpSourceConfig.model_validate(config)
    except ValidationError as exc:
        return [
            f"config {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()
        ]

    problems: list[str] = []
    if cfg.select is not None and cfg.static_ids is not None:
        problems.append(
            "config 'select' and 'static_ids' are mutually exclusive: a source "
            "has one selector"
        )
    if cfg.select is None and not cfg.static_ids:
        problems.append(
            "config needs either 'select' (a select tool) or 'static_ids' (a "
            "configured id list); a server whose select tool returns only prose "
            "is supported through 'static_ids'"
        )
    auth_env = getattr(cfg.transport, "auth_env", None)
    if auth_env and not _ENV_NAME.match(auth_env):
        problems.append(
            "config 'transport.auth_env' must name an environment variable "
            f"(ALL_CAPS), not hold its value: {auth_env!r}"
        )
    if isinstance(cfg.transport, StdioTransport):
        for entry in cfg.transport.env:
            if not _ENV_NAME.match(entry):
                problems.append(
                    "config 'transport.env' entries must name environment "
                    f"variables (ALL_CAPS), not hold their values: {entry!r}"
                )
    return problems
