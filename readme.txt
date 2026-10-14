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
Copy this entire PlasticityBridge folder to your Cinema 4D plugins directory:

  Windows:  %APPDATA%\Maxon\Cinema 4D\plugins\
  macOS:    ~/Library/Application Support/Maxon/Cinema 4D/plugins/

Restart Cinema 4D. The plugin appears under Extensions > Plasticity Bridge,
and the Console shows "[Plasticity Bridge] <version> registered".

To upgrade, quit Cinema 4D, delete the old PlasticityBridge folder, then copy
this one in its place (since 1.2.0 the code lives in plasticity_bridge/;
the old core/, dialogs/ and libs/ folders are no longer used).

Closing the Plasticity Bridge window disconnects from Plasticity.

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

Third-party code
----------------
plasticity_bridge/_vendor/websockets: websockets 10.4, (c) Aymeric Augustin
and contributors, BSD 3-Clause license - see
plasticity_bridge/_vendor/websockets/LICENSE.