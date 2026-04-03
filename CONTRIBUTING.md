# Contributing to Plasticity Cinema 4D Bridge

Thank you for your interest in contributing to Plasticity Bridge! Contributions, bug reports, and feature suggestions are all welcome.

## Quick Start

1. Fork and clone the repository:
```bash
git clone https://github.com/ninsent/plasticity-c4d-plugin.git
```

2. Install the plugin in Cinema 4D:
   - Copy the plugin folder to your C4D plugins directory:
     - **Windows:** `%APPDATA%\Maxon\Cinema 4D\plugins\`
     - **macOS:** `~/Library/Application Support/Maxon/Cinema 4D/plugins/`
   - Restart Cinema 4D
   - The plugin appears under **Extensions → Plasticity Bridge**

3. Create a feature branch:
```bash
git checkout -b feature/your-feature-name
```

---

## Guidelines

### Code Style
- Follow the existing code structure and naming conventions
- Use meaningful variable and method names
- Add docstrings to all public methods
- Keep methods focused — prefer small, single-purpose functions

### Pull Requests
1. Update `CHANGELOG.md` under an `[Unreleased]` section
2. Submit a PR with a clear description:
   - What changed
   - Why it was changed
   - How to test the changes

### Project Structure
```
plasticity-bridge-c4d/
├── plasticity_c4d.pyp         # Plugin entry point, command registration, tag registration
├── dialogs/
│   ├── __init__.py
│   └── main_dialog.py         # UI layout, state, and user commands
├── modules/
│   ├── __init__.py
│   ├── client.py              # WebSocket client (handshake, send/recv, async loop)
│   ├── handler.py             # Scene handler: geometry, hierarchy, utilities, auto-refacet
│   ├── protocol.py            # Binary protocol encoding/decoding (all message types)
│   ├── refacet_tag.py         # Auto-Refacet TagData plugin and settings helper
│   └── threading_bridge.py    # Thread-safe event bridge to main thread
├── libs/
│   └── websockets/            # Bundled websockets library
├── res/
│   ├── icon.tif               # Plugin icon (also used for the tag)
│   ├── c4d_symbols.h
│   ├── description/
│   │   ├── tplasticityrefacet.h    # Auto-Refacet tag parameter symbols
│   │   └── tplasticityrefacet.res  # Auto-Refacet tag description resource
│   └── strings_us/
│       ├── c4d_strings.str
│       └── description/
│           └── tplasticityrefacet.str  # Auto-Refacet tag display names
├── CHANGELOG.md
├── CONTRIBUTING.md
└── README.md
```

### Key Modules

- **`protocol.py`** — Binary encoder/decoder matching the Blender addon's wire format. All message types from `HANDSHAKE_1` through `PUT_SOME_1`.
- **`client.py`** — Async WebSocket client running in a background thread. Sends handshake on connect, dispatches parsed messages as typed `BridgeEvent`s.
- **`threading_bridge.py`** — Lock-protected queue + callback registry. The dialog's `Timer()` drains it on the main thread.
- **`handler.py`** — The core scene handler. In-place geometry updates, tri/N-gon paths, ear-clipping triangulation, `MCOMMAND_MELT` with polygon-identity hashing, Inbox/Outbox hierarchy, auto-refacet triggering, and all utility operations.
- **`refacet_tag.py`** — `PlasticityRefacetTag` TagData subclass. Stores refacet settings, hides Simple/Advanced parameter groups via `GetDDescription`, exposes `read_tag_refacet_kwargs()` for the handler.
- **`main_dialog.py`** — 3-tab QuickTab UI (Server, Basic, Utilities) with all refacet, selection, and utility controls.

### Commit Convention
Use imperative mood, as per standard Git convention:

- `Add` — new feature or file
- `Fix` — bug fix
- `Remove` — deleted feature or code
- `Refactor` — internal change with no user-facing effect
- `Update` — change to existing behaviour or content

Example: `Add Auto-Refacet tag with per-object tessellation settings`

---

## Need Help?

- Open a GitHub Discussion for questions
- Open an Issue for bugs or feature requests — please include your C4D version and a description of the problem

By contributing, you agree that your contributions will be licensed under the MIT License.