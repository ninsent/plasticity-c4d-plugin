"""
Plasticity Bridge for Cinema 4D
Connects to Plasticity's WebSocket server to import mesh data.

Based on the Plasticity Blender Bridge addon by Nick Kallen.
Ported to Cinema 4D Python SDK.
"""

import c4d
import os
import sys

# Registered Plugin ID from plugincafe.maxon.net
PLUGIN_ID = 1066929

plugin_dir = os.path.dirname(os.path.abspath(__file__))
libs_path = os.path.join(plugin_dir, "libs")

# Development only: set PLASTICITY_BRIDGE_DEV=1 before starting Cinema 4D to
# reload the plugin's modules on "Reload Python Plugins".  Maxon recommends
# against autoReload in release builds.
_DEV_RELOAD = os.environ.get("PLASTICITY_BRIDGE_DEV") == "1"

try:
    # Cinema 4D 2024.0+: puts the path on sys.path only while importing and
    # refuses to import over a same-named package another plugin loaded
    # (suggested by Maxon SDK support for this plugin).
    from mxutils import LocalImportPath as _ImportPath
except ImportError:
    _ImportPath = None


class _LegacyImportPath:
    """Cinema 4D 2023 stand-in for ``mxutils.LocalImportPath``.

    Same contract: ``path`` is on ``sys.path`` only inside the ``with``
    block, and a package of the same name that is already loaded from
    another location raises ``ImportError`` instead of being reused.
    """

    def __init__(self, path, autoReload=False):
        self._path = path if os.path.isdir(path) else os.path.dirname(path)
        self._added = False

    def __enter__(self):
        for name in sorted(os.listdir(self._path)):
            pkg = os.path.join(self._path, name)
            if not os.path.isfile(os.path.join(pkg, "__init__.py")):
                continue
            loaded = sys.modules.get(name)
            if loaded is None:
                continue
            origin = getattr(loaded, "__file__", None)
            if origin is None or not os.path.abspath(origin).startswith(
                    os.path.abspath(pkg) + os.sep):
                raise ImportError(
                    f"Encountered module label collision for '{name}': "
                    f"already loaded from {origin}")
        if self._path not in sys.path:
            sys.path.insert(0, self._path)
            self._added = True
        return self

    def __exit__(self, *exc):
        if self._added:
            try:
                sys.path.remove(self._path)
            except ValueError:
                pass
        return False


if _ImportPath is None:
    _ImportPath = _LegacyImportPath


def _load_websockets():
    """Import the bundled websockets 10.4 from libs/.

    Returns True when the bundled copy is used.  If another plugin already
    imported its own ``websockets``, keep using that one (as 1.1.x silently
    did) but say so in the console.
    """
    try:
        with _ImportPath(libs_path):
            import websockets.client  # noqa: F401
            import websockets.exceptions  # noqa: F401
        return True
    except ImportError as e:
        if "collision" not in str(e):
            raise
        import websockets
        print(f"[Plasticity Bridge] Warning: using websockets "
              f"{getattr(websockets, '__version__', '?')} from "
              f"{getattr(websockets, '__file__', '?')}, loaded by another "
              f"plugin, instead of the bundled 10.4")
        return False


class _NoImportPath:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Resource handler for description files (required by RegisterTagPlugin)
__res__ = c4d.plugins.GeResource()
__res__.Init(plugin_dir)

# Now try to import modules with error handling
__version__ = "?"
try:
    _bundled_websockets = _load_websockets()
    # libs/ stays on the path while the plugin imports — and, in dev mode,
    # while LocalImportPath(autoReload=True) reloads every module under the
    # plugin folder on exit, the bundled websockets included.
    with (_ImportPath(libs_path) if _bundled_websockets else _NoImportPath()), \
            _ImportPath(plugin_dir, autoReload=_DEV_RELOAD):
        from core.version import __version__
        from core.client import PlasticityClient
        from core.handler import SceneHandler
        from core.threading_bridge import ThreadingBridge
        from dialogs.main_dialog import PlasticityDialog
        from core.refacet_tag import register_refacet_tag
    IMPORT_SUCCESS = True
    IMPORT_ERROR = None
except Exception as e:
    IMPORT_SUCCESS = False
    IMPORT_ERROR = str(e)
    print(f"[Plasticity Bridge] Import error: {e}")
    import traceback
    traceback.print_exc()


# Global reference for cleanup
_command_instance = None


class PlasticityCommand(c4d.plugins.CommandData):
    """Main command plugin that opens the Plasticity Bridge dialog."""

    def __init__(self):
        self.dialog = None
        self.threading_bridge = None
        self.handler = None
        self.client = None

    def _ensure_dialog(self):
        """Create dialog and all dependencies if they don't exist yet."""
        if self.dialog is None:
            self.threading_bridge = ThreadingBridge()
            self.handler = SceneHandler(self.threading_bridge)
            self.client = PlasticityClient(self.handler, self.threading_bridge)
            # Back-reference so the handler can invoke client methods
            self.handler.client = self.client
            self.dialog = PlasticityDialog(
                self.client, self.handler, self.threading_bridge
            )

    def Execute(self, doc):
        if not IMPORT_SUCCESS:
            c4d.gui.MessageDialog(
                f"Plasticity Bridge failed to load:\n\n{IMPORT_ERROR}\n\n"
                "Check the Console for details."
            )
            return False

        self._ensure_dialog()
        return self.dialog.Open(
            dlgtype=c4d.DLG_TYPE_ASYNC,
            pluginid=PLUGIN_ID,
            defaultw=300,
            defaulth=420,
        )

    def RestoreLayout(self, sec_ref):
        """
        Called by Cinema 4D when restoring a saved layout.
        MUST call GeDialog.Restore(pluginid, secret) — NOT Open().
        SDK ref: GeDialog.Restore(pluginid, secret)
        """
        if not IMPORT_SUCCESS:
            return False

        self._ensure_dialog()
        return self.dialog.Restore(pluginid=PLUGIN_ID, secret=sec_ref)

    def GetState(self, doc):
        return c4d.CMD_ENABLED

    def shutdown(self):
        """Clean shutdown of all resources (C4DPL_ENDACTIVITY).

        Stops the timer, closes the connection — also one that is still
        being established — and waits a bounded time for the WebSocket
        thread to exit.  The thread is a daemon, so a stuck socket can never
        block Cinema 4D from quitting.
        """
        if self.dialog is not None:
            try:
                self.dialog.SetTimer(0)
            except Exception:
                pass
        if self.client is not None:
            try:
                self.client.shutdown(timeout=2.0)
            except Exception as e:
                print(f"[Plasticity Bridge] Shutdown error: {e}")


def PluginMessage(id, data):
    """Handle plugin messages."""
    if id == c4d.C4DPL_ENDACTIVITY:
        # Maxon: "This allows to close dialogs, end threads, delete
        # temporary/undo buffers etc." (Python SDK, Plugin Structure).
        if _command_instance:
            _command_instance.shutdown()
    return True


# Register the plugin
if __name__ == "__main__":
    icon = None
    icon_path = os.path.join(plugin_dir, "res", "icon.tif")
    if os.path.exists(icon_path):
        icon = c4d.bitmaps.BaseBitmap()
        if icon.InitWith(icon_path)[0] != c4d.IMAGERESULT_OK:
            icon = None

    _command_instance = PlasticityCommand()

    result = c4d.plugins.RegisterCommandPlugin(
        id=PLUGIN_ID,
        str="Plasticity Bridge",
        info=0,
        icon=icon,
        help="Connect to Plasticity and import mesh data",
        dat=_command_instance,
    )

    if result:
        print(f"[Plasticity Bridge] {__version__} registered (ID: {PLUGIN_ID})")
    else:
        print("[Plasticity Bridge] Failed to register plugin")

    # Register the auto-refacet tag plugin
    if IMPORT_SUCCESS:
        try:
            register_refacet_tag(icon, __res__)
        except Exception as e:
            print(f"[Plasticity Bridge] Failed to register refacet tag: {e}")
            import traceback
            traceback.print_exc()
