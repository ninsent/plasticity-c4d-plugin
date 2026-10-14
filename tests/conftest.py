"""
Test bootstrap.

Cinema 4D's ``c4d`` module only exists inside Cinema 4D.  The suite installs
the small stand-in from ``tests/stubs/c4d`` instead, and puts the repository
root and the bundled ``libs/`` directory on ``sys.path`` exactly like
``plasticity_c4d.pyp`` does at load time.
"""

import os
import sys
import threading

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)

for path in (os.path.join(TESTS_DIR, "stubs"), REPO_ROOT,
             os.path.join(REPO_ROOT, "libs"), TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import c4d  # noqa: E402  (the stub)


@pytest.fixture
def doc():
    """A fresh active document."""
    d = c4d.documents.BaseDocument()
    c4d.documents.SetActiveDocument(d)
    c4d.gui.messages.clear()
    c4d.utils.modeling_calls.clear()
    yield d
    c4d.documents.SetActiveDocument(None)


@pytest.fixture
def no_thread_leaks():
    """Fail the test if it leaves new non-daemon or bridge threads running."""
    before = set(threading.enumerate())
    yield
    deadline = 5.0
    import time
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        leaked = [t for t in threading.enumerate()
                  if t not in before and t.is_alive()]
        if not leaked:
            return
        time.sleep(0.02)
    pytest.fail(f"leaked threads: {leaked}")
