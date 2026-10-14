"""
Scene handler for creating and updating Cinema 4D objects from Plasticity data.

This is the C4D-facing layer.  The geometry itself (coordinate conversion,
welding, ear clipping, N-gon grouping, normals, poly-face maps, edge maps,
PUT_SOME payloads) lives in the c4d-free ``core.geometry`` module; the
methods here turn its results into objects, tags and undo steps.

KEY DESIGN: When updating an existing object, geometry is replaced IN-PLACE via
ResizeObject() + SetAllPoints()/SetPolygon(). This preserves the C4D object
identity, keeping all user-assigned tags (materials, textures, selections),
animation tracks, constraints, and scene references intact — matching the
Blender addon's mesh.clear_geometry() + refill approach.

Only Plasticity-managed tags are touched: the NormalTag named
"__plasticity_normals__" is rebuilt on every update; the "Plasticity Face <n>"
and "__plasticity_edges__" selection tags are removed (their indices would be
stale) and are recreated by the Store utilities.  User tags are never touched.

N-GON APPROACH (refacet path only):
  Plasticity sends N-gon data as a 'faces' membership array paired with an
  'indices' array; each run of equal values in 'faces' is one polygon.
  Tris/quads are written directly, larger polygons are ear-clipped and then
  merged back into true C4D N-gons with MCOMMAND_MELT.  Ferdinand's
  polygon-identity hashing (Maxon Developer Forum) keeps the melt correct when
  indices shift after each successive melt.  The object's polygon selection
  is saved before and restored after melting.

NORMALS:
  Written straight into the NormalTag's low-level buffer: 12 int16 per
  polygon, scaled by 32000 (Maxon forum, topic 11584).  In N-gon mode they
  are rebuilt after the melt from a (welded vertex, group) → normal map.

POLY-FACE MAP (BC_PLASTICITY_POLY_FACE_MAP):
  The Plasticity group (CAD face) index of every C4D polygon.  Stored
  run-length encoded; maps written by 1.1.x (a JSON list) are still read.

COORDINATE SYSTEM: Plasticity is Z-up, C4D is Y-up.
  C4D X = Plasticity X,  C4D Y = Plasticity Z,  C4D Z = Plasticity Y.

WINDING ORDER: Reversed — CPolygon(a, c, b) instead of (a, b, c).

UNIT SCALE: Lives on the root null's scale transform, not baked into vertices.

HIERARCHY:
  Root null (per filename)
    ├── Outbox null (BC_PLASTICITY_OUTBOX) — user places SDS objects here
    └── Inbox null (BC_PLASTICITY_INBOX)  — Plasticity objects land here

UNDO:
  Every incoming update (list, transaction, refacet) is exactly one undo
  step, like the Blender bridge.  The step is opened lazily, so updates that
  change nothing leave no empty step.  Undoing a Live Link step reverts the
  C4D copy to the previous Plasticity state; the next update or a Refresh
  resyncs it.  Remote changes are deliberately not made invisible to the
  undo system: removing or changing scene objects behind its back can leave
  the undo stack referring to states it never recorded.
"""

import contextlib
import json
import math
import uuid

import c4d

from core import geometry as geo
from core.protocol import ObjectType, MessageType, LIST_TYPES
from core.threading_bridge import ThreadingBridge, BridgeEvent, EventType
from core.refacet_tag import TAG_PLUGIN_ID as REFACET_TAG_ID, read_tag_refacet_kwargs

# BaseContainer keys — offsets from registered plugin ID 1066929.
# Never change these: they are stored in users' scenes.
PLUGIN_ID              = 1066929
BC_PLASTICITY_ID       = PLUGIN_ID + 1   # 1066930
BC_PLASTICITY_FILENAME = PLUGIN_ID + 2   # 1066931
BC_PLASTICITY_GROUPS   = PLUGIN_ID + 3   # 1066932
BC_PLASTICITY_FACE_IDS = PLUGIN_ID + 4   # 1066933
BC_PLASTICITY_ROOT     = PLUGIN_ID + 5   # 1066934
BC_PLASTICITY_POLY_FACE_MAP = PLUGIN_ID + 6   # 1066935
# Added in 1.1.0 (Inbox/Outbox, PUT_SOME)
BC_PLASTICITY_INBOX         = PLUGIN_ID + 7    # 1066936
BC_PLASTICITY_OUTBOX        = PLUGIN_ID + 8    # 1066937
BC_PLASTICITY_PNS_ID        = PLUGIN_ID + 9    # 1066938  (persistent string ID for outbox items)
BC_PLASTICITY_COLLECTION_ID = PLUGIN_ID + 10   # 1066939  (persistent string ID for outbox groups)
BC_PLASTICITY_VERSION       = PLUGIN_ID + 11   # 1066940
BC_PLASTICITY_GROUP_ID      = PLUGIN_ID + 12   # 1066941
BC_PLASTICITY_FLAGS         = PLUGIN_ID + 13   # 1066942  (last applied Plasticity flags)

MANAGED_NORMAL_TAG_NAME  = "__plasticity_normals__"
MANAGED_FACE_SEL_PREFIX  = "Plasticity Face "
MANAGED_EDGES_TAG_NAME   = "__plasticity_edges__"

# Plasticity sends vertex positions in metres; C4D's internal unit is
# centimetres (see core.geometry.IMPORT_SCALE).
_IMPORT_SCALE = geo.IMPORT_SCALE

# PutSome options bitfield constants
_KIND_MESH = 0
_KIND_SUBD = 1

_PHONG_ANGLE_DEG = 40


# =============================================================================
# Shared utility functions for reading Plasticity metadata from objects
# =============================================================================

def _poly_tuples(obj):
    return [(cp.a, cp.b, cp.c, cp.d) for cp in obj.GetAllPolygons()]


def _read_poly_face_map(obj):
    """Validated (poly_face_map, poly_count) of a Plasticity polygon object,
    or None.  Reads both the run-length format and 1.1.x JSON lists; corrupt
    data yields None instead of raising."""
    if not obj.CheckType(c4d.Opolygon):
        return None
    bc = obj.GetDataInstance()
    if bc.GetInt32(BC_PLASTICITY_ID, 0) == 0:
        return None
    poly_face_map = geo.decode_poly_face_map(
        bc.GetString(BC_PLASTICITY_POLY_FACE_MAP, ""))
    poly_count = obj.GetPolygonCount()
    if poly_count == 0 or not poly_face_map:
        return None
    return poly_face_map, poly_count


def _get_poly_face_map(obj):
    """Read and validate the poly-face map from a Plasticity polygon object.

    Returns (poly_face_map, face_ids, poly_count) on success, or None if the
    object is not a valid Plasticity polygon object or has no map data.
    """
    result = _read_poly_face_map(obj)
    if result is None:
        return None
    poly_face_map, poly_count = result
    try:
        face_ids = json.loads(obj.GetDataInstance().GetString(
            BC_PLASTICITY_FACE_IDS, "") or "null")
    except ValueError:
        return None
    if not face_ids or not isinstance(face_ids, list):
        return None
    return poly_face_map, face_ids, poly_count


def _get_poly_face_map_only(obj):
    """Like _get_poly_face_map but doesn't require face_ids."""
    return _read_poly_face_map(obj)


def _build_edge_map(obj, poly_face_map, poly_count):
    """Build a global edge map for a polygon object.

    Returns a dict: (min_vertex, max_vertex) → [(poly_idx, slot, group_idx), ...]
    """
    return geo.build_edge_map(_poly_tuples(obj), poly_face_map, poly_count)


def _write_lowlevel(buf, raw):
    """Copy raw bytes into a variable tag's low-level buffer.  Returns False
    if the buffer is missing or too small (caller falls back)."""
    if buf is None:
        return False
    mv = memoryview(buf)
    if mv.format != "B" or mv.ndim != 1:
        mv = mv.cast("B")
    if len(mv) < len(raw):
        return False
    mv[:len(raw)] = raw
    return True


class _UndoStep:
    """One document undo step, opened lazily on the first ``add``."""

    def __init__(self, doc):
        self.doc = doc
        self.open = False

    def add(self, undotype, obj):
        if not self.open:
            self.doc.StartUndo()
            self.open = True
        self.doc.AddUndo(undotype, obj)

    def close(self):
        if self.open:
            self.open = False
            self.doc.EndUndo()


class SceneHandler:
    def __init__(self, bridge: ThreadingBridge):
        self.bridge = bridge
        self.unit_scale = 1.0

        self._items  = {}   # (filename, id) -> c4d.BaseObject  (mesh objects)
        self._groups = {}   # (filename, id) -> c4d.BaseObject  (null groups)
        self._roots  = {}   # filename       -> c4d.BaseObject  (root nulls)

        # Plasticity IDs returned by PUT_SOME responses this session, per
        # Plasticity file (IDs are small per-file integers), so uploaded
        # objects are not re-imported:  filename -> (item_ids, group_ids).
        # Items and groups have overlapping ID spaces and are kept apart.
        self._uploaded = {}
        # Plasticity file the last Outbox upload belongs to (the PUT_SOME
        # response does not say).
        self._upload_filename = None

        # Auto-Refacet: incoming tri geometry kept aside for tagged objects
        # until their refacet response arrives ((filename, id) -> item).
        self._pending_auto_refacet = {}

        self._undo = None   # current _UndoStep while an update runs

        # Back-reference to the client (set in plasticity_c4d.pyp)
        self.client = None

        bridge.register_callback(EventType.CONNECTED,           self._on_connected)
        bridge.register_callback(EventType.DISCONNECTED,        self._on_disconnected)
        bridge.register_callback(EventType.LIST_RESPONSE,       self._on_list_response)
        bridge.register_callback(EventType.TRANSACTION,         self._on_transaction)
        bridge.register_callback(EventType.REFACET_RESPONSE,    self._on_refacet_response)
        bridge.register_callback(EventType.NEW_VERSION,         self._on_new_version)
        bridge.register_callback(EventType.NEW_FILE,            self._on_new_file)
        bridge.register_callback(EventType.HANDSHAKE_RESPONSE,  self._on_handshake)
        bridge.register_callback(EventType.PUT_SOME_RESPONSE,   self._on_put_some)
        bridge.register_callback(EventType.OPERATION_FAILED,    self._on_operation_failed)

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
        self._uploaded.clear()
        self._upload_filename = None
        self._pending_auto_refacet.clear()

    def on_disconnect(self):
        # A refacet that was still in flight will never arrive: give
        # Auto-Refacet objects the geometry kept aside for them.
        doc = c4d.documents.GetActiveDocument()
        if doc and self._pending_auto_refacet:
            self._apply_pending_auto_refacet(
                doc, list(self._pending_auto_refacet))
        self._pending_auto_refacet.clear()
        self._items.clear()
        self._groups.clear()
        self._roots.clear()

    def _on_operation_failed(self, event: BridgeEvent):
        """A request could not be sent or its response was unreadable."""
        operation = (event.data or {}).get('operation')
        if operation == MessageType.REFACET_SOME_1:
            doc = c4d.documents.GetActiveDocument()
            if doc and self._pending_auto_refacet:
                self._apply_pending_auto_refacet(
                    doc, list(self._pending_auto_refacet))
        elif operation in LIST_TYPES and self.client:
            # Don't let the Refresh upload fire on some later list.
            self.client.on_list_complete = None

    def _uploaded_for(self, filename):
        ids = self._uploaded.get(filename)
        if ids is None:
            ids = self._uploaded[filename] = (set(), set())
        return ids

    # =========================================================================
    # Undo helpers
    # =========================================================================

    @contextlib.contextmanager
    def _undo_step(self, doc):
        """Group everything inside into (at most) one undo step."""
        if self._undo is not None:            # nested: join the outer step
            yield self._undo
            return
        step = _UndoStep(doc)
        self._undo = step
        try:
            yield step
        finally:
            self._undo = None
            step.close()

    def _add_undo(self, doc, undotype, obj):
        if self._undo is not None and self._undo.doc is doc:
            self._undo.add(undotype, obj)
        else:
            with self._undo_step(doc) as step:
                step.add(undotype, obj)

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
        if data.get('error'):
            self.bridge.status_message = (
                f"Error: Refresh failed (Plasticity returned {data['error']})")
            if self.client:
                self.client.on_list_complete = None
            return
        filename = data.get('filename', '')
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        all_objects = data.get('add', []) + data.get('update', [])
        all_item_ids = set()
        all_group_ids = set()
        for obj_data in all_objects:
            if int(obj_data.get('type', -1)) == ObjectType.GROUP:
                all_group_ids.add(obj_data.get('id', 0))
            else:
                all_item_ids.add(obj_data.get('id', 0))

        with self._undo_step(doc):
            inbox = self._prepare(doc, filename)
            outbox_ids = self._get_outbox_plasticity_ids(doc, filename)
            outbox_items, outbox_groups = outbox_ids[0], outbox_ids[1]

            deferred_ngons, auto_ids = self._process_objects(
                doc, filename, inbox, all_objects, outbox_ids)

            # Delete stale items (but not outbox items)
            stale = [k for k in self._items
                     if k[0] == filename
                     and k[1] not in all_item_ids
                     and k[1] not in outbox_items]
            for k in stale:
                obj = self._items.pop(k, None)
                if obj and obj.GetDocument() == doc:
                    self._add_undo(doc, c4d.UNDOTYPE_DELETEOBJ, obj)
                    obj.Remove()

            # Delete stale groups (but not outbox groups)
            stale = [k for k in self._groups
                     if k[0] == filename
                     and k[1] not in all_group_ids
                     and k[1] not in outbox_groups]
            for k in stale:
                grp = self._groups.pop(k, None)
                if grp and grp.GetDocument() == doc:
                    self._add_undo(doc, c4d.UNDOTYPE_DELETEOBJ, grp)
                    grp.Remove()

            self._process_deferred_ngons(deferred_ngons)

        c4d.EventAdd()
        self.bridge.status_message = f"Refreshed {filename}"

        # Request Auto-Refacet first, so nothing below can leave tagged
        # objects waiting for a refacet that was never requested.
        self._auto_refacet_tagged(doc, filename, auto_ids)

        # Fire the list-complete callback (uploads the Outbox if supported)
        callback = self.client.on_list_complete if self.client else None
        if callback is not None:
            self.client.on_list_complete = None
            try:
                callback(filename)
            except Exception as e:
                self.bridge.status_message = f"Error: Outbox upload failed: {e}"
                print(f"[Plasticity] Outbox upload failed: {e}")
                import traceback
                traceback.print_exc()

    def _on_transaction(self, event: BridgeEvent):
        """Incremental live-link update."""
        data = event.data
        if not data:
            return
        filename = data.get('filename', '')
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        all_objects = data.get('add', []) + data.get('update', [])
        auto_ids = []
        with self._undo_step(doc):
            inbox = self._prepare(doc, filename)
            outbox_ids = self._get_outbox_plasticity_ids(doc, filename)

            for oid in data.get('delete', []):
                if oid not in outbox_ids[0]:
                    self._delete_item(doc, filename, oid)

            deferred_ngons = []
            if all_objects:
                deferred_ngons, auto_ids = self._process_objects(
                    doc, filename, inbox, all_objects, outbox_ids)
            self._process_deferred_ngons(deferred_ngons)

        c4d.EventAdd()
        self._auto_refacet_tagged(doc, filename, auto_ids)

    def _on_refacet_response(self, event: BridgeEvent):
        """Re-tessellate objects with new facet settings."""
        data = event.data
        if not data:
            return
        doc = c4d.documents.GetActiveDocument()
        if data.get('error'):
            self.bridge.status_message = (
                f"Error: Refacet failed (Plasticity returned {data['error']})")
            if doc and self._pending_auto_refacet:
                # Auto-Refacet requests may have failed: never leave tagged
                # objects stale — apply the geometry kept aside for them.
                self._apply_pending_auto_refacet(
                    doc, list(self._pending_auto_refacet))
            return
        filename = data.get('filename', '')
        if not doc:
            return

        with self._undo_step(doc):
            self._prepare(doc, filename)
            deferred_ngons = []

            for item in data.get('refaceted_objects', []):
                pid  = item.get('plasticity_id')
                key  = (filename, pid)
                stashed = self._pending_auto_refacet.pop(key, None)
                obj  = self._items.get(key)
                if not obj or obj.GetDocument() != doc:
                    continue

                result = None
                if len(item.get('vertices', [])):
                    result = self._replace_geometry(
                        doc, obj, item.get('vertices', []),
                        item.get('indices', []), item.get('faces', []),
                        item.get('normals', []), item.get('groups', []))
                if result is None:
                    # Unusable refacet data: fall back to the update that
                    # was kept aside for an Auto-Refacet object.
                    if stashed is not None:
                        self._apply_stashed(doc, filename, pid, obj, stashed)
                    continue
                pg, cn, pfv, pfm = result
                self._copy_plasticity_meta(
                    obj, pid, filename, item.get('groups', []),
                    item.get('face_ids', []),
                    poly_face_map=pfm if not pg else None)
                if pg:
                    deferred_ngons.append((obj, pg, cn, pfv))

            self._process_deferred_ngons(deferred_ngons)

        c4d.EventAdd()

    def _on_new_version(self, event: BridgeEvent):
        # Status display and auto-refresh are handled by the dialog, which
        # knows whether Live Link is active.  Update filename only.
        data = event.data
        if data:
            fn = data.get('filename', '')
            if fn:
                self.bridge.filename = fn

    def _on_new_file(self, event: BridgeEvent):
        # Status display and auto-refresh are handled by the dialog.
        data = event.data
        if data:
            fn = data.get('filename', '')
            if fn:
                self.bridge.filename = fn

    # =========================================================================
    # Handshake & PutSome handlers
    # =========================================================================

    def _on_handshake(self, event: BridgeEvent):
        data = event.data
        if not data:
            return
        supported = data.get('supported_messages', set())
        if self.client:
            self.client.supported_messages = set(supported)
        self.bridge.status_message = f"Handshake OK — server supports {len(supported)} message types"
        print(f"[Plasticity] Handshake complete: {sorted(supported)}")

    def _on_put_some(self, event: BridgeEvent):
        """Handle PUT_SOME_1 response — stamp outbox objects with Plasticity IDs."""
        data = event.data
        if not data:
            return

        code = data.get('code', 0)
        if code != 200:
            self.bridge.status_message = f"Error: Upload failed (Plasticity returned {code})"
            print(f"[Plasticity] PutSome failed with code: {code}")
            return

        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return

        group_results = data.get('group_results', [])
        item_results  = data.get('item_results', [])

        # One walk instead of one full-scene walk per result.
        index = self._index_by_bc_strings(
            doc, (BC_PLASTICITY_PNS_ID, BC_PLASTICITY_COLLECTION_ID))

        with self._undo_step(doc):
            for item in item_results:
                obj = index.get((BC_PLASTICITY_PNS_ID, item["c4d_id"]))
                if obj:
                    self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, obj)
                    bc = obj.GetDataInstance()
                    bc.SetInt32(BC_PLASTICITY_ID, item["stable_id"])
                    bc.SetInt32(BC_PLASTICITY_VERSION, item["version_id"])

            for group in group_results:
                obj = index.get((BC_PLASTICITY_COLLECTION_ID,
                                 group["c4d_collection_id"]))
                if obj:
                    self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, obj)
                    obj.GetDataInstance().SetInt32(
                        BC_PLASTICITY_GROUP_ID, group["group_id"])

        self.bridge.status_message = (
            f"Upload OK: {len(group_results)} groups, {len(item_results)} items")
        print(f"[Plasticity] PutSome succeeded: "
              f"{len(group_results)} groups, {len(item_results)} items")

        # Remember all uploaded IDs so they are filtered on subsequent
        # refreshes of the same Plasticity file.
        fn = self._upload_filename
        if fn is None:
            fn = self.bridge.filename or ""
        item_ids, group_ids = self._uploaded_for(fn)
        for item in item_results:
            sid = item.get("stable_id", 0)
            if sid:
                item_ids.add(sid)
        for group in group_results:
            gid = group.get("group_id", 0)
            if gid:
                group_ids.add(gid)
        c4d.EventAdd()

    @staticmethod
    def _index_by_bc_strings(doc, keys):
        """{(bc_key, value): object} for every object carrying one of the
        given string keys (first occurrence wins), in one tree walk."""
        index = {}
        stack = [doc.GetFirstObject()]
        while stack:
            obj = stack.pop()
            while obj:
                bc = obj.GetDataInstance()
                for key in keys:
                    v = bc.GetString(key, "")
                    if v and (key, v) not in index:
                        index[(key, v)] = obj
                down = obj.GetDown()
                if down:
                    stack.append(obj.GetNext())
                    obj = down
                else:
                    obj = obj.GetNext()
        return index

    # =========================================================================
    # Auto-refacet: trigger refacet for tagged objects after geometry updates
    # =========================================================================

    def _auto_refacet_eligible(self, obj):
        """True if ``obj`` carries an Auto-Refacet tag and a refacet can be
        requested right now (so its tri update may be skipped)."""
        return bool(self.client and self.client.connected
                    and obj.GetTag(REFACET_TAG_ID))

    def _auto_refacet_tagged(self, doc, filename, updated_item_ids):
        """Send refacet requests for updated objects that carry the auto-refacet tag.

        Called from _on_list_response and _on_transaction — NEVER from
        _on_refacet_response (to avoid infinite loops).  Objects whose request
        cannot be sent get the tri geometry that was kept aside for them.
        """
        if not updated_item_ids:
            return
        if not self.client or not self.client.connected:
            self._apply_pending_auto_refacet(
                doc, [(filename, i) for i in updated_item_ids])
            return

        # Group objects by identical refacet settings so we can batch them
        # into as few server requests as possible.
        settings_groups = {}  # settings_key → (kwargs, [plasticity_ids])
        failed = []

        for obj_id in updated_item_ids:
            key = (filename, obj_id)
            obj = self._items.get(key)
            if not obj or obj.GetDocument() != doc:
                continue

            tag = obj.GetTag(REFACET_TAG_ID)
            if not tag:
                failed.append(key)
                continue

            try:
                kwargs = read_tag_refacet_kwargs(tag)
            except Exception as e:
                print(f"[Plasticity] Error reading refacet tag on "
                      f"'{obj.GetName()}': {e}")
                failed.append(key)
                continue

            settings_key = tuple(sorted(kwargs.items()))
            if settings_key not in settings_groups:
                settings_groups[settings_key] = (kwargs, [])
            settings_groups[settings_key][1].append(obj_id)

        for kwargs, obj_ids in settings_groups.values():
            sent = self.client.refacet_some(
                filename=filename, plasticity_ids=obj_ids, **kwargs)
            if sent is None:
                failed.extend((filename, i) for i in obj_ids)

        if failed:
            self._apply_pending_auto_refacet(doc, failed)

    def _apply_pending_auto_refacet(self, doc, keys):
        """Apply the tri geometry kept aside for Auto-Refacet objects."""
        pending = [(k, self._pending_auto_refacet.pop(k, None)) for k in keys]
        pending = [(k, item) for k, item in pending if item is not None]
        if not pending:
            return
        with self._undo_step(doc):
            prepared = set()
            for (filename, obj_id), item in pending:
                if filename not in prepared:
                    self._prepare(doc, filename)
                    prepared.add(filename)
                obj = self._items.get((filename, obj_id))
                if obj and obj.GetDocument() == doc:
                    self._apply_stashed(doc, filename, obj_id, obj, item)
        c4d.EventAdd()

    def _apply_stashed(self, doc, filename, obj_id, obj, item):
        """Apply a kept-aside tri update (list/transaction item)."""
        result = self._replace_geometry(
            doc, obj, item.get('vertices', []), item.get('faces', []),
            [], item.get('normals', []), item.get('groups', []))
        if result is not None:
            self._copy_plasticity_meta(
                obj, obj_id, filename, item.get('groups', []),
                item.get('face_ids', []), poly_face_map=result[3])

    # =========================================================================
    # Cache management
    # =========================================================================

    def _prepare(self, doc, filename):
        """Rebuild caches by scanning the inbox — undo-safe.  Returns the
        Inbox null (creating root/Inbox/Outbox as needed)."""
        root = self._get_or_create_root(doc, filename)
        inbox = self._get_or_create_inbox(doc, filename, root)
        self._get_or_create_outbox(doc, filename, root)  # ensure it exists

        self._items  = {k: v for k, v in self._items.items()  if k[0] != filename}
        self._groups = {k: v for k, v in self._groups.items() if k[0] != filename}

        stack = [inbox.GetDown()]
        while stack:
            child = stack.pop()
            while child:
                bc  = child.GetDataInstance()
                pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
                if pid != 0 and bc.GetString(BC_PLASTICITY_FILENAME, "") == filename:
                    if child.CheckType(c4d.Onull):
                        self._groups[(filename, pid)] = child
                    else:
                        self._items[(filename, pid)] = child
                down = child.GetDown()
                if down:
                    stack.append(child.GetNext())
                    child = down
                else:
                    child = child.GetNext()

        # Migration: also scan root directly for legacy scenes where objects
        # live directly under root (pre-1.1.0 hierarchy).  Objects found
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
        return inbox

    # =========================================================================
    # Outbox protection
    # =========================================================================

    def _get_outbox_plasticity_ids(self, doc, filename):
        """Plasticity IDs owned by the Outbox: ``(item_ids, group_ids,
        uploaded_item_ids)``.

        Only IDs the plugin itself stamped after an upload count — the
        ``BC_PLASTICITY_ID`` of Subdivision Surfaces carrying a
        ``BC_PLASTICITY_PNS_ID`` and the ``BC_PLASTICITY_GROUP_ID`` of nulls
        carrying a ``BC_PLASTICITY_COLLECTION_ID`` — plus the IDs returned by
        PUT_SOME this session for this file.  Cages under an SDS are not
        searched: a copy of an Inbox mesh used as a cage still carries that
        mesh's ID and must not hide (or delete) the original.  Items and
        groups have overlapping ID spaces, so they are kept apart.

        ``uploaded_item_ids`` (this session's PUT_SOME results) are the only
        IDs whose Inbox copies are deleted as mirrors of an upload.
        """
        outbox = self._get_or_create_outbox(doc, filename)
        uploaded_items, uploaded_groups = self._uploaded_for(filename)
        item_ids = set(uploaded_items)
        group_ids = set(uploaded_groups)

        stack = [outbox.GetDown()]
        while stack:
            child = stack.pop()
            while child:
                bc = child.GetDataInstance()
                if child.CheckType(c4d.Onull):
                    gid = bc.GetInt32(BC_PLASTICITY_GROUP_ID, 0)
                    if gid and bc.GetString(BC_PLASTICITY_COLLECTION_ID, ""):
                        group_ids.add(gid)
                    if child.GetDown():
                        stack.append(child.GetNext())
                        child = child.GetDown()
                        continue
                elif child.CheckType(c4d.Osds):
                    pid = bc.GetInt32(BC_PLASTICITY_ID, 0)
                    if pid and bc.GetString(BC_PLASTICITY_PNS_ID, ""):
                        item_ids.add(pid)
                child = child.GetNext()
        return item_ids, group_ids, set(uploaded_items)

    # =========================================================================
    # Hierarchy / visibility preservation helpers
    # =========================================================================

    def _is_managed_parent(self, parent, filename, inbox):
        """True if `parent` is a location the plugin itself manages: the
        Inbox null or a Plasticity group null of the same file.

        If the user has re-parented an object anywhere else (under a
        generator like ZRemesher/SDS, their own null, ...), hierarchy sync
        must leave it alone — that re-parenting was intentional.
        """
        if parent is None:
            return False
        if parent == inbox:
            return True
        bc = parent.GetDataInstance()
        if bc.GetBool(BC_PLASTICITY_INBOX):
            return True
        # The root null itself: objects directly under it are the pre-1.1.0
        # layout and must be migrated into the Inbox (1.1.6 skipped them).
        if (bc.GetBool(BC_PLASTICITY_ROOT)
                and bc.GetString(BC_PLASTICITY_FILENAME, "") == filename):
            return True
        return (parent.CheckType(c4d.Onull)
                and bc.GetInt32(BC_PLASTICITY_ID, 0) != 0
                and bc.GetString(BC_PLASTICITY_FILENAME, "") == filename)

    def _apply_visibility(self, doc, obj, flags, vis):
        """Push editor/render visibility only when the Plasticity-side flags
        actually changed since the last refresh.  This lets the user
        hide/show objects in C4D without every refresh reverting it, while
        visibility toggles made in Plasticity still propagate.
        """
        bc = obj.GetDataInstance()
        if bc.GetInt32(BC_PLASTICITY_FLAGS, -1) != flags:
            self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, obj)
            obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = vis
            obj[c4d.ID_BASEOBJECT_VISIBILITY_RENDER] = vis
            bc.SetInt32(BC_PLASTICITY_FLAGS, flags)

    # =========================================================================
    # Two-pass object processing
    # =========================================================================

    def _process_objects(self, doc, filename, inbox, objects, outbox_ids=None):
        """
        Pass 1: Geometry — create new objects or update existing in-place.
        Pass 2: Hierarchy — re-parent, apply visibility.

        ``outbox_ids`` is ``(item_ids, group_ids)``; those objects are owned
        by the Outbox and skipped (an Inbox mirror of an uploaded item is
        removed).  Existing objects that carry an Auto-Refacet tag keep
        their geometry: the update is kept aside and their IDs are returned
        so a single refacet response rebuilds them.

        Returns ``(deferred_ngons, auto_refacet_ids)``; N-gon melts are
        deferred until after all insertions in Pass 1.
        """
        outbox_ids = tuple(outbox_ids or (set(), set()))
        outbox_items, outbox_groups = outbox_ids[0], outbox_ids[1]
        upload_mirrors = outbox_ids[2] if len(outbox_ids) > 2 else set()

        deferred_ngons = []
        auto_refacet_ids = []

        # ── Pass 1: Geometry ──────────────────────────────────────────────────
        for item in objects:
            obj_type = int(item.get('type', -1))
            obj_id   = item.get('id', 0)
            name     = item.get('name', f"Object_{obj_id}")

            if obj_type == ObjectType.GROUP:
                if obj_id == 0 or obj_id in outbox_groups:
                    continue
                key = (filename, obj_id)
                grp = self._groups.get(key)
                if grp is None:
                    grp = c4d.BaseObject(c4d.Onull)
                    grp.SetName(name)
                    self._copy_plasticity_meta(grp, obj_id, filename)
                    self._insert_last_child(doc, grp, inbox)
                    self._add_undo(doc, c4d.UNDOTYPE_NEWOBJ, grp)
                    self._groups[key] = grp
                elif grp.GetName() != name:
                    self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, grp)
                    grp.SetName(name)

            elif obj_type in (ObjectType.SOLID, ObjectType.SHEET):
                key = (filename, obj_id)
                if obj_id in outbox_items:
                    if obj_id in upload_mirrors:
                        # An Inbox copy of an object uploaded this session
                        # (a Live Link update that arrived before the
                        # PUT_SOME response) is redundant — remove it.
                        self._delete_item(doc, filename, obj_id)
                    continue

                verts    = item.get('vertices', [])
                indices  = item.get('faces',    [])
                normals  = item.get('normals',  [])
                groups   = item.get('groups',   [])
                face_ids = item.get('face_ids', [])

                if not len(verts):
                    continue

                existing = self._items.get(key)

                if existing and existing.GetDocument() == doc:
                    if self._auto_refacet_eligible(existing):
                        self._pending_auto_refacet[key] = item
                        auto_refacet_ids.append(obj_id)
                        if existing.GetName() != name:
                            self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL,
                                           existing)
                            existing.SetName(name)
                        continue
                    result = self._replace_geometry(
                        doc, existing, verts, indices, [], normals, groups,
                        name=name)
                    if result is None:
                        continue
                    pg, cn, pfv, pfm = result
                    self._copy_plasticity_meta(
                        existing, obj_id, filename, groups, face_ids,
                        poly_face_map=pfm if not pg else None)
                    if pg:
                        deferred_ngons.append((existing, pg, cn, pfv))

                else:
                    built = self._compute_geometry(verts, indices, [],
                                                   normals, groups)
                    if not built.polys:
                        continue

                    new_obj = c4d.PolygonObject(len(built.points),
                                                len(built.polys))
                    new_obj.SetName(name)
                    self._write_points_and_polys(new_obj, built.points,
                                                 built.polys)

                    if not built.poly_groups and built.normal_data:
                        self._write_normal_tag(new_obj, built.normal_data)

                    phong = new_obj.MakeTag(c4d.Tphong)
                    phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(
                        _PHONG_ANGLE_DEG)
                    phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(built.poly_groups)
                    new_obj.Message(c4d.MSG_UPDATE)

                    self._copy_plasticity_meta(
                        new_obj, obj_id, filename, groups, face_ids,
                        poly_face_map=(built.face_map
                                       if not built.poly_groups else None))
                    self._insert_last_child(doc, new_obj, inbox)
                    self._add_undo(doc, c4d.UNDOTYPE_NEWOBJ, new_obj)
                    self._items[key] = new_obj

                    if built.poly_groups:
                        deferred_ngons.append(
                            (new_obj, built.poly_groups, built.corner_normals,
                             built.poly_face_val))

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
                if obj_id in outbox_groups:
                    continue
                target = self._groups.get((filename, obj_id))
            elif obj_type in (ObjectType.SOLID, ObjectType.SHEET):
                if obj_id in outbox_items:
                    continue
                target = self._items.get((filename, obj_id))
            else:
                continue
            if not target:
                continue

            target_parent = inbox
            if parent_id > 0 and (filename, parent_id) in self._groups:
                target_parent = self._groups[(filename, parent_id)]
            if target.GetUp() != target_parent:
                if self._is_managed_parent(target.GetUp(), filename, inbox):
                    self._add_undo(doc, c4d.UNDOTYPE_CHANGE, target)
                    target.Remove()
                    self._insert_last_child(doc, target, target_parent)
            self._apply_visibility(doc, target, flags, vis)

        return deferred_ngons, auto_refacet_ids

    # =========================================================================
    # Geometry computation
    # =========================================================================

    class _Built:
        __slots__ = ("points", "polys", "poly_groups", "corner_normals",
                     "poly_face_val", "face_map", "normal_data", "dropped")

    def _compute_geometry(self, vertices, indices, faces, normals, groups):
        """Build C4D-ready geometry.  ``faces`` (refacet data) selects the
        N-gon path, otherwise the data is plain triangles."""
        b = SceneHandler._Built()
        if len(faces):
            m = geo.build_ngon_mesh(vertices, indices, faces, normals, groups)
            b.points = [c4d.Vector(x, y, z) for x, y, z in m.points]
            b.polys = [c4d.CPolygon(a, bb, c, d) for a, bb, c, d in m.polys]
            b.poly_groups = m.poly_groups
            b.corner_normals = m.corner_normals
            b.poly_face_val = m.poly_face_val
            # Without N-gons to melt, each polygon's group is known exactly
            # (1.1.x used tri arithmetic here, wrong for quads — F13).
            b.face_map = m.poly_face_val
            corners = m.corners
            b.dropped = m.dropped
        else:
            m = geo.build_tri_mesh(vertices, indices, make_point=c4d.Vector,
                                   make_poly=c4d.CPolygon)
            b.points = m.points
            b.polys = m.polys
            b.poly_groups = []
            b.corner_normals = {}
            b.poly_face_val = []
            b.face_map = geo.tri_face_map(len(b.polys), groups)
            corners = m
            b.dropped = m.dropped
        if b.dropped:
            print(f"[Plasticity] Skipped {b.dropped} invalid polygon(s) "
                  f"(missing vertices or fewer than 3 corners)")
        b.normal_data = None
        if len(normals) >= 3 and not b.poly_groups:
            if isinstance(corners, geo.TriMesh):
                corners = corners.corners
            b.normal_data = geo.pack_vertex_normals(normals, corners)
        return b

    # =========================================================================
    # Write geometry
    # =========================================================================

    @staticmethod
    def _write_points_and_polys(obj, points, polys):
        """Bulk point write (``SetAllPoints``) and one SetPolygon per polygon
        — the Python API has no bulk polygon setter."""
        obj.SetAllPoints(points)
        set_polygon = obj.SetPolygon
        for i, poly in enumerate(polys):
            set_polygon(i, poly)

    @staticmethod
    def _resize(obj, point_count, poly_count):
        """ResizeObject and clear stale N-gon data.

        1.1.6 passes ``ncnt=0`` to drop the old N-gon structure (fixes N-gon
        collapse on repeated refacet).  The Python docs only document
        ``ResizeObject(pcnt[, vcnt])``, so fall back if a build rejects the
        third argument.
        """
        try:
            return obj.ResizeObject(point_count, poly_count, 0)
        except TypeError:
            return obj.ResizeObject(point_count, poly_count)

    # =========================================================================
    # In-place geometry update
    # =========================================================================

    def _replace_geometry(self, doc, obj, vertices, indices, faces, normals,
                          groups, name=None):
        """Replace all geometry on an existing PolygonObject in-place.

        Returns ``(poly_groups, corner_normals, poly_face_val, poly_face_map)``
        or ``None`` when the data has no usable polygons — then the object,
        including its metadata, is left untouched.
        """
        built = self._compute_geometry(vertices, indices, faces, normals,
                                       groups)
        if not built.polys:
            return None
        self._add_undo(doc, c4d.UNDOTYPE_CHANGE, obj)
        if name is not None:
            obj.SetName(name)
        self._strip_managed_tags(obj)
        self._resize(obj, len(built.points), len(built.polys))
        self._write_points_and_polys(obj, built.points, built.polys)

        poly_face_map = None
        if not built.poly_groups:
            if built.normal_data:
                self._write_normal_tag(obj, built.normal_data)
            poly_face_map = built.face_map

        phong = obj.GetTag(c4d.Tphong)
        if not phong:
            phong = obj.MakeTag(c4d.Tphong)
            phong[c4d.PHONGTAG_PHONG_ANGLE] = c4d.utils.DegToRad(_PHONG_ANGLE_DEG)
        phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = bool(built.poly_groups)

        obj.Message(c4d.MSG_UPDATE)
        return (built.poly_groups, built.corner_normals, built.poly_face_val,
                poly_face_map)

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

    @staticmethod
    def _write_normal_tag(obj, raw):
        """Insert a managed NormalTag holding ``raw`` (12 int16 per polygon).

        Fast path: copy into the tag's low-level buffer
        (``VariableTag.GetLowlevelDataAddressW``).  Fallback: the R26+
        high-level ``NormalTag.Set(data, i, {'a'..'d': Vector})``.
        (1.1.x called a non-existent ``NormalTag.SetPolygon`` first, so only
        its low-level path ever ran.)
        """
        poly_count = obj.GetPolygonCount()
        if poly_count == 0 or not raw:
            return None

        tag = obj.GetFirstTag()
        while tag:
            next_tag = tag.GetNext()
            if tag.CheckType(c4d.Tnormal) and tag.GetName() == MANAGED_NORMAL_TAG_NAME:
                tag.Remove()
            tag = next_tag

        tag = c4d.NormalTag(poly_count)
        tag.SetName(MANAGED_NORMAL_TAG_NAME)
        obj.InsertTag(tag)

        try:
            if _write_lowlevel(tag.GetLowlevelDataAddressW(), raw):
                return tag
        except (AttributeError, TypeError, ValueError) as e:
            print(f"[Plasticity] Low-level normal write failed ({e}); "
                  f"using NormalTag.Set")

        try:
            data_w = tag.GetDataAddressW()
            for i, (na, nb, nc, nd) in enumerate(geo.unpack_normals(raw)):
                c4d.NormalTag.Set(data_w, i, {
                    'a': c4d.Vector(*na), 'b': c4d.Vector(*nb),
                    'c': c4d.Vector(*nc), 'd': c4d.Vector(*nd)})
        except Exception as e:
            print(f"[Plasticity] Could not write normals: {e}")
        return tag

    @staticmethod
    def _apply_normals_from_corner_map(obj, corner_normals, post_melt_face_map):
        """Write per-corner normals AFTER N-gon melt."""
        if obj.GetPolygonCount() == 0 or not corner_normals:
            return
        raw = geo.pack_corner_normals(_poly_tuples(obj), post_melt_face_map,
                                      corner_normals)
        SceneHandler._write_normal_tag(obj, raw)

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

        pre_polys = _poly_tuples(obj)
        identity_to_group = {}
        for orig_pid, id_key in enumerate(pre_polys):
            if orig_pid < len(poly_face_val):
                identity_to_group[id_key] = poly_face_val[orig_pid]

        batches = geo.melt_batches(groups_to_melt, pre_polys)

        # MELT works on (and replaces) the polygon selection: keep the
        # user's selection and put it back afterwards.
        sel = obj.GetPolygonS()
        saved = [i for i, s in enumerate(sel.GetAll(len(pre_polys))) if s]

        try:
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
                        if 0 <= orig_pid < len(pre_polys):
                            idx = inverted.get(pre_polys[orig_pid])
                            if idx is not None:
                                real_indices.append(idx)

                if len(real_indices) < 2:
                    continue

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
                    print(f"[Plasticity] Warning: MCOMMAND_MELT failed for "
                          f"batch of {len(batch)} groups")
        finally:
            sel.DeselectAll()
            count = obj.GetPolygonCount()
            for i in saved:
                if i < count:
                    sel.Select(i)

        post_polys = _poly_tuples(obj)
        vert_groups = (geo.vertex_groups_from_corner_normals(corner_normals)
                       if corner_normals
                       else geo.vertex_groups(pre_polys, poly_face_val))
        post_melt_face_map = geo.post_melt_face_map(
            post_polys, identity_to_group, vert_groups)

        if corner_normals:
            self._write_normal_tag(obj, geo.pack_corner_normals(
                post_polys, post_melt_face_map, corner_normals))
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
            bc.SetString(BC_PLASTICITY_GROUPS,   json.dumps(list(groups)))
        if face_ids is not None:
            bc.SetString(BC_PLASTICITY_FACE_IDS, json.dumps(list(face_ids)))
        if poly_face_map is not None:
            bc.SetString(BC_PLASTICITY_POLY_FACE_MAP,
                         geo.encode_poly_face_map(poly_face_map))

    @staticmethod
    def _store_poly_face_map(obj, poly_face_map):
        bc = obj.GetDataInstance()
        bc.SetString(BC_PLASTICITY_POLY_FACE_MAP,
                     geo.encode_poly_face_map(poly_face_map))

    # =========================================================================
    # Scene-tree helpers
    # =========================================================================

    def _get_or_create_root(self, doc, filename):
        """Get or create the root Null for a Plasticity filename."""
        root = self._roots.get(filename)
        if root is None or root.GetDocument() != doc:
            root = self._find_root_recursive(doc.GetFirstObject(), doc, filename)
            if root is None:
                display_name = f"Plasticity: {filename}" if filename else "Plasticity"
                root = c4d.BaseObject(c4d.Onull)
                root.SetName(display_name)
                rbc = root.GetDataInstance()
                rbc.SetBool(BC_PLASTICITY_ROOT, True)
                rbc.SetString(BC_PLASTICITY_FILENAME, filename)
                s = self.unit_scale
                root[c4d.ID_BASEOBJECT_SCALE] = c4d.Vector(s, s, s)
                doc.InsertObject(root)
                self._add_undo(doc, c4d.UNDOTYPE_NEWOBJ, root)
                # Outbox (first) and Inbox (second) under the new root
                self._create_outbox(doc, root)
                self._create_inbox(doc, root)
            self._roots[filename] = root
        self._apply_root_scale(doc, root)
        return root

    def _apply_root_scale(self, doc, root):
        s = self.unit_scale
        target = c4d.Vector(s, s, s)
        if root[c4d.ID_BASEOBJECT_SCALE] != target:
            self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, root)
            root[c4d.ID_BASEOBJECT_SCALE] = target

    def _create_outbox(self, doc, root):
        outbox = c4d.BaseObject(c4d.Onull)
        outbox.SetName("Outbox")
        outbox.GetDataInstance().SetBool(BC_PLASTICITY_OUTBOX, True)
        doc.InsertObject(outbox, parent=root)          # first child
        self._add_undo(doc, c4d.UNDOTYPE_NEWOBJ, outbox)
        return outbox

    def _create_inbox(self, doc, root):
        inbox = c4d.BaseObject(c4d.Onull)
        inbox.SetName("Inbox")
        inbox.GetDataInstance().SetBool(BC_PLASTICITY_INBOX, True)
        self._insert_last_child(doc, inbox, root)
        self._add_undo(doc, c4d.UNDOTYPE_NEWOBJ, inbox)
        return inbox

    @staticmethod
    def _find_flagged_child(root, flag):
        child = root.GetDown()
        while child:
            if child.GetDataInstance().GetBool(flag):
                return child
            child = child.GetNext()
        return None

    def _get_or_create_inbox(self, doc, filename, root=None):
        """Find or create the Inbox null under the root for this filename.

        Only the missing null is created (1.1.x recreated both, leaving a
        duplicate Inbox when the user had deleted the Outbox)."""
        if root is None:
            root = self._get_or_create_root(doc, filename)
        return (self._find_flagged_child(root, BC_PLASTICITY_INBOX)
                or self._create_inbox(doc, root))

    def _get_or_create_outbox(self, doc, filename, root=None):
        """Find or create the Outbox null under the root for this filename."""
        if root is None:
            root = self._get_or_create_root(doc, filename)
        return (self._find_flagged_child(root, BC_PLASTICITY_OUTBOX)
                or self._create_outbox(doc, root))

    def _find_root_recursive(self, obj, doc, filename):
        """Walk the full scene hierarchy to find a root null matching filename."""
        for root in self._iter_roots(obj):
            if root.GetDataInstance().GetString(BC_PLASTICITY_FILENAME, "") == filename:
                return root
        return None

    @staticmethod
    def _iter_roots(obj):
        """All Plasticity root nulls in the tree starting at ``obj``."""
        stack = [obj]
        while stack:
            o = stack.pop()
            while o:
                if (o.CheckType(c4d.Onull)
                        and o.GetDataInstance().GetBool(BC_PLASTICITY_ROOT)):
                    yield o
                down = o.GetDown()
                if down:
                    stack.append(o.GetNext())
                    o = down
                else:
                    o = o.GetNext()

    def _delete_item(self, doc, filename, obj_id):
        key = (filename, obj_id)
        obj = self._items.pop(key, None)
        self._pending_auto_refacet.pop(key, None)
        if obj and obj.GetDocument() == doc:
            self._add_undo(doc, c4d.UNDOTYPE_DELETEOBJ, obj)
            obj.Remove()

    @staticmethod
    def _insert_last_child(doc, obj, parent):
        """Append obj as last child of parent (C++-side, instead of walking
        all siblings in Python for every insertion)."""
        try:
            obj.InsertUnderLast(parent)
            return
        except AttributeError:
            pass
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
    # Outbox data gathering for PutSome
    # =========================================================================

    def get_outbox_data(self, filename, only_visible=False):
        """Walk the Outbox and gather SDS mesh data for PUT_SOME_1.

        In Cinema 4D a Subdivision Surface (c4d.Osds) is a generator that
        subdivides its FIRST child object, including that child's whole
        hierarchy (C4D help, "Subdivision Surface").  That child — a polygon
        object, a parametric primitive, any generator (Symmetry, Boole,
        Loft, Sweep, Cloner …) or a null holding several of them — is the
        control cage that is uploaded as a SubD.

        Nulls in the Outbox become groups.  Every other object type placed
        directly in the Outbox is skipped with a warning.

        Returns {"groups": [...], "items": [...]}.
        """
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return {"groups": [], "items": []}

        groups = []
        items  = []
        skipped = []
        seen_coll = set()
        seen_pns = set()
        self._upload_filename = filename

        with self._undo_step(doc):
            outbox = self._get_or_create_outbox(doc, filename)
            root = outbox.GetUp()
            root_inv = ~root.GetMg() if root is not None else c4d.Matrix()

            def gather(parent, parent_collection_id=""):
                child = parent.GetDown()
                while child:
                    if only_visible and not self._is_visible(doc, child):
                        child = child.GetNext()
                        continue

                    bc = child.GetDataInstance()

                    if child.CheckType(c4d.Onull):
                        coll_id = bc.GetString(BC_PLASTICITY_COLLECTION_ID, "")
                        if not coll_id or coll_id in seen_coll:
                            # New null, or a copy of an uploaded one: give it
                            # its own identity.
                            self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, child)
                            if coll_id:
                                bc.SetInt32(BC_PLASTICITY_GROUP_ID, 0)
                            coll_id = uuid.uuid4().hex
                            bc.SetString(BC_PLASTICITY_COLLECTION_ID, coll_id)
                        seen_coll.add(coll_id)

                        groups.append({
                            "c4d_collection_id": coll_id,
                            "name": child.GetName(),
                            "parent_c4d_collection_id": parent_collection_id,
                            "existing_group_id": bc.GetInt32(
                                BC_PLASTICITY_GROUP_ID, 0),
                        })
                        gather(child, coll_id)

                    elif child.CheckType(c4d.Osds):
                        item_data = self._extract_sds_item(
                            child, parent_collection_id, root_inv, seen_pns)
                        if item_data:
                            items.append(item_data)
                        else:
                            skipped.append(child.GetName())

                    else:
                        skipped.append(child.GetName())
                        print(f"[Plasticity] Warning: '{child.GetName()}' in the "
                              f"Outbox is not a Subdivision Surface or a Null "
                              f"— skipped.  Put it under a Subdivision Surface "
                              f"to upload it.")

                    child = child.GetNext()

            gather(outbox)

        if skipped:
            self.bridge.status_message = (
                f"Warning: {len(skipped)} Outbox object(s) skipped — "
                f"see the Console")
        return {"groups": groups, "items": items}

    def _is_visible(self, doc, obj):
        """Effective editor visibility: the nearest explicit ON/OFF dot up the
        hierarchy wins, undefined defers to the parent (GetEditorMode docs);
        a layer with "view" disabled also hides the object."""
        modes = []
        o = obj
        while o is not None:
            modes.append(o.GetEditorMode())
            o = o.GetUp()
        if not geo.effective_editor_visibility(modes, c4d.MODE_ON, c4d.MODE_OFF):
            return False
        try:
            layer_data = obj.GetLayerData(doc)
            if layer_data and not layer_data.get("view", True):
                return False
        except (AttributeError, TypeError):
            pass
        return True

    # =========================================================================
    # Control-cage resolution
    # =========================================================================

    def _resolve_control_cage(self, sds_obj):
        """Resolve an SDS's control cage into polygon sources.

        Returns ``(sources, keepalive)``: ``sources`` is a list of
        ``(PolygonObject, matrix)`` pairs where ``matrix`` maps the
        polygon's points into the SDS's local space; ``keepalive`` holds the
        temporary document (if one was needed) and must stay referenced
        until the points have been read.

        Primary: walk the caches Cinema 4D has already built for the first
        child's hierarchy.  Fallback: clone the child into a temporary
        document, build its caches with ExecutePasses, and as a last resort
        run MCOMMAND_CURRENTSTATETOOBJECT (whose result list is used — 1.1.x
        ignored it).  No MCOMMAND_JOIN: it only joins the children of an
        object passed to it, so the 1.1.x call never merged anything; parts
        are merged in Python instead.
        """
        first_child = sds_obj.GetDown()
        if first_child is None:
            return [], None
        if first_child.GetNext() is not None:
            print(f"[Plasticity] Note: SDS '{sds_obj.GetName()}' has several "
                  f"children; Cinema 4D only subdivides the first one "
                  f"('{first_child.GetName()}'), so only it is uploaded.")

        sources = []
        self._collect_cage_sources(first_child, c4d.Matrix(), sources)
        if sources:
            return sources, None

        # ── Fallback: evaluate a clone in a temporary document ───────────
        temp_doc = c4d.documents.BaseDocument()
        clone = first_child.GetClone(c4d.COPYFLAGS_NONE)
        if clone is None:
            return [], None
        temp_doc.InsertObject(clone)       # keeps its SDS-relative Ml
        temp_doc.ExecutePasses(None, True, True, True, 0)
        self._collect_cage_sources(clone, c4d.Matrix(), sources)
        if sources:
            return sources, temp_doc

        result = c4d.utils.SendModelingCommand(
            command=c4d.MCOMMAND_CURRENTSTATETOOBJECT,
            list=[clone],
            mode=c4d.MODELINGCOMMANDMODE_ALL,
            bc=c4d.BaseContainer(),
            doc=temp_doc,
        )
        if isinstance(result, list):
            result = result[0] if result else None
        if isinstance(result, c4d.BaseObject):
            # CSTO returns a new object carrying the input's matrix.
            self._collect_cage_sources(result, c4d.Matrix(), sources)
            return sources, (temp_doc, result)

        print(f"[Plasticity] CSTO failed for SDS '{sds_obj.GetName()}'")
        return [], None

    @staticmethod
    def _consumes_children(obj):
        """True for generators that use their children as input (Symmetry,
        Boole, Loft, Cloner …): their cache already contains the children."""
        try:
            return bool(obj.GetInfo() & c4d.OBJECT_INPUT)
        except (AttributeError, TypeError):
            return True   # 1.1.x never walked children of cached generators

    def _collect_cage_sources(self, obj, parent_mat, out, depth=0):
        """Collect polygon objects of ``obj``'s hierarchy with their matrices.

        Order follows Maxon's cache recursion (BaseObject.GetCache docs):
          1. deform cache — the host's deformed points, in the host's space
             (so the host's matrix is used, not the cache's own);
          2. a polygon object itself;
          3. a generator's cache — the cache root's matrix is relative to
             the generator, nested cache objects relative to their parent
             (GetMg() is not valid on cache objects, so the chain is built
             explicitly);
          4. children, unless a generator consumed them as input.
        """
        if depth > 256:
            return
        mat = parent_mat * obj.GetMl()

        deform_cache = obj.GetDeformCache()
        if deform_cache is not None:
            if (deform_cache.CheckType(c4d.Opolygon)
                    and deform_cache.GetPolygonCount() > 0):
                out.append((deform_cache, mat))
        elif obj.CheckType(c4d.Opolygon):
            if obj.GetPointCount() > 0 and obj.GetPolygonCount() > 0:
                out.append((obj, mat))
        else:
            cache = obj.GetCache()
            if cache is not None:
                node = cache
                while node:
                    self._collect_cage_sources(node, mat, out, depth + 1)
                    node = node.GetNext()
                if self._consumes_children(obj):
                    return

        child = obj.GetDown()
        while child:
            self._collect_cage_sources(child, mat, out, depth + 1)
            child = child.GetNext()

    @staticmethod
    def _ngon_groups(poly_obj, poly_count):
        """Polygon index groups, one per visible face (N-gon or polygon)."""
        try:
            _count, polymap = poly_obj.GetPolygonTranslationMap()
        except Exception:
            polymap = None
        return geo.groups_from_polymap(polymap, poly_count)

    # =========================================================================
    # SDS item extraction
    # =========================================================================

    def _extract_sds_item(self, sds_obj, parent_collection_id, root_inv=None,
                          seen_ids=None):
        """Build the PUT_SOME item for one Subdivision Surface.

        Points are baked through the full matrix chain (part → SDS → world)
        and expressed relative to the Plasticity root null, which inverts
        the unit scale and any root transform; then scaled back to metres
        and axis-swapped.  N-gons are exported as single faces.

        Returns a dict ready for encode_put_some(), or None on failure.
        """
        sources, keepalive = self._resolve_control_cage(sds_obj)
        if not sources:
            print(f"[Plasticity] Warning: SDS '{sds_obj.GetName()}' "
                  f"could not be resolved to a polygon mesh — skipping")
            return None

        if root_inv is None:
            root_inv = c4d.Matrix()
        base = root_inv * sds_obj.GetMg()

        parts = []
        fragmented = 0
        for poly_obj, mat in sources:
            m = base * mat
            points = [m * p for p in poly_obj.GetAllPoints()]
            polys = _poly_tuples(poly_obj)
            faces, frag = geo.faces_from_groups(
                polys, self._ngon_groups(poly_obj, len(polys)))
            fragmented += frag
            parts.append((geo.export_positions(points), faces))
        del keepalive

        if fragmented:
            print(f"[Plasticity] Warning: {fragmented} N-gon(s) on "
                  f"'{sds_obj.GetName()}' have holes or touch themselves and "
                  f"were exported as their triangle/quad pieces")

        positions, indices, sizes = geo.merge_mesh_parts(parts)
        if not sizes:
            return None

        # Persistent ID — always stored on the SDS so it survives even when
        # the control cage is rebuilt from generators on every export.
        # A copy of an uploaded SDS carries the original's IDs: give it its
        # own so both are uploaded as separate objects.
        sds_bc = sds_obj.GetDataInstance()
        pns_id = sds_bc.GetString(BC_PLASTICITY_PNS_ID, "")
        if not pns_id or (seen_ids is not None and pns_id in seen_ids):
            doc = sds_obj.GetDocument()
            if doc is not None:
                self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SMALL, sds_obj)
            if pns_id:
                sds_bc.SetInt32(BC_PLASTICITY_ID, 0)
            pns_id = uuid.uuid4().hex
            sds_bc.SetString(BC_PLASTICITY_PNS_ID, pns_id)
        if seen_ids is not None:
            seen_ids.add(pns_id)

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
        """Update unit scale on all Plasticity root nulls in the document."""
        self.unit_scale = max(0.0001, float(scale))
        doc = c4d.documents.GetActiveDocument()
        if not doc:
            return
        with self._undo_step(doc):
            for root in list(self._iter_roots(doc.GetFirstObject())):
                self._apply_root_scale(doc, root)
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

        with self._undo_step(doc):
            for obj in selection:
                if self._apply_face_sel_tags_to_obj(doc, obj):
                    affected += 1

        c4d.EventAdd()
        return affected > 0

    def _apply_face_sel_tags_to_obj(self, doc, obj):
        result = _get_poly_face_map(obj)
        if result is None:
            return False
        poly_face_map, face_ids, poly_count = result

        self._add_undo(doc, c4d.UNDOTYPE_CHANGE, obj)
        tag = obj.GetFirstTag()
        while tag:
            next_t = tag.GetNext()
            if (tag.CheckType(c4d.Tpolygonselection)
                    and tag.GetName().startswith(MANAGED_FACE_SEL_PREFIX)):
                tag.Remove()
            tag = next_t

        n_groups  = len(face_ids)
        tag_sels  = []
        for group_idx in range(n_groups):
            sel_tag = obj.MakeTag(c4d.Tpolygonselection)
            sel_tag.SetName(f"{MANAGED_FACE_SEL_PREFIX}{group_idx}")
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

        with self._undo_step(doc):
            for obj in selection:
                if self._apply_edge_sel_tags_to_obj(doc, obj):
                    affected += 1

        c4d.EventAdd()
        return affected > 0

    def _apply_edge_sel_tags_to_obj(self, doc, obj):
        result = _get_poly_face_map(obj)
        if result is None:
            return False
        poly_face_map, face_ids, poly_count = result

        self._add_undo(doc, c4d.UNDOTYPE_CHANGE, obj)
        tag = obj.GetFirstTag()
        while tag:
            next_t = tag.GetNext()
            if (tag.CheckType(c4d.Tedgeselection)
                    and tag.GetName() == MANAGED_EDGES_TAG_NAME):
                tag.Remove()
            tag = next_t

        edge_map = _build_edge_map(obj, poly_face_map, poly_count)

        sel_tag = obj.MakeTag(c4d.Tedgeselection)
        sel_tag.SetName(MANAGED_EDGES_TAG_NAME)
        bs = sel_tag.GetBaseSelect()

        for poly_idx, slot in geo.boundary_edge_addresses(edge_map):
            bs.Select(poly_idx * 4 + slot)

        obj.Message(c4d.MSG_UPDATE)
        return True

    # -------------------------------------------------------------------------
    # Shared sharp-edge helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _get_sharp_edge_addresses(obj, angle_deg=0.0):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return []
        poly_face_map, poly_count = result

        polys = _poly_tuples(obj)
        edge_map = geo.build_edge_map(polys, poly_face_map, poly_count)
        cos_threshold = None
        points = None
        if angle_deg > 0.0:
            cos_threshold = math.cos(math.radians(angle_deg))
            points = [(p.x, p.y, p.z) for p in obj.GetAllPoints()]
        return geo.sharp_edge_addresses(edge_map, polys, points, cos_threshold)

    # -------------------------------------------------------------------------
    # Select Sharp Edges
    # -------------------------------------------------------------------------

    def select_sharp_edges(self, doc, angle_deg=0.0):
        """Select Plasticity boundary edges, optionally filtered by angle."""
        if not doc:
            return False

        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
        affected = 0

        with self._undo_step(doc):
            for obj in selection:
                if self._select_sharp_edges_on_obj(doc, obj, angle_deg):
                    affected += 1

        if affected:
            doc.SetMode(c4d.Medges)
            c4d.EventAdd()

        return affected > 0

    def _select_sharp_edges_on_obj(self, doc, obj, angle_deg):
        if not obj.CheckType(c4d.Opolygon):
            return False

        addresses = self._get_sharp_edge_addresses(obj, angle_deg)
        if not addresses:
            return False

        self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SELECTION, obj)
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

        with self._undo_step(doc):
            for obj in selection:
                if self._select_plasticity_faces_on_obj(doc, obj):
                    affected += 1

        c4d.EventAdd()
        return affected > 0

    def _select_plasticity_faces_on_obj(self, doc, obj):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return False
        poly_face_map, poly_count = result

        poly_sel = obj.GetPolygonS()
        map_len  = min(len(poly_face_map), poly_count)
        pfm = poly_face_map[:map_len]

        selected = [i for i, s in enumerate(poly_sel.GetAll(map_len)) if s]
        groups, polys = geo.expand_to_groups(pfm, selected)
        if not groups:
            return False

        self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SELECTION, obj)
        for pi in polys:
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

        with self._undo_step(doc):
            for obj in selection:
                if self._select_plasticity_edges_on_obj(doc, obj):
                    affected += 1

        if affected:
            doc.SetMode(c4d.Medges)
            c4d.EventAdd()

        return affected > 0

    def _select_plasticity_edges_on_obj(self, doc, obj):
        result = _get_poly_face_map_only(obj)
        if result is None:
            return False
        poly_face_map, poly_count = result

        poly_sel = obj.GetPolygonS()
        map_len  = min(len(poly_face_map), poly_count)

        selected_groups = {poly_face_map[i] for i, s in
                           enumerate(poly_sel.GetAll(map_len)) if s}
        if not selected_groups:
            return False

        edge_map = _build_edge_map(obj, poly_face_map, poly_count)

        self._add_undo(doc, c4d.UNDOTYPE_CHANGE_SELECTION, obj)
        edge_sel = obj.GetEdgeS()
        edge_sel.DeselectAll()
        for poly_idx, slot in geo.perimeter_edge_addresses(edge_map,
                                                           selected_groups):
            edge_sel.Select(poly_idx * 4 + slot)

        obj.Message(c4d.MSG_UPDATE)
        return True
