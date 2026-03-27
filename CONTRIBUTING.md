# Contributing to Plasticity Cinema 4D Bridge

Thank you for your interest in contributing to Plasticity Bridge! Contributions, bug reports, and feature suggestions are all welcome.

## Quick Start

1. Fork and clone the repository:
```bash
git clone https://github.com/yourusername/plasticity-bridge-c4d.git
```

2. Install the plugin in Cinema 4D:
   - Copy the plugin folder to your C4D plugins directory:
     - **Windows:** `%APPDATA%\Maxon\Cinema 4D\plugins\`
     - **macOS:** `~/Library/Application Support/Maxon/Cinema 4D/plugins/`
   - Restart Cinema 4D
   - The plugin will appear under **Extensions → Plasticity Bridge**

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
├── plasticity_c4d.pyp         # Plugin entry point and registration
├── dialogs/
│   ├── __init__.py
│   └── main_dialog.py         # UI layout, state, and user commands
├── modules/
│   ├── __init__.py
│   ├── client.py              # WebSocket client (Plasticity protocol)
│   ├── handler.py             # Scene handler: geometry, utilities, metadata
│   ├── protocol.py            # Protocol constants and message types
│   └── threading_bridge.py    # Thread-safe event bridge to main thread
├── libs/
│   └── websockets/            # Bundled websockets library
├── res/
│   ├── icon.tif               # Plugin icon
│   ├── c4d_symbols.h
│   └── strings_us/
│       └── c4d_strings.str
├── CHANGELOG.md
├── CONTRIBUTING.md
└── README.md
```

### Commit Convention
Use imperative mood, as per standard Git convention:

- `Add` — new feature or file
- `Fix` — bug fix
- `Remove` — deleted feature or code
- `Refactor` — internal change with no user-facing effect
- `Update` — change to existing behaviour or content

Example: `Add Select Plasticity Edges utility`

---

## Need Help?

- Open a GitHub Discussion for questions
- Open an Issue for bugs or feature requests — please include your C4D version and a description of the problem

By contributing, you agree that your contributions will be licensed under the MIT License.
