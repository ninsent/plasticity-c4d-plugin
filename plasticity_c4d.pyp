"""
Plasticity Bridge for Cinema 4D
Connects to Plasticity's WebSocket server to import mesh data.

Based on the Plasticity Blender Bridge addon by Nick Kallen.
Ported to Cinema 4D Python SDK.
"""

import c4d
import importlib.util
import os
import sys

# Registered Plugin ID from plugincafe.maxon.net
PLUGIN_ID = 1066929

plugin_dir = os.path.dirname(os.path.abspath(__file__))

# All plugin code lives in the uniquely named package ``plasticity_bridge``
# next to this file.  Cinema 4D runs every Python plugin in one interpreter,
# so this file never adds a folder to the import path and nothing is ever
# imported under a generic global name (``core``, ``dialogs``,
# ``websockets`` …): such names leak into every other plugin.  Up to
# 1.2.0-dev the bundled websockets 10.4 was imported as ``websockets`` and
# broke Maxon's MCP server, whose uvicorn needs websockets >= 11.
_PACKAGE = "plasticity_bridge"
_PACKAGE_DIR = os.path.join(plugin_dir, _PACKAGE)

# Development only: with PLASTICITY_BRIDGE_DEV=1 set before starting
# Cinema 4D, "Reload Python Plugins" re-imports the whole package from disk
# instead of reusing the loaded modules.  Close the Plasticity Bridge dialog
# (disconnect) before reloading.
_DEV_RELOAD = os.environ.get("PLASTICITY_BRIDGE_DEV") == "1"


def _unload_package():
    """Drop ``plasticity_bridge`` and all its submodules from sys.modules."""
    for name in [n for n in sys.modules
                 if n == _PACKAGE or n.startswith(_PACKAGE + ".")]:
        del sys.modules[name]


def _same_dir(a, b):
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


def _load_package():
    """Import ``plasticity_bridge`` from this folder by its file location.

    Returns True if this call loaded it (False if the copy loaded earlier
    from this same folder is reused).  Submodules — including the vendored
    ``plasticity_bridge._vendor.websockets`` — are then found through the
    package's ``__path__``; the global import path is never consulted.
    """
    loaded = sys.modules.get(_PACKAGE)
    if loaded is not None:
        origin = os.path.dirname(getattr(loaded, "__file__", None) or "")
        if not origin or not _same_dir(origin, _PACKAGE_DIR):
            raise ImportError(
                f"another '{_PACKAGE}' package is already loaded from "
                f"{origin or 'an unknown location'} — is Plasticity Bridge "
                f"installed twice?")
        if not _DEV_RELOAD:
            return False
        _unload_package()

    spec = importlib.util.spec_from_file_location(
        _PACKAGE, os.path.join(_PACKAGE_DIR, "__init__.py"),
        submodule_search_locations=[_PACKAGE_DIR])
    package = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = package
    try:
        spec.loader.exec_module(package)
    except BaseException:
        sys.modules.pop(_PACKAGE, None)
        raise
    return True


# Resource handler for description files (required by RegisterTagPlugin)
__res__ = c4d.plugins.GeResource()
__res__.Init(plugin_dir)

# Now try to import modules with error handling
__version__ = "?"
_loaded_here = False
try:
    _loaded_here = _load_package()
    from plasticity_bridge.core.version import __version__
    from plasticity_bridge.core.client import PlasticityClient
    from plasticity_bridge.core.handler import SceneHandler
    from plasticity_bridge.core.threading_bridge import ThreadingBridge
    from plasticity_bridge.dialogs.main_dialog import PlasticityDialog
    from plasticity_bridge.core.refacet_tag import register_refacet_tag
    IMPORT_SUCCESS = True
    IMPORT_ERROR = None
except Exception as e:
    if _loaded_here:
        _unload_package()        # leave nothing half-imported behind
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
