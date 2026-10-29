import builtins
import sys
from pathlib import Path

import pytest
import yaml

from kbforge.assembly import (
    Outcome,
    SetupError,
    build_synthesizer,
    execute,
    load_grounding_value,
    load_links_value,
    synthesizer_grounds,
)
from kbforge.pipeline import NoOp, RedoRefused
from kbforge.project import DEFAULT_FILE, ProjectError, load_project

SPEC_EXAMPLE = """\
state_dir: .kbforge
synthesizer:
  name: llm
  model: deepseek/deepseek-v4-flash
  instructions: |
    Write for application managers.
publisher:
  name: github
  repo: acme/ev-traction-kb
  token_env: GITHUB_TOKEN
links: links.yaml
grounding: grounding.yaml
sources:
  products:
    connector: sql
    config:
      system: products
      url_env: DENODO_URL
      id: [product_id]
  docs:
    connector: mcp
    config: { system: docs }
    chunking: { max_concepts: 40, group_by: family }
  handbook:
    connector: local_files
    config: { path: ./handbook }
    synthesizer: { name: describe, model: deepseek/deepseek-v4-flash }
"""


def write(d: Path, text: str) -> Path:
    p = d / DEFAULT_FILE
    p.write_text(text, "utf-8")
    return p


def test_loads_the_spec_example(tmp_path):
    p = load_project(write(tmp_path, SPEC_EXAMPLE))
    assert list(p.sources) == ["products", "docs", "handbook"]
    assert p.mirror() == tmp_path / ".kbforge" / "mirror"
    assert p.state() == tmp_path / ".kbforge" / "state"
    assert p.out() == tmp_path / ".kbforge" / "out"
    assert p.root == tmp_path
    override = p.sources["handbook"].synthesizer
    chunking = p.sources["docs"].chunking
    assert override is not None and chunking is not None
    assert override.name == "describe"
    assert (p.synthesizer.model_extra or {})["model"] == "deepseek/deepseek-v4-flash"
    assert chunking.max_concepts == 40
    assert p.publisher.name == "github"


def test_defaults(tmp_path):
    p = load_project(write(tmp_path, "sources:\n  a: {connector: x}\n"))
    assert p.synthesizer.name == "stub"
    assert p.publisher.name == "dry-run"
    assert p.links is None and p.grounding is None


def test_reports_every_problem_with_its_yaml_path(tmp_path):
    with pytest.raises(ProjectError) as exc:
        load_project(
            write(
                tmp_path,
                "sources:\n  Docs: {connector: x}\n"
                "  ok: {connector: y, chunking: {max_concepts: 0}, stages: []}\n",
            )
        )
    msgs = exc.value.problems
    assert any(
        m.startswith("sources.Docs:") and "^[a-z][a-z0-9_-]*$" in m for m in msgs
    )
    assert any(m.startswith("sources.ok.chunking.max_concepts:") for m in msgs)
    assert any(m.startswith("sources.ok.stages:") and "Extra inputs" in m for m in msgs)


def test_root_cannot_be_set_from_yaml(tmp_path):
    with pytest.raises(ProjectError) as exc:
        load_project(write(tmp_path, "root: /etc\nsources:\n  a: {connector: x}\n"))
    assert any(
        m.startswith("root:") and "Extra inputs" in m for m in exc.value.problems
    )


def test_no_sources_is_an_error(tmp_path):
    with pytest.raises(ProjectError) as exc:
        load_project(write(tmp_path, "sources: {}\n"))
    assert any(m.startswith("sources:") for m in exc.value.problems)


def test_a_missing_file_names_the_path(tmp_path):
    with pytest.raises(ProjectError) as exc:
        load_project(tmp_path / DEFAULT_FILE)
    assert exc.value.problems == [f"{DEFAULT_FILE}: not found in {tmp_path}"]


def test_bad_yaml_is_a_project_error(tmp_path):
    p = write(tmp_path, "sources: [unclosed\n")
    with pytest.raises(ProjectError) as exc:
        load_project(p)
    assert exc.value.problems[0].startswith(f"{p}:")


def test_inline_links_and_grounding(tmp_path):
    p = load_project(
        write(
            tmp_path,
            "links: {links: {'a:1': ['b:2']}}\ngrounding: {max_grounding_docs: 2}\n"
            "sources:\n  a: {connector: x}\n",
        )
    )
    links = load_links_value(p.links)
    assert links is not None and links.links == {"a:1": ["b:2"]}
    assert load_grounding_value(p.grounding).max_grounding_docs == 2
    assert load_links_value(None) is None
    assert load_grounding_value(None).rules == []


def test_path_values_load_from_the_file(tmp_path):
    f = tmp_path / "links.yaml"
    f.write_text(yaml.safe_dump({"links": {"a:1": ["b:2"]}}), "utf-8")
    links = load_links_value(f)
    assert links is not None and links.links == {"a:1": ["b:2"]}


def test_synthesizer_grounds_needs_no_llm_extra(monkeypatch):
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("pydantic_ai"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "kbforge.llm_synthesizer", raising=False)
    assert synthesizer_grounds("llm") is True
    assert synthesizer_grounds("stub") is False
    assert synthesizer_grounds("describe") is False


def test_grounds_table_matches_the_classes():
    from kbforge.llm_synthesizer import DescribeSynthesizer, LLMSynthesizer

    assert synthesizer_grounds("llm") == LLMSynthesizer.grounds
    assert synthesizer_grounds("describe") == DescribeSynthesizer.grounds


def test_build_synthesizer_stub_is_none(tmp_path):
    assert build_synthesizer("stub", {}, tmp_path) is None


def test_build_synthesizer_bad_setting_is_a_setup_error(tmp_path):
    with pytest.raises(SetupError):
        build_synthesizer("llm", {"nonsense": 1}, tmp_path)


def test_execute_maps_outcomes():
    o = execute(lambda: NoOp(), "c")
    assert o == Outcome(0, ["NoOp: no change detected; no MR opened."], o.result)
    assert isinstance(o.result, NoOp)

    def refuse():
        raise RedoRefused("nope")

    o = execute(refuse, "c")
    assert (o.code, o.lines) == (1, ["Redo refused: nope"])


def test_links_and_grounding_paths_resolve_against_the_project_file(
    tmp_path, monkeypatch
):
    (tmp_path / "links.yaml").write_text("links: {'a:1': ['b:2']}\n", "utf-8")
    (tmp_path / "grounding.yaml").write_text("max_grounding_docs: 7\n", "utf-8")
    write(tmp_path, SPEC_EXAMPLE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    p = load_project(tmp_path / DEFAULT_FILE)
    links = load_links_value(p.links)
    assert links is not None and links.links == {"a:1": ["b:2"]}
    assert load_grounding_value(p.grounding).max_grounding_docs == 7


def test_a_relative_project_path_gives_an_absolute_root(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    write(sub, "sources:\n  a: {connector: x}\n")
    monkeypatch.chdir(tmp_path)
    p = load_project(Path("sub") / DEFAULT_FILE)
    assert p.root.is_absolute()
    assert p.mirror() == sub.resolve() / ".kbforge" / "mirror"
    with pytest.raises(ProjectError) as exc:
        load_project(Path("sub") / "nope" / DEFAULT_FILE)
    assert exc.value.problems == [
        f"{DEFAULT_FILE}: not found in {(sub / 'nope').resolve()}"
    ]
