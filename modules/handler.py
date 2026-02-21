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

  After melting, the NormalTag would have stale polygon indices, so in N-gon
  mode we skip it and rely on the Phong tag for smooth shading.

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

MANAGED_NORMAL_TAG_NAME = "__plasticity_normals__"


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
        for obj, poly_groups in deferred_ngons:
            self._create_ngon_groups(obj, poly_groups)

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
        for obj, poly_groups in deferred_ngons:
            self._create_ngon_groups(obj, poly_groups)

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

        deferred_ngons = []   # [(obj, poly_groups)] — melted OUTSIDE undo block

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
                poly_groups = self._update_object_geometry(
                    obj, verts, indices, faces, normals)
                self._copy_plasticity_meta(obj, pid, filename, groups, face_ids)

                if poly_groups:
                    deferred_ngons.append((obj, poly_groups))

        finally:
            doc.EndUndo()

        # SendModelingCommand(MCOMMAND_MELT) manages its own undo entries and
        # fails silently when called inside StartUndo/EndUndo — run it after.
        for obj, poly_groups in deferred_ngons:
            self._create_ngon_groups(obj, poly_groups)

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
        deferred_ngons = []   # list of (obj, poly_groups)

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
                    pg = self._update_object_geometry(existing, verts, indices, [], normals)
                    self._copy_plasticity_meta(existing, obj_id, filename, groups, face_ids)
                    if pg:
                        deferred_ngons.append((existing, pg))

                else:
                    # NEW OBJECT (standard path = tri mode, poly_groups = [])
                    points, polys, normal_map, poly_groups = \
                        self._compute_geometry(verts, indices, [], normals)

                    if not polys:
                        continue

                    new_obj = c4d.PolygonObject(len(points), len(polys))
                    new_obj.SetName(name)
                    self._write_points_and_polys(new_obj, points, polys)

                    if not poly_groups and normals and normal_map:
                        self._apply_normals(new_obj, normals, normal_map)

                    phong = new_obj.MakeTag(c4d.Tphong)
                    phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)
                    new_obj.Message(c4d.MSG_UPDATE)

                    self._copy_plasticity_meta(new_obj, obj_id, filename, groups, face_ids)
                    self._insert_last_child(doc, new_obj, root)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, new_obj)
                    self._items[key] = new_obj

                    if poly_groups:
                        deferred_ngons.append((new_obj, poly_groups))

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

    def _compute_geometry(self, vertices, indices, faces, normals):
        """
        Route to the correct geometry builder based on whether N-gon
        polygon-membership data is present.

        Returns:
            (points, polys, normal_map, poly_groups)
            poly_groups = [] means tri mode (no merging needed).
            poly_groups = [[...], [...]] means N-gon mode: groups of triangle
            poly indices that should be merged by _create_ngon_groups().
        """
        if faces:
            return self._compute_ngon_geometry(vertices, indices, faces, normals)
        else:
            return self._compute_tri_geometry(vertices, indices, normals)

    def _compute_tri_geometry(self, vertices, indices, normals):
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
        for i in range(vert_count):
            points.append(c4d.Vector(
                vertices[i * 3],
                vertices[i * 3 + 2],   # Plasticity Z → C4D Y
                vertices[i * 3 + 1],   # Plasticity Y → C4D Z
            ))

        polys      = []
        normal_map = []
        for i in range(tri_count):
            a  = indices[i * 3]
            b  = indices[i * 3 + 1]
            c_ = indices[i * 3 + 2]
            polys.append(c4d.CPolygon(a, c_, b))
            normal_map.append((a, c_, b, b))

        return points, polys, normal_map, []

    def _compute_ngon_geometry(self, vertices, indices, faces, normals):
        """
        Build geometry for N-gon refacet data.

        The refacet protocol sends:
          indices: vertex-index per loop position (flat list)
          faces:   polygon-membership per loop (same length);
                   a run of equal values = one polygon's vertices in order

        Steps:
          1. Weld duplicate vertices (Plasticity may send shared vertices as
             separate entries in N-gon mode).
          2. Fan-triangulate each polygon from vertex 0. This is geometrically
             correct because Plasticity guarantees convex polygon outlines.
          3. Record which output poly indices belong to each input polygon as
             poly_groups, so _create_ngon_groups() can melt them later.

        Notes:
          - The visible "fan" wireframe is expected before the MELT step and
            disappears once triangles are merged into proper N-gons.
          - NormalTag is NOT applied in N-gon mode: the melt operation changes
            the polygon topology, invalidating any pre-built normal data. The
            Phong tag handles smooth shading instead.
        """
        vert_count = len(vertices) // 3

        # Step 1: weld duplicate vertices
        vert_map   = {}
        old_to_new = [0] * vert_count
        unique_pts = []

        for i in range(vert_count):
            px = vertices[i * 3]
            py = vertices[i * 3 + 1]
            pz = vertices[i * 3 + 2]
            key = (round(px, 7), round(py, 7), round(pz, 7))
            if key not in vert_map:
                vert_map[key] = len(unique_pts)
                unique_pts.append(c4d.Vector(px, pz, py))   # coord swap
            old_to_new[i] = vert_map[key]

        new_indices = [old_to_new[idx] for idx in indices]

        polys       = []
        normal_map  = []
        poly_groups = []   # list[list[int]]
        poly_idx    = 0    # running polygon counter

        # Step 2: find polygon boundaries (runs of equal values in 'faces')
        poly_starts = [0]
        for i in range(1, len(faces)):
            if faces[i] != faces[i - 1]:
                poly_starts.append(i)
        poly_starts.append(len(faces))

        for p in range(len(poly_starts) - 1):
            start = poly_starts[p]
            end   = poly_starts[p + 1]
            count = end - start

            if count < 3:
                continue

            face_vindices = new_indices[start:end]   # welded indices, in order
            orig_vindices = indices[start:end]        # pre-weld for normal lookup

            # Fan triangulation from vertex 0:
            #   (v0,v1,v2), (v0,v2,v3), ..., (v0,vN-2,vN-1)
            group_poly_indices = []

            for t in range(count - 2):
                ia = face_vindices[0]
                ib = face_vindices[t + 1]
                ic = face_vindices[t + 2]

                polys.append(c4d.CPolygon(ia, ic, ib))   # reversed winding
                normal_map.append((
                    orig_vindices[0],     orig_vindices[t + 2],
                    orig_vindices[t + 1], orig_vindices[t + 1],
                ))
                group_poly_indices.append(poly_idx)
                poly_idx += 1

            # Only groups with 2+ triangles need merging
            if len(group_poly_indices) > 1:
                poly_groups.append(group_poly_indices)

        return unique_pts, polys, normal_map, poly_groups

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

    def _update_object_geometry(self, obj, vertices, indices, faces, normals):
        """
        Replace all geometry on an existing PolygonObject in-place.

        Only the managed NormalTag is stripped; all user tags survive.
        In N-gon mode (faces is non-empty), NormalTag is skipped entirely
        because it would be invalidated by MCOMMAND_MELT. Phong handles shading.

        Returns:
            poly_groups (list[list[int]]) — non-empty in N-gon mode.
            The CALLER is responsible for running _create_ngon_groups() OUTSIDE
            any active StartUndo/EndUndo block, because SendModelingCommand
            (MCOMMAND_MELT) manages its own undo entries and fails silently
            when called inside an existing undo context.
        """
        points, polys, normal_map, poly_groups = self._compute_geometry(
            vertices, indices, faces, normals
        )
        if not polys:
            return []

        self._strip_managed_tags(obj)
        obj.ResizeObject(len(points), len(polys))
        self._write_points_and_polys(obj, points, polys)

        # Normals: tri mode only (N-gon mode skips to avoid post-melt stale data)
        if not poly_groups and normals and normal_map:
            self._apply_normals(obj, normals, normal_map)

        phong = obj.GetTag(c4d.Tphong)
        if not phong:
            phong = obj.MakeTag(c4d.Tphong)
            phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(40)

        obj.Message(c4d.MSG_UPDATE)

        # Do NOT call _create_ngon_groups here — must run outside undo block.
        return poly_groups

    def _strip_managed_tags(self, obj):
        tag = obj.GetFirstTag()
        while tag:
            next_tag = tag.GetNext()
            if tag.CheckType(c4d.Tnormal) and tag.GetName() == MANAGED_NORMAL_TAG_NAME:
                tag.Remove()
            tag = next_tag

    # =========================================================================
    # Custom normals (tri mode only)
    # =========================================================================

    def _apply_normals(self, obj, normals, normal_map):
        """Write per-corner normals. Tries modern SetPolygon API, falls back to int16."""
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

    # =========================================================================
    # N-gon creation — MCOMMAND_MELT with polygon-identity hashing
    # =========================================================================

    @staticmethod
    def _create_ngon_groups(obj, poly_groups):
        """
        Merge groups of adjacent triangles into C4D N-gons via MCOMMAND_MELT.

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

        Args:
            obj        : c4d.PolygonObject already in a document
            poly_groups: list[list[int]] — each sub-list is original polygon
                         indices (from _compute_ngon_geometry's poly_idx counter)
                         that should be melted into a single N-gon.

        Ref: https://developers.maxon.net/forum/topic/13458/set-ngons-with-python/7
        """
        groups_to_melt = [g for g in poly_groups if len(g) >= 2]
        if not groups_to_melt:
            return

        doc = obj.GetDocument()

        # Build the identity index from the initial mesh state (before any melts).
        # Maps original_poly_index -> (a, b, c, d) vertex tuple.
        polygon_identity = {
            i: (cp.a, cp.b, cp.c, cp.d)
            for i, cp in enumerate(obj.GetAllPolygons())
        }

        for group in groups_to_melt:
            # Rebuild the inverted index for the CURRENT mesh state.
            # Must be done on every iteration because each melt changes the mesh.
            inverted = {
                (cp.a, cp.b, cp.c, cp.d): i
                for i, cp in enumerate(obj.GetAllPolygons())
            }

            # Translate original poly indices to current live indices.
            real_indices = []
            for orig_pid in group:
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
                print(f"[Plasticity] Warning: MCOMMAND_MELT failed for group {group}")

    # =========================================================================
    # Metadata helpers
    # =========================================================================

    def _copy_plasticity_meta(self, obj, plasticity_id, filename,
                              groups=None, face_ids=None):
        bc = obj.GetDataInstance()
        bc.SetInt32(BC_PLASTICITY_ID, int(plasticity_id))
        bc.SetString(BC_PLASTICITY_FILENAME, str(filename))
        if groups   is not None:
            bc.SetString(BC_PLASTICITY_GROUPS,   json.dumps(groups))
        if face_ids is not None:
            bc.SetString(BC_PLASTICITY_FACE_IDS, json.dumps(face_ids))

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