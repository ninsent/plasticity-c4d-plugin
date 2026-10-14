"""
Plasticity Bridge for Cinema 4D — all plugin code lives in this package.

Every Python plugin in Cinema 4D shares one interpreter and one
``sys.modules``.  Top-level names such as ``core``, ``dialogs`` or
``websockets`` therefore leak into every other plugin: Maxon's own MCP
server (uvicorn) failed to start because it imported this plugin's bundled
websockets 10.4 instead of its own.  So the plugin code uses one uniquely
named package, imports only relatively inside it, and ``plasticity_c4d.pyp``
loads it from its folder without touching ``sys.path``.
"""
