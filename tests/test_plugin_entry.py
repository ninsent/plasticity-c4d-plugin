"""
plasticity_c4d.pyp loading, run in clean subprocesses against the c4d stub:
LocalImportPath / 2023 fallback, sys.path hygiene, module collisions,
version print, shutdown.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from conftest import REPO_ROOT, TESTS_DIR

STUBS = os.path.join(TESTS_DIR, "stubs")
PYP = os.path.join(REPO_ROOT, "plasticity_c4d.pyp")


def run(body, env_extra=None):
    code = textwrap.dedent(f"""
        import sys, runpy, os
        sys.path.insert(0, {STUBS!r})
        sys.dont_write_bytecode = True
    """) + textwrap.dedent(body)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PLASTICITY_BRIDGE_DEV", None)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=TESTS_DIR, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_loads_registers_and_prints_version_without_polluting_sys_path():
    out = run(f"""
        before = list(sys.path)
        ns = runpy.run_path({PYP!r}, run_name="__main__")
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
        import c4d
        assert ("command", 1066929) in c4d.plugins.registered
        assert ("tag", 1068209) in c4d.plugins.registered
        assert sys.path == before, set(sys.path) ^ set(before)
        import core.version, websockets
        print("VERSION", core.version.__version__)
        print("WS", websockets.__file__)
        ns["PluginMessage"](c4d.C4DPL_ENDACTIVITY, None)
    """)
    version = [l.split()[1] for l in out.splitlines() if l.startswith("VERSION")][0]
    assert f"[Plasticity Bridge] {version} registered (ID: 1066929)" in out
    ws = [l.split(None, 1)[1] for l in out.splitlines() if l.startswith("WS ")][0]
    assert os.path.join(REPO_ROOT, "libs", "websockets") in ws


def test_core_collision_is_reported_not_silently_used():
    out = run(f"""
        import types
        fake = types.ModuleType("core")
        fake.__file__ = "/some/other/plugin/core/__init__.py"
        sys.modules["core"] = fake
        ns = runpy.run_path({PYP!r}, run_name="__main__")
        assert not ns["IMPORT_SUCCESS"]
        print("ERR", ns["IMPORT_ERROR"])
    """)
    assert "collision" in out and "/some/other/plugin/core" in out


def test_foreign_websockets_is_used_with_a_warning(tmp_path):
    import shutil
    shutil.copytree(os.path.join(REPO_ROOT, "libs", "websockets"),
                    tmp_path / "websockets")
    out = run(f"""
        sys.path.insert(0, {str(tmp_path)!r})
        import websockets.client
        sys.path.pop(0)
        ns = runpy.run_path({PYP!r}, run_name="__main__")
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
    """)
    assert "loaded by another plugin" in out


def test_mxutils_local_import_path_is_used_when_available():
    out = run(f"""
        import types, contextlib
        calls = []
        class LocalImportPath:
            def __init__(self, path, autoReload=False):
                calls.append((os.path.basename(path), autoReload))
                self.path = path
            def __enter__(self):
                sys.path.insert(0, self.path)
            def __exit__(self, *a):
                sys.path.remove(self.path)
        mx = types.ModuleType("mxutils")
        mx.LocalImportPath = LocalImportPath
        sys.modules["mxutils"] = mx
        ns = runpy.run_path({PYP!r}, run_name="__main__")
        assert ns["IMPORT_SUCCESS"], ns["IMPORT_ERROR"]
        print("CALLS", calls)
    """, env_extra={"PLASTICITY_BRIDGE_DEV": "1"})
    assert "('libs', False)" in out
    assert f"('{os.path.basename(REPO_ROOT)}', True)" in out
