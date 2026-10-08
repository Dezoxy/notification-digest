"""Explicit opt-in for local Blob SDK protocol tests."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--azurite",
        action="store_true",
        default=False,
        help="Run SDK protocol tests against Azurite at 127.0.0.1:10000",
    )


@pytest.fixture
def azurite_enabled(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--azurite"):
        pytest.skip("Local Blob SDK protocol test requires explicit --azurite")
