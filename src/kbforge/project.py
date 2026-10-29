"""The project file: `kbforge.yaml`, one knowledge base declared in one place.

Every model is `extra="forbid"` except `Component`, whose settings belong to
their owners (LLMConfig/DescribeConfig, the publisher's validate hook). No key
here reaches the pipeline's invariants: the file only chooses what the CLI
flags already choose."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StringConstraints,
    ValidationError,
)

from kbforge.chunking import ChunkingConfig

DEFAULT_FILE = "kbforge.yaml"

SOURCE_NAME = r"^[a-z][a-z0-9_-]*$"
SourceName = Annotated[str, StringConstraints(pattern=SOURCE_NAME)]


class ProjectError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class Component(BaseModel):
    """A named synthesizer or publisher plus its settings (`model_extra`)."""

    model_config = ConfigDict(extra="allow")

    name: str


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str
    config: dict[str, Any] = {}
    chunking: ChunkingConfig | None = None
    synthesizer: Component | None = None
    publisher: Component | None = None


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_dir: Path = Path(".kbforge")
    synthesizer: Component = Component(name="stub")
    publisher: Component = Component(name="dry-run")
    links: Path | dict | None = None
    grounding: Path | dict | None = None
    sources: Annotated[dict[SourceName, SourceSpec], Field(min_length=1)]

    # Not a field: a `root:` key in the YAML must be an extra-key error, since
    # the file cannot be allowed to say where it lives.
    _root: Path = PrivateAttr(default=Path("."))

    @property
    def root(self) -> Path:
        return self._root

    def mirror(self) -> Path:
        return self.root / self.state_dir / "mirror"

    def state(self) -> Path:
        return self.root / self.state_dir / "state"

    def out(self) -> Path:
        return self.root / self.state_dir / "out"


def _problems(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = [p for p in err["loc"] if p != "[key]"]
        out.append(".".join(map(str, loc)) + ": " + err["msg"])
    return out


def load_project(path: Path) -> Project:
    # Absolute before anything reads it: the CLI chdirs to the file's directory,
    # and a root taken from a relative path would then point at the wrong place.
    path = path.resolve()
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        raise ProjectError([f"{path.name}: not found in {path.parent}"]) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError([f"{path}: {exc}"]) from None
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProjectError([f"{path}: {exc}"]) from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProjectError([f"{path}: expected a mapping at the top level"])
    try:
        project = Project.model_validate(raw)
    except ValidationError as exc:
        raise ProjectError(_problems(exc)) from None
    project._root = path.parent
    # `links`/`grounding` paths are relative to the project file, not the cwd.
    if isinstance(project.links, Path):
        project.links = project._root / project.links
    if isinstance(project.grounding, Path):
        project.grounding = project._root / project.grounding
    return project
