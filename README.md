# Plasticity Bridge for Cinema 4D

A Cinema 4D plugin that connects to [Plasticity](https://www.plasticity.xyz/) via WebSocket, enabling live mesh synchronization between the two applications.
Model in Plasticity's NURBS environment, see the tessellated result in Cinema 4D in real time.

**[Download Latest Release](https://github.com/ninsent/plasticity-c4d-plugin/releases/download/v1.1.0/PlasticityBridge.zip)**

[![Version](https://img.shields.io/badge/version-1.1.0-blue.svg)](https://github.com/ninsent/plasticity-c4d-plugin) [![License](https://img.shields.io/badge/license-MIT-teallight.svg)](LICENSE) [![Cinema 4D](https://img.shields.io/badge/Cinema_4D-2023+-orange.svg)](https://www.maxon.net/cinema-4d)

## Features

- **Live Link** — Subscribe to Plasticity's WebSocket server and receive geometry updates in real time
- **Handshake** — Capability negotiation with the Plasticity server on connect
- **In-Place Updates** — Geometry is replaced without destroying the C4D object; materials, tags, animation, and constraints survive every update
- **Tri & N-gon Modes** — Pure-triangle output or reconstructed N-gon topology via ear-clipping and `MCOMMAND_MELT`
- **Custom Normals** — Per-corner normals written to a managed `NormalTag` with accurate shading in both modes
- **Refacet** — Re-tessellate selected objects with full control over tolerance, angle, width, and chord parameters
- **Auto-Refacet Tag** — Custom tag that persists refacet settings per object; geometry is automatically re-tessellated after every refresh or live-link update
- **Inbox / Outbox** — Objects from Plasticity land in the Inbox; user-created SDS objects in the Outbox can be uploaded back to Plasticity via `PUT_SOME`
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

Cinema 4D's API is not thread-safe. The plugin runs the WebSocket client in a background thread and passes parsed messages to the main thread via a lock-protected queue. The dialog's `Timer()` callback (16ms interval) drains the queue and dispatches events to registered handlers.

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
├── Outbox                      ← user-created SDS objects for upload
└── Inbox                       ← objects received from Plasticity
    ├── Group A                 ← Plasticity group → C4D null
    │   ├── Solid 1             ← Plasticity solid → C4D polygon object
    │   └── Solid 2
    └── Sheet 1
```

Objects in the Outbox are protected from incoming updates and can be uploaded to Plasticity via the `PUT_SOME_1` protocol message when the server supports it.

---

## Installation

### For Users

1. Download the latest release from **[GitHub Releases](https://github.com/ninsent/plasticity-c4d-plugin/releases/latest)**
2. Extract the archive
3. Copy the entire plugin folder to your Cinema 4D plugins directory:
   - **Windows:** `%APPDATA%\Maxon\Cinema 4D\plugins\`
   - **macOS:** `~/Library/Application Support/Maxon/Cinema 4D/plugins/`
4. Restart Cinema 4D
5. The plugin appears under **Extensions → Plasticity Bridge**

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

No build step or `pip install` needed — the `websockets` library is bundled in `libs/`.

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

4. **Adjust settings**
   - **Unit Scale** (0.0001–100): Scale factor applied to the root null
   - **Topology**: Switch between Tris and Ngons
   - **Refacet Options**: Simple (Tolerance + Angle) or Advanced (all six parameters)

5. **Auto-Refacet**
   - Check **Auto-Refacet** before clicking **Refacet Selected**
   - A custom tag is stamped on each selected object with the current refacet settings
   - On every subsequent refresh or live-link update, tagged objects are automatically re-tessellated
   - Edit the tag's attributes directly in the Attribute Manager to fine-tune per-object settings
   - Delete the tag to stop auto-refaceting an object

6. **Outbox (upload to Plasticity)**
   - Place Subdivision Surface objects in the **Outbox** null under the root
   - On Refresh, outbox meshes are uploaded to Plasticity via `PUT_SOME` (if the server supports it)

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

These are automatically stripped and recreated on every geometry update. User-created tags are never touched.

---

## Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) for details on commit conventions, code style, and the process for submitting pull requests.

---

## Resources

### Learn More

- [Plasticity Official Website](https://www.plasticity.xyz/)
- [Plasticity Manual — Blender Bridge Setup](https://doc.plasticity.xyz/blender/install-blender-addon)
- [Plasticity Blender Bridge (original addon)](https://github.com/nkallen/plasticity-blender-addon)
- [Cinema 4D Python SDK Documentation](https://developers.maxon.net/docs/py/)

---

## Changelog

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

- Portfolio: [bento.me/ninsent](https://behance.net/ninsent)

---

## Support

- Report Bug: [GitHub Issues](https://github.com/ninsent/plasticity-c4d-plugin/issues)
- Request Feature: [GitHub Issues](https://github.com/ninsent/plasticity-c4d-plugin/issues)
- Contact: akim.off.nur@gmail.com

---

## License

[MIT](LICENSE) © 2026 Nursultan Akim