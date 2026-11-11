"""Shared test setup: point the engine at a throwaway copy of the sample vault.

ea.config reads its paths from the environment at import time, so the
environment is set here, before anything imports `ea`.
"""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = Path(__file__).resolve().parent / "sample_vault"
WORK = Path(tempfile.mkdtemp(prefix="ea-test-"))

os.environ["EA_VAULT_ROOT"] = str(WORK / "vault")
os.environ["EA_VAULT_NAME"] = "Notes"
os.environ["EA_STATE_DIR"] = str(WORK / "state")
os.environ["EA_LOG_DIR"] = str(WORK / "logs")
os.environ["EA_ENV_FILES"] = str(WORK / "no-such.env")
os.environ["EA_ENGINE_PATH"] = str(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "mcp_server"))


def reset():
    """Fresh vault and empty state before each test."""
    for name in ("vault", "state", "logs"):
        shutil.rmtree(WORK / name, ignore_errors=True)
    shutil.copytree(SAMPLE, WORK / "vault")
    (WORK / "state").mkdir()
    (WORK / "logs").mkdir()


def note(*parts):
    return WORK / "vault" / "Notes" / Path(*parts)


def load_server():
    """Import the MCP server module.

    Where fastmcp is not installed, a stand-in whose @tool() decorator returns
    the function unchanged is used, so the tool bodies are still the real code.
    """
    try:
        import fastmcp  # noqa: F401
    except ImportError:
        fake = types.ModuleType("fastmcp")

        class FastMCP:
            def __init__(self, *a, **k):
                pass

            def tool(self, *a, **k):
                return lambda fn: fn

            def run(self, *a, **k):
                raise RuntimeError("stand-in fastmcp cannot serve")

        fake.FastMCP = FastMCP
        sys.modules["fastmcp"] = fake
    import server
    return server


def call(tool, *args, **kwargs):
    """Call a tool whether the decorator returned the function or a wrapper."""
    fn = getattr(tool, "fn", tool)
    return fn(*args, **kwargs)
