from pathlib import Path

import pytest

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.models import ConnectorInfo
from kbforge.pipeline import run
from kbforge.publishers._http import ForgeError

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


class ExplodingPublisher:
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="exploding", version="1.0", source_system="Test")

    def kbforge_publish(self, change, config) -> str:
        raise ForgeError(500, "https://api.example/x", "boom")


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list = []

    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="recording", version="1.0", source_system="Test")

    def kbforge_publish(self, change, config) -> str:
        self.published.append(change)
        return "https://forge.example/pr/1"


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app-x.md").write_text(DOC, "utf-8")
    return src


def test_failed_publish_does_not_advance_the_mirror(tmp_path: Path):
    src = _source(tmp_path)
    mirror = tmp_path / "mirror"
    state = tmp_path / "state"

    with pytest.raises(ForgeError):
        run(
            LocalFilesConnector(),
            ExplodingPublisher(),
            config={"path": str(src)},
            mirror=str(mirror),
            state_dir=str(state),
            publish_config={},
        )

    # Nothing was committed, so a retry still sees the change.
    from kbforge.pipeline import Published

    recorder = RecordingPublisher()
    result = run(
        LocalFilesConnector(),
        recorder,
        config={"path": str(src)},
        mirror=str(mirror),
        state_dir=str(state),
        publish_config={},
    )

    assert isinstance(result, Published)
    assert result.url == "https://forge.example/pr/1"
    assert len(recorder.published) == 1
    assert recorder.published[0].files  # the change survived the failure


def test_successful_publish_advances_the_mirror_so_a_rerun_is_a_noop(tmp_path: Path):
    from kbforge.pipeline import NoOp

    src = _source(tmp_path)
    mirror = tmp_path / "mirror"
    state = tmp_path / "state"

    run(
        LocalFilesConnector(),
        RecordingPublisher(),
        config={"path": str(src)},
        mirror=str(mirror),
        state_dir=str(state),
        publish_config={},
    )
    second = run(
        LocalFilesConnector(),
        RecordingPublisher(),
        config={"path": str(src)},
        mirror=str(mirror),
        state_dir=str(state),
        publish_config={},
    )

    assert isinstance(second, NoOp)
