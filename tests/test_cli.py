import os
import subprocess
from pathlib import Path
from unittest import mock

import pluggy
import pytest

from kbforge.__main__ import _parse_settings, main
from kbforge.canonical import FetchContractError
from kbforge.hookspecs import CONNECTOR_ENTRYPOINTS, PUBLISHER_ENTRYPOINTS
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={"PATH": os.environ.get("PATH", ""), **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror",
        str(tmp_path / "mirror"),
        "--out",
        str(tmp_path / "out"),
        "--state",
        str(tmp_path / "state"),
    ]


def test_registry_exposes_connectors_and_publisher():
    pm = build_registry()
    names = {p.__class__.__name__ for p in pm.get_plugins()}
    assert {"LocalFilesConnector", "GitCommitsConnector", "DryRunPublisher"} <= names


def test_registry_loads_setuptools_entrypoints(monkeypatch):
    # The drop-in seam: build_registry must ask pluggy to discover third-party
    # plugins advertised under the connector and publisher entry-point groups.
    seen: list[str] = []

    def spy(self, group, name=None):
        seen.append(group)
        return 0

    monkeypatch.setattr(pluggy.PluginManager, "load_setuptools_entrypoints", spy)
    build_registry()
    assert seen == [CONNECTOR_ENTRYPOINTS, PUBLISHER_ENTRYPOINTS]


def test_parse_settings_yaml_types_values():
    cfg = _parse_settings(["path=/docs", "max_commits=5", "ignore_globs=[drafts, x]"])
    assert cfg["path"] == "/docs"  # str
    assert cfg["max_commits"] == 5  # int, not "5"
    assert cfg["ignore_globs"] == ["drafts", "x"]  # list


def test_parse_settings_rejects_missing_equals():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _parse_settings(["justakey"])


@pytest.mark.parametrize(
    "value", ["Keep it short #mandatory", "[a, b] # c", '"quoted" # c', "x #"]
)
def test_parse_settings_rejects_a_value_yaml_would_cut_at_a_comment(value):
    """YAML reads ` #` as a comment and drops the rest, so an unquoted
    `instructions=Keep it short #mandatory` reached the model as `Keep it
    short`, with nothing to say text was lost (#44 review)."""
    with pytest.raises(ValueError, match="comment") as err:
        _parse_settings([f"instructions={value}"])
    assert "instructions" in str(err.value) and "quote" in str(err.value)


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ('"Keep it short #mandatory"', "Keep it short #mandatory"),
        ("issue#44", "issue#44"),  # no space before '#': not a comment
        ("[a, '#b']", ["a", "#b"]),
        ("", None),
    ],
)
def test_parse_settings_keeps_a_hash_yaml_does_not_read_as_a_comment(value, parsed):
    assert _parse_settings([f"k={value}"]) == {"k": parsed}


def test_list_command_shows_connectors(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "local_files" in out
    assert "git_commits" in out


def test_cli_run_local_files_via_generic_config(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert (tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md").exists()


def test_cli_run_git_commits_via_generic_config(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("1")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "initial commit")
    code = main(
        [
            "run",
            "--connector",
            "git_commits",
            "--set",
            f"repo={repo}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert len(list((tmp_path / "out" / "sync-git_commits").rglob("overview.md"))) == 1


def test_cli_unknown_connector_lists_available(tmp_path: Path, capsys):
    code = main(["run", "--connector", "jira", "--set", "x=1", *_plumbing(tmp_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "unknown connector" in out
    assert "local_files" in out and "git_commits" in out  # available list shown


def test_cli_config_error_surfaces_nonzero(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={tmp_path / 'nope'}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "path" in capsys.readouterr().out  # the connector's config problem


def test_list_shows_every_synthesizer_run_accepts(capsys):
    """`list` and `--synthesizer` drew from two hand-kept lists, and `describe`
    reached one but not the other. Both now read one table."""
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    listed = out.split("synthesizers:\n", 1)[1].split("publishers:", 1)[0]
    names = [line.split("\t", 1)[0].strip() for line in listed.splitlines()]
    assert names == ["stub", "llm", "describe"], listed


def test_run_stub_synthesizer_default(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0 and "Published" in capsys.readouterr().out


def test_run_llm_synthesizer_offline(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kbforge import llm_synthesizer
    from kbforge.llm_synthesizer import SynthesizedConcept

    def fake_agent(config):
        def fn(messages, info: AgentInfo):
            c = SynthesizedConcept(title="T", description="D", body="Body from LLM.")
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, c.model_dump())]
            )

        return Agent(FunctionModel(fn), output_type=SynthesizedConcept)

    monkeypatch.setattr(
        llm_synthesizer.LLMSynthesizer, "_build_agent", staticmethod(fake_agent)
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--synthesizer",
            "llm",
            "--llm-set",
            "model=deepseek/deepseek-v4-flash",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0 and "Published" in capsys.readouterr().out
    assert (
        "Body from LLM."
        in (
            tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md"
        ).read_text()
    )


def test_run_llm_synthesizer_missing_extra_is_clean_cli_error(
    tmp_path: Path, capsys, monkeypatch
):
    pytest.importorskip("pydantic_ai")

    from kbforge import llm_synthesizer

    def raise_missing_extra(config):
        raise ImportError("install kbforge[llm]")

    monkeypatch.setattr(
        llm_synthesizer.LLMSynthesizer,
        "_build_agent",
        staticmethod(raise_missing_extra),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--synthesizer",
            "llm",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "install kbforge[llm]" in capsys.readouterr().out


@pytest.mark.parametrize("synthesizer", ["llm", "describe"])
def test_instructions_that_yaml_reads_as_a_mapping_exit_2(
    tmp_path: Path, capsys, monkeypatch, synthesizer
):
    """`--llm-set` values are YAML-typed, so an unquoted instruction containing
    `: ` arrives as a dict. It crashed prompt assembly with AttributeError, for
    `describe` since #40 and for `llm` in the first cut of #44, found live."""
    pytest.importorskip("pydantic_ai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--synthesizer",
            synthesizer,
            "--llm-set",
            "instructions=End with a line that reads: DONE",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "instructions must be a string" in capsys.readouterr().out


def test_a_malformed_grounding_map_exits_2_before_fetching(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text("---\ntitle: X\n---\nbody\n", "utf-8")
    g = tmp_path / "g.yaml"
    g.write_text("grounding:\n  payments:\n    - servicenow:SVC0042\n", "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--grounding",
            str(g),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "qualified doc_id" in capsys.readouterr().out


def test_grounding_rules_with_the_stub_synthesizer_prints_an_inactive_notice(
    tmp_path: Path, capsys
):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    g = tmp_path / "g.yaml"
    g.write_text(
        "rules:\n"
        "  - for:\n"
        "      type: product\n"
        "    from:\n"
        "      system: web\n"
        "    match:\n"
        "      - '{native_id}'\n",
        "utf-8",
    )
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--grounding",
            str(g),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "grounding rules are validated but inactive: the stub synthesizer "
        "does not ground; use --synthesizer llm"
    ) in out


def test_a_rules_free_grounding_config_prints_no_inactive_notice(
    tmp_path: Path, capsys
):
    """The notice is specific to declared rules; a subject map alone -- the
    grounding this CLI already supported -- must not trip it."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    g = tmp_path / "g.yaml"
    g.write_text("grounding: {}\n", "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--grounding",
            str(g),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "grounding rules are validated but inactive" not in out


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("missing.yaml", None),  # FileNotFoundError
        ("bad.yaml", "grounding: [unclosed\n"),  # yaml.YAMLError
        ("wrong.yaml", "groundings: {}\n"),  # pydantic ValidationError (extra=forbid)
        ("typed.yaml", "max_grounding_docs: not-a-number\n"),  # ValidationError
    ],
)
def test_an_unreadable_grounding_file_exits_2_with_a_message(
    tmp_path: Path, capsys, name: str, body: str | None
):
    """`load_grounding` was the one unguarded call left in the CLI: a typo in a
    --grounding path, or a stray tab in the YAML, reached the terminal as a
    traceback while every other operator mistake exits 2 with a sentence."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text("---\ntitle: X\n---\nbody\n", "utf-8")
    g = tmp_path / name
    if body is not None:
        g.write_text(body, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--grounding",
            str(g),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert str(g) in out  # which file, not just "something went wrong"
    assert "grounding config" in out


def test_a_non_utf8_grounding_file_exits_2_with_a_message(tmp_path: Path, capsys):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError`, so it slipped
    past the guard above and reached the terminal as the traceback that guard
    exists to prevent."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text("---\ntitle: X\n---\nbody\n", "utf-8")
    g = tmp_path / "latin1.yaml"
    g.write_bytes(b"grounding:\n  sys:\xe9: []\n")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--grounding",
            str(g),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert str(g) in out and "grounding config" in out


def test_cli_reports_a_fetch_contract_violation_as_a_message(tmp_path, capsys):
    """A third-party connector tripping the new law is the only thing most
    plugin authors will see of this release; a traceback is the wrong first
    impression."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# A\n\nbody\n", "utf-8")

    with mock.patch(
        "kbforge.pipeline.assert_fetch_contract",
        side_effect=FetchContractError("duplicate doc_id in fetch output: sys:a.md"),
    ):
        code = main(
            [
                "run",
                "--connector",
                "local_files",
                "--set",
                f"path={src}",
                *_plumbing(tmp_path),
            ]
        )

    assert code == 2
    out = capsys.readouterr().out
    assert "Connector contract violation (local_files):" in out
    assert "duplicate doc_id in fetch output: sys:a.md" in out


def test_a_synthesis_failure_is_one_line_not_a_traceback(tmp_path, capsys, monkeypatch):
    """#35: an invalid model output used to escape as a ~150-line traceback."""
    pytest.importorskip("pydantic_ai")
    from kbforge import __main__ as cli
    from kbforge.llm_synthesizer import SynthesisError

    def boom(*args, **kwargs):
        raise SynthesisError("concepts/x/overview.md: model output hit max_tokens=1500")

    monkeypatch.setattr(cli, "run", boom)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(["run", "--connector", "local_files", "--set", f"path={src}",
                 "--synthesizer", "llm", *_plumbing(tmp_path)])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1
    assert out.strip() == (
        "Synthesis failed: concepts/x/overview.md: model output hit max_tokens=1500"
        " (nothing was published; the next run retries)"
    ), out


def test_run_describe_synthesizer_offline(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kbforge import llm_synthesizer

    real = llm_synthesizer.DescribeSynthesizer._build_agent

    def fake_agent(config, model=None):
        def fn(messages, info: AgentInfo):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"description": "Says what X is.", "tags": []},
                    )
                ]
            )

        return real(config, model=FunctionModel(fn))

    monkeypatch.setattr(
        llm_synthesizer.DescribeSynthesizer, "_build_agent", staticmethod(fake_agent)
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(["run", "--connector", "local_files", "--set", f"path={src}",
                 "--synthesizer", "describe",
                 "--llm-set", "tags_vocabulary={x: [App X]}",
                 *_plumbing(tmp_path)])  # fmt: skip
    assert code == 0 and "Published" in capsys.readouterr().out
    text = (
        tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md"
    ).read_text()
    assert "description: Says what X is." in text and "- x" in text


def test_describe_rejects_a_bad_vocabulary(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code = main(["run", "--connector", "local_files", "--set", f"path={tmp_path}",
                 "--synthesizer", "describe", "--llm-set", "tags_vocabulary={x: [' ']}",
                 *_plumbing(tmp_path)])  # fmt: skip
    assert code == 2
    assert (
        "tags_vocabulary['x'] must be a list of non-blank phrases"
        in capsys.readouterr().out
    )


def test_describe_rejects_a_non_string_vocabulary_key(
    tmp_path: Path, capsys, monkeypatch
):
    """Important 2 (#40 review): `--llm-set` values are YAML-typed, so
    `tags_vocabulary={2024: [x]}` gives an int key. That used to pass
    `validate_env` unchecked and crash later with a raw traceback (a
    TypeError sorting or joining a set of tags that mixes `int` and `str`).
    It must instead exit 2 with one sentence, like every other operator
    mistake this file covers."""
    pytest.importorskip("pydantic_ai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code = main(["run", "--connector", "local_files", "--set", f"path={tmp_path}",
                 "--synthesizer", "describe",
                 "--llm-set", "tags_vocabulary={2024: [x]}",
                 *_plumbing(tmp_path)])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 2
    assert "tags_vocabulary has a non-string or blank tag: 2024" in out
    assert "Traceback" not in out


def _two_docs(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("---\ntitle: A\n---\nA.\n", "utf-8")
    (src / "b.md").write_text("---\ntitle: B\n---\nB.\n", "utf-8")
    return src


def test_links_flag_renders_a_related_section(tmp_path: Path, capsys):
    src = _two_docs(tmp_path)
    links = tmp_path / "links.yaml"
    links.write_text(
        "links:\n  local_files:a.md:\n    - to: local_files:b.md\n"
        "      note: why they relate\n",
        "utf-8",
    )
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0, capsys.readouterr().out
    page = tmp_path / "out" / "sync-local_files" / "concepts" / "a" / "overview.md"
    assert "- [B](/concepts/b/overview.md) — why they relate" in page.read_text("utf-8")


def test_a_malformed_links_file_exits_2_before_fetching(tmp_path: Path, capsys):
    src = _two_docs(tmp_path)
    links = tmp_path / "links.yaml"
    links.write_text("links:\n  a.md:\n    - local_files:b.md\n", "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "links config: links key 'a.md' must be a qualified doc_id" in out
    assert not (tmp_path / "mirror").exists()


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("missing.yaml", None),
        ("bad.yaml", "links: [unclosed\n"),
        ("wrong.yaml", "link: {}\n"),
    ],
)
def test_an_unreadable_links_file_exits_2_with_a_message(
    tmp_path: Path, capsys, name: str, body: str | None
):
    src = _two_docs(tmp_path)
    links = tmp_path / name
    if body is not None:
        links.write_text(body, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert f"links config {links}" in out
