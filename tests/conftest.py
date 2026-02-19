import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit real APIs",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip_integration = pytest.mark.skip(reason="needs --run-integration flag")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
