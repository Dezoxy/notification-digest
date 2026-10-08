"""Real SDK lease/concurrency test; no production identity or external APIs."""

import pytest
from test_cloud_state import run_azurite_smoke


@pytest.mark.usefixtures("azurite_enabled")
def test_cloud_state_azurite_protocol() -> None:
    run_azurite_smoke()
