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

# Get plugin directory
plugin_dir = os.path.dirname(__file__)

# Add plugin directory and libs to path BEFORE any imports
if plugin_dir not in sys.path:
    sys.path.insert(0, plugin_dir)
libs_path = os.path.join(plugin_dir, "libs")
if libs_path not in sys.path:
    sys.path.insert(0, libs_path)

# Now try to import modules with error handling
try:
    from modules.client import PlasticityClient
    from modules.handler import SceneHandler
    from modules.threading_bridge import ThreadingBridge
    from dialogs.main_dialog import PlasticityDialog
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
        """Clean shutdown of all resources."""
        if self.client and self.client.connected:
            try:
                self.client.disconnect()
            except Exception as e:
                print(f"[Plasticity Bridge] Shutdown error: {e}")


def PluginMessage(id, data):
    """Handle plugin messages."""
    global _command_instance
    if id == c4d.C4DPL_ENDACTIVITY:
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
        print("[Plasticity Bridge] Plugin registered successfully (ID: 1066929)")
    else:
        print("[Plasticity Bridge] Failed to register plugin")
