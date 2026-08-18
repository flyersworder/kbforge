import pytest

# Hard guarantee: no test may reach a real model provider. TestModel/FunctionModel
# are unaffected; any accidental real request raises instead of spending tokens.
try:
    import pydantic_ai.models

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
except ImportError:  # pragma: no cover - the [llm] extra is not installed
    pass


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help=(
            "run the opt-in live tests that call real external services: the "
            "LLM synthesizer (OPENROUTER_API_KEY), the forge publishers "
            "(GITHUB_TOKEN/GITLAB_TOKEN plus KBFORGE_LIVE_*_REPO), and the MCP "
            "source connector (no credentials for AWS Documentation; "
            "GITHUB_TOKEN for GitHub)"
        ),
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test; pass --run-live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: test that calls a real external service (LLM provider or forge)",
    )
    # The module-level guard above disables ALL real model requests. When the user
    # explicitly opts in with --run-live, re-enable them so the live test can call a
    # real provider; offline tests use TestModel/FunctionModel and never make one.
    if config.getoption("--run-live"):
        try:
            import pydantic_ai.models

            pydantic_ai.models.ALLOW_MODEL_REQUESTS = True
        except ImportError:  # pragma: no cover - the [llm] extra is not installed
            pass
