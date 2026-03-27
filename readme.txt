Plasticity Bridge for Cinema 4D
================================

Connect Cinema 4D to Plasticity via WebSocket for live mesh synchronization.
Based on the Plasticity Blender Bridge addon by Nick Kallen.

Author:     Nursultan Akim
GitHub:     https://github.com/ninsent
Repository: https://github.com/ninsent/plasticity-c4d-plugin
License:    MIT

Installation
------------
Copy this entire folder to your Cinema 4D plugins directory:

  Windows:  %APPDATA%\Maxon\Cinema 4D\plugins\
  macOS:    ~/Library/Application Support/Maxon/Cinema 4D/plugins/

Restart Cinema 4D. The plugin appears under Extensions > Plasticity Bridge.

Requirements
------------
- Cinema 4D 2023 or later
- Plasticity with WebSocket server enabled (v1.3+)

Usage
-----
1. Open a model in Plasticity.
2. In Cinema 4D, open Extensions > Plasticity Bridge.
3. Enter the server address (default: localhost:8980) and click Connect.
4. Click Refresh to import, or enable Live Link for real-time sync.

For full documentation, visit:
https://github.com/ninsent/plasticity-c4d-plugin
