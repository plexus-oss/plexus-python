"""Test isolation.

Two things the suite did before this file existed:

1. It dialled the **production** gateway. `plexus/config.py` defaults to
   `wss://gateway.plexus.company`, and any test that builds a `Plexus(...)` and
   triggers a send opens a real socket to it. Every run put a burst of
   `device auth failed / invalid API key` warnings into production gateway logs
   — noise that reads exactly like a customer with a broken key, and that cost
   real time to rule out during a support investigation.

2. It read the developer's own `~/.plexus/config.json`. `get_api_key()` falls
   back to that file, so a developer with a working key could have had the suite
   authenticate as them and write test telemetry into a real org.

Both are closed here, for every test, without opt-in.
"""

import os

import pytest

# Set before any test module imports plexus.client: the endpoint getters read
# os.environ at call time, but a module-level constant read at import would be
# baked in already.
_UNROUTABLE = "127.0.0.1:9"  # discard port — refuses fast, never leaves the host

os.environ["PLEXUS_ENDPOINT"] = f"http://{_UNROUTABLE}"
os.environ["PLEXUS_GATEWAY_URL"] = f"http://{_UNROUTABLE}"
os.environ["PLEXUS_GATEWAY_WS_URL"] = f"ws://{_UNROUTABLE}"
os.environ.pop("PLEXUS_API_KEY", None)


@pytest.fixture(autouse=True)
def isolate_plexus_config(tmp_path, monkeypatch):
    """Point config reads at an empty tmp dir instead of the real ~/.plexus.

    CONFIG_DIR is computed from Path.home() at import time and no env var
    overrides it, so the constants have to be patched directly.
    """
    import plexus.config as config

    cfg_dir = tmp_path / ".plexus"
    cfg_dir.mkdir()
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", cfg_dir / "config.json")
    yield


@pytest.fixture(autouse=True)
def _no_production_endpoints():
    """Fail loudly if a test re-points the SDK at a real Plexus host.

    A test that sets its own endpoint is fine; one that sets a production one is
    the bug this file exists to prevent, and it should not pass quietly.
    """
    yield
    for var in ("PLEXUS_ENDPOINT", "PLEXUS_GATEWAY_URL", "PLEXUS_GATEWAY_WS_URL"):
        value = os.environ.get(var, "")
        assert "plexus.company" not in value, (
            f"{var}={value!r} points at production. Tests must not reach a real "
            f"Plexus host — see tests/conftest.py."
        )
