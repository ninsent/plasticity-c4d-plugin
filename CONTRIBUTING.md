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

### Tests
The suite runs outside Cinema 4D: `tests/stubs/c4d` is a small stand-in for the `c4d` module, and `tests/reference/` holds frozen copies of the 1.1.6 code and the Blender addon's wire layouts that new code is compared against.

```bash
python -m pip install -r requirements-dev.txt   # dev only — never shipped
python -m pytest
python tests/perf/bench.py --sizes 100000       # optional: before/after timings
```

- Keep geometry and protocol logic in the c4d-free modules (`plasticity_bridge/core/geometry.py`, `protocol.py`, `refacet_settings.py`) so it can be tested; the C4D-facing code in `plasticity_bridge/core/handler.py` should stay a thin layer.
- Add a regression test for every bug fix. Behaviour that can only be checked inside Cinema 4D goes into [docs/MANUAL_TEST_CHECKLIST.md](docs/MANUAL_TEST_CHECKLIST.md); `tests/test_docs.py` checks that every `M-…` ID cited in `AUDIT.md` exists there.
- CI runs the tests on Python 3.9, 3.10 and 3.11 — the Python versions embedded in Cinema 4D 2023.0/2023.1, 2023.2 and 2024–2026 — so don't use newer syntax.

### Imports
Cinema 4D runs **every** Python plugin in one interpreter with one shared `sys.modules`, so a module this plugin imports under a global name is what every other plugin — and Cinema 4D itself — gets for that name (versions before 1.2.0 broke Maxon's MCP server by exposing their bundled websockets 10.4 as `websockets`; see `AUDIT.md`, F30).

- **Never modify `sys.path`** in plugin code. `plasticity_c4d.pyp` loads the package by file location (`importlib.util.spec_from_file_location`); tests may change `sys.path`, the plugin may not.
- **All plugin code lives in `plasticity_bridge/`** and imports its own modules **relatively** (`from .protocol import …`, `from ..core.threading_bridge import …`). No new top-level packages next to the `.pyp`.
- **Never import a third-party library by its global name.** Vendor it unmodified under `plasticity_bridge/_vendor/<name>/` (with its license) and import it as `from .._vendor.<name> import …`. Check first that the library only uses relative imports internally.

### Compatibility rules
- Never change the plugin IDs (`1066929`, `1068209`), the `BC_PLASTICITY_*` BaseContainer keys (`1066930`–`1066942`), the managed tag names, or the tag description IDs in `res/description/tplasticityrefacet.h` — they are stored in users' scenes.
- The wire protocol must stay byte-compatible with the [Plasticity Blender Bridge](https://github.com/nkallen/plasticity-blender-addon); `tests/test_protocol.py` pins it with golden bytes.
- Only the main thread may touch the C4D API; the WebSocket client talks to it exclusively through `ThreadingBridge` events.

### Pull Requests
1. Update `CHANGELOG.md` under an `[Unreleased]` section
2. Make sure `python -m pytest` passes
3. Submit a PR with a clear description:
   - What changed
   - Why it was changed
   - How to test the changes (including any manual checks in Cinema 4D)

### Releases
1. Bump `__version__` in `plasticity_bridge/core/version.py` (the single source of truth — the plugin prints it on load, the build script names the zip after it).
2. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`. The next change adds a fresh `## [Unreleased]` section when it needs one.
3. Build and check locally:
   ```bash
   python scripts/build_release.py --release
   ```
   This writes `dist/PlasticityBridge_vX.Y.Z.zip` (one top-level `PlasticityBridge/` folder, allowlisted runtime files only), byte-compiles every Python file, and prints the size and SHA-256. Rebuilding the same commit gives a byte-identical zip. Without `--release`, a build of an unreleased version is allowed if its changes are described under `[Unreleased]`.
4. Run through [docs/MANUAL_TEST_CHECKLIST.md](docs/MANUAL_TEST_CHECKLIST.md) in Cinema 4D with that zip.
5. Push a tag `vX.Y.Z`, then run the *Release* workflow by hand (*Actions → Release → Run workflow*, enter the tag). It runs the tests, checks that the tag matches `plasticity_bridge/core/version.py`, rebuilds the zip and publishes it on the GitHub Release with the matching CHANGELOG section as release notes. Pushing commits or tags never publishes anything on its own.

### Project Structure
```
plasticity-c4d-plugin/
├── plasticity_c4d.pyp         # Entry point: loads plasticity_bridge by file location, registers command and tag
├── plasticity_bridge/         # ALL plugin code — one private package (see "Imports")
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── client.py              # WebSocket client (handshake, non-blocking sends, async loop)
│   │   ├── geometry.py            # c4d-free geometry: conversion, N-gons, normals, face maps, export
│   │   ├── handler.py             # Scene handler: C4D objects, hierarchy, undo, utilities, auto-refacet
│   │   ├── protocol.py            # Binary protocol encoding/decoding (all message types)
│   │   ├── refacet_settings.py    # Refacet settings → request parameters (dialog and tag)
│   │   ├── refacet_tag.py         # Auto-Refacet TagData plugin
│   │   ├── threading_bridge.py    # Thread-safe event bridge to main thread
│   │   └── version.py             # Plugin version (single source of truth)
│   ├── dialogs/
│   │   ├── __init__.py
│   │   └── main_dialog.py         # UI layout, state, and user commands
│   └── _vendor/
│       ├── __init__.py
│       └── websockets/            # Bundled websockets 10.4, unmodified (BSD license in websockets/LICENSE)
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
├── tests/                     # pytest suite (c4d stub, references, fake server) — not shipped
├── scripts/build_release.py   # Release zip builder — not shipped
├── docs/MANUAL_TEST_CHECKLIST.md  # Checks only Cinema 4D can do — not shipped
├── .github/workflows/         # CI (tests) and the manual release workflow
├── AUDIT.md
├── CHANGELOG.md
├── CONTRIBUTING.md
└── README.md
```

### Key Modules

- **`protocol.py`** — Binary encoder/decoder matching the Blender addon's wire format. All message types from `HANDSHAKE_1` through `PUT_SOME_1`; bounds-checked, array-based bulk reads.
- **`client.py`** — Async WebSocket client running in a background thread. Sends handshake on connect, dispatches parsed messages as typed `BridgeEvent`s; all requests are non-blocking and report failures as `OPERATION_FAILED` events.
- **`threading_bridge.py`** — Unbounded lock-protected queue + callback registry. The dialog's `Timer()` drains it on the main thread with a time budget.
- **`geometry.py`** — Everything geometric that doesn't need Cinema 4D: coordinate conversion, welding, ear clipping, N-gon outline reconstruction, normal tag data, poly-face map encoding, edge maps, PUT_SOME payloads.
- **`handler.py`** — The scene handler. In-place geometry updates, `MCOMMAND_MELT` with polygon-identity hashing, Inbox/Outbox hierarchy, undo steps, control-cage resolution, auto-refacet triggering, and all utility operations.
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