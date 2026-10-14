"""
Third-party libraries bundled with Plasticity Bridge.

They are imported only as ``plasticity_bridge._vendor.<name>`` — never under
their global names — because all Cinema 4D plugins share one interpreter: a
global ``websockets`` from here would shadow (or be shadowed by) the copy
another plugin or Cinema 4D itself needs, e.g. the MCP server's uvicorn.

* websockets 10.4 (BSD 3-Clause, see websockets/LICENSE) — unmodified;
  it uses only relative imports internally, so it works at this location.
"""
