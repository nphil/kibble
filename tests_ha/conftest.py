"""Shared fixtures for the tests_ha real-Home-Assistant integration suite.

This suite runs under /data/home/tmp/hatest/bin/python (real homeassistant +
pytest-homeassistant-custom-component), never the repo's own minimal uv venv -- see
kibble/tests/ for the HA-independent unit-style suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Point the plugin's own `hass` fixture at a config dir that has this repo's
    custom_components/kibble symlinked in, live.

    The plugin defaults `hass_config_dir` to `get_test_config_dir()`, a directory bundled
    inside the plugin package itself -- it never contains this repo's integration, so
    `enable_custom_integrations` would find nothing to enable without this override. The
    symlink (not a copy) means edits to the real package are picked up without re-running
    any fixture setup.
    """
    (tmp_path / "custom_components").mkdir()
    (tmp_path / "custom_components" / "kibble").symlink_to(
        Path(__file__).resolve().parent.parent / "custom_components" / "kibble",
        target_is_directory=True,
    )
    return str(tmp_path)
