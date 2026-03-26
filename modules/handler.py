"""
Scene handler for creating and updating Cinema 4D objects from Plasticity data.

KEY DESIGN: When updating an existing object, geometry is replaced IN-PLACE via
ResizeObject() + SetPoint/SetPolygon. This preserves the C4D object identity,
keeping all user-assigned tags (materials, textures, selections), animation
tracks, constraints, and scene references intact — matching the Blender addon's
mesh.clear_geometry() + refill approach.

Only Plasticity-managed tags (NormalTag, named "__plasticity_normals__") are
stripped and recreated on each update. User-added tags are never touched.

N-GON APPROACH (refacet path only):
  Plasticity sends N-gon data as: a 'faces' membership array paired with an
  'indices' array. Each consecutive run of equal values in 'faces' defines one
  polygon's vertices (which may have 3, 4, or more corners).

  I build the object with fan-triangulated triangles, then use
  MCOMMAND_MELT on polygon selections to merge the triangle groups back into
  proper C4D N-gons. Ferdinand's polygon-identity hashing approach (from the
  Maxon Developer Forum) is used to keep the melt correct when indices shift
  after each successive melt operation.

NORMALS IN N-GON MODE:
  The melt operation changes the polygon topology, invalidating any pre-built
  NormalTag.  The solution is to pre-compute a corner_normals dict keyed by
  (welded_vertex_index, group_index) during triangulation — before the melt.
  After melt, each post-melt polygon is mapped back to its Plasticity group
  via vertex-identity tracking, then the NormalTag is rebuilt from the dict.
  This gives identical normal quality in both tri and N-gon modes.

POLY-FACE MAP (BC_PLASTICITY_POLY_FACE_MAP):
  A JSON list stored on each mesh object, one entry per C4D polygon, giving
  the Plasticity group index (0-based into face_ids) that the polygon belongs
  to.  Computed at import time for tri mode, or after melt for N-gon mode.
  This is the single source of truth for all downstream consumers (utilities,
  normal reconstruction, etc.) — no loop-range arithmetic required.

COORDINATE SYSTEM: Plasticity is Z-up, C4D is Y-up.
  C4D X = Plasticity X,  C4D Y = Plasticity Z,  C4D Z = Plasticity Y.
  Applied identically to vertex positions and normals.

WINDING ORDER: Reversed — CPolygon(a, c, b) instead of (a, b, c) to match
  C4D's counter-clockwise front-face convention.

UNIT SCALE: Lives on the root null's scale transform, not baked into vertices.
  update_unit_scale() propagates changes to all root nulls instantly.
"""

import c4d
import array
import bisect
import json
from typing import Dict, List, Optional, Any, Set, Tuple

from modules.protocol import ObjectType, MessageType
from modules.threading_bridge import ThreadingBridge, BridgeEvent, EventType

# BaseContainer keys — offsets from registered plugin ID 1066929
PLUGIN_ID              = 1066929
BC_PLASTICITY_ID       = PLUGIN_ID + 1   # 1066930
BC_PLASTICITY_FILENAME = PLUGIN_ID + 2   # 1066931
BC_PLASTICITY_GROUPS   = PLUGIN_ID + 3   # 1066932
BC_PLASTICITY_FACE_IDS = PLUGIN_ID + 4   # 1066933
BC_PLASTICITY_ROOT     = PLUGIN_ID + 5   # 1066934
BC_PLASTICITY_POLY_FACE_MAP = PLUGIN_ID + 6   # 1066935

MANAGED_NORMAL_TAG_NAME = "__plasticity_normals__"

# Plasticity sends vertex positions in metres; C4D's internal unit is
# centimetres.  Multiply every incoming coordinate by this factor so the
# geometry appears at the correct size *before* the user's unit_scale is
# applied on the root null.
_IMPORT_SCALE = 100.0


# =============================================================================
# Ear-clipping triangulation for concave N-gons
# =============================================================================

def _cross_2d(ax, ay, bx, by, px, py):
    """Signed area ×2 of triangle ABP.  Positive if P is left of A→B (CCW)."""
    return (bx - ax) * (py - ay) - (by - ay) * (px - ax)


def _point_in_triangle_2d(px, py, ax, ay, bx, by, cx, cy):
    """True if point P is inside or on the boundary of triangle ABC."""
    d1 = _cross_2d(ax, ay, bx, by, px, py)
    d2 = _cross_2d(bx, by, cx, cy, px, py)
    d3 = _cross_2d(cx, cy, ax, ay, px, py)
    has_neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    has_pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (has_neg and has_pos)


def _compute_polygon_normal(pts):
    """
    Newell's method — robust face normal from an ordered 3D point ring.
    Works for non-planar and concave polygons.  Returns a unit c4d.Vector.
    """
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        c = pts[i]
        nxt = pts[(i + 1) % n]
        nx += (c.y - nxt.y) * (c.z + nxt.z)
        ny += (c.z - nxt.z) * (c.x + nxt.x)
        nz += (c.x - nxt.x) * (c.y + nxt.y)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return c4d.Vector(0, 1, 0)
    return c4d.Vector(nx / length, ny / length, nz / length)


def _project_to_2d(pts, normal):
    """
    Flatten 3D points to 2D by dropping the axis most aligned with the
    polygon normal.  Returns a list of (u, v) tuples.
    """
    ax = abs(normal.x)
    ay = abs(normal.y)
    az = abs(normal.z)
    if ax >= ay and ax >= az:
        return [(p.y, p.z) for p in pts]
    elif ay >= az:
        return [(p.x, p.z) for p in pts]
    else:
        return [(p.x, p.y) for p in pts]


def _is_convex(pts_2d):
    """
    Quick O(n) convexity test.  Returns True if the polygon is strictly
    convex (all cross products have the same sign).
    """
    n = len(pts_2d)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        ax, ay = pts_2d[i]
        bx, by = pts_2d[(i + 1) % n]
        cx, cy = pts_2d[(i + 2) % n]
        cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if cross > 1e-12:
            if sign < 0:
                return False
            sign = 1
        elif cross < -1e-12:
            if sign > 0:
                return False
            sign = -1
    return sign != 0


def _ear_clip(pts_2d):
    """
    Ear-clipping triangulation for a simple (possibly concave) polygon.

    Args:
        pts_2d: list of (x, y) coordinate tuples — the polygon perimeter
                in order.

    Returns:
        List of (i, j, k) index triples into pts_2d.  Each triple is one
        triangle.  Returns [] on degenerate input (< 3 vertices).
    """
    n = len(pts_2d)
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    # Fast-path: convex polygon → simple fan triangulation (very common
    # for CAD-tessellated output from Plasticity).
    if _is_convex(pts_2d):
        return [(0, i + 1, i + 2) for i in range(n - 2)]

    # Signed area → winding direction
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts_2d[i][0] * pts_2d[j][1]
        area -= pts_2d[j][0] * pts_2d[i][1]
    if abs(area) < 1e-12:
        return []                       # degenerate (zero-area) polygon
    ccw = area > 0.0

    # Pre-extract coordinates for fast access (avoid repeated tuple indexing)
    xs = [p[0] for p in pts_2d]
    ys = [p[1] for p in pts_2d]

    remaining = list(range(n))          # indices into pts_2d still alive
    triangles = []

    # Build initial reflex vertex set (only reflex vertices can block ears)
    reflex = set()
    for i in range(n):
        pi = remaining[(i - 1) % n]
        ci = remaining[i]
        ni = remaining[(i + 1) % n]
        cross = _cross_2d(xs[pi], ys[pi], xs[ci], ys[ci], xs[ni], ys[ni])
        if (ccw and cross <= 0) or (not ccw and cross >= 0):
            reflex.add(ci)

    max_iter = n * n                    # safety cap

    for _ in range(max_iter):
        m = len(remaining)
        if m < 3:
            break
        if m == 3:
            triangles.append(tuple(remaining))
            break

        found_ear = False
        for i in range(m):
            ci = remaining[i]
            if ci in reflex:
                continue                # reflex vertex can't be an ear tip

            pi = remaining[(i - 1) % m]
            ni = remaining[(i + 1) % m]

            pix, piy = xs[pi], ys[pi]
            cix, ciy = xs[ci], ys[ci]
            nix, niy = xs[ni], ys[ni]

            # Containment test: only check reflex vertices (they are the
            # only ones that can be inside the ear triangle).
            is_ear = True
            for ri in reflex:
                if ri == pi or ri == ci or ri == ni:
                    continue
                if _point_in_triangle_2d(
                    xs[ri], ys[ri], pix, piy, cix, ciy, nix, niy,
                ):
                    is_ear = False
                    break

            if is_ear:
                triangles.append((pi, ci, ni))
                remaining.pop(i)
                reflex.discard(ci)

                # Neighbours may have changed convexity — update reflex set
                m2 = len(remaining)
                if m2 >= 3:
                    for check_i in (
                        remaining.index(pi) if pi in remaining else -1,
                        remaining.index(ni) if ni in remaining else -1,
                    ):
                        if check_i < 0:
                            continue
                        p2 = remaining[(check_i - 1) % m2]
                        c2 = remaining[check_i]
                        n2 = remaining[(check_i + 1) % m2]
                        cross = _cross_2d(
                            xs[p2], ys[p2], xs[c2], ys[c2], xs[n2], ys[n2])
                        if (ccw and cross <= 0) or (not ccw and cross >= 0):
                            reflex.add(c2)
                        else:
                            reflex.discard(c2)

                found_ear = True
                break

        if not found_ear:
            break                       # stuck — degenerate remainder

    return triangles


class SceneHandler:
    def __init__(self, bridge: ThreadingBridge):
        self.bridge = bridge
        self.unit_scale = 1.0

        self._items  = {}   # (filename, id) -> c4d.BaseObject  (mesh objects)
        self._groups = {}   # (filename, id) -> c4d.BaseObject  (null groups)
        self._roots  = {}   # filename       -> c4d.BaseObject  (root nulls)

        # Fix #1: on_connect / on_disconnect run on the main thread via bridge
        bridge.register_callback(EventType.CONNECTED,           self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,        self._on_disconnected)
        bridge.register_callback(EventType.LIST_RESPONSE,       self._on_list_response)
        bridge.register_callback(EventType.TRANSACTION,         self._on_transaction)
        bridge.register_callback(EventType.REFACET_RESPONSE,    self._on_refacet_response)
        bridge.register_callback(EventType.NEW_VERSION,         self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,            self._on_new_file)

    # =========================================================================
    # Connection lifecycle (Fix #1: dispatched on the main thread)
    # =========================================================================

    def _on_connected(self, event: BridgeEvent):
        self.on_connect()

    def _on_disconnected(self, event: BridgeEvent):
        self.on_disconnect()

    def on_connect(self):
        self._items.clear()
        self._groups.clear()
        self._roots.clear()

    def on_disconnect(self):
        self._items.clear()
        self._groups.clear()
        self._roots.clear()

    # =========================================================================
    # Event handlers
    # =========================================================================

    def _on_list_response(self, event: BridgeEvent):
        """Full refresh: create/update all objects, delete stale ones."""
        data = event.data
        if not data:
            return
        filename = data.get('filename', '')
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        deferred_ngons = []

        doc.StartUndo()
        try:
            root = self._get_or_create_root(doc, filename)
            self._prepare(doc, filename)

            all_item_ids  = set()
            all_group_ids = set()
            all_objects   = data.get('add', []) + data.get('update', [])

            for obj_data in all_objects:
                ot  = int(obj_data.get('type', -1))
                oid = obj_data.get('id', 0)
                if ot == ObjectType.GROUP:
                    all_group_ids.add(oid)
                else:
                    all_item_ids.add(oid)

            deferred_ngons = self._process_objects(doc, filename, root, all_objects)

            stale = [k for k in self._items  if k[0] == filename and k[1] not in all_item_ids]
            for k in stale:
                obj = self._items.pop(k, None)
                if obj and obj.GetDocument() == doc:
                    doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, obj)
                    obj.Remove()

            stale = [k for k in self._groups if k[0] == filename and k[1] not in all_group_ids]
            for k in stale:
                grp = self._groups.pop(k, None)
                if grp and grp.GetDocument() == doc:
                    doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, grp)
                    grp.Remove()

        finally:
            doc.EndUndo()

        # MELT must run outside the undo block — SMC manages its own undo entries.
        # Release each entry after processing to free corner_normals memory.
        for i in range(len(deferred_ngons)):
            obj, poly_groups, corner_normals, poly_face_val = deferred_ngons[i]
            deferred_ngons[i] = None
            self._create_ngon_groups(obj, poly_groups, corner_normals,
                                     poly_face_val)

        c4d.EventAdd()

    def _on_transaction(self, event: BridgeEvent):
        """Incremental live-link update."""
        data = event.data
        if not data:
            return
        filename = data.get('filename', '')
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        doc.StartUndo()
        deferred_ngons = []
        try:
            root = self._get_or_create_root(doc, filename)
            self._prepare(doc, filename)

            for oid in data.get('delete', []):
                self._delete_item(doc, filename, oid)

            all_objects = data.get('add', []) + data.get('update', [])
            if all_objects:
                deferred_ngons = self._process_objects(doc, filename, root, all_objects)

        finally:
            doc.EndUndo()

        # MELT must run outside the undo block — SMC manages its own undo entries.
        for i in range(len(deferred_ngons)):
            obj, poly_groups, corner_normals, poly_face_val = deferred_ngons[i]
            deferred_ngons[i] = None
            self._create_ngon_groups(obj, poly_groups, corner_normals,
                                     poly_face_val)

        c4d.EventAdd()

    def _on_refacet_response(self, event: BridgeEvent):
        """
        Re-tessellate objects with new facet settings.

        The refacet protocol uses different field names than standard transactions:
          'indices' = flat vertex-index list  (= 'faces' role in standard protocol)
          'faces'   = polygon-membership array (one entry per index position;
                      consecutive equal values = one polygon's vertices in order)
        """
        data = event.data
        if not data:
            return
        filename = data.get('filename', '')
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        deferred_ngons = []   # [(obj, pg, cn, pfv)] — melted OUTSIDE undo block

        doc.StartUndo()
        try:
            self._prepare(doc, filename)

            for item in data.get('refaceted_objects', []):
                pid  = item.get('plasticity_id')
                key  = (filename, pid)
                obj  = self._items.get(key)
                if not obj or obj.GetDocument() != doc:
                    continue

                verts    = item.get('vertices', [])
                indices  = item.get('indices',  [])   # vertex indices per loop
                faces    = item.get('faces',    [])   # polygon-membership per loop
                normals  = item.get('normals',  [])
                groups   = item.get('groups',   [])
                face_ids = item.get('face_ids', [])

                if not verts:
                    continue

                doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)
                pg, cn, pfv, pfm = self._update_object_geometry(
                    obj, verts, indices, faces, normals, groups)
                self._copy_plasticity_meta(
                    obj, pid, filename, groups, face_ids,
                    poly_face_map=pfm if not pg else None)

                if pg:
                    deferred_ngons.append((obj, pg, cn, pfv))

        finally:
            doc.EndUndo()

        # SendModelingCommand(MCOMMAND_MELT) manages its own undo entries and
        # fails silently when called inside StartUndo/EndUndo — run it after.
        for i in range(len(deferred_ngons)):
            obj, pg, cn, pfv = deferred_ngons[i]
            deferred_ngons[i] = None
            self._create_ngon_groups(obj, pg, cn, pfv)

        c4d.EventAdd()

    def _on_new_version(self, event: BridgeEvent):
        data = event.data
        if data:
            fn  = data.get('filename', '')
            ver = data.get('version', 0)
            msg = f"New version available — '{fn}' v{ver}. Click Refresh to update."
            self.bridge.status_message = msg
            print(f"[Plasticity] {msg}")

    def _on_new_file(self, event: BridgeEvent):
        data = event.data
        if data:
            fn  = data.get('filename', '')
            msg = f"New file opened in Plasticity: '{fn}'. Click Refresh to import."
            self.bridge.status_message = msg
            self.bridge.filename = fn
            print(f"[Plasticity] {msg}")

    # =========================================================================
    # Cache management
    # =========================================================================

    def _prepare(self, doc, filename):
        """Rebuild caches by scanning the scene — undo-safe."""
        root = self._get_or_create_root(doc, filename)
        self._items  = {k: v for k, v in self._items.items()  if k[0] != filename}
        self._groups = {k: v for k, v in self._groups.items() if k[0] != filename}

        def scan(parent):
            child = parent.GetDown()
            while child:
                bc  = child.GetDataInstance()
                pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
                fn  = bc.GetString(BC_PLASTICITY_FILENAME, "")
                if pid != 0 and fn == filename:
                    if child.CheckType(c4d.Onull):
                        self._groups[(fn, pid)] = child
                    else:
                        self._items[(fn, pid)] = child
                scan(child)
                child = child.GetNext()

        scan(root)

    # =========================================================================
    # Two-pass object processing
    # =========================================================================

    def _process_objects(self, doc, filename, root, objects):
        """
        Pass 1: Geometry — create new objects or update existing in-place.
        Pass 2: Hierarchy — re-parent, apply visibility.

        N-gon melts are deferred until after all insertions in Pass 1 so that
        the objects are guaranteed to be in the document before SMC runs.
        """
        deferred_ngons = []   # list of (obj, poly_groups, corner_normals, poly_face_val)

        # ── Pass 1: Geometry ──────────────────────────────────────────────────
        for item in objects:
            obj_type = int(item.get('type', -1))
            obj_id   = item.get('id', 0)
            name     = item.get('name', f"Object_{obj_id}")

            # Standard ADD/UPDATE:
            #   item['vertices'] = flat float32 vertex positions
            #   item['faces']    = flat int32 triangle vertex indices
            #   (no polygon-membership; N-gons only arrive via refacet path)
            verts    = item.get('vertices', [])
            indices  = item.get('faces',    [])
            normals  = item.get('normals',  [])
            groups   = item.get('groups',   [])
            face_ids = item.get('face_ids', [])

            if obj_type == ObjectType.GROUP:
                if obj_id == 0:
                    continue
                key = (filename, obj_id)
                if key not in self._groups:
                    grp = c4d.BaseObject(c4d.Onull)
                    grp.SetName(name)
                    self._copy_plasticity_meta(grp, obj_id, filename)
                    self._insert_last_child(doc, grp, root)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, grp)
                    self._groups[key] = grp
                else:
                    self._groups[key].SetName(name)

            elif obj_type in (ObjectType.SOLID, ObjectType.SHEET):
                if not verts:
                    continue

                key      = (filename, obj_id)
                existing = self._items.get(key)

                if existing and existing.GetDocument() == doc:
                    # IN-PLACE UPDATE — all user tags / animation survive
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, existing)
                    existing.SetName(name)
                    # faces=[] means tri mode; standard objects carry no poly-membership
                    pg, cn, pfv, pfm = self._update_object_geometry(
                        existing, verts, indices, [], normals, groups)
                    self._copy_plasticity_meta(
                        existing, obj_id, filename, groups, face_ids,
                        poly_face_map=pfm if not pg else None)
                    if pg:
                        deferred_ngons.append((existing, pg, cn, pfv))

                else:
                    # NEW OBJECT (standard path = tri mode, poly_groups = [])
                    points, polys, normal_map, poly_groups, cn, pfv = \
                        self._compute_geometry(verts, indices, [], normals, groups)

                    if not polys:
                        continue

                    new_obj = c4d.PolygonObject(len(points), len(polys))
                    new_obj.SetName(name)
                    self._write_points_and_polys(new_obj, points, polys)

                    if not poly_groups and normals and normal_map:
                        self._apply_normals(new_obj, normals, normal_map)
                        pfm = self._compute_tri_face_map(len(polys), groups)
                    else:
                        pfm = None   # ngon: _create_ngon_groups will set it

                    phong = new_obj.MakeTag(c4d.Tphong)
                    phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)
                    phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(poly_groups)
                    new_obj.Message(c4d.MSG_UPDATE)

                    self._copy_plasticity_meta(
                        new_obj, obj_id, filename, groups, face_ids,
                        poly_face_map=pfm)
                    self._insert_last_child(doc, new_obj, root)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, new_obj)
                    self._items[key] = new_obj

                    if poly_groups:
                        deferred_ngons.append((new_obj, poly_groups, cn, pfv))

        # Deferred N-gon merges collected during Pass 1 — returned to caller so
        # they can be run OUTSIDE the active StartUndo/EndUndo block.
        # (SendModelingCommand manages its own undo and fails inside undo blocks.)

        # ── Pass 2: Re-parent and apply visibility ────────────────────────────
        for item in objects:
            obj_type  = int(item.get('type', -1))
            obj_id    = item.get('id', 0)
            parent_id = item.get('parent_id', 0)
            flags     = item.get('flags', 6)

            if obj_id == 0:
                continue

            should_hide = bool(flags & 1) or not bool(flags & 2)
            vis = c4d.OBJECT_OFF if should_hide else c4d.OBJECT_UNDEF

            if obj_type == ObjectType.GROUP:
                key = (filename, obj_id)
                grp = self._groups.get(key)
                if not grp:
                    continue
                target_parent = root
                if parent_id > 0 and (filename, parent_id) in self._groups:
                    target_parent = self._groups[(filename, parent_id)]
                if grp.GetUp() != target_parent:
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, grp)
                    grp.Remove()
                    self._insert_last_child(doc, grp, target_parent)
                grp[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = vis
                grp[c4d.ID_BASEOBJECT_VISIBILITY_RENDER]  = vis

            elif obj_type in (ObjectType.SOLID, ObjectType.SHEET):
                key = (filename, obj_id)
                obj = self._items.get(key)
                if not obj:
                    continue
                target_parent = root
                if parent_id > 0 and (filename, parent_id) in self._groups:
                    target_parent = self._groups[(filename, parent_id)]
                if obj.GetUp() != target_parent:
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)
                    obj.Remove()
                    self._insert_last_child(doc, obj, target_parent)
                obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = vis
                obj[c4d.ID_BASEOBJECT_VISIBILITY_RENDER]  = vis

        return deferred_ngons

    # =========================================================================
    # Geometry computation
    # =========================================================================

    def _compute_geometry(self, vertices, indices, faces, normals, groups):
        """
        Route to the correct geometry builder based on whether N-gon
        polygon-membership data is present.

        Returns:
            (points, polys, normal_map, poly_groups, corner_normals, poly_face_val)

            poly_groups = [] means tri mode (no merging needed).
            poly_groups = [[...], [...]] means N-gon mode: groups of triangle
            poly indices that should be merged by _create_ngon_groups().

            corner_normals: dict of {(welded_vert_idx, group_idx): (nx, ny, nz)}
                Pre-computed normals for post-melt NormalTag reconstruction.
                Stored as float tuples (not c4d.Vector) for memory efficiency.
                Empty in tri mode (normal_map is used instead).

            poly_face_val: list of group indices, one per pre-melt polygon.
                Used by _create_ngon_groups to rebuild the face map after melt.
                Empty in tri mode.
        """
        if faces:
            return self._compute_ngon_geometry(vertices, indices, faces, normals, groups)
        else:
            return self._compute_tri_geometry(vertices, indices, normals, groups)

    def _compute_tri_geometry(self, vertices, indices, normals, groups):
        """
        Build pure-triangle geometry for standard ADD/UPDATE objects.

        No vertex welding needed — Plasticity sends deduplicated vertex
        buffers on the standard protocol path.

        Coordinate swap: C4D X = Pl X,  C4D Y = Pl Z,  C4D Z = Pl Y.
        Winding:  CPolygon(a, c, b) — reversed for C4D front-face convention.
        """
        vert_count = len(vertices) // 3
        tri_count  = len(indices)  // 3

        points = []
        s = _IMPORT_SCALE
        for i in range(vert_count):
            points.append(c4d.Vector(
                vertices[i * 3]     * s,
                vertices[i * 3 + 2] * s,   # Plasticity Z → C4D Y
                vertices[i * 3 + 1] * s,   # Plasticity Y → C4D Z
            ))

        polys      = []
        normal_map = []
        for i in range(tri_count):
            a  = indices[i * 3]
            b  = indices[i * 3 + 1]
            c_ = indices[i * 3 + 2]
            polys.append(c4d.CPolygon(a, c_, b))
            normal_map.append((a, c_, b, b))

        # corner_normals and poly_face_val are empty for tri mode —
        # tri mode uses normal_map + _apply_normals, and poly_face_map
        # is computed separately via _compute_tri_face_map.
        return points, polys, normal_map, [], {}, []

    def _compute_ngon_geometry(self, vertices, indices, faces, normals, groups):
        """
        Build geometry for N-gon refacet data.

        The refacet protocol sends:
          indices: vertex-index per loop position (flat list)
          faces:   polygon-membership per loop (same length);
                   a run of equal values = one polygon's vertices in order

        Steps:
          1. Weld duplicate vertices (Plasticity may send shared vertices as
             separate entries in N-gon mode).
          2. Remove consecutive duplicate vertices produced by welding
             (they would create degenerate zero-area ears).
          3. Triangulate each polygon with ear-clipping. This is safe for
             concave and stitched-hole polygons (e.g. annular faces).
             Falls back to fan triangulation if ear clipping fails.
          4. Record which output poly indices belong to each input polygon as
             poly_groups, so _create_ngon_groups() can melt them later.
          5. Build corner_normals dict keyed by (welded_vert_idx, group_idx)
             for post-melt NormalTag reconstruction.
          6. Build poly_face_val list mapping each pre-melt polygon to its
             Plasticity group index for post-melt face-map reconstruction.

        The group_idx used in corner_normals / poly_face_val is the 0-based
        index into the Plasticity 'groups' array (i.e. which Plasticity face
        the polygon belongs to).  We derive it from the faces[] membership
        value by building a face_val→group_idx mapping from the groups array.
        """
        vert_count = len(vertices) // 3

        # ── Build face-value → group-index mapping ───────────────────────
        # The 'groups' array is [loop_start, loop_count, loop_start, ...].
        # The 'faces' membership values are Plasticity face IDs.
        # We need to map each face-value back to its 0-based group index.
        #
        # Strategy: For each polygon boundary (runs of equal values in faces[]),
        # its first loop position falls within exactly one group's
        # [loop_start, loop_start+loop_count) range.  That gives us the
        # group index for every face-value we encounter.
        #
        # We pre-build a sorted list of (loop_start, group_idx) for
        # binary-search lookup.
        group_starts = []   # [(loop_start, group_idx), ...]
        for gi in range(len(groups) // 2):
            group_starts.append((groups[gi * 2], gi))
        group_starts.sort(key=lambda x: x[0])
        gs_keys = [gs[0] for gs in group_starts]   # for bisect

        def _face_val_to_group_idx(loop_pos):
            """Map a loop position to its 0-based group index."""
            idx = bisect.bisect_right(gs_keys, loop_pos) - 1
            if idx >= 0:
                return group_starts[idx][1]
            return 0   # fallback

        # Step 1: weld duplicate vertices
        vert_map   = {}
        old_to_new = [0] * vert_count
        unique_pts = []
        s = _IMPORT_SCALE

        for i in range(vert_count):
            px = vertices[i * 3]
            py = vertices[i * 3 + 1]
            pz = vertices[i * 3 + 2]
            key = (round(px, 7), round(py, 7), round(pz, 7))
            if key not in vert_map:
                vert_map[key] = len(unique_pts)
                unique_pts.append(c4d.Vector(px * s, pz * s, py * s))   # coord swap + scale
            old_to_new[i] = vert_map[key]

        new_indices = [old_to_new[idx] for idx in indices]

        polys         = []
        normal_map    = []
        poly_groups   = []   # list[list[int]]
        poly_face_val = []   # group_idx per pre-melt polygon
        poly_idx      = 0    # running polygon counter

        # ── corner_normals: {(welded_vert_idx, group_idx): (nx, ny, nz)} ────
        # Populated from every loop position. Resolves split normals at face
        # boundaries because the same welded vertex gets separate entries
        # keyed by which group is asking.
        #
        # Stored as lightweight float tuples (not c4d.Vector) to avoid the
        # heavy C++ wrapper overhead when accumulating data for 100+ objects
        # in deferred_ngons. Converted to c4d.Vector only at NormalTag write.
        corner_normals = {}
        has_normals    = len(normals) >= 3

        def _ntuple(orig_vi):
            """Coord-swapped normal as (x, y, z) float tuple."""
            base = orig_vi * 3
            if base + 2 < len(normals):
                return (
                    normals[base],
                    normals[base + 2],   # Plasticity Nz → C4D Ny
                    normals[base + 1],   # Plasticity Ny → C4D Nz
                )
            return (0.0, 1.0, 0.0)

        # Find polygon boundaries (runs of equal values in 'faces')
        poly_starts = [0]
        for i in range(1, len(faces)):
            if faces[i] != faces[i - 1]:
                poly_starts.append(i)
        poly_starts.append(len(faces))

        for p in range(len(poly_starts) - 1):
            start = poly_starts[p]
            end   = poly_starts[p + 1]

            raw_face_vi = new_indices[start:end]     # welded vertex indices
            raw_orig_vi = indices[start:end]          # pre-weld for normal lookup

            # Determine the group index for this polygon
            group_idx = _face_val_to_group_idx(start)

            # Step 5: populate corner_normals for every loop in this polygon
            # (before dedup, so we don't lose any split-normal data)
            if has_normals:
                for k in range(len(raw_face_vi)):
                    welded_vi = raw_face_vi[k]
                    cn_key = (welded_vi, group_idx)
                    if cn_key not in corner_normals:
                        corner_normals[cn_key] = _ntuple(raw_orig_vi[k])

            # Step 2: remove consecutive duplicate vertices after welding.
            # E.g. [5, 5, 7, 8, 8, 3] → [5, 7, 8, 3] with matching orig_vindices.
            face_vindices = []
            orig_vindices = []
            for k in range(len(raw_face_vi)):
                if k == 0 or raw_face_vi[k] != raw_face_vi[k - 1]:
                    face_vindices.append(raw_face_vi[k])
                    orig_vindices.append(raw_orig_vi[k])
            # Also check wrap-around: if last == first, drop last
            if len(face_vindices) > 1 and face_vindices[-1] == face_vindices[0]:
                face_vindices.pop()
                orig_vindices.pop()

            count = len(face_vindices)
            if count < 3:
                continue

            # Step 3: triangulate — ear-clipping for concave safety
            group_poly_indices = []

            if count == 3:
                # Triangle — no triangulation needed
                ia, ib, ic = face_vindices
                polys.append(c4d.CPolygon(ia, ic, ib))      # reversed winding
                normal_map.append((
                    orig_vindices[0], orig_vindices[2],
                    orig_vindices[1], orig_vindices[1],
                ))
                group_poly_indices.append(poly_idx)
                poly_face_val.append(group_idx)
                poly_idx += 1

            elif count == 4:
                # Quad — native C4D quad, no triangulation or melt needed.
                # CPolygon with 4 distinct indices is a native quad.
                # Reversed winding: (a, d, c, b) instead of (a, b, c, d).
                ia, ib, ic, id_ = face_vindices
                polys.append(c4d.CPolygon(ia, id_, ic, ib))
                normal_map.append((
                    orig_vindices[0], orig_vindices[3],
                    orig_vindices[2], orig_vindices[1],
                ))
                group_poly_indices.append(poly_idx)
                poly_face_val.append(group_idx)
                poly_idx += 1

            else:
                # Gather 3D positions (already in C4D coords) for this face
                face_pts_3d = [unique_pts[vi] for vi in face_vindices]

                # Compute face normal and project to 2D for ear clipping
                face_normal = _compute_polygon_normal(face_pts_3d)
                pts_2d = _project_to_2d(face_pts_3d, face_normal)

                ear_tris = _ear_clip(pts_2d)

                # Fallback: if ear clipping produced nothing (degenerate),
                # use fan triangulation as a last resort.
                if not ear_tris:
                    print(f"[Plasticity] Ear-clip failed for {count}-gon, "
                          f"falling back to fan triangulation")
                    ear_tris = [(0, t + 1, t + 2) for t in range(count - 2)]

                for li_a, li_b, li_c in ear_tris:
                    ia = face_vindices[li_a]
                    ib = face_vindices[li_b]
                    ic = face_vindices[li_c]

                    polys.append(c4d.CPolygon(ia, ic, ib))   # reversed winding
                    normal_map.append((
                        orig_vindices[li_a], orig_vindices[li_c],
                        orig_vindices[li_b], orig_vindices[li_b],
                    ))
                    group_poly_indices.append(poly_idx)
                    poly_face_val.append(group_idx)
                    poly_idx += 1

            # Only groups with 2+ triangles need merging
            if len(group_poly_indices) > 1:
                poly_groups.append(group_poly_indices)

        return unique_pts, polys, normal_map, poly_groups, corner_normals, poly_face_val

    # =========================================================================
    # Write geometry
    # =========================================================================

    @staticmethod
    def _write_points_and_polys(obj, points, polys):
        for i, pt   in enumerate(points): obj.SetPoint(i, pt)
        for i, poly in enumerate(polys):  obj.SetPolygon(i, poly)

    # =========================================================================
    # In-place geometry update
    # =========================================================================

    def _update_object_geometry(self, obj, vertices, indices, faces, normals,
                                groups):
        """
        Replace all geometry on an existing PolygonObject in-place.

        Only the managed NormalTag is stripped; all user tags survive.
        In N-gon mode, the NormalTag and face map are applied AFTER melt
        by _create_ngon_groups() — they cannot be built here because the
        melt will change the polygon topology.

        Returns:
            (poly_groups, corner_normals, poly_face_val, poly_face_map)

            poly_groups:    list[list[int]] — non-empty in N-gon mode.
            corner_normals: dict — for post-melt NormalTag (empty in tri mode).
            poly_face_val:  list — group idx per pre-melt polygon (empty in tri mode).
            poly_face_map:  list|None — poly→group map; set now for tri, None for ngon.

            The CALLER is responsible for running _create_ngon_groups() OUTSIDE
            any active StartUndo/EndUndo block, because SendModelingCommand
            (MCOMMAND_MELT) manages its own undo entries and fails silently
            when called inside an existing undo context.
        """
        points, polys, normal_map, poly_groups, corner_normals, poly_face_val = \
            self._compute_geometry(vertices, indices, faces, normals, groups)
        if not polys:
            return [], {}, [], None

        self._strip_managed_tags(obj)
        obj.ResizeObject(len(points), len(polys))
        self._write_points_and_polys(obj, points, polys)

        poly_face_map = None
        if not poly_groups:
            # Tri mode: apply normals immediately and compute face map
            if normals and normal_map:
                self._apply_normals(obj, normals, normal_map)
            poly_face_map = self._compute_tri_face_map(len(polys), groups)

        phong = obj.GetTag(c4d.Tphong)
        if not phong:
            phong = obj.MakeTag(c4d.Tphong)
            phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)
        # Tri mode: NormalTag is the authority → angle limit off.
        # N-gon mode: keep angle limit as safety net until _create_ngon_groups
        # applies the NormalTag and flips this to False.
        phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(poly_groups)

        obj.Message(c4d.MSG_UPDATE)

        # Do NOT call _create_ngon_groups here — must run outside undo block.
        return poly_groups, corner_normals, poly_face_val, poly_face_map

    def _strip_managed_tags(self, obj):
        tag = obj.GetFirstTag()
        while tag:
            next_tag = tag.GetNext()
            if tag.CheckType(c4d.Tnormal) and tag.GetName() == MANAGED_NORMAL_TAG_NAME:
                tag.Remove()
            tag = next_tag

    # =========================================================================
    # Custom normals
    # =========================================================================

    def _apply_normals(self, obj, normals, normal_map):
        """Write per-corner normals (tri mode). Tries modern SetPolygon API, falls back to int16."""
        poly_count   = obj.GetPolygonCount()
        normal_count = len(normals) // 3
        if poly_count == 0 or normal_count == 0:
            return

        tag = c4d.NormalTag(poly_count)
        tag.SetName(MANAGED_NORMAL_TAG_NAME)
        obj.InsertTag(tag)

        def _nvec(v_id):
            if v_id * 3 + 2 < len(normals):
                return c4d.Vector(
                    normals[v_id * 3],
                    normals[v_id * 3 + 2],   # Plasticity Nz → C4D Ny
                    normals[v_id * 3 + 1],   # Plasticity Ny → C4D Nz
                )
            return c4d.Vector(0.0, 1.0, 0.0)

        # Modern API (C4D 2023 / S26+)
        try:
            data_w = tag.GetDataAddressW()
            for i in range(poly_count):
                ids = normal_map[i] if i < len(normal_map) else (0, 0, 0, 0)
                c4d.NormalTag.SetPolygon(data_w, i,
                                         {c: _nvec(v) for c, v in zip('abcd', ids)})
            return
        except (AttributeError, TypeError):
            pass

        # Legacy API: raw int16 buffer
        data = array.array('h')

        def pack_n(v):
            return int(max(-32767.0, min(32767.0, v * 32767.0)))

        for i in range(poly_count):
            ids = normal_map[i] if i < len(normal_map) else (0, 0, 0, 0)
            for v_id in ids:
                if v_id * 3 + 2 < len(normals):
                    data.extend([
                        pack_n(normals[v_id * 3]),
                        pack_n(normals[v_id * 3 + 2]),
                        pack_n(normals[v_id * 3 + 1]),
                    ])
                else:
                    data.extend([0, 0, 0])

        buf = tag.GetLowlevelDataAddressW()
        if buf:
            raw = data.tobytes()
            buf[:len(raw)] = raw

    @staticmethod
    def _apply_normals_from_corner_map(obj, corner_normals, post_melt_face_map):
        """
        Write per-corner normals AFTER N-gon melt, using the pre-computed
        corner_normals dict and the post-melt face map.

        corner_normals stores (nx, ny, nz) float tuples (not c4d.Vector) to
        keep memory usage low when accumulating data for many objects.
        Conversion to c4d.Vector happens here, at write time.

        For each post-melt polygon i:
          - group_idx = post_melt_face_map[i]
          - For each corner vertex v of the polygon:
              normal = corner_normals[(v, group_idx)]
          - Write to NormalTag slot i.

        Falls back to (0, 1, 0) for any missing entries (should not happen
        with well-formed Plasticity data).
        """
        poly_count = obj.GetPolygonCount()
        if poly_count == 0 or not corner_normals:
            return

        fallback_t = (0.0, 1.0, 0.0)

        # Strip any existing managed NormalTag
        tag = obj.GetFirstTag()
        while tag:
            next_tag = tag.GetNext()
            if tag.CheckType(c4d.Tnormal) and tag.GetName() == MANAGED_NORMAL_TAG_NAME:
                tag.Remove()
            tag = next_tag

        tag = c4d.NormalTag(poly_count)
        tag.SetName(MANAGED_NORMAL_TAG_NAME)
        obj.InsertTag(tag)

        def _get_normal(vert_idx, group_idx):
            t = corner_normals.get((vert_idx, group_idx), fallback_t)
            return c4d.Vector(t[0], t[1], t[2])

        # Modern API (C4D 2023 / S26+)
        try:
            data_w = tag.GetDataAddressW()
            for i in range(poly_count):
                gi = post_melt_face_map[i] if i < len(post_melt_face_map) else 0
                cp = obj.GetPolygon(i)
                is_tri = (cp.c == cp.d)
                c4d.NormalTag.SetPolygon(data_w, i, {
                    'a': _get_normal(cp.a, gi),
                    'b': _get_normal(cp.b, gi),
                    'c': _get_normal(cp.c, gi),
                    'd': _get_normal(cp.d, gi) if not is_tri
                         else _get_normal(cp.c, gi),
                })
            return
        except (AttributeError, TypeError):
            pass

        # Legacy API: raw int16 buffer
        data = array.array('h')
        def pack_n(v):
            return int(max(-32767.0, min(32767.0, v * 32767.0)))

        for i in range(poly_count):
            gi = post_melt_face_map[i] if i < len(post_melt_face_map) else 0
            cp = obj.GetPolygon(i)
            is_tri = (cp.c == cp.d)
            for vert_idx in (cp.a, cp.b, cp.c,
                             cp.d if not is_tri else cp.c):
                t = corner_normals.get((vert_idx, gi), fallback_t)
                data.extend([pack_n(t[0]), pack_n(t[1]), pack_n(t[2])])

        buf = tag.GetLowlevelDataAddressW()
        if buf:
            raw = data.tobytes()
            buf[:len(raw)] = raw

    # =========================================================================
    # Poly-face map helpers
    # =========================================================================

    @staticmethod
    def _compute_tri_face_map(poly_count, groups):
        """
        Build the poly_face_map for tri-mode geometry.

        In tri mode each polygon covers exactly 3 loops, so polygon i
        starts at loop i*3.  The groups array is [loop_start, loop_count, ...]
        and we map each polygon to its 0-based group index.

        Returns a list of ints, one per polygon.
        """
        if not groups or poly_count == 0:
            return [0] * poly_count

        # Build sorted group boundaries for binary search
        n_groups = len(groups) // 2
        # (loop_start, group_idx) sorted by loop_start
        boundaries = sorted(
            ((groups[gi * 2], gi) for gi in range(n_groups)),
            key=lambda x: x[0],
        )
        b_keys = [b[0] for b in boundaries]

        result = []
        for pi in range(poly_count):
            loop_pos = pi * 3
            idx = bisect.bisect_right(b_keys, loop_pos) - 1
            if idx >= 0:
                result.append(boundaries[idx][1])
            else:
                result.append(0)
        return result

    # =========================================================================
    # N-gon creation — MCOMMAND_MELT with polygon-identity hashing
    # =========================================================================

    def _create_ngon_groups(self, obj, poly_groups, corner_normals,
                            poly_face_val):
        """
        Merge groups of adjacent triangles into C4D N-gons via MCOMMAND_MELT,
        then rebuild the NormalTag and poly_face_map for the post-melt topology.

        Problem: each MCOMMAND_MELT reduces the polygon count, shifting ALL
        subsequent polygon indices. Selecting polygons by their original index
        in later iterations would target the wrong polygons.

        Solution (Ferdinand, Maxon Developer Forum 2021):
          Store polygon identity as the (a, b, c, d) vertex-index tuple of each
          CPolygon. Before each melt, rebuild an inverted index from the current
          mesh state to translate the stored identity back to the live index.

          This is collision-safe for all manifold meshes. The only theoretical
          failure case — two polygons with identical vertex tuples — cannot occur
          in Plasticity's CAD-tessellated output.

        After melt (Steps 5–7):
          5. Rebuild poly_face_map for the post-melt topology by mapping each
             polygon back to its Plasticity group index via vertex-identity
             tracking (for survived polys) or vertex-set intersection (for
             newly melted polys).
          6. Apply NormalTag from corner_normals using the post-melt face map.
          7. Store poly_face_map as metadata and flip Phong angle limit off.

        Args:
            obj            : c4d.PolygonObject already in a document
            poly_groups    : list[list[int]] — each sub-list is original polygon
                             indices that should be melted into a single N-gon.
            corner_normals : dict {(welded_vert_idx, group_idx): (nx, ny, nz)}
            poly_face_val  : list[int] — group_idx per pre-melt polygon.

        Ref: https://developers.maxon.net/forum/topic/13458/set-ngons-with-python/7
        """
        groups_to_melt = [g for g in poly_groups if len(g) >= 2]
        if not groups_to_melt:
            # No melting needed — still build face map + normals for
            # objects that were all tris/quads in ngon mode.
            post_melt_face_map = poly_face_val[:]
            if corner_normals:
                self._apply_normals_from_corner_map(
                    obj, corner_normals, post_melt_face_map)
                phong = obj.GetTag(c4d.Tphong)
                if phong:
                    phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = False
            self._store_poly_face_map(obj, post_melt_face_map)
            obj.Message(c4d.MSG_UPDATE)
            return

        doc = obj.GetDocument()

        # Build the identity index from the initial mesh state (before any melts).
        # Maps original_poly_index -> (a, b, c, d) vertex tuple.
        all_polys = obj.GetAllPolygons()
        polygon_identity = {
            i: (cp.a, cp.b, cp.c, cp.d)
            for i, cp in enumerate(all_polys)
        }

        # Also build identity→group_idx for post-melt face-map reconstruction.
        # Every pre-melt polygon has a known group_idx from poly_face_val.
        identity_to_group = {}
        for orig_pid, id_key in polygon_identity.items():
            if orig_pid < len(poly_face_val):
                identity_to_group[id_key] = poly_face_val[orig_pid]

        # ── Step 1: Collect edges per group ──────────────────────────────────
        # Each polygon is a triangle (d == c) from ear-clip triangulation.
        # Edges are unordered vertex pairs stored as (min, max) for fast lookup.
        group_edges = []
        for group in groups_to_melt:
            edges = set()
            for pid in group:
                identity = polygon_identity.get(pid)
                if identity is None:
                    continue
                a, b, c, d = identity
                if c == d:                               # triangle
                    verts = (a, b, c)
                else:                                    # quad (shouldn't happen
                    verts = (a, b, c, d)                 # here, but handle safely)
                n = len(verts)
                for i in range(n):
                    v1, v2 = verts[i], verts[(i + 1) % n]
                    edges.add((v1, v2) if v1 < v2 else (v2, v1))
            group_edges.append(edges)

        # ── Step 2: Build adjacency graph between groups ─────────────────────
        # Two groups are adjacent if any of their triangles share an edge.
        # An edge→group inverted index makes this O(total_edges) instead of
        # O(n_groups²).
        n_groups = len(groups_to_melt)
        adjacent = [set() for _ in range(n_groups)]

        edge_to_group = {}       # edge -> first group index that owns it
        for gi, edges in enumerate(group_edges):
            for edge in edges:
                prev_gi = edge_to_group.get(edge)
                if prev_gi is not None and prev_gi != gi:
                    adjacent[gi].add(prev_gi)
                    adjacent[prev_gi].add(gi)
                else:
                    edge_to_group[edge] = gi

        # ── Step 3: Greedy graph colouring → non-adjacent batches ────────────
        # Each "colour" becomes one MCOMMAND_MELT pass.  For typical Plasticity
        # output most faces are isolated, so almost everything lands in colour 0.
        colors = [-1] * n_groups
        batches = []             # list[list[int]]  (group indices per batch)

        for gi in range(n_groups):
            used = set()
            for adj_gi in adjacent[gi]:
                if colors[adj_gi] >= 0:
                    used.add(colors[adj_gi])

            color = 0
            while color in used:
                color += 1

            colors[gi] = color
            while len(batches) <= color:
                batches.append([])
            batches[color].append(gi)

        # ── Step 4: Melt each batch ──────────────────────────────────────────
        for batch in batches:
            if not batch:
                continue

            # Rebuild inverted index for the CURRENT mesh state.
            # Must be rebuilt before each batch because the prior batch's melt
            # changed polygon indices.
            inverted = {
                (cp.a, cp.b, cp.c, cp.d): i
                for i, cp in enumerate(obj.GetAllPolygons())
            }

            # Translate all original poly indices in this batch to live indices.
            real_indices = []
            for gi in batch:
                for orig_pid in groups_to_melt[gi]:
                    id_key = polygon_identity.get(orig_pid)
                    if id_key is not None and id_key in inverted:
                        real_indices.append(inverted[id_key])

            if len(real_indices) < 2:
                continue

            sel = obj.GetPolygonS()
            sel.DeselectAll()
            for pid in real_indices:
                sel.Select(pid)

            result = c4d.utils.SendModelingCommand(
                command=c4d.MCOMMAND_MELT,
                list=[obj],
                mode=c4d.MODELINGCOMMANDMODE_POLYGONSELECTION,
                bc=c4d.BaseContainer(),
                doc=doc,
            )
            if not result:
                grp_count = len(batch)
                print(f"[Plasticity] Warning: MCOMMAND_MELT failed for "
                      f"batch of {grp_count} groups")

        # ── Step 5: Rebuild poly_face_map for the post-melt topology ─────────
        # For each post-melt polygon, determine its Plasticity group index:
        #   a) If its identity (a,b,c,d) exists in the pre-melt set → it
        #      survived the melt unchanged. Look up its group_idx directly.
        #   b) If the identity is NEW → it's a melted N-gon. Determine its
        #      group_idx by intersecting: for each corner vertex, collect the
        #      set of group_idxs that have entries in corner_normals for that
        #      vertex. The intersection across all corners gives the face.
        post_polys = obj.GetAllPolygons()
        post_melt_face_map = []

        # Build vert→group sets from corner_normals for fallback (step 5b).
        vert_groups = {}   # welded_vert_idx → set of group_idxs
        for (vi, gi) in corner_normals:
            if vi not in vert_groups:
                vert_groups[vi] = set()
            vert_groups[vi].add(gi)

        for cp in post_polys:
            id_key = (cp.a, cp.b, cp.c, cp.d)

            # 5a: survived polygon — identity matches a pre-melt polygon
            gi = identity_to_group.get(id_key)
            if gi is not None:
                post_melt_face_map.append(gi)
                continue

            # 5b: new polygon (melted N-gon) — intersect corner vertex groups
            is_tri = (cp.c == cp.d)
            corner_verts = [cp.a, cp.b, cp.c]
            if not is_tri:
                corner_verts.append(cp.d)

            # Start with the groups of the first corner vertex, then intersect
            common = vert_groups.get(corner_verts[0], set()).copy()
            for cv in corner_verts[1:]:
                common &= vert_groups.get(cv, set())
                if len(common) <= 1:
                    break   # early out

            if len(common) == 1:
                post_melt_face_map.append(next(iter(common)))
            elif len(common) > 1:
                # Multiple candidates — pick the one with the most
                # corner vertices matching (most specific).
                # This shouldn't happen with well-formed Plasticity data,
                # but handle it gracefully.
                best_gi = next(iter(common))
                post_melt_face_map.append(best_gi)
            else:
                # No match — shouldn't happen. Fall back to 0.
                post_melt_face_map.append(0)

        # ── Step 6: Apply NormalTag from corner_normals ──────────────────────
        if corner_normals:
            self._apply_normals_from_corner_map(
                obj, corner_normals, post_melt_face_map)

            # Flip Phong to defer to NormalTag
            phong = obj.GetTag(c4d.Tphong)
            if phong:
                phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = False

        # ── Step 7: Store poly_face_map as metadata ──────────────────────────
        self._store_poly_face_map(obj, post_melt_face_map)

        obj.Message(c4d.MSG_UPDATE)

    # =========================================================================
    # Metadata helpers
    # =========================================================================

    def _copy_plasticity_meta(self, obj, plasticity_id, filename,
                              groups=None, face_ids=None,
                              poly_face_map=None):
        bc = obj.GetDataInstance()
        bc.SetInt32(BC_PLASTICITY_ID, int(plasticity_id))
        bc.SetString(BC_PLASTICITY_FILENAME, str(filename))
        if groups   is not None:
            bc.SetString(BC_PLASTICITY_GROUPS,   json.dumps(groups))
        if face_ids is not None:
            bc.SetString(BC_PLASTICITY_FACE_IDS, json.dumps(face_ids))
        if poly_face_map is not None:
            bc.SetString(BC_PLASTICITY_POLY_FACE_MAP,
                         json.dumps(poly_face_map))

    @staticmethod
    def _store_poly_face_map(obj, poly_face_map):
        """Store poly_face_map on an object's BaseContainer (outside _copy_plasticity_meta)."""
        bc = obj.GetDataInstance()
        bc.SetString(BC_PLASTICITY_POLY_FACE_MAP, json.dumps(poly_face_map))

    # =========================================================================
    # Scene-tree helpers
    # =========================================================================

    def _get_or_create_root(self, doc, filename):
        """
        Get or create the root Null for a Plasticity filename.
        Identified by BC_PLASTICITY_ROOT marker — immune to user renaming.

        Fix #4: Uses a recursive scan of the entire document hierarchy so that
        root nulls accidentally moved inside other objects are still found,
        preventing duplicate root creation.
        """
        # Fast path: check cache
        if filename in self._roots:
            r = self._roots[filename]
            if r.GetDocument() == doc:
                s = self.unit_scale
                r[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
                return r

        # Recursive scan of the full document tree
        found = self._find_root_recursive(doc.GetFirstObject(), doc, filename)
        if found:
            self._roots[filename] = found
            s = self.unit_scale
            found[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
            return found

        # Not found anywhere — create new root
        display_name = f"Plasticity: {filename}" if filename else "Plasticity"
        root = c4d.BaseObject(c4d.Onull)
        root.SetName(display_name)
        s = self.unit_scale
        root[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
        rbc = root.GetDataInstance()
        rbc.SetBool(BC_PLASTICITY_ROOT, True)
        rbc.SetString(BC_PLASTICITY_FILENAME, filename)
        doc.InsertObject(root)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, root)
        self._roots[filename] = root
        return root

    def _find_root_recursive(self, obj, doc, filename):
        """
        Walk the full scene hierarchy to find a root null matching filename.
        Returns the first match or None.
        """
        while obj:
            bc = obj.GetDataInstance()
            if (obj.CheckType(c4d.Onull)
                    and bc.GetBool(BC_PLASTICITY_ROOT)
                    and bc.GetString(BC_PLASTICITY_FILENAME, "") == filename):
                return obj
            # Recurse into children
            found = self._find_root_recursive(obj.GetDown(), doc, filename)
            if found:
                return found
            obj = obj.GetNext()
        return None

    def _delete_item(self, doc, filename, obj_id):
        key = (filename, obj_id)
        obj = self._items.pop(key, None)
        if obj and obj.GetDocument() == doc:
            doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, obj)
            obj.Remove()

    @staticmethod
    def _insert_last_child(doc, obj, parent):
        """Append obj as last child of parent (InsertObject default is first child)."""
        last  = None
        child = parent.GetDown()
        while child:
            last  = child
            child = child.GetNext()
        if last:
            doc.InsertObject(obj, pred=last)
        else:
            doc.InsertObject(obj, parent=parent)

    # =========================================================================
    # Public interface for dialog
    # =========================================================================

    def update_unit_scale(self, scale: float):
        """Update unit scale on all existing root nulls immediately."""
        self.unit_scale = max(0.0001, float(scale))
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        s = self.unit_scale
        for fn, root in self._roots.items():
            if root and root.GetDocument() == doc:
                root[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
        c4d.EventAdd()

    def get_selected_plasticity_ids(self, doc):
        """Return (filename, plasticity_id) for all selected Plasticity objects."""
        ids       = []
        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)

        def collect(obj):
            bc  = obj.GetDataInstance()
            pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
            fn  = bc.GetString(BC_PLASTICITY_FILENAME, "")
            if pid != 0 and fn:
                ids.append((fn, pid))
            if obj.CheckType(c4d.Onull):
                child = obj.GetDown()
                while child:
                    collect(child)
                    child = child.GetNext()

        for obj in selection:
            collect(obj)
        return ids