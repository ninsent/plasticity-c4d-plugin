"""
plasticity_c4d.pyp loading, each scenario in a clean ``python -I``
subprocess against the c4d stub.

Cinema 4D runs all Python plugins in one interpreter.  The plugin must load
its code as the private package ``plasticity_bridge`` without touching
``sys.path`` and without leaking generic top-level modules — in particular
not ``websockets``: Maxon's MCP server (uvicorn) needs websockets >= 11 and
failed with ``ImportError: cannot import name 'ServerProtocol'`` while the
bridge exposed its bundled 10.4 under that global name (AUDIT.md, F30).
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

from conftest import REPO_ROOT, TESTS_DIR

STUBS = os.path.join(TESTS_DIR, "stubs")
PYP = os.path.join(REPO_ROOT, "plasticity_c4d.pyp")
GLOBAL_NAMES = ("websockets", "core", "dialogs", "libs")

PRELUDE = f"""
import os, runpy, sys, warnings
sys.dont_write_bytecode = True
sys.path.insert(0, {STUBS!r})               # the fake c4d module
GLOBAL_NAMES = {GLOBAL_NAMES!r}

def load(pyp={PYP!r}, run_name="__main__"):
    before = list(sys.path)
    ns = runpy.run_path(pyp, run_name=run_name)
    assert sys.path == before, ("sys.path changed", set(sys.path) ^ set(before))
    return ns

def leaked():
    return [n for n in GLOBAL_NAMES if n in sys.modules]

def handshake_against_fake_server():
    # Test harness only: the fake server and wire builders live in tests/.
    sys.path.insert(0, {TESTS_DIR!r})
    import time
    from fake_server import FakePlasticityServer
    from plasticity_bridge.core.client import PlasticityClient
    from plasticity_bridge.core.protocol import MessageType
    from plasticity_bridge.core.threading_bridge import EventType, ThreadingBridge
    bridge = ThreadingBridge()
    seen = []
    bridge.register_callback(EventType.HANDSHAKE_RESPONSE, seen.append)
    client = PlasticityClient(None, bridge)
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        deadline = time.monotonic() + 10
        while not seen and time.monotonic() < deadline:
            bridge.process_pending_events(time_budget=None)
            time.sleep(0.01)
        ok = bool(seen) and client.supports(MessageType.PUT_SOME_1)
        client.shutdown(timeout=5.0)
    sys.path.remove({TESTS_DIR!r})
    return ok
"""


def run(body, env_extra=None):
    code = PRELUDE + textwrap.dedent(body)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PLASTICITY_BRIDGE_DEV", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                       text=True, env=env, cwd=TESTS_DIR, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def _newer_websockets_and_uvicorn():
    try:
        from importlib.metadata import version
        major = int(version("websockets").split(".")[0])
    except Exception:
        return False
    return (major >= 13 and importlib.util.find_spec("uvicorn") is not None
            and importlib.util.find_spec(
                "uvicorn.protocols.websockets.websockets_sansio_impl") is not None)


needs_mcp_stack = pytest.mark.skipif(
    not _newer_websockets_and_uvicorn(),
    reason="needs websockets>=13 and uvicorn>=0.35 (requirements-dev.txt)")


def test_entry_point_leaks_nothing_and_uses_private_websockets():
    out = run("""
        ns = load()
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
        assert leaked() == [], leaked()
        client = sys.modules["plasticity_bridge.core.client"]
        assert client.ws_client.__name__ == "plasticity_bridge._vendor.websockets.client"
        assert client.ConnectionClosed.__module__ == "plasticity_bridge._vendor.websockets.exceptions"
        import c4d
        assert ("command", 1066929) in c4d.plugins.registered
        assert ("tag", 1068209) in c4d.plugins.registered
        ns["PluginMessage"](c4d.C4DPL_ENDACTIVITY, None)
        print("VERSION", sys.modules["plasticity_bridge.core.version"].__version__)
        print("PKG", sys.modules["plasticity_bridge"].__file__)
    """)
    version = [l.split()[1] for l in out.splitlines() if l.startswith("VERSION")][0]
    assert version == "1.2.0"
    assert f"[Plasticity Bridge] {version} registered (ID: 1066929)" in out
    assert os.path.join(REPO_ROOT, "plasticity_bridge", "__init__.py") in out


@needs_mcp_stack
def test_mcp_stack_works_after_bridge_loads_first():
    """The reported bug: bridge first, then Maxon's MCP adapter (uvicorn)."""
    out = run("""
        ns = load()
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
        assert leaked() == [], leaked()
        from websockets.server import ServerProtocol      # websockets >= 11
        from uvicorn.protocols.websockets.websockets_sansio_impl import (
            WebSocketsSansIOProtocol)
        import websockets
        assert "plasticity_bridge" not in websockets.__file__
        assert handshake_against_fake_server()            # bridge still works
        print("MCP OK", websockets.__version__)
    """)
    assert "MCP OK" in out


@needs_mcp_stack
def test_bridge_keeps_private_websockets_when_newer_one_loaded_first():
    out = run("""
        import websockets
        from websockets.server import ServerProtocol
        foreign = os.path.dirname(websockets.__file__)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ns = load()
            assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
            client = sys.modules["plasticity_bridge.core.client"]
            assert client.ws_client.__name__ == \\
                "plasticity_bridge._vendor.websockets.client"
            assert sys.modules["websockets"] is websockets   # untouched
            assert handshake_against_fake_server()
        from_foreign = [str(w.message) for w in caught
                        if os.path.abspath(w.filename).startswith(foreign)]
        assert from_foreign == [], from_foreign
        print("REVERSE OK")
    """)
    assert "REVERSE OK" in out


def test_foreign_core_and_dialogs_packages_do_not_affect_bridge():
    out = run("""
        import types
        foreign = {}
        for name in ("core", "dialogs"):
            m = types.ModuleType(name)
            m.__file__ = "/some/other/plugin/%s/__init__.py" % name
            m.__path__ = ["/some/other/plugin/%s" % name]
            sys.modules[name] = foreign[name] = m
        ns = load()
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
        assert all(sys.modules[n] is foreign[n] for n in foreign)
        handler = sys.modules["plasticity_bridge.core.handler"]
        assert handler.geo.__name__ == "plasticity_bridge.core.geometry"
        print("COLLISION OK")
    """)
    assert "COLLISION OK" in out


def _copy_plugin(dst):
    shutil.copy(PYP, dst / "plasticity_c4d.pyp")
    shutil.copytree(os.path.join(REPO_ROOT, "plasticity_bridge"),
                    dst / "plasticity_bridge",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(os.path.join(REPO_ROOT, "res"), dst / "res")
    return str(dst / "plasticity_c4d.pyp")


def test_failed_load_leaves_nothing_behind(tmp_path):
    pyp = _copy_plugin(tmp_path)
    with open(tmp_path / "plasticity_bridge" / "core" / "handler.py", "a") as f:
        f.write("\ndef broken(:\n")
    out = run(f"""
        ns = load({pyp!r})
        assert not ns["IMPORT_SUCCESS"]
        left = [n for n in sys.modules if n.startswith("plasticity_bridge")]
        assert left == [], left
        assert leaked() == []
        print("ERR", ns["IMPORT_ERROR"])
    """)
    assert "ERR" in out and "Import error" in out


def test_second_installed_copy_is_reported_not_unloaded(tmp_path):
    other = _copy_plugin(tmp_path)
    out = run(f"""
        first = load({other!r})
        assert first["IMPORT_SUCCESS"], first["IMPORT_ERROR"]
        pkg = sys.modules["plasticity_bridge"]
        second = load()
        assert not second["IMPORT_SUCCESS"]
        assert sys.modules["plasticity_bridge"] is pkg          # untouched
        print("ERR", second["IMPORT_ERROR"])
    """)
    assert "installed twice" in out


@pytest.mark.parametrize("dev", [False, True])
def test_running_the_pyp_again(dev):
    """"Reload Python Plugins" re-runs the .pyp: the loaded package is
    reused, or re-imported from disk with PLASTICITY_BRIDGE_DEV=1."""
    out = run("""
        a = load()
        mod_a = sys.modules["plasticity_bridge.core.client"]
        b = load()
        assert a["IMPORT_SUCCESS"] and b["IMPORT_SUCCESS"], b["IMPORT_ERROR"]
        mod_b = sys.modules["plasticity_bridge.core.client"]
        assert leaked() == []
        print("FRESH" if mod_a is not mod_b else "REUSED")
    """, env_extra={"PLASTICITY_BRIDGE_DEV": "1"} if dev else None)
    assert ("FRESH" if dev else "REUSED") in out
