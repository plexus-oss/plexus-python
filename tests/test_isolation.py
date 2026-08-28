"""Proves tests/conftest.py's isolation is actually in effect.

pytest does not collect test functions from conftest.py, so these guards live
here — where they run.
"""

from pathlib import Path


def test_defaults_are_not_reachable():
    """The isolation above is load-bearing; prove it is actually in effect."""
    from plexus.config import get_endpoint, get_gateway_url, get_gateway_ws_url

    for resolved in (get_endpoint(), get_gateway_url(), get_gateway_ws_url()):
        assert "plexus.company" not in resolved, resolved
        # Loopback only — a test must never open a socket that leaves the host.
        assert "127.0.0.1" in resolved or "localhost" in resolved, resolved


def test_real_user_config_is_not_read(tmp_path):
    """A key in the developer's own ~/.plexus must never reach a test."""
    import plexus.config as config
    from plexus.config import get_api_key, get_source_id, load_config

    # Reads are redirected away from the real file...
    assert config.CONFIG_FILE.is_relative_to(tmp_path)
    assert config.CONFIG_FILE != Path.home() / ".plexus" / "config.json"

    # ...so no real credential or device identity can leak into a test.
    assert get_api_key() is None
    assert load_config().get("api_key") is None

    # get_source_id() generates and *persists* one; it must land in tmp, not
    # in the developer's real config.
    generated = get_source_id()
    assert generated.startswith("source-")
    assert config.CONFIG_FILE.exists()
    assert config.CONFIG_FILE.is_relative_to(tmp_path)
