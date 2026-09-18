"""The one exception kbforge-sql raises."""


class SqlSourceError(RuntimeError):
    """A SQL source failed in a way an operator must fix or retry.

    Raised unprefixed by the helpers; `kbforge_fetch` re-raises it once with
    `sql source '<system>': ` in front, so every message names its source
    exactly once."""
