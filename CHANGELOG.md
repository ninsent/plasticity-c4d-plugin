# Changelog

All notable changes to Plasticity Cinema 4D Bridge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

## [Unreleased]

Version 1.2.0 (see `core/version.py`). Audit findings are referenced as F-numbers; see `AUDIT.md`.

### Fixed
- **Multi-part control cages upload completely.** 1.1.x passed several objects to `MCOMMAND_JOIN`. JOIN only joins the children of the object it is given, so only the first part was uploaded. Cage parts are now merged in Python (F01).
- **Offset and rotated cage children keep their placement.** Child, cache and deform-cache matrices are baked into the uploaded points, and points are expressed relative to the Plasticity root, which inverts unit scale and any root move. 1.1.x dropped every matrix except the SDS's (F03).
- **The CSTO fallback uses the command's result list.** 1.1.x ignored it (F02).
- **Only the first child of a Subdivision Surface is uploaded**, matching what Cinema 4D subdivides. A note is printed when an SDS has more children (F04).
- **N-gons upload as single faces** instead of triangle/quad fragments. N-gons with holes fall back to fragments with a warning (F05).
- **Deformers are honoured for single-polygon cages too** (F12).
- **Refresh no longer stays disabled forever.** This used to happen after a failed list, an unreadable response or a failed send. Every busy operation now ends on a response, an error or a disconnect, with a 120 s safety timeout (F06).
- **A refacet error no longer creates an empty "Plasticity" root null** (F07).
- **Live Link never drops updates.** The event queue is unbounded and a backlog is reported in the console (F08).
- **Cinema 4D no longer freezes while sending.** Sends are non-blocking, and failures are reported in the status line. Disconnecting no longer blocks for up to 5 s (F09).
- **Reconnecting no longer breaks the new connection.** Double-clicking Connect, or reconnecting quickly, could start two connections, or let the closing one reset the new one (F10).
- **Wrong face groups after an N-gon refacet with only tris/quads**, which affected the Select/Store utilities (F13).
- **Custom normals use Cinema 4D's int16 scale of 32000.** 1.1.x used 32767, which made normals about 2.4 % too long (F14).
- **N-gon melting no longer changes the object's polygon selection** (F15).
- **"Only Visible" respects hidden parents and layers** (F17).
- **Copies of uploaded Outbox objects upload as new objects** instead of overwriting the original (F18).
- **Outbox IDs no longer hide unrelated Inbox objects.** Items and groups have overlapping IDs (F19). Uploaded IDs are now tracked per Plasticity file (F47). A copy of an Inbox mesh used as an Outbox cage no longer stops the original from syncing (F46). A copy of an uploaded object that arrives over Live Link before the upload finishes is removed from the Inbox (F20).
- **Malformed geometry can no longer crash Cinema 4D.** Polygons that reference missing vertices are skipped (F21).
- **An auto-refresh no longer cancels a pending Outbox upload** (F22).
- **Scenes from before 1.1.0 migrate into the Inbox again.** The migration was broken in 1.1.6 (F45).
- **Deleting the Outbox null no longer creates a second, empty Inbox** (F44).
- **Changing Unit Scale updates every Plasticity root in the scene**, including ones not seen since connecting (F32).
- **Closing the dialog or quitting Cinema 4D** shuts the connection down cleanly, even while it is still connecting (F29).

### Changed
- **Undo.** Every incoming update (list, Live Link transaction, refacet) is exactly one undo step. N-gon melting happens inside that step. Updates that change nothing leave no step. Name, visibility and selection changes made by the plugin and its utilities are undoable (F11, F23, F24).
- **Auto-Refacet tessellates once per update.** Tagged objects skip the intermediate triangle rebuild, so an update produces one rebuild and one undo step. The triangle data is applied if the refacet fails (F11).
- **Plugin loading uses `mxutils.LocalImportPath` on Cinema 4D 2024+**, with an equivalent fallback on 2023. The plugin no longer leaves its folders on `sys.path`. A clash with another plugin's `core`/`dialogs` package is reported instead of silently importing the wrong code. Set `PLASTICITY_BRIDGE_DEV=1` to reload modules on "Reload Python Plugins" while developing (F30).
- **Poly-face maps are stored run-length encoded**: about 600 bytes instead of about 3.8 MB for a 1M-polygon object. Maps written by 1.1.x are still read.
- **Every skipped Outbox object is reported** in the console and in the status line (F16).
- **The version is printed when the plugin loads**, from a single source of truth (`core/version.py`).
- **Performance.** Messages decode 90–140× faster. Multi-object uploads are encoded up to about 490× faster, because 1.1.x was quadratic in the number of objects. Normals are built about 5× faster. Points are written with `SetAllPoints`. See `AUDIT.md` for the numbers.

### Added
- A pytest suite (protocol golden bytes, threading, client against a local WebSocket server, geometry, handler and Outbox via a `c4d` stub) and GitHub Actions CI on Python 3.9, 3.10 and 3.11.
- `scripts/build_release.py`: a deterministic release zip with an allowlist and a self-check, plus a release workflow that only runs when started by hand.
- The bundled websockets license (`libs/websockets/LICENSE`).
- `AUDIT.md`.

### Removed
- `speedups.c` and `py.typed` are no longer shipped. The C extension was never compiled, so nothing changes at runtime.
- The unused `libs/__init__.py`.

## [1.1.6] - 2026-06-13

### Fixed
- **N-gon collapse on repeated refacet** — re-faceting in N-gon mode with unchanged settings no longer collapses N-gons back into triangles; `ResizeObject` is now called with `ncnt=0` to clear stale N-gon structure before the in-place melt
- **User edits lost on refresh** — objects re-parented under user generators (ZRemesher, SDS) or custom nulls are no longer pulled back into the Inbox; hierarchy is only synced while an object stays in a plugin-managed location
- **Visibility overrides reset on refresh** — editor/render visibility set in C4D is preserved; Plasticity-side visibility is only re-applied when the incoming flags change, tracked via the new `BC_PLASTICITY_FLAGS` entry

## [1.1.5] - 2026-04-24

### Fixed
- **Parametric generator export** — polysplines, spline-driven generators (Loft, Sweep, Lathe), Symmetry, Boole, Array, and parametric primitives placed in the Outbox are now correctly evaluated and uploaded to Plasticity via `PUT_SOME_1`; previously only pre-baked polygon meshes round-tripped
- **Temp-document lifetime** — evaluation document is retained on the scene handler so GC can't free it before the resulting polygon object is read; fixes intermittent empty-mesh on Outbox refresh
- **Live Link auto-refresh** — `NEW_VERSION` and `NEW_FILE` callbacks now trigger re-import while Live Link is active; previously empty stubs
- **Cache traversal** — `_collect_polygon_caches` rewritten with explicit deform-cache → polygon → generator-cache → children traversal; handles null/group cache containers returned by Boole

### Changed
- Internal package renamed from `modules/` to `core/`; all imports updated
- `NEW_VERSION` / `NEW_FILE` handling moved from scene handler to dialog (honours Live Link + busy state)
- **Auto-Refacet tag plugin ID** changed from `1066943` to `1068209` (properly registered via Maxon); scenes from 1.1.0 will lose tag bindings on load — re-apply via the Auto-Refacet checkbox

### Removed
- Dead imports (`struct`, `FacetShapeType`, `array`, stale handler imports in dialog)
- Noisy `[Plasticity] Send failed` print in WebSocket send loop

## [1.1.0] - 2026-04-03

### Added
- **Handshake protocol** — client sends `HANDSHAKE_1` on connect; server responds with supported message types for capability negotiation
- **Inbox / Outbox hierarchy** — root null now contains an Inbox (incoming Plasticity objects) and an Outbox (user-created SDS objects for upload); objects in the Outbox are protected from incoming updates
- **PutSome upload** — SDS objects placed in the Outbox are uploaded to Plasticity via `PUT_SOME_1` on Refresh (when the server supports it); control-cage resolution supports single polygon children (fast path), cache-based generator resolution, and CSTO fallback
- **Auto-Refacet tag** — custom `TagData` plugin (`Plasticity Auto-Refacet`) that stores refacet settings per object; tagged objects are automatically re-tessellated after every refresh or live-link geometry update
- **Auto-Refacet checkbox** — in the Basic tab below the Refacet button; when checked, clicking Refacet Selected stamps the tag on selected objects with current dialog settings
- Auto-refacet batching — objects with identical tag settings are grouped into a single `refacet_some` server request
- Legacy scene migration — objects from pre-1.1.0 scenes (directly under root) are detected and re-parented into Inbox on refresh

### Changed
- **Select Sharp Edges** — selects all Plasticity boundary edges on selected objects and switches to edge mode; replaces the former Mark Sharp (PhongBreak) feature
- **Angle threshold** — compact degree input field below the Select Sharp Edges button; filters edge selection to only include edges sharper than the specified dihedral angle (0° = all boundary edges)
- **Refacet Angle inputs** — All angle-related input values have been switched from radians to degrees
- Scene hierarchy restructured from flat root children to Inbox/Outbox sub-nulls
- `_process_objects` now accepts `outbox_ids` set to skip protected objects during list/transaction processing

### Removed
- **Mark Sharp Edges (PhongBreak)** — replaced by Select Sharp Edges with angle threshold
- **Smart Edge Marking checkbox** — replaced by the angle input field

## [1.0.0] - 2026-03-27

### Added
- Initial release
- WebSocket connection to Plasticity server with connect/disconnect controls
- Live Link mode for real-time mesh synchronization
- Full refresh via list all / list visible
- Tri and N-gon topology modes
- Refacet with simple and advanced options (tolerance, angle, min/max width, chord tolerances)
- Unit scale control applied via root null transform
- Plasticity-managed scene hierarchy with root nulls per file
- In-place geometry updates preserving user tags, materials, and animation
- Custom normals via NormalTag (tri mode) and post-melt reconstruction (N-gon mode)
- Poly-face map stored per object for downstream utility use
- **Utilities tab:**
  - Store Plasticity Faces — creates one PolygonSelectionTag per CAD face group
  - Store Plasticity Edges — creates a single EdgeSelectionTag with all Plasticity boundary edges
  - Select Plasticity Face(s) — expands polygon selection to whole CAD face groups
  - Select Plasticity Edges — selects perimeter edges of current face selection and switches to edge mode
  - Mark Sharp Edges (PhongBreak) — applies Phong Breaks at all CAD face boundaries
  - Smart Edge Marking — optional 5° normal threshold for PhongBreak marking
- All utility tags automatically reset on refresh, live-link update, or refacet
- All UI controls disabled until connected to server