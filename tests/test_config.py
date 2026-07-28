"""Tests for config directory and file permissions."""

import os
import stat
from unittest.mock import patch

from plexus.config import load_config, save_config


class TestConfigPermissions:
    def test_directory_permissions(self, tmp_path):
        """Config directory should be 0o700 (drwx------)."""
        config_dir = tmp_path / ".plexus"
        config_file = config_dir / "config.json"

        with patch("plexus.config.CONFIG_DIR", config_dir), \
             patch("plexus.config.CONFIG_FILE", config_file):
            save_config({"api_key": "test"})

        dir_mode = stat.S_IMODE(os.stat(config_dir).st_mode)
        assert dir_mode == 0o700

    def test_file_permissions(self, tmp_path):
        """Config file should be 0o600 (-rw-------)."""
        config_dir = tmp_path / ".plexus"
        config_file = config_dir / "config.json"

        with patch("plexus.config.CONFIG_DIR", config_dir), \
             patch("plexus.config.CONFIG_FILE", config_file):
            save_config({"api_key": "test"})

        file_mode = stat.S_IMODE(os.stat(config_file).st_mode)
        assert file_mode == 0o600

    def test_config_roundtrip(self, tmp_path):
        """Config should survive save/load cycle."""
        config_dir = tmp_path / ".plexus"
        config_file = config_dir / "config.json"

        with patch("plexus.config.CONFIG_DIR", config_dir), \
             patch("plexus.config.CONFIG_FILE", config_file):
            save_config({"api_key": "plx_test123", "source_id": "src-1"})
            loaded = load_config()

        assert loaded["api_key"] == "plx_test123"
        assert loaded["source_id"] == "src-1"
