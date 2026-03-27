# Changelog

All notable changes to Plasticity Cinema 4D Bridge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)

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
