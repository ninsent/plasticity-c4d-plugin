# Changelog

All notable changes to Plasticity Cinema 4D Bridge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

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