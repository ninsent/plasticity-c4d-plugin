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

HIERARCHY (v2.1.0):
  Root null (per filename)
    ├── Outbox null (BC_PLASTICITY_OUTBOX) — user places SDS objects here
    └── Inbox null (BC_PLASTICITY_INBOX)  — Plasticity objects land here

  Outbox is first child, Inbox is second.  Objects in the Outbox are
  protected from incoming updates and can be uploaded to Plasticity via
  the PUT_SOME_1 protocol message.
"""

import c4d
import array
import bisect
import json
import math
import uuid
from typing import Dict, List, Optional, Any, Set, Tuple

from modules.protocol import ObjectType, MessageType
from modules.threading_bridge import ThreadingBridge, BridgeEvent, EventType
from modules.refacet_tag import TAG_PLUGIN_ID as REFACET_TAG_ID, read_tag_refacet_kwargs

# BaseContainer keys — offsets from registered plugin ID 1066929
PLUGIN_ID              = 1066929
BC_PLASTICITY_ID       = PLUGIN_ID + 1   # 1066930
BC_PLASTICITY_FILENAME = PLUGIN_ID + 2   # 1066931
BC_PLASTICITY_GROUPS   = PLUGIN_ID + 3   # 1066932
BC_PLASTICITY_FACE_IDS = PLUGIN_ID + 4   # 1066933
BC_PLASTICITY_ROOT     = PLUGIN_ID + 5   # 1066934
BC_PLASTICITY_POLY_FACE_MAP = PLUGIN_ID + 6   # 1066935
# v2.1.0 additions
BC_PLASTICITY_INBOX         = PLUGIN_ID + 7    # 1066936
BC_PLASTICITY_OUTBOX        = PLUGIN_ID + 8    # 1066937
BC_PLASTICITY_PNS_ID        = PLUGIN_ID + 9    # 1066938  (persistent string ID for outbox items)
BC_PLASTICITY_COLLECTION_ID = PLUGIN_ID + 10   # 1066939  (persistent string ID for outbox groups)
BC_PLASTICITY_VERSION       = PLUGIN_ID + 11   # 1066940
BC_PLASTICITY_GROUP_ID      = PLUGIN_ID + 12   # 1066941

MANAGED_NORMAL_TAG_NAME  = "__plasticity_normals__"
MANAGED_FACE_SEL_PREFIX  = "Plasticity Face "
MANAGED_EDGES_TAG_NAME   = "__plasticity_edges__"

# Plasticity sends vertex positions in metres; C4D's internal unit is
# centimetres.  Multiply every incoming coordinate by this factor so the
# geometry appears at the correct size *before* the user's unit_scale is
# applied on the root null.
_IMPORT_SCALE = 100.0

# PutSome options bitfield constants
_KIND_MESH = 0
_KIND_SUBD = 1


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


# =============================================================================
# Shared utility functions for reading Plasticity metadata from objects
# =============================================================================

def _get_poly_face_map(obj):
    """Read and validate the poly-face map from a Plasticity polygon object.

    Returns (poly_face_map, face_ids, poly_count) on success, or None if the
    object is not a valid Plasticity polygon object or has no map data.
    """
    if not obj.CheckType(c4d.Opolygon):
        return None

    bc = obj.GetDataInstance()
    if bc.GetInt32(BC_PLASTICITY_ID, 0) == 0:
        return None

    pfm_str      = bc.GetString(BC_PLASTICITY_POLY_FACE_MAP, "")
    face_ids_str = bc.GetString(BC_PLASTICITY_FACE_IDS, "")
    if not pfm_str or not face_ids_str:
        return None

    poly_face_map = json.loads(pfm_str)
    face_ids      = json.loads(face_ids_str)

    poly_count = obj.GetPolygonCount()
    if poly_count == 0 or not poly_face_map or not face_ids:
        return None

    return poly_face_map, face_ids, poly_count


def _get_poly_face_map_only(obj):
    """Like _get_poly_face_map but doesn't require face_ids."""
    if not obj.CheckType(c4d.Opolygon):
        return None

    bc = obj.GetDataInstance()
    if bc.GetInt32(BC_PLASTICITY_ID, 0) == 0:
        return None

    pfm_str = bc.GetString(BC_PLASTICITY_POLY_FACE_MAP, "")
    if not pfm_str:
        return None

    poly_face_map = json.loads(pfm_str)
    poly_count = obj.GetPolygonCount()
    if poly_count == 0 or not poly_face_map:
        return None

    return poly_face_map, poly_count


def _build_edge_map(obj, poly_face_map, poly_count):
    """Build a global edge map for a polygon object.

    Returns a dict: (min_vertex, max_vertex) → [(poly_idx, slot, group_idx), ...]
    """
    edge_map = {}
    map_len  = min(len(poly_face_map), poly_count)

    for poly_idx in range(map_len):
        group_idx = poly_face_map[poly_idx]
        cp        = obj.GetPolygon(poly_idx)
        is_tri    = (cp.c == cp.d)

        edge_slots = [
            (0, cp.a, cp.b),
            (1, cp.b, cp.c),
            (3, cp.d, cp.a),
        ]
        if not is_tri:
            edge_slots.append((2, cp.c, cp.d))

        for slot, v1, v2 in edge_slots:
            key = (min(v1, v2), max(v1, v2))
            if key not in edge_map:
                edge_map[key] = []
            edge_map[key].append((poly_idx, slot, group_idx))

    return edge_map


class SceneHandler:
    def __init__(self, bridge: ThreadingBridge):
        self.bridge = bridge
        self.unit_scale = 1.0

        self._items  = {}   # (filename, id) -> c4d.BaseObject  (mesh objects)
        self._groups = {}   # (filename, id) -> c4d.BaseObject  (null groups)
        self._roots  = {}   # filename       -> c4d.BaseObject  (root nulls)

        # Persistent set of all Plasticity IDs returned by PUT_SOME responses.
        # Prevents uploaded objects from being re-imported on subsequent refreshes.
        self._uploaded_ids = set()

        # v2.1.0: back-reference to client (set in plasticity_c4d.pyp)
        self.client = None

        bridge.register_callback(EventType.CONNECTED,           self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,        self._on_disconnected)
        bridge.register_callback(EventType.LIST_RESPONSE,       self._on_list_response)
        bridge.register_callback(EventType.TRANSACTION,         self._on_transaction)
        bridge.register_callback(EventType.REFACET_RESPONSE,    self._on_refacet_response)
        bridge.register_callback(EventType.NEW_VERSION,         self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,            self._on_new_file)
        # v2.1.0
        bridge.register_callback(EventType.HANDSHAKE_RESPONSE,  self._on_handshake)
        bridge.register_callback(EventType.PUT_SOME_RESPONSE,   self._on_put_some)

    # =========================================================================
    # Connection lifecycle
    # =========================================================================

    def _on_connected(self, event: BridgeEvent):
        self.on_connect()

    def _on_disconnected(self, event: BridgeEvent):
        self.on_disconnect()

    def on_connect(self):
        self._items.clear()
        self._groups.clear()
        self._roots.clear()
        self._uploaded_ids.clear()

    def on_disconnect(self):
        self._items.clear()
        self._groups.clear()
        self._roots.clear()

    # =========================================================================
    # Deferred N-gon processing
    # =========================================================================

    def _process_deferred_ngons(self, deferred_ngons):
        """Run MCOMMAND_MELT for each deferred N-gon entry."""
        for i in range(len(deferred_ngons)):
            obj, poly_groups, corner_normals, poly_face_val = deferred_ngons[i]
            deferred_ngons[i] = None
            self._create_ngon_groups(obj, poly_groups, corner_normals,
                                     poly_face_val)

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
            inbox = self._get_or_create_inbox(doc, filename)
            self._prepare(doc, filename)

            outbox_ids = self._get_outbox_plasticity_ids(doc, filename)

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

            deferred_ngons = self._process_objects(
                doc, filename, inbox, all_objects, outbox_ids)

            # Delete stale items (but not outbox items)
            stale = [k for k in self._items
                     if k[0] == filename
                     and k[1] not in all_item_ids
                     and k[1] not in outbox_ids]
            for k in stale:
                obj = self._items.pop(k, None)
                if obj and obj.GetDocument() == doc:
                    doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, obj)
                    obj.Remove()

            # Delete stale groups (but not outbox groups)
            stale = [k for k in self._groups
                     if k[0] == filename
                     and k[1] not in all_group_ids
                     and k[1] not in outbox_ids]
            for k in stale:
                grp = self._groups.pop(k, None)
                if grp and grp.GetDocument() == doc:
                    doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, grp)
                    grp.Remove()

        finally:
            doc.EndUndo()

        self._process_deferred_ngons(deferred_ngons)
        c4d.EventAdd()

        # v2.1.0: fire list-complete callback (triggers PutSome if supported)
        if self.client and self.client.on_list_complete:
            callback = self.client.on_list_complete
            self.client.on_list_complete = None
            callback(filename)

        # Auto-refacet objects that carry the refacet tag
        self._auto_refacet_tagged(doc, filename, all_item_ids)

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
            inbox = self._get_or_create_inbox(doc, filename)
            self._prepare(doc, filename)

            outbox_ids = self._get_outbox_plasticity_ids(doc, filename)

            for oid in data.get('delete', []):
                if oid not in outbox_ids:
                    self._delete_item(doc, filename, oid)

            all_objects = data.get('add', []) + data.get('update', [])
            if all_objects:
                deferred_ngons = self._process_objects(
                    doc, filename, inbox, all_objects, outbox_ids)

        finally:
            doc.EndUndo()

        self._process_deferred_ngons(deferred_ngons)
        c4d.EventAdd()

        # Auto-refacet objects that carry the refacet tag
        if all_objects:
            updated_ids = set()
            for obj_data in all_objects:
                ot  = int(obj_data.get('type', -1))
                oid = obj_data.get('id', 0)
                if ot in (ObjectType.SOLID, ObjectType.SHEET):
                    updated_ids.add(oid)
            self._auto_refacet_tagged(doc, filename, updated_ids)

    def _on_refacet_response(self, event: BridgeEvent):
        """Re-tessellate objects with new facet settings."""
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
            self._prepare(doc, filename)

            for item in data.get('refaceted_objects', []):
                pid  = item.get('plasticity_id')
                key  = (filename, pid)
                obj  = self._items.get(key)
                if not obj or obj.GetDocument() != doc:
                    continue

                verts    = item.get('vertices', [])
                indices  = item.get('indices',  [])
                faces    = item.get('faces',    [])
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

        self._process_deferred_ngons(deferred_ngons)
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
    # v2.1.0: Handshake & PutSome handlers
    # =========================================================================

    def _on_handshake(self, event: BridgeEvent):
        data = event.data
        if not data:
            return
        supported = data.get('supported_messages', set())
        if self.client:
            self.client.supported_messages = supported
        self.bridge.status_message = f"Handshake OK — server supports {len(supported)} message types"
        print(f"[Plasticity] Handshake complete: {supported}")

    def _on_put_some(self, event: BridgeEvent):
        """Handle PUT_SOME_1 response — stamp outbox objects with Plasticity IDs."""
        data = event.data
        if not data:
            return

        code = data.get('code', 0)
        if code != 200:
            self.bridge.status_message = f"PutSome failed with code: {code}"
            print(f"[Plasticity] PutSome failed with code: {code}")
            return

        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        group_results = data.get('group_results', [])
        item_results  = data.get('item_results', [])

        doc.StartUndo()
        try:
            # Stamp items with Plasticity IDs
            for item in item_results:
                c4d_id    = item["c4d_id"]
                stable_id = item["stable_id"]
                version_id = item["version_id"]

                obj = self._find_object_by_bc_string(
                    doc, BC_PLASTICITY_PNS_ID, c4d_id)
                if obj:
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)
                    bc = obj.GetDataInstance()
                    bc.SetInt32(BC_PLASTICITY_ID, stable_id)
                    bc.SetInt32(BC_PLASTICITY_VERSION, version_id)

            # Stamp groups with Plasticity IDs
            for group in group_results:
                collection_id = group["c4d_collection_id"]
                group_id      = group["group_id"]

                obj = self._find_object_by_bc_string(
                    doc, BC_PLASTICITY_COLLECTION_ID, collection_id)
                if obj:
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)
                    bc = obj.GetDataInstance()
                    bc.SetInt32(BC_PLASTICITY_GROUP_ID, group_id)
        finally:
            doc.EndUndo()

        self.bridge.status_message = (
            f"PutSome OK: {len(group_results)} groups, {len(item_results)} items")
        print(f"[Plasticity] PutSome succeeded: "
              f"{len(group_results)} groups, {len(item_results)} items")

        # Remember all uploaded IDs so they are filtered on subsequent refreshes
        for item in item_results:
            sid = item.get("stable_id", 0)
            if sid:
                self._uploaded_ids.add(sid)
        for group in group_results:
            gid = group.get("group_id", 0)
            if gid:
                self._uploaded_ids.add(gid)
        c4d.EventAdd()

    @staticmethod
    def _find_object_by_bc_string(doc, bc_key, value):
        """Walk the entire document tree to find an object with a matching BC string."""
        def _search(obj):
            while obj:
                bc = obj.GetDataInstance()
                if bc.GetString(bc_key, "") == value:
                    return obj
                found = _search(obj.GetDown())
                if found:
                    return found
                obj = obj.GetNext()
            return None
        return _search(doc.GetFirstObject())

    # =========================================================================
    # Auto-refacet: trigger refacet for tagged objects after geometry updates
    # =========================================================================

    def _auto_refacet_tagged(self, doc, filename, updated_item_ids):
        """Send refacet requests for updated objects that carry the auto-refacet tag.

        Called from _on_list_response and _on_transaction — NEVER from
        _on_refacet_response (to avoid infinite loops).
        """
        if not self.client or not self.client.connected:
            return
        if not updated_item_ids:
            return

        # Group objects by identical refacet settings so we can batch them
        # into as few server requests as possible.
        settings_groups = {}  # settings_key → (kwargs, [plasticity_ids])

        for obj_id in updated_item_ids:
            key = (filename, obj_id)
            obj = self._items.get(key)
            if not obj or obj.GetDocument() != doc:
                continue

            tag = obj.GetTag(REFACET_TAG_ID)
            if not tag:
                continue

            try:
                kwargs = read_tag_refacet_kwargs(tag)
            except Exception as e:
                print(f"[Plasticity] Error reading refacet tag on "
                      f"'{obj.GetName()}': {e}")
                continue

            # Build a hashable key from the settings dict
            settings_key = tuple(sorted(kwargs.items()))

            if settings_key not in settings_groups:
                settings_groups[settings_key] = (kwargs, [])
            settings_groups[settings_key][1].append(obj_id)

        # Send one refacet_some per unique settings group
        for kwargs, obj_ids in settings_groups.values():
            self.client.refacet_some(
                filename=filename, plasticity_ids=obj_ids, **kwargs)

    # =========================================================================
    # Cache management
    # =========================================================================

    def _prepare(self, doc, filename):
        """Rebuild caches by scanning the inbox — undo-safe."""
        root = self._get_or_create_root(doc, filename)
        inbox = self._get_or_create_inbox(doc, filename)
        self._get_or_create_outbox(doc, filename)  # ensure it exists

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

        scan(inbox)

        # Migration: also scan root directly for legacy scenes where objects
        # live directly under root (pre-v2.1.0 hierarchy).  Objects found
        # here will be re-parented into inbox on the next full refresh.
        child = root.GetDown()
        while child:
            bc = child.GetDataInstance()
            # Skip inbox and outbox nulls themselves
            if (not bc.GetBool(BC_PLASTICITY_INBOX)
                    and not bc.GetBool(BC_PLASTICITY_OUTBOX)):
                pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
                fn  = bc.GetString(BC_PLASTICITY_FILENAME, "")
                if pid != 0 and fn == filename:
                    if child.CheckType(c4d.Onull):
                        if (fn, pid) not in self._groups:
                            self._groups[(fn, pid)] = child
                    else:
                        if (fn, pid) not in self._items:
                            self._items[(fn, pid)] = child
            child = child.GetNext()

    # =========================================================================
    # v2.1.0: Outbox protection
    # =========================================================================

    def _get_outbox_plasticity_ids(self, doc, filename):
        """Gather all Plasticity IDs associated with outbox objects.

        Includes:
          - BC_PLASTICITY_ID on outbox items (stable_id from PUT_SOME)
          - BC_PLASTICITY_GROUP_ID on outbox nulls (group_id from PUT_SOME)
          - All IDs accumulated from prior PUT_SOME responses this session
        """
        outbox = self._get_or_create_outbox(doc, filename)
        ids = set(self._uploaded_ids)

        def gather(parent):
            child = parent.GetDown()
            while child:
                bc = child.GetDataInstance()
                pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
                if pid != 0:
                    ids.add(pid)
                gid = bc.GetInt32(BC_PLASTICITY_GROUP_ID, 0)
                if gid != 0:
                    ids.add(gid)
                gather(child)
                child = child.GetNext()

        gather(outbox)
        return ids

    # =========================================================================
    # Two-pass object processing
    # =========================================================================

    def _process_objects(self, doc, filename, inbox, objects, outbox_ids=None):
        """
        Pass 1: Geometry — create new objects or update existing in-place.
        Pass 2: Hierarchy — re-parent, apply visibility.

        N-gon melts are deferred until after all insertions in Pass 1.
        Objects whose ID is in outbox_ids are skipped entirely.
        """
        if outbox_ids is None:
            outbox_ids = set()

        deferred_ngons = []

        # ── Pass 1: Geometry ──────────────────────────────────────────────────
        for item in objects:
            obj_type = int(item.get('type', -1))
            obj_id   = item.get('id', 0)
            name     = item.get('name', f"Object_{obj_id}")

            if obj_id in outbox_ids:
                continue

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
                    self._insert_last_child(doc, grp, inbox)
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
                    doc.AddUndo(c4d.UNDOTYPE_CHANGE, existing)
                    existing.SetName(name)
                    pg, cn, pfv, pfm = self._update_object_geometry(
                        existing, verts, indices, [], normals, groups)
                    self._copy_plasticity_meta(
                        existing, obj_id, filename, groups, face_ids,
                        poly_face_map=pfm if not pg else None)
                    if pg:
                        deferred_ngons.append((existing, pg, cn, pfv))

                else:
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
                        pfm = None

                    phong = new_obj.MakeTag(c4d.Tphong)
                    phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)
                    phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(poly_groups)
                    new_obj.Message(c4d.MSG_UPDATE)

                    self._copy_plasticity_meta(
                        new_obj, obj_id, filename, groups, face_ids,
                        poly_face_map=pfm)
                    self._insert_last_child(doc, new_obj, inbox)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, new_obj)
                    self._items[key] = new_obj

                    if poly_groups:
                        deferred_ngons.append((new_obj, poly_groups, cn, pfv))

        # ── Pass 2: Re-parent and apply visibility ────────────────────────────
        for item in objects:
            obj_type  = int(item.get('type', -1))
            obj_id    = item.get('id', 0)
            parent_id = item.get('parent_id', 0)
            flags     = item.get('flags', 6)

            if obj_id == 0 or obj_id in outbox_ids:
                continue

            should_hide = bool(flags & 1) or not bool(flags & 2)
            vis = c4d.OBJECT_OFF if should_hide else c4d.OBJECT_UNDEF

            if obj_type == ObjectType.GROUP:
                key = (filename, obj_id)
                grp = self._groups.get(key)
                if not grp:
                    continue
                target_parent = inbox
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
                target_parent = inbox
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
        """Route to the correct geometry builder."""
        if faces:
            return self._compute_ngon_geometry(vertices, indices, faces, normals, groups)
        else:
            return self._compute_tri_geometry(vertices, indices, normals, groups)

    def _compute_tri_geometry(self, vertices, indices, normals, groups):
        """Build pure-triangle geometry for standard ADD/UPDATE objects."""
        vert_count = len(vertices) // 3
        tri_count  = len(indices)  // 3

        points = []
        s = _IMPORT_SCALE
        for i in range(vert_count):
            points.append(c4d.Vector(
                vertices[i * 3]     * s,
                vertices[i * 3 + 2] * s,
                vertices[i * 3 + 1] * s,
            ))

        polys      = []
        normal_map = []
        for i in range(tri_count):
            a  = indices[i * 3]
            b  = indices[i * 3 + 1]
            c_ = indices[i * 3 + 2]
            polys.append(c4d.CPolygon(a, c_, b))
            normal_map.append((a, c_, b, b))

        return points, polys, normal_map, [], {}, []

    def _compute_ngon_geometry(self, vertices, indices, faces, normals, groups):
        """Build geometry for N-gon refacet data."""
        vert_count = len(vertices) // 3

        group_starts = []
        for gi in range(len(groups) // 2):
            group_starts.append((groups[gi * 2], gi))
        group_starts.sort(key=lambda x: x[0])
        gs_keys = [gs[0] for gs in group_starts]

        def _face_val_to_group_idx(loop_pos):
            idx = bisect.bisect_right(gs_keys, loop_pos) - 1
            if idx >= 0:
                return group_starts[idx][1]
            return 0

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
                unique_pts.append(c4d.Vector(px * s, pz * s, py * s))
            old_to_new[i] = vert_map[key]

        new_indices = [old_to_new[idx] for idx in indices]

        polys         = []
        normal_map    = []
        poly_groups   = []
        poly_face_val = []
        poly_idx      = 0

        corner_normals = {}
        has_normals    = len(normals) >= 3

        def _ntuple(orig_vi):
            base = orig_vi * 3
            if base + 2 < len(normals):
                return (
                    normals[base],
                    normals[base + 2],
                    normals[base + 1],
                )
            return (0.0, 1.0, 0.0)

        poly_starts = [0]
        for i in range(1, len(faces)):
            if faces[i] != faces[i - 1]:
                poly_starts.append(i)
        poly_starts.append(len(faces))

        for p in range(len(poly_starts) - 1):
            start = poly_starts[p]
            end   = poly_starts[p + 1]

            raw_face_vi = new_indices[start:end]
            raw_orig_vi = indices[start:end]
            group_idx = _face_val_to_group_idx(start)

            if has_normals:
                for k in range(len(raw_face_vi)):
                    welded_vi = raw_face_vi[k]
                    cn_key = (welded_vi, group_idx)
                    if cn_key not in corner_normals:
                        corner_normals[cn_key] = _ntuple(raw_orig_vi[k])

            face_vindices = []
            orig_vindices = []
            for k in range(len(raw_face_vi)):
                if k == 0 or raw_face_vi[k] != raw_face_vi[k - 1]:
                    face_vindices.append(raw_face_vi[k])
                    orig_vindices.append(raw_orig_vi[k])
            if len(face_vindices) > 1 and face_vindices[-1] == face_vindices[0]:
                face_vindices.pop()
                orig_vindices.pop()

            count = len(face_vindices)
            if count < 3:
                continue

            group_poly_indices = []

            if count == 3:
                ia, ib, ic = face_vindices
                polys.append(c4d.CPolygon(ia, ic, ib))
                normal_map.append((
                    orig_vindices[0], orig_vindices[2],
                    orig_vindices[1], orig_vindices[1],
                ))
                group_poly_indices.append(poly_idx)
                poly_face_val.append(group_idx)
                poly_idx += 1

            elif count == 4:
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
                face_pts_3d = [unique_pts[vi] for vi in face_vindices]
                face_normal = _compute_polygon_normal(face_pts_3d)
                pts_2d = _project_to_2d(face_pts_3d, face_normal)
                ear_tris = _ear_clip(pts_2d)

                if not ear_tris:
                    print(f"[Plasticity] Ear-clip failed for {count}-gon, "
                          f"falling back to fan triangulation")
                    ear_tris = [(0, t + 1, t + 2) for t in range(count - 2)]

                for li_a, li_b, li_c in ear_tris:
                    ia = face_vindices[li_a]
                    ib = face_vindices[li_b]
                    ic = face_vindices[li_c]

                    polys.append(c4d.CPolygon(ia, ic, ib))
                    normal_map.append((
                        orig_vindices[li_a], orig_vindices[li_c],
                        orig_vindices[li_b], orig_vindices[li_b],
                    ))
                    group_poly_indices.append(poly_idx)
                    poly_face_val.append(group_idx)
                    poly_idx += 1

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
        """Replace all geometry on an existing PolygonObject in-place."""
        points, polys, normal_map, poly_groups, corner_normals, poly_face_val = \
            self._compute_geometry(vertices, indices, faces, normals, groups)
        if not polys:
            return [], {}, [], None

        self._strip_managed_tags(obj)
        obj.ResizeObject(len(points), len(polys))
        self._write_points_and_polys(obj, points, polys)

        poly_face_map = None
        if not poly_groups:
            if normals and normal_map:
                self._apply_normals(obj, normals, normal_map)
            poly_face_map = self._compute_tri_face_map(len(polys), groups)

        phong = obj.GetTag(c4d.Tphong)
        if not phong:
            phong = obj.MakeTag(c4d.Tphong)
            phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)
        phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(poly_groups)

        obj.Message(c4d.MSG_UPDATE)
        return poly_groups, corner_normals, poly_face_val, poly_face_map

    def _strip_managed_tags(self, obj):
        tag = obj.GetFirstTag()
        while tag:
            next_tag = tag.GetNext()
            if tag.CheckType(c4d.Tnormal) and tag.GetName() == MANAGED_NORMAL_TAG_NAME:
                tag.Remove()
            elif (tag.CheckType(c4d.Tpolygonselection)
                  and tag.GetName().startswith(MANAGED_FACE_SEL_PREFIX)):
                tag.Remove()
            elif (tag.CheckType(c4d.Tedgeselection)
                  and tag.GetName() == MANAGED_EDGES_TAG_NAME):
                tag.Remove()
            tag = next_tag

    # =========================================================================
    # Custom normals
    # =========================================================================

    def _apply_normals(self, obj, normals, normal_map):
        """Write per-corner normals (tri mode)."""
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
                    normals[v_id * 3 + 2],
                    normals[v_id * 3 + 1],
                )
            return c4d.Vector(0.0, 1.0, 0.0)

        try:
            data_w = tag.GetDataAddressW()
            for i in range(poly_count):
                ids = normal_map[i] if i < len(normal_map) else (0, 0, 0, 0)
                c4d.NormalTag.SetPolygon(data_w, i,
                                         {c: _nvec(v) for c, v in zip('abcd', ids)})
            return
        except (AttributeError, TypeError):
            pass

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
        """Write per-corner normals AFTER N-gon melt."""
        poly_count = obj.GetPolygonCount()
        if poly_count == 0 or not corner_normals:
            return

        fallback_t = (0.0, 1.0, 0.0)

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
        """Build the poly_face_map for tri-mode geometry."""
        if not groups or poly_count == 0:
            return [0] * poly_count

        n_groups = len(groups) // 2
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
        """Merge groups of adjacent triangles into C4D N-gons via MCOMMAND_MELT."""
        groups_to_melt = [g for g in poly_groups if len(g) >= 2]
        if not groups_to_melt:
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

        all_polys = obj.GetAllPolygons()
        polygon_identity = {
            i: (cp.a, cp.b, cp.c, cp.d)
            for i, cp in enumerate(all_polys)
        }

        identity_to_group = {}
        for orig_pid, id_key in polygon_identity.items():
            if orig_pid < len(poly_face_val):
                identity_to_group[id_key] = poly_face_val[orig_pid]

        group_edges = []
        for group in groups_to_melt:
            edges = set()
            for pid in group:
                identity = polygon_identity.get(pid)
                if identity is None:
                    continue
                a, b, c, d = identity
                if c == d:
                    verts = (a, b, c)
                else:
                    verts = (a, b, c, d)
                n = len(verts)
                for i in range(n):
                    v1, v2 = verts[i], verts[(i + 1) % n]
                    edges.add((v1, v2) if v1 < v2 else (v2, v1))
            group_edges.append(edges)

        n_groups = len(groups_to_melt)
        adjacent = [set() for _ in range(n_groups)]

        edge_to_group = {}
        for gi, edges in enumerate(group_edges):
            for edge in edges:
                prev_gi = edge_to_group.get(edge)
                if prev_gi is not None and prev_gi != gi:
                    adjacent[gi].add(prev_gi)
                    adjacent[prev_gi].add(gi)
                else:
                    edge_to_group[edge] = gi

        colors = [-1] * n_groups
        batches = []

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

        for batch in batches:
            if not batch:
                continue

            inverted = {
                (cp.a, cp.b, cp.c, cp.d): i
                for i, cp in enumerate(obj.GetAllPolygons())
            }

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

        post_polys = obj.GetAllPolygons()
        post_melt_face_map = []

        vert_groups = {}
        for (vi, gi) in corner_normals:
            if vi not in vert_groups:
                vert_groups[vi] = set()
            vert_groups[vi].add(gi)

        for cp in post_polys:
            id_key = (cp.a, cp.b, cp.c, cp.d)

            gi = identity_to_group.get(id_key)
            if gi is not None:
                post_melt_face_map.append(gi)
                continue

            is_tri = (cp.c == cp.d)
            corner_verts = [cp.a, cp.b, cp.c]
            if not is_tri:
                corner_verts.append(cp.d)

            common = vert_groups.get(corner_verts[0], set()).copy()
            for cv in corner_verts[1:]:
                common &= vert_groups.get(cv, set())
                if len(common) <= 1:
                    break

            if len(common) == 1:
                post_melt_face_map.append(next(iter(common)))
            elif len(common) > 1:
                best_gi = next(iter(common))
                post_melt_face_map.append(best_gi)
            else:
                post_melt_face_map.append(0)

        if corner_normals:
            self._apply_normals_from_corner_map(
                obj, corner_normals, post_melt_face_map)
            phong = obj.GetTag(c4d.Tphong)
            if phong:
                phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = False

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
        bc = obj.GetDataInstance()
        bc.SetString(BC_PLASTICITY_POLY_FACE_MAP, json.dumps(poly_face_map))

    # =========================================================================
    # Scene-tree helpers
    # =========================================================================

    def _get_or_create_root(self, doc, filename):
        """Get or create the root Null for a Plasticity filename."""
        if filename in self._roots:
            r = self._roots[filename]
            if r.GetDocument() == doc:
                s = self.unit_scale
                r[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
                return r

        found = self._find_root_recursive(doc.GetFirstObject(), doc, filename)
        if found:
            self._roots[filename] = found
            s = self.unit_scale
            found[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
            return found

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

        # v2.1.0: create Outbox (first) and Inbox (second) under new root
        self._create_inbox_outbox(doc, root)

        return root

    def _create_inbox_outbox(self, doc, root):
        """Create Outbox and Inbox nulls under root (order: Outbox first, Inbox second)."""
        outbox = c4d.BaseObject(c4d.Onull)
        outbox.SetName("Outbox")
        outbox.GetDataInstance().SetBool(BC_PLASTICITY_OUTBOX, True)
        self._insert_last_child(doc, outbox, root)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, outbox)

        inbox = c4d.BaseObject(c4d.Onull)
        inbox.SetName("Inbox")
        inbox.GetDataInstance().SetBool(BC_PLASTICITY_INBOX, True)
        self._insert_last_child(doc, inbox, root)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, inbox)

    def _get_or_create_inbox(self, doc, filename):
        """Find or create the Inbox null under the root for this filename."""
        root = self._get_or_create_root(doc, filename)
        child = root.GetDown()
        while child:
            if child.GetDataInstance().GetBool(BC_PLASTICITY_INBOX):
                return child
            child = child.GetNext()

        # Inbox not found — create both inbox and outbox
        # (handles migration from pre-v2.1.0 scenes)
        self._create_inbox_outbox(doc, root)
        child = root.GetDown()
        while child:
            if child.GetDataInstance().GetBool(BC_PLASTICITY_INBOX):
                return child
            child = child.GetNext()

        # Should not reach here, but be safe
        inbox = c4d.BaseObject(c4d.Onull)
        inbox.SetName("Inbox")
        inbox.GetDataInstance().SetBool(BC_PLASTICITY_INBOX, True)
        self._insert_last_child(doc, inbox, root)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, inbox)
        return inbox

    def _get_or_create_outbox(self, doc, filename):
        """Find or create the Outbox null under the root for this filename."""
        root = self._get_or_create_root(doc, filename)
        child = root.GetDown()
        while child:
            if child.GetDataInstance().GetBool(BC_PLASTICITY_OUTBOX):
                return child
            child = child.GetNext()

        # Outbox not found — create both
        self._create_inbox_outbox(doc, root)
        child = root.GetDown()
        while child:
            if child.GetDataInstance().GetBool(BC_PLASTICITY_OUTBOX):
                return child
            child = child.GetNext()

        outbox = c4d.BaseObject(c4d.Onull)
        outbox.SetName("Outbox")
        outbox.GetDataInstance().SetBool(BC_PLASTICITY_OUTBOX, True)
        doc.InsertObject(outbox, parent=root)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, outbox)
        return outbox

    def _find_root_recursive(self, obj, doc, filename):
        """Walk the full scene hierarchy to find a root null matching filename."""
        while obj:
            bc = obj.GetDataInstance()
            if (obj.CheckType(c4d.Onull)
                    and bc.GetBool(BC_PLASTICITY_ROOT)
                    and bc.GetString(BC_PLASTICITY_FILENAME, "") == filename):
                return obj
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
        """Append obj as last child of parent."""
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
    # v2.1.0: Outbox data gathering for PutSome
    # =========================================================================

    def get_outbox_data(self, filename, only_visible=False):
        """Walk the Outbox and gather SDS mesh data for PUT_SOME_1.

        In Cinema 4D a Subdivision Surface (c4d.Osds) is a generator object.
        Its children form the control cage — which can be a single polygon
        object, parametric primitives (Cube, Sphere, Torus …), or any nesting
        of generators (Symmetry, Connect, Boole, Array …).

        Only SDS objects are eligible for upload; plain polygon objects placed
        directly in the Outbox are skipped with a warning.

        The control cage is resolved via a two-tier approach:
          Fast path  — single Opolygon child → use directly (zero overhead).
          Generator  — anything else → clone children, evaluate with
                       MCOMMAND_CURRENTSTATETOOBJECT in a temp document,
                       JOIN if multiple polygon results.

        Null objects (c4d.Onull) in the outbox are treated as groups.

        Returns {"groups": [...], "items": [...]}.
        """
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"groups": [], "items": []}

        outbox = self._get_or_create_outbox(doc, filename)

        groups = []
        items  = []

        def gather(parent, parent_collection_id=""):
            child = parent.GetDown()
            while child:
                if only_visible and not child.GetEditorMode() == c4d.MODE_ON:
                    # Skip hidden objects — but only when truly hidden, not
                    # when in default mode.  C4D's GetEditorMode returns
                    # MODE_UNDEF (use parent), MODE_ON (visible),
                    # MODE_OFF (hidden).
                    if child.GetEditorMode() == c4d.MODE_OFF:
                        child = child.GetNext()
                        continue

                bc = child.GetDataInstance()

                if child.CheckType(c4d.Onull):
                    # Null → treat as group/collection
                    coll_id = bc.GetString(BC_PLASTICITY_COLLECTION_ID, "")
                    if not coll_id:
                        coll_id = uuid.uuid4().hex
                        bc.SetString(BC_PLASTICITY_COLLECTION_ID, coll_id)

                    if child != outbox:
                        groups.append({
                            "c4d_collection_id": coll_id,
                            "name": child.GetName(),
                            "parent_c4d_collection_id": parent_collection_id,
                            "existing_group_id": bc.GetInt32(
                                BC_PLASTICITY_GROUP_ID, 0),
                        })

                    current_parent_id = coll_id
                    gather(child, current_parent_id)

                elif child.CheckType(c4d.Osds):
                    # Subdivision Surface → resolve control cage
                    poly_obj, is_temp = self._resolve_control_cage(child)
                    if not poly_obj:
                        print(f"[Plasticity] Warning: SDS '{child.GetName()}' "
                              f"could not be resolved to a polygon mesh "
                              f"— skipping")
                        child = child.GetNext()
                        continue

                    item_data = self._extract_sds_item(
                        child, poly_obj, parent_collection_id, is_temp)
                    if item_data:
                        items.append(item_data)

                elif child.CheckType(c4d.Opolygon):
                    print(f"[Plasticity] Warning: Object '{child.GetName()}' "
                          f"is not under a Subdivision Surface — skipping. "
                          f"Only SDS objects can be uploaded to Plasticity.")

                child = child.GetNext()

        gather(outbox)
        return {"groups": groups, "items": items}

    # =========================================================================
    # Control-cage resolution (fast path + CSTO generator path)
    # =========================================================================

    def _resolve_control_cage(self, sds_obj):
        """Resolve an SDS's children into a single PolygonObject.

        Fast path:
            If the SDS has exactly one direct child and that child is already
            an Opolygon, return it directly.  Zero overhead.

        Generator path (two tiers):
            PRIMARY — read the polygon caches that Cinema 4D has already
            computed for the real-document objects.  Parametric primitives
            (Cube, Sphere, Tube …) and generators (Symmetry, Connect, Boole,
            Array …) all populate their caches as part of normal scene
            evaluation.  Reading these is instant and requires no temp
            document.

            FALLBACK — if no caches are found (e.g. freshly created objects
            whose caches have not been built yet), clone children into a
            temporary BaseDocument, force-build caches with ExecutePasses,
            then run MCOMMAND_CURRENTSTATETOOBJECT.

            If either tier produces multiple polygon objects they are joined
            with MCOMMAND_JOIN.

        Returns:
            (poly_obj, is_temp)

            poly_obj : c4d.PolygonObject — the resolved control cage.
            is_temp  : bool — True if poly_obj lives in a temporary document
                       (caller must NOT store persistent references to it).

            Returns (None, False) on failure.
        """
        first_child = sds_obj.GetDown()
        if first_child is None:
            return None, False

        # ── Fast path ────────────────────────────────────────────────────
        has_sibling = first_child.GetNext() is not None
        if first_child.CheckType(c4d.Opolygon) and not has_sibling:
            return first_child, False

        # ── Primary: cache-based resolution ──────────────────────────────
        # C4D has already computed polygon caches for all generators and
        # parametrics in the real document.  Walk each child and harvest
        # its resolved polygon data — no temp document required.
        poly_sources = []
        child = sds_obj.GetDown()
        while child:
            self._collect_polygon_caches(child, poly_sources)
            child = child.GetNext()

        if poly_sources:
            return self._combine_polygon_sources(poly_sources)

        # ── Fallback: CSTO in temp document ──────────────────────────────
        return self._csto_resolve(sds_obj)

    def _collect_polygon_caches(self, obj, result):
        """Recursively collect polygon data from an object's cache hierarchy.

        Deformers (Bend, Twist, FFD …) have no cache and are safely skipped.
        GetDeformCache() is checked first so that deformer effects are included.
        """
        # Object is already a polygon — use it directly.
        if obj.CheckType(c4d.Opolygon) and obj.GetPointCount() > 0:
            result.append(obj)
            return

        # Deform cache takes priority (includes deformer effects).
        cache = obj.GetDeformCache()
        if cache is None:
            cache = obj.GetCache()

        if cache is not None:
            # The cache itself may be a polygon or a hierarchy (e.g. a Null
            # with polygon children for generators like Symmetry).
            self._collect_polygon_caches(cache, result)
            # Some generators produce sibling caches — walk those too.
            sibling = cache.GetNext()
            while sibling:
                self._collect_polygon_caches(sibling, result)
                sibling = sibling.GetNext()

    def _combine_polygon_sources(self, poly_sources):
        """Clone polygon sources and optionally JOIN them into one mesh.

        Returns (poly_obj, is_temp=True) or (None, False) on failure.
        """
        temp_doc = c4d.documents.BaseDocument()
        clones = []
        for src in poly_sources:
            clone = src.GetClone(c4d.COPYFLAGS_NONE)
            if clone and clone.CheckType(c4d.Opolygon) and clone.GetPointCount() > 0:
                temp_doc.InsertObject(clone)
                clones.append(clone)

        if not clones:
            return None, False

        if len(clones) == 1:
            return clones[0], True

        # Multiple polygon objects → join into a single mesh.
        join_ok = c4d.utils.SendModelingCommand(
            command=c4d.MCOMMAND_JOIN,
            list=clones,
            mode=c4d.MODELINGCOMMANDMODE_ALL,
            bc=c4d.BaseContainer(),
            doc=temp_doc,
        )

        if join_ok and clones:
            joined = clones[0]
            if joined.CheckType(c4d.Opolygon) and joined.GetPointCount() > 0:
                return joined, True

        # Fallback: return the largest clone by vertex count.
        clones.sort(key=lambda o: o.GetPointCount(), reverse=True)
        return clones[0], True

    def _csto_resolve(self, sds_obj):
        """Fallback: resolve SDS children via CSTO in a temporary document.

        Used when the primary cache-based approach finds no polygon data
        (e.g. objects whose caches have not been built yet).

        Returns (poly_obj, is_temp=True) or (None, False) on failure.
        """
        temp_doc = c4d.documents.BaseDocument()

        # Collect and clone the SDS's direct children (preserving order).
        children = []
        child = sds_obj.GetDown()
        while child:
            children.append(child)
            child = child.GetNext()

        if not children:
            return None, False

        # Build the target for CSTO.
        # Single child → clone it directly.
        # Multiple children → wrap clones in a Connect Object so CSTO merges
        # them into a single mesh (exactly how the real SDS would see them).
        if len(children) == 1:
            target = children[0].GetClone(c4d.COPYFLAGS_NONE)
            temp_doc.InsertObject(target)
        else:
            target = c4d.BaseObject(c4d.Oconnector)
            temp_doc.InsertObject(target)
            prev_clone = None
            for orig in children:
                clone = orig.GetClone(c4d.COPYFLAGS_NONE)
                if prev_clone is None:
                    clone.InsertUnder(target)
                else:
                    clone.InsertAfter(prev_clone)
                prev_clone = clone

        # Force cache evaluation — parametric objects and generators only
        # build their polygon caches when the document executes its passes.
        # Without this, CSTO has no geometry to convert.
        temp_doc.ExecutePasses(None, True, True, True, 0)

        # Run Current State to Object.
        csto_list = [target]
        csto_result = c4d.utils.SendModelingCommand(
            command=c4d.MCOMMAND_CURRENTSTATETOOBJECT,
            list=csto_list,
            mode=c4d.MODELINGCOMMANDMODE_ALL,
            bc=c4d.BaseContainer(),
            doc=temp_doc,
        )

        if csto_result is False:
            print(f"[Plasticity] CSTO failed for SDS '{sds_obj.GetName()}'")
            return None, False

        # Collect all polygon objects from every possible result location.
        # Depending on the C4D version, CSTO may:
        #   (a) return the BaseObject directly as the function result,
        #   (b) replace the element in the input list, or
        #   (c) insert the result into the temp document.
        poly_objects = []

        def _collect_polys(obj):
            while obj:
                if (isinstance(obj, c4d.BaseObject)
                        and obj.CheckType(c4d.Opolygon)
                        and obj.GetPointCount() > 0):
                    poly_objects.append(obj)
                if isinstance(obj, c4d.BaseObject):
                    _collect_polys(obj.GetDown())
                    obj = obj.GetNext()
                else:
                    break

        # (a) Direct BaseObject return
        if isinstance(csto_result, c4d.BaseObject):
            if csto_result.CheckType(c4d.Opolygon) and csto_result.GetPointCount() > 0:
                poly_objects.append(csto_result)
            else:
                _collect_polys(csto_result.GetDown())

        # (b) Modified input list
        if not poly_objects:
            for item in csto_list:
                if not isinstance(item, c4d.BaseObject):
                    continue
                if item.CheckType(c4d.Opolygon) and item.GetPointCount() > 0:
                    poly_objects.append(item)
                else:
                    _collect_polys(item.GetDown())

        # (c) Temp document contents
        if not poly_objects:
            _collect_polys(temp_doc.GetFirstObject())

        if not poly_objects:
            print(f"[Plasticity] CSTO produced no polygon data for "
                  f"SDS '{sds_obj.GetName()}'")
            return None, False

        if len(poly_objects) == 1:
            return poly_objects[0], True

        # Multiple polygon results → join into a single mesh.
        for po in poly_objects:
            if po.GetDocument() is None:
                temp_doc.InsertObject(po)

        join_ok = c4d.utils.SendModelingCommand(
            command=c4d.MCOMMAND_JOIN,
            list=poly_objects,
            mode=c4d.MODELINGCOMMANDMODE_ALL,
            bc=c4d.BaseContainer(),
            doc=temp_doc,
        )

        if join_ok and poly_objects:
            joined = poly_objects[0]
            if joined.CheckType(c4d.Opolygon) and joined.GetPointCount() > 0:
                return joined, True

        poly_objects.sort(key=lambda o: o.GetPointCount(), reverse=True)
        return poly_objects[0], True

    # =========================================================================
    # SDS item extraction
    # =========================================================================

    def _extract_sds_item(self, sds_obj, poly_obj, parent_collection_id,
                          is_temp=False):
        """Extract mesh data from a resolved control cage polygon object.

        Args:
            sds_obj              : the Subdivision Surface object (in the real doc)
            poly_obj             : the resolved polygon control cage
            parent_collection_id : parent group's collection ID
            is_temp              : True if poly_obj is a temporary CSTO result

        Returns a dict ready for encode_put_some(), or None on failure.
        """
        point_count = poly_obj.GetPointCount()
        poly_count  = poly_obj.GetPolygonCount()
        if point_count == 0 or poly_count == 0:
            return None

        # Persistent ID — always stored on the SDS so it survives even when
        # the control cage is rebuilt from generators on every export.
        sds_bc = sds_obj.GetDataInstance()
        pns_id = sds_bc.GetString(BC_PLASTICITY_PNS_ID, "")
        if not pns_id:
            pns_id = uuid.uuid4().hex
            sds_bc.SetString(BC_PLASTICITY_PNS_ID, pns_id)

        # World-space positions with coordinate reverse-transform.
        # C4D → Plasticity:  Pl X = C4D X,  Pl Y = C4D Z,  Pl Z = C4D Y
        #
        # Transform selection:
        #   Fast path  (is_temp=False):
        #       poly_obj is a real child of the SDS in the document.
        #       GetMg() naturally includes the entire parent chain.
        #
        #   Generator path  (is_temp=True):
        #       poly_obj lives in a temp document at the root level.
        #       Its vertex positions already include all child-local
        #       transforms (baked by CSTO), but NOT the SDS's world
        #       transform.  Apply the SDS's world matrix manually.
        if is_temp:
            mg = sds_obj.GetMg()
        else:
            mg = poly_obj.GetMg()

        scale_inv = 1.0 / (_IMPORT_SCALE * self.unit_scale)

        positions = []
        for i in range(point_count):
            local_pt = poly_obj.GetPoint(i)
            world_pt = mg * local_pt
            positions.append(world_pt.x * scale_inv)   # Pl X = C4D X
            positions.append(world_pt.z * scale_inv)   # Pl Y = C4D Z
            positions.append(world_pt.y * scale_inv)   # Pl Z = C4D Y

        # Polygon extraction — reverse the winding back to Plasticity convention.
        # Import does CPolygon(a, c, b) for tris and CPolygon(a, d, c, b) for quads.
        # Export reverses: tri (a,c,b)→(a,b,c), quad (a,d,c,b)→(a,b,c,d).
        indices = []
        sizes   = []
        for i in range(poly_count):
            cp = poly_obj.GetPolygon(i)
            is_tri = (cp.c == cp.d)
            if is_tri:
                indices.extend([cp.a, cp.c, cp.b])
                sizes.append(3)
            else:
                indices.extend([cp.a, cp.d, cp.c, cp.b])
                sizes.append(4)

        # Options bitfield
        options = _KIND_SUBD

        # User data for Plasticity-specific SDS options.
        # These can be added as User Data booleans on the SDS object:
        #   "pns_rounded_corners"       (default False)
        #   "pns_merge_patches"         (default True)
        #   "pns_interpolate_boundary"  (default False)
        rounded_corners = self._get_user_data_bool(
            sds_obj, "pns_rounded_corners", False)
        merge_patches = self._get_user_data_bool(
            sds_obj, "pns_merge_patches", True)
        interpolate_boundary = self._get_user_data_bool(
            sds_obj, "pns_interpolate_boundary", False)

        if rounded_corners:
            options |= (1 << 8)
        if merge_patches:
            options |= (1 << 9)
        if interpolate_boundary:
            options |= (1 << 10)

        return {
            "c4d_id": pns_id,
            "name": sds_obj.GetName(),
            "parent_c4d_collection_id": parent_collection_id,
            "existing_stable_id": sds_bc.GetInt32(BC_PLASTICITY_ID, 0),
            "options": options,
            "positions": positions,
            "indices": indices,
            "sizes": sizes,
        }

    @staticmethod
    def _get_user_data_bool(obj, name, default):
        """Read a boolean User Data field by name. Returns default if not found."""
        ud = obj.GetUserDataContainer()
        if ud:
            for desc_id, bc in ud:
                if bc.GetString(c4d.DESC_NAME) == name:
                    return obj[desc_id]
        return default

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

    # =========================================================================
    # Utilities
    # =========================================================================

    def apply_face_selection_tags(self, doc):
        """Create one PolygonSelectionTag per Plasticity face group on selected objects."""
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected  = 0

        doc.StartUndo()
        try:
            for obj in selection:
                if self._apply_face_sel_tags_to_obj(doc, obj):
                    affected += 1
        finally:
            doc.EndUndo()

        c4d.EventAdd()
        return affected > 0

    def _apply_face_sel_tags_to_obj(self, doc, obj):
        result = _get_poly_face_map(obj)
        if result is None:
            return False
        poly_face_map, face_ids, poly_count = result

        tag = obj.GetFirstTag()
        while tag:
            next_t = tag.GetNext()
            if (tag.CheckType(c4d.Tpolygonselection)
                    and tag.GetName().startswith(MANAGED_FACE_SEL_PREFIX)):
                doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, tag)
                tag.Remove()
            tag = next_t

        doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)

        n_groups  = len(face_ids)
        tag_sels  = []
        for group_idx in range(n_groups):
            sel_tag = obj.MakeTag(c4d.Tpolygonselection)
            sel_tag.SetName(f"{MANAGED_FACE_SEL_PREFIX}{group_idx}")
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, sel_tag)
            tag_sels.append(sel_tag.GetBaseSelect())

        map_len = min(len(poly_face_map), poly_count)
        for poly_idx in range(map_len):
            group_idx = poly_face_map[poly_idx]
            if 0 <= group_idx < n_groups:
                tag_sels[group_idx].Select(poly_idx)

        obj.Message(c4d.MSG_UPDATE)
        return True

    def apply_edge_selection_tags(self, doc):
        """Create a single EdgeSelectionTag on selected objects."""
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected  = 0

        doc.StartUndo()
        try:
            for obj in selection:
                if self._apply_edge_sel_tags_to_obj(doc, obj):
                    affected += 1
        finally:
            doc.EndUndo()

        c4d.EventAdd()
        return affected > 0

    def _apply_edge_sel_tags_to_obj(self, doc, obj):
        result = _get_poly_face_map(obj)
        if result is None:
            return False
        poly_face_map, face_ids, poly_count = result

        tag = obj.GetFirstTag()
        while tag:
            next_t = tag.GetNext()
            if (tag.CheckType(c4d.Tedgeselection)
                    and tag.GetName() == MANAGED_EDGES_TAG_NAME):
                doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, tag)
                tag.Remove()
            tag = next_t

        doc.AddUndo(c4d.UNDOTYPE_CHANGE, obj)

        edge_map = _build_edge_map(obj, poly_face_map, poly_count)

        sel_tag = obj.MakeTag(c4d.Tedgeselection)
        sel_tag.SetName(MANAGED_EDGES_TAG_NAME)
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, sel_tag)
        bs = sel_tag.GetBaseSelect()

        for occurrences in edge_map.values():
            if len(occurrences) == 1:
                poly_idx, slot, _ = occurrences[0]
                bs.Select(poly_idx * 4 + slot)
            elif len(occurrences) == 2:
                _, _, g0 = occurrences[0]
                _, _, g1 = occurrences[1]
                if g0 != g1:
                    poly_idx, slot, _ = occurrences[0]
                    bs.Select(poly_idx * 4 + slot)

        obj.Message(c4d.MSG_UPDATE)
        return True

    # -------------------------------------------------------------------------
    # Shared sharp-edge helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _poly_geometric_normal(obj, cp):
        a = obj.GetPoint(cp.a)
        b = obj.GetPoint(cp.b)
        c = obj.GetPoint(cp.c)
        n = (b - a).Cross(c - a)
        length = n.GetLength()
        return n / length if length > 1e-12 else c4d.Vector(0.0, 1.0, 0.0)

    @staticmethod
    def _get_sharp_edge_addresses(obj, angle_deg=0.0):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return []
        poly_face_map, poly_count = result

        edge_map = _build_edge_map(obj, poly_face_map, poly_count)

        use_angle_filter = angle_deg > 0.0
        if use_angle_filter:
            poly_normals = [
                SceneHandler._poly_geometric_normal(obj, obj.GetPolygon(pi))
                for pi in range(poly_count)
            ]
            cos_threshold = math.cos(math.radians(angle_deg))

        addresses = []
        for occurrences in edge_map.values():
            if len(occurrences) == 1:
                poly_idx, slot, _ = occurrences[0]
                addresses.append((poly_idx, slot))

            elif len(occurrences) == 2:
                _, _, g0 = occurrences[0]
                _, _, g1 = occurrences[1]
                if g0 == g1:
                    continue

                if use_angle_filter:
                    pi0 = occurrences[0][0]
                    pi1 = occurrences[1][0]
                    if poly_normals[pi0].Dot(poly_normals[pi1]) >= cos_threshold:
                        continue

                poly_idx, slot, _ = occurrences[0]
                addresses.append((poly_idx, slot))

        return addresses

    # -------------------------------------------------------------------------
    # Select Sharp Edges
    # -------------------------------------------------------------------------

    def select_sharp_edges(self, doc, angle_deg=0.0):
        """Select Plasticity boundary edges, optionally filtered by angle."""
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected = 0

        for obj in selection:
            if self._select_sharp_edges_on_obj(obj, angle_deg):
                affected += 1

        if affected:
            doc.SetMode(c4d.Medges)
            c4d.EventAdd()

        return affected > 0

    def _select_sharp_edges_on_obj(self, obj, angle_deg):
        if not obj.CheckType(c4d.Opolygon):
            return False

        addresses = self._get_sharp_edge_addresses(obj, angle_deg)
        if not addresses:
            return False

        edge_sel = obj.GetEdgeS()
        edge_sel.DeselectAll()
        for poly_idx, slot in addresses:
            edge_sel.Select(poly_idx * 4 + slot)

        obj.Message(c4d.MSG_UPDATE)
        return True

    # -------------------------------------------------------------------------
    # Select Plasticity Face(s)
    # -------------------------------------------------------------------------

    def select_plasticity_faces(self, doc):
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected  = 0

        for obj in selection:
            if self._select_plasticity_faces_on_obj(obj):
                affected += 1

        c4d.EventAdd()
        return affected > 0

    def _select_plasticity_faces_on_obj(self, obj):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return False
        poly_face_map, poly_count = result

        poly_sel = obj.GetPolygonS()
        map_len  = min(len(poly_face_map), poly_count)

        selected_groups = set()
        for pi in range(map_len):
            if poly_sel.IsSelected(pi):
                selected_groups.add(poly_face_map[pi])

        if not selected_groups:
            return False

        for pi in range(map_len):
            if poly_face_map[pi] in selected_groups:
                poly_sel.Select(pi)

        obj.Message(c4d.MSG_UPDATE)
        return True

    # -------------------------------------------------------------------------
    # Select Plasticity Edges
    # -------------------------------------------------------------------------

    def select_plasticity_edges(self, doc):
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected  = 0

        for obj in selection:
            if self._select_plasticity_edges_on_obj(obj):
                affected += 1

        if affected:
            doc.SetMode(c4d.Medges)
            c4d.EventAdd()

        return affected > 0

    def _select_plasticity_edges_on_obj(self, obj):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return False
        poly_face_map, poly_count = result

        poly_sel = obj.GetPolygonS()
        map_len  = min(len(poly_face_map), poly_count)

        selected_groups = set()
        for pi in range(map_len):
            if poly_sel.IsSelected(pi):
                selected_groups.add(poly_face_map[pi])

        if not selected_groups:
            return False

        edge_map = _build_edge_map(obj, poly_face_map, poly_count)

        edge_sel = obj.GetEdgeS()
        edge_sel.DeselectAll()

        for occurrences in edge_map.values():
            if len(occurrences) == 1:
                poly_idx, slot, g = occurrences[0]
                if g in selected_groups:
                    edge_sel.Select(poly_idx * 4 + slot)
            elif len(occurrences) == 2:
                _, _, g0 = occurrences[0]
                _, _, g1 = occurrences[1]
                in0 = g0 in selected_groups
                in1 = g1 in selected_groups
                if in0 != in1:
                    poly_idx, slot, _ = occurrences[0]
                    edge_sel.Select(poly_idx * 4 + slot)

        obj.Message(c4d.MSG_UPDATE)
        return True