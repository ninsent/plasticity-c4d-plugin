# Plasticity Bridge for Cinema 4D

A Cinema 4D plugin that connects to [Plasticity](https://www.plasticity.xyz/) via WebSocket, enabling live mesh synchronization between the two applications.
Model in Plasticity's NURBS environment, see the tessellated result in Cinema 4D in real time.

**[Download Latest Release](https://github.com/ninsent/plasticity-c4d-plugin/releases/latest)** · **[Official Documentation](https://doc.plasticity.xyz/cinema-4d)**

[![Version](https://img.shields.io/github/v/release/ninsent/plasticity-c4d-plugin?color=blue&label=version)](https://github.com/ninsent/plasticity-c4d-plugin/releases/latest) [![License](https://img.shields.io/badge/license-MIT-teallight)](LICENSE) [![Cinema 4D](https://img.shields.io/badge/Cinema_4D-2023+-royalblue)](https://www.maxon.net/cinema-4d) ![Downloads](https://img.shields.io/github/downloads/ninsent/plasticity-c4d-plugin/total?color=orchid&cacheSeconds=3600)

## Features

- **Live Link** — Subscribe to Plasticity's WebSocket server and receive geometry updates in real time
- **Handshake** — Capability negotiation with the Plasticity server on connect
- **In-Place Updates** — Geometry is replaced without destroying the C4D object; materials, tags, animation, and constraints survive every update
- **Tri & N-gon Modes** — Pure-triangle output or reconstructed N-gon topology via ear-clipping and `MCOMMAND_MELT`
- **Custom Normals** — Per-corner normals written to a managed `NormalTag` with accurate shading in both modes
- **Refacet** — Re-tessellate selected objects with full control over tolerance, angle, width, and chord parameters
- **Auto-Refacet Tag** — Custom tag that persists refacet settings per object; geometry is automatically re-tessellated after every refresh or live-link update
- **Inbox / Outbox** — Objects from Plasticity land in the Inbox; Subdivision Surface objects in the Outbox are uploaded back to Plasticity as SubDs via `PUT_SOME`. The control cage under the SDS can be a polygon object, a parametric primitive or any generator (polysplines, Loft, Sweep, Lathe, Symmetry, Boole, Cloner …), including deformers, offsets and N-gons
- **Store Faces** — Creates one `PolygonSelectionTag` per CAD face group, ready for per-face material assignment
- **Store Edges** — Creates an `EdgeSelectionTag` containing all CAD boundary edges
- **Select Faces** — Expands current polygon selection to whole CAD face groups
- **Select Edges** — Selects perimeter edges of the current face-group selection and switches to edge mode
- **Select Sharp Edges** — Selects all Plasticity boundary edges with an adjustable angle threshold and switches to edge mode
- **Unit Scale** — Lives on the root null's scale transform, adjustable at any time without reimporting

---

## Architecture

```
┌────────────────────┐  WebSocket   ┌──────────────────────┐
│     Plasticity     │◄────────────►│   PlasticityClient   │
│   (CAD modeler)    │   binary     │  (background thread) │
└────────────────────┘  protocol    └──────────┬───────────┘
                                               │ thread-safe
                                               │ event queue
                                    ┌──────────▼───────────┐
                                    │   ThreadingBridge    │
                                    │  (Queue + callbacks) │
                                    └──────────┬───────────┘
                                               │ Timer() @ 60fps
                                    ┌──────────▼───────────┐
                                    │    SceneHandler      │
                                    │  (C4D main thread)   │
                                    └──────────────────────┘
```

Cinema 4D's API is not thread-safe. The plugin runs the WebSocket client in a background thread and passes parsed messages to the main thread via a lock-protected queue. The dialog's `Timer()` callback (16ms interval) drains the queue — with a small time budget per tick so a burst of updates never freezes the UI — and dispatches events to registered handlers. The queue never drops events; a growing backlog is reported in the console.

Requests to Plasticity are non-blocking: they are encoded on the main thread and sent from the background thread, and a failure comes back as an error in the status line.

Because the event queue is drained by the dialog's timer, **closing the Plasticity Bridge dialog disconnects** (Cinema 4D also closes docked dialogs on layout switches). Reopen the dialog and click Connect to continue.

### Protocol

On connect, the client sends a `HANDSHAKE_1` message. The server responds with the set of message types it supports, enabling feature detection (e.g. whether `PUT_SOME_1` is available for Outbox uploads).

The binary protocol uses little-endian encoding with 4-byte aligned strings. All message types and layouts match the [Plasticity Blender Bridge](https://github.com/nkallen/plasticity-blender-addon) addon exactly.

### Coordinate System

Plasticity is Z-up; Cinema 4D is Y-up. The plugin swaps axes on import:

| Plasticity | Cinema 4D |
|------------|-----------|
| X          | X         |
| Y          | Z         |
| Z          | Y         |

Vertex positions are scaled by 100× (Plasticity uses metres, C4D uses centimetres). Winding order is reversed (`CPolygon(a, c, b)`) to match C4D's counter-clockwise front-face convention.

### Scene Hierarchy

```
Plasticity: <filename>          ← root null (unit scale lives here)
├── Outbox                      ← user-created SDS objects & parametric generators for upload
└── Inbox                       ← objects received from Plasticity
    ├── Group A                 ← Plasticity group → C4D null
    │   ├── Solid 1             ← Plasticity solid → C4D polygon object
    │   └── Solid 2
    └── Sheet 1
```

Objects in the Outbox are protected from incoming updates and can be uploaded to Plasticity via the `PUT_SOME_1` protocol message when the server supports it.

What is uploaded:

- **Subdivision Surface objects** become SubD objects in Plasticity. As in Cinema 4D, only the SDS's **first child** (with its whole hierarchy) is the control cage — a polygon object, parametric primitive, generator (Loft, Sweep, Lathe, Symmetry, Boole, Cloner/Array, polyspline-driven objects …) or a null holding several of them. Deformers, child offsets and N-gons are respected; the cage is expressed relative to the root null, so unit scale and moving the root don't change the uploaded size.
- **Nulls** become groups.
- Anything else placed directly in the Outbox (a loose polygon object, generator or spline) is **skipped with a warning** — put it under a Subdivision Surface to upload it.
- With **Only Visible**, objects hidden in the editor — directly, through a hidden parent, or by a layer — are skipped.

Only objects inside the Inbox are kept in sync. Re-parenting an object inside the Inbox (for example under your own Subdivision Surface or ZRemesher) is preserved; moving it out of the Plasticity hierarchy detaches it, and the next refresh imports a fresh copy.

---

## Installation

> **Official setup guide:** [doc.plasticity.xyz/cinema-4d/install-c4d-plugin](https://doc.plasticity.xyz/cinema-4d/install-c4d-plugin)

### For Users

1. Download `PlasticityBridge_v<version>.zip` from **[GitHub Releases](https://github.com/ninsent/plasticity-c4d-plugin/releases/latest)**
2. Unzip it into your Cinema 4D plugins directory — the archive contains a single `PlasticityBridge/` folder:
   - **Windows:** `%APPDATA%\Maxon\Cinema 4D\plugins\`
   - **macOS:** `~/Library/Application Support/Maxon/Cinema 4D/plugins/`
3. Restart Cinema 4D
4. The plugin appears under **Extensions → Plasticity Bridge**; the Console shows `[Plasticity Bridge] <version> registered`

**Upgrading:** quit Cinema 4D, **delete the old `PlasticityBridge` folder**, then unzip the new one. Since 1.2.0 all code lives in `PlasticityBridge/plasticity_bridge/`; unzipping over an old install would leave the unused `core/`, `dialogs/` and `libs/` folders behind. Scenes saved with earlier versions keep working.

> **Upgrading from 1.1.0:** the Auto-Refacet tag plugin ID has changed to a registered Maxon ID. Scenes saved with 1.1.0 will lose their Auto-Refacet tag bindings on load — re-apply the tag via the **Auto-Refacet** checkbox in the Basic tab.

### For Developers

1. Clone the project:
```bash
git clone https://github.com/ninsent/plasticity-c4d-plugin.git
```

2. Install the plugin by symlinking or copying to your C4D plugins directory:
```bash
# macOS / Linux
ln -s "$(pwd)/plasticity-c4d-plugin" ~/Library/Application\ Support/Maxon/Cinema\ 4D/plugins/

# Windows (PowerShell, run as admin)
New-Item -ItemType SymbolicLink -Path "$env:APPDATA\Maxon\Cinema 4D\plugins\plasticity-c4d-plugin" -Target "$(Get-Location)\plasticity-c4d-plugin"
```

3. Restart Cinema 4D. The plugin will appear under **Extensions → Plasticity Bridge**

No build step or `pip install` needed — all plugin code is the package `plasticity_bridge/`, with the `websockets` library (10.4, pure Python) bundled privately in `plasticity_bridge/_vendor/`. Cinema 4D runs every Python plugin in one interpreter, so the plugin never adds folders to `sys.path` and never imports anything under a generic global name (see [CONTRIBUTING.md](CONTRIBUTING.md#imports)).

To re-import the plugin's modules with *Extensions → Reload Python Plugins* instead of restarting Cinema 4D, start Cinema 4D with the environment variable `PLASTICITY_BRIDGE_DEV=1` (close the Plasticity Bridge dialog before reloading).

#### Tests

The test suite runs outside Cinema 4D (a small `c4d` stand-in lives in `tests/stubs/`):

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Things only Cinema 4D can verify are listed in [docs/MANUAL_TEST_CHECKLIST.md](docs/MANUAL_TEST_CHECKLIST.md).

#### Building a release

```bash
python scripts/build_release.py
```

This writes `dist/PlasticityBridge_v<version>.zip` (version from `plasticity_bridge/core/version.py`), containing only the files the plugin needs, and prints its size and SHA-256. See [CONTRIBUTING.md](CONTRIBUTING.md#releases) for the release process.

---

## Usage Guide

### Basic Workflow

1. **Connect**
   - Open **Extensions → Plasticity Bridge**
   - Enter the server address (default: `localhost:8980`)
   - Click **Connect**

2. **Import geometry**
   - Click **Refresh** to import all objects, or toggle **Only Visible** first
   - Objects appear under a root null named `Plasticity: <filename>`

3. **Enable Live Link**
   - Toggle **Live Link** to receive real-time updates
   - Every change in Plasticity is reflected in Cinema 4D within a frame
   - When a new file or new version is opened in Plasticity, Cinema 4D re-imports automatically
   - Each incoming update is **one undo step**. Undoing it reverts the Cinema 4D copy to the previous Plasticity state (Plasticity itself is not changed); the next update or a Refresh brings it back in sync

4. **Adjust settings**
   - **Unit Scale** (0.0001–100): Scale factor applied to the root null
   - **Topology**: Switch between Tris and Ngons
   - **Refacet Options**: Simple (Tolerance + Angle) or Advanced (all six parameters)

5. **Auto-Refacet**
   - Check **Auto-Refacet** before clicking **Refacet Selected**
   - A custom tag is stamped on each selected object with the current refacet settings
   - On every subsequent refresh or live-link update, tagged objects are automatically re-tessellated — in a single step: the intermediate triangle mesh is skipped
   - Edit the tag's attributes directly in the Attribute Manager to fine-tune per-object settings
   - Delete the tag to stop auto-refaceting an object

6. **Outbox (upload to Plasticity)**
   - Place **Subdivision Surface** objects in the **Outbox** null under the root (nulls inside the Outbox become groups). The SDS's first child is the control cage — a polygon object, a parametric primitive, or any generator (Loft, Sweep, Lathe, Symmetry, Boole, Cloner/Array, polyspline-driven objects …)
   - On Refresh, outbox objects are evaluated and uploaded to Plasticity via `PUT_SOME` (if the server supports it); objects that can't be uploaded are listed in the Console

7. **Use utilities**
   - Select one or more Plasticity objects in the viewport
   - Use the Utilities tab to store face/edge selections, select groups, or select sharp edges

### Auto-Refacet Tag

The **Plasticity Auto-Refacet** tag can be attached to any Plasticity mesh object. It appears in the Tags menu and can also be created automatically via the Auto-Refacet checkbox. The tag's Attribute Manager panel mirrors the plugin's refacet controls:

- **Topology** — Tris / Ngons
- **Refacet Options** — Simple / Advanced (toggles which parameter group is visible)
- **Simple**: Tolerance, Angle (with sliders)
- **Advanced**: Min Width, Max Width, Edge Chord Tol, Edge Chord Angle, Face Plane Tol, Face Angle Tol (with sliders)

### Managed Tags

All tags created by the plugin are prefixed or named with internal identifiers:

- `__plasticity_normals__` — NormalTag (custom normals)
- `Plasticity Face <n>` — PolygonSelectionTag (per-face selections)
- `__plasticity_edges__` — EdgeSelectionTag (boundary edges)

On every geometry update the normal tag is rebuilt; the `Plasticity Face <n>` and `__plasticity_edges__` selection tags are removed (their polygon indices would no longer match) — run **Store** again to recreate them. User-created tags are never touched.

---

## Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) for details on commit conventions, code style, and the process for submitting pull requests.

---

## Resources

### Learn More

- [Plasticity Official Website](https://www.plasticity.xyz/)
- [Plasticity Manual — Cinema 4D Plugin Setup](https://doc.plasticity.xyz/cinema-4d/install-c4d-plugin)
- [Plasticity Manual — Blender Bridge Setup](https://doc.plasticity.xyz/blender/install-blender-addon)
- [Plasticity Blender Bridge (original addon)](https://github.com/nkallen/plasticity-blender-addon)
- [Cinema 4D Python SDK Documentation](https://developers.maxon.net/docs/py/)

---

## Changelog

### [1.2.0] - 2026-10-14

- Works with Maxon's MCP server, Outbox uploads that match what you see in C4D, steadier Live Link, one undo step per update, faster import and upload, new `plasticity_bridge/` folder layout

### [1.1.6] - 2026-06-13

- Fixed N-gon collapse on repeated refacet, preserved user re-parenting (generators / custom nulls) and C4D-side visibility overrides across refresh and live-link updates

### [1.1.5] - 2026-04-24

- Parametric generator export (polysplines, Loft, Sweep, Lathe, Symmetry, Boole, Array, primitives), Live Link auto-refresh on new file / version, `modules/` → `core/` rename, registered Auto-Refacet tag plugin ID

### [1.1.0] - 2026-04-03

- Auto-Refacet tag, Select Sharp Edges, Inbox/Outbox hierarchy, Handshake protocol, PutSome upload

### [1.0.0] - 2026-03-27

- Initial release with WebSocket connection, Live Link, tri/N-gon modes, refacet, and full utility suite

Check the [CHANGELOG.md](CHANGELOG.md) for a detailed history of changes.

---

## Credits

- **[Plasticity](https://www.plasticity.xyz/)** by Nick Kallen — the CAD modeler this plugin connects to
- **[Plasticity Blender Bridge](https://github.com/nkallen/plasticity-blender-addon)** by Nick Kallen — the original addon whose protocol and architecture this port is based on
- **Ferdinand ([Maxon Developer Forum](https://developers.maxon.net/forum/topic/13458))** — polygon-identity hashing technique used for N-gon melt tracking

---

## Author

**Nursultan Akim**

- Portfolio: [behance.net/ninsent](https://behance.net/ninsent)

---

## Support

- Report Bug: [GitHub Issues](https://github.com/ninsent/plasticity-c4d-plugin/issues)
- Request Feature: [GitHub Issues](https://github.com/ninsent/plasticity-c4d-plugin/issues)
- Contact: akim.off.nur@gmail.com

---

## License

[MIT](LICENSE) © 2026 Nursultan Akim