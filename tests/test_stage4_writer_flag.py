"""Stage 4: DRHIRO_LIQUID_WRITER flag tests.

Verifies the fail-closed behavior of the controllable writer flag
that gates the MCP liquid auto-log side effect between 'legacy'
and 'unified' modes.
"""

import os
import sys

import pytest

# Ensure MCP package importable
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "packages",
        "drhiro-mcp",
        "src",
    ),
)


@pytest.fixture()
def mcp_module(monkeypatch):
    """Import the sse_module with a controlled env."""
    # Clear any prior import so the env var is re-read
    sys.modules.pop("drhiro_mcp.sse_server", None)
    monkeypatch.setenv("DRHIRO_API_URL", "http://localhost:8010/api/v1")
    import drhiro_mcp.sse_server as mod

    return mod


class TestWriterFlagModes:
    def test_default_is_legacy(self, mcp_module):
        """When DRHIRO_LIQUID_WRITER is unset, default to legacy."""
        assert os.environ.get("DRHIRO_LIQUID_WRITER") is None
        assert mcp_module.get_liquid_writer_mode() == "legacy"
        assert mcp_module.is_unified_writer() is False

    def test_legacy_mode(self, mcp_module, monkeypatch):
        """Explicit legacy value."""
        monkeypatch.setenv("DRHIRO_LIQUID_WRITER", "legacy")
        # re-import not needed: get_liquid_writer_mode re-reads module global
        # but the module global was set at import time. We must re-import.
        sys.modules.pop("drhiro_mcp.sse_server", None)
        monkeypatch.setenv("DRHIRO_API_URL", "http://localhost:8010/api/v1")
        import drhiro_mcp.sse_server as mod2

        assert mod2.get_liquid_writer_mode() == "legacy"
        assert mod2.is_unified_writer() is False

    def test_unified_mode(self, mcp_module, monkeypatch):
        """Explicit unified value."""
        monkeypatch.setenv("DRHIRO_LIQUID_WRITER", "unified")
        sys.modules.pop("drhiro_mcp.sse_server", None)
        monkeypatch.setenv("DRHIRO_API_URL", "http://localhost:8010/api/v1")
        import drhiro_mcp.sse_server as mod2

        assert mod2.get_liquid_writer_mode() == "unified"
        assert mod2.is_unified_writer() is True

    def test_invalid_value_fails_closed(self, mcp_module, monkeypatch):
        """Unknown value → ValueError (fail-closed)."""
        monkeypatch.setenv("DRHIRO_LIQUID_WRITER", "garbage")
        sys.modules.pop("drhiro_mcp.sse_server", None)
        monkeypatch.setenv("DRHIRO_API_URL", "http://localhost:8010/api/v1")
        import drhiro_mcp.sse_server as mod2

        with pytest.raises(ValueError, match="Invalid DRHIRO_LIQUID_WRITER"):
            mod2.get_liquid_writer_mode()

    def test_invalid_value_is_unified_raises(self, mcp_module, monkeypatch):
        """is_unified_writer must also fail on bad value."""
        monkeypatch.setenv("DRHIRO_LIQUID_WRITER", "")
        sys.modules.pop("drhiro_mcp.sse_server", None)
        monkeypatch.setenv("DRHIRO_API_URL", "http://localhost:8010/api/v1")
        import drhiro_mcp.sse_server as mod2

        with pytest.raises(ValueError):
            mod2.is_unified_writer()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
