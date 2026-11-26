"""Real SDK lease/concurrency test; no production identity or external APIs."""

import pytest
from test_cloud_state import run_azurite_smoke


@pytest.mark.usefixtures("azurite_enabled")
@pytest.mark.parametrize("state_format", ["bundle", "objects"])
def test_cloud_state_azurite_protocol(state_format: str) -> None:
    run_azurite_smoke(state_format)
