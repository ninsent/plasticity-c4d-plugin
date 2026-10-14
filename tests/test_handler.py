"""
SceneHandler against the c4d stub: hierarchy sync, undo, Auto-Refacet,
N-gon melt, Outbox protection, utilities and 1.1.x scene compatibility.
"""

import json

import c4d
import pytest

from core import geometry as g
from core import handler as H
from core.protocol import MessageParser
from core.refacet_tag import TAG_PLUGIN_ID, PREF_TOPOLOGY, PREF_OPTIONS_MODE, \
    PREF_TOLERANCE, PREF_ANGLE, PREF_MIN_WIDTH, PREF_MAX_WIDTH
from core.threading_bridge import BridgeEvent, EventType, ThreadingBridge
from meshgen import ngon_grid, tri_grid
import wire as W

FILE = "part.plasticity"
parser = MessageParser()


class FakeClient:
    def __init__(self, bridge):
        self.bridge = bridge
        bridge.connected = True
        self.refacets = []
        self.on_list_complete = None
        self.supported_messages = set()
        self.refacet_ok = True

    @property
    def connected(self):
        return self.bridge.connected

    def refacet_some(self, filename, plasticity_ids, **kw):
        self.refacets.append((filename, list(plasticity_ids), kw))
        return len(self.refacets) if self.refacet_ok else None


@pytest.fixture
def env(doc):
    bridge = ThreadingBridge()
    handler = H.SceneHandler(bridge)
    client = FakeClient(bridge)
    handler.client = client
    return handler, bridge, client, doc


def send(bridge, et, data):
    bridge.push_event(BridgeEvent(et, data=data))
    bridge.process_pending_events(time_budget=None)


def tri_obj(obj_id, name="Solid", nx=2, ny=2, parent=0, flags=6, seed=0):
    m = tri_grid(nx, ny, rows_per_group=1, seed=seed)
    return W.mesh_object(obj_id, name, m["vertices"], m["faces"], m["normals"],
                         m["groups"], m["face_ids"], parent_id=parent,
                         flags=flags), m


def list_data(objs, filename=FILE):
    return parser.parse_message(W.list_response(filename, [W.item(1, objs)]))


def tx_data(items, filename=FILE):
    return parser.parse_message(W.transaction(filename, items))


def root_of(doc):
    return next(o for o in doc.GetObjects()
                if o.GetDataInstance().GetBool(H.BC_PLASTICITY_ROOT))


def children(o):
    return o.GetChildren()


def inbox_of(doc):
    return [c for c in children(root_of(doc))
            if c.GetDataInstance().GetBool(H.BC_PLASTICITY_INBOX)][0]


def outbox_of(doc):
    return [c for c in children(root_of(doc))
            if c.GetDataInstance().GetBool(H.BC_PLASTICITY_OUTBOX)][0]


# =============================================================================
# Import / hierarchy
# =============================================================================

def test_list_builds_root_outbox_inbox_and_objects(env):
    h, bridge, client, doc = env
    grp = W.group_object(10, "Group A")
    solid, m = tri_obj(11, "Solid 1", parent=10)
    sheet, _ = tri_obj(12, "Sheet", seed=1)
    send(bridge, EventType.LIST_RESPONSE, list_data([grp, solid, sheet]))

    root = root_of(doc)
    assert root.GetName() == f"Plasticity: {FILE}"
    assert [c.GetName() for c in children(root)] == ["Outbox", "Inbox"]
    inbox = inbox_of(doc)
    assert [c.GetName() for c in children(inbox)] == ["Group A", "Sheet"]
    s1 = children(children(inbox)[0])[0]
    assert s1.GetName() == "Solid 1"
    bc = s1.GetDataInstance()
    assert bc.GetInt32(H.BC_PLASTICITY_ID) == 11
    assert bc.GetString(H.BC_PLASTICITY_FILENAME) == FILE
    assert json.loads(bc.GetString(H.BC_PLASTICITY_FACE_IDS)) == m["face_ids"]
    pfm = g.decode_poly_face_map(bc.GetString(H.BC_PLASTICITY_POLY_FACE_MAP))
    assert pfm == g.tri_face_map(s1.GetPolygonCount(), m["groups"])
    tags = {t.GetName() for t in s1.GetTags()}
    assert H.MANAGED_NORMAL_TAG_NAME in tags and s1.GetTag(c4d.Tphong)
    # geometry: axis swap + scale + reversed winding
    assert s1.GetPointCount() == len(m["vertices"]) // 3
    p0 = s1.GetPoint(0)
    assert (p0.x, p0.z, p0.y) == pytest.approx(
        [v * 100 for v in m["vertices"][:3]], rel=1e-6)
    a, b, c = m["faces"][:3]
    cp = s1.GetPolygon(0)
    assert (cp.a, cp.b, cp.c) == (a, c, b)


def test_points_written_in_bulk(env):
    h, bridge, client, doc = env
    solid, _ = tri_obj(1, nx=5, ny=5)
    send(bridge, EventType.LIST_RESPONSE, list_data([solid]))
    obj = children(inbox_of(doc))[0]
    assert obj.set_point_calls == 0          # SetAllPoints, not SetPoint


def test_normal_tag_contents(env):
    h, bridge, client, doc = env
    solid, m = tri_obj(1, nx=3, ny=2)
    send(bridge, EventType.LIST_RESPONSE, list_data([solid]))
    obj = children(inbox_of(doc))[0]
    tag = [t for t in obj.GetTags() if t.GetName() == H.MANAGED_NORMAL_TAG_NAME][0]
    mesh = g.build_tri_mesh(m["vertices"], m["faces"])
    import array
    normals = array.array("f", m["normals"]).tolist()    # wire is float32
    assert bytes(tag._buf) == g.pack_vertex_normals(normals, mesh.corners)


def test_in_place_update_keeps_identity_user_tags_and_strips_managed(env):
    h, bridge, client, doc = env
    solid, _ = tri_obj(1)
    send(bridge, EventType.LIST_RESPONSE, list_data([solid]))
    obj = children(inbox_of(doc))[0]
    user_tag = obj.MakeTag(c4d.Tpolygonselection)
    user_tag.SetName("My Selection")
    stale = obj.MakeTag(c4d.Tpolygonselection)
    stale.SetName(H.MANAGED_FACE_SEL_PREFIX + "0")
    edges = obj.MakeTag(c4d.Tedgeselection)
    edges.SetName(H.MANAGED_EDGES_TAG_NAME)

    solid2, _ = tri_obj(1, "Renamed", nx=4, ny=3, seed=5)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [solid2])]))
    obj2 = children(inbox_of(doc))[0]
    assert obj2 is obj and obj.GetName() == "Renamed"
    names = [t.GetName() for t in obj.GetTags()]
    assert "My Selection" in names
    assert H.MANAGED_FACE_SEL_PREFIX + "0" not in names
    assert H.MANAGED_EDGES_TAG_NAME not in names
    assert obj.GetPolygonCount() == 4 * 3 * 2


def test_stale_items_and_groups_deleted_on_refresh(env):
    h, bridge, client, doc = env
    grp = W.group_object(5, "G")
    a, _ = tri_obj(1, "A")
    b, _ = tri_obj(2, "B", parent=5)
    send(bridge, EventType.LIST_RESPONSE, list_data([grp, a, b]))
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    assert [c.GetName() for c in children(inbox_of(doc))] == ["A"]


def test_transaction_delete(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    b, _ = tri_obj(2, "B")
    send(bridge, EventType.LIST_RESPONSE, list_data([a, b]))
    send(bridge, EventType.TRANSACTION, tx_data([W.delete_item([1, 99])]))
    assert [c.GetName() for c in children(inbox_of(doc))] == ["B"]


def test_user_reparenting_inside_inbox_is_preserved(env):
    """1.1.6 behaviour: an object the user put under their own generator or
    null inside the Inbox stays there and keeps syncing in place."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    user_gen = c4d.BaseObject(c4d.Osds)
    doc.InsertObject(user_gen, parent=inbox_of(doc))
    obj.InsertUnder(user_gen)
    a2, _ = tri_obj(1, "A", nx=3)
    send(bridge, EventType.LIST_RESPONSE, list_data([a2]))
    assert obj.GetUp() is user_gen
    assert children(inbox_of(doc)) == [user_gen]
    assert obj.GetPolygonCount() == 3 * 2 * 2


def test_object_moved_out_of_plasticity_root_is_detached(env):
    """Like the Blender bridge, only objects inside the Inbox are synced: one
    moved out of the Plasticity hierarchy becomes a frozen copy and a fresh
    object is imported."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    user_null = c4d.BaseObject(c4d.Onull)
    doc.InsertObject(user_null)
    obj.InsertUnder(user_null)
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    assert obj.GetUp() is user_null
    fresh = children(inbox_of(doc))
    assert len(fresh) == 1 and fresh[0] is not obj


def test_visibility_applied_only_when_flags_change(env):
    h, bridge, client, doc = env
    hidden, _ = tri_obj(1, "A", flags=7)            # hidden bit
    send(bridge, EventType.LIST_RESPONSE, list_data([hidden]))
    obj = children(inbox_of(doc))[0]
    assert obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] == c4d.OBJECT_OFF
    obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] = c4d.OBJECT_ON   # user override
    send(bridge, EventType.LIST_RESPONSE, list_data([hidden]))
    assert obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] == c4d.OBJECT_ON
    shown, _ = tri_obj(1, "A", flags=6)
    send(bridge, EventType.LIST_RESPONSE, list_data([shown]))
    assert obj[c4d.ID_BASEOBJECT_VISIBILITY_EDITOR] == c4d.OBJECT_UNDEF


def test_invalid_indices_do_not_crash_and_are_dropped(env, capsys):
    h, bridge, client, doc = env
    bad = W.mesh_object(1, "Bad", [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                        [0, 1, 2, 0, 1, 50])
    send(bridge, EventType.LIST_RESPONSE, list_data([bad]))
    obj = children(inbox_of(doc))[0]
    assert obj.GetPolygonCount() == 1
    assert "Skipped 1 invalid polygon" in capsys.readouterr().out


def test_empty_update_keeps_metadata(env):
    h, bridge, client, doc = env
    a, m = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    before = obj.GetDataInstance().GetString(H.BC_PLASTICITY_GROUPS)
    degenerate = W.mesh_object(1, "A", [0.0] * 9, [0, 1, 50], groups=[0, 3, 0, 3],
                               face_ids=[1, 2])
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [degenerate])]))
    assert obj.GetDataInstance().GetString(H.BC_PLASTICITY_GROUPS) == before


# =============================================================================
# Undo
# =============================================================================

def test_one_undo_step_per_update(env):
    h, bridge, client, doc = env
    grp = W.group_object(5, "G")
    a, _ = tri_obj(1, "A", parent=5)
    b, _ = tri_obj(2, "B")
    send(bridge, EventType.LIST_RESPONSE, list_data([grp, a, b]))
    assert len(doc.undo_steps) == 1
    a2, _ = tri_obj(1, "A", nx=3, parent=5)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [a2]),
                                                  W.delete_item([2])]))
    assert len(doc.undo_steps) == 2
    kinds = [k for k, _ in doc.undo_steps[-1]]
    assert c4d.UNDOTYPE_DELETEOBJ in kinds and c4d.UNDOTYPE_CHANGE in kinds


def test_no_empty_undo_steps(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    n = len(doc.undo_steps)
    send(bridge, EventType.TRANSACTION, tx_data([W.delete_item([999])]))
    send(bridge, EventType.NEW_VERSION, {"filename": FILE, "version": 2})
    assert len(doc.undo_steps) == n


def test_ngon_melt_happens_inside_the_same_undo_step(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    n = len(doc.undo_steps)
    m = ngon_grid(6, 4, seed=2)
    obj_data = parser.parse_message(W.refacet_response(FILE, [dict(
        id=1, faces=m["faces"], vertices=m["vertices"], indices=m["indices"],
        normals=m["normals"], groups=m["groups"], face_ids=m["face_ids"])]))
    send(bridge, EventType.REFACET_RESPONSE, obj_data)
    assert len(doc.undo_steps) == n + 1
    assert doc._undo_depth == 0


# =============================================================================
# Refacet / N-gons
# =============================================================================

def _refacet(bridge, obj_id, m, filename=FILE):
    data = parser.parse_message(W.refacet_response(filename, [dict(
        id=obj_id, faces=m["faces"], vertices=m["vertices"],
        indices=m["indices"], normals=m["normals"], groups=m["groups"],
        face_ids=m["face_ids"])]))
    send(bridge, EventType.REFACET_RESPONSE, data)


def test_refacet_error_does_not_touch_scene(env):
    h, bridge, client, doc = env
    send(bridge, EventType.REFACET_RESPONSE,
         parser.parse_message(W.refacet_response("x", [], code=500)))
    assert doc.GetFirstObject() is None
    assert "Refacet failed" in bridge.status_message


def test_list_error_does_not_touch_scene_and_clears_callback(env):
    h, bridge, client, doc = env
    client.on_list_complete = lambda fn: pytest.fail("must not run")
    send(bridge, EventType.LIST_RESPONSE,
         parser.parse_message(W.list_response("", [], code=500)))
    assert doc.GetFirstObject() is None
    assert client.on_list_complete is None
    assert "Refresh failed" in bridge.status_message


def test_quad_only_ngon_refacet_face_map(env):
    """F13: N-gon mode with only quads must map each quad to its own group
    (1.1.x used 3 loops per polygon)."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "Box")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    # 3 quads, one CAD face each: groups start at loops 0, 4, 8
    verts, idx, faces = [], [], []
    for q in range(3):
        for (x, y) in ((0, 0), (1, 0), (1, 1), (0, 1)):
            verts += [x + q, y, 0.0]
            idx.append(len(verts) // 3 - 1)
            faces.append(q)
    m = dict(vertices=verts, indices=idx, faces=faces, normals=[0.0, 0.0, 1.0] * 12,
             groups=[0, 4, 4, 4, 8, 4], face_ids=[7, 8, 9])
    _refacet(bridge, 1, m)
    obj = children(inbox_of(doc))[0]
    pfm = g.decode_poly_face_map(
        obj.GetDataInstance().GetString(H.BC_PLASTICITY_POLY_FACE_MAP))
    assert obj.GetPolygonCount() == 3 and pfm == [0, 1, 2]


def test_ngon_refacet_melts_and_maps_faces(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    m = ngon_grid(8, 6, seed=4)
    _refacet(bridge, 1, m)
    obj = children(inbox_of(doc))[0]
    n_ngons = sum(1 for p in set(m["faces"]) if m["faces"].count(p) > 4)
    assert obj.GetNgonCount() == n_ngons
    pfm = g.decode_poly_face_map(
        obj.GetDataInstance().GetString(H.BC_PLASTICITY_POLY_FACE_MAP))
    assert len(pfm) == obj.GetPolygonCount()
    assert set(pfm) <= set(range(len(m["face_ids"])))
    assert any(t.GetName() == H.MANAGED_NORMAL_TAG_NAME for t in obj.GetTags())
    melt_calls = [c for c in c4d.utils.modeling_calls if c[0] == c4d.MCOMMAND_MELT]
    assert melt_calls and all(c[2] == c4d.MODELINGCOMMANDFLAGS_NONE for c in melt_calls)


def test_melt_restores_polygon_selection(env):
    """F15: melting must not leave its working selection behind."""
    h, bridge, client, doc = env
    m = ngon_grid(6, 4, seed=1)
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    _refacet(bridge, 1, m)                  # first refacet (creates N-gons)
    sel = obj.GetPolygonS()
    sel.DeselectAll()
    sel.Select(0)
    sel.Select(2)
    _refacet(bridge, 1, m)                  # update again: melt runs again
    assert sorted(sel._sel) == [0, 2]


# =============================================================================
# Auto-Refacet
# =============================================================================

def _tag(obj):
    tag = c4d.BaseTag(TAG_PLUGIN_ID)
    tag[PREF_TOPOLOGY] = 0
    tag[PREF_OPTIONS_MODE] = 0
    tag[PREF_TOLERANCE] = 0.01
    tag[PREF_ANGLE] = 0.4
    tag[PREF_MIN_WIDTH] = 0.0
    tag[PREF_MAX_WIDTH] = 0.0
    obj.InsertTag(tag)
    return tag


def test_auto_refacet_skips_tri_rebuild(env):
    """F11: one tessellation and one undo step per Live Link update."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    _tag(obj)
    before = obj.GetAllPolygons()
    steps = len(doc.undo_steps)

    a2, _ = tri_obj(1, "A", nx=5, ny=5, seed=3)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [a2])]))
    assert [p for p in obj.GetAllPolygons()] == before      # untouched
    assert client.refacets and client.refacets[-1][1] == [1]
    assert len(doc.undo_steps) == steps                     # nothing changed yet

    _refacet(bridge, 1, ngon_grid(4, 4, seed=2))
    assert obj.GetAllPolygons() != before
    assert len(doc.undo_steps) == steps + 1
    assert h._pending_auto_refacet == {}


def test_auto_refacet_failure_applies_stashed_geometry(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    _tag(obj)
    a2, _ = tri_obj(1, "A", nx=5, ny=4, seed=3)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [a2])]))
    assert obj.GetPolygonCount() == 2 * 2 * 2
    send(bridge, EventType.REFACET_RESPONSE,
         parser.parse_message(W.refacet_response("", [], code=500)))
    assert obj.GetPolygonCount() == 5 * 4 * 2               # stashed tris
    assert h._pending_auto_refacet == {}


def test_auto_refacet_send_failure_applies_stash_immediately(env):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    _tag(obj)
    client.refacet_ok = False
    a2, _ = tri_obj(1, "A", nx=3, ny=3, seed=3)
    send(bridge, EventType.LIST_RESPONSE, list_data([a2]))
    assert obj.GetPolygonCount() == 3 * 3 * 2


def test_auto_refacet_batches_by_settings(env):
    h, bridge, client, doc = env
    objs = [tri_obj(i, f"S{i}", seed=i)[0] for i in (1, 2, 3)]
    send(bridge, EventType.LIST_RESPONSE, list_data(objs))
    for o in children(inbox_of(doc)):
        t = _tag(o)
        if o.GetName() == "S3":
            t[PREF_TOLERANCE] = 0.05
    client.refacets.clear()
    send(bridge, EventType.LIST_RESPONSE, list_data(objs))
    batches = sorted(sorted(ids) for _, ids, _ in client.refacets)
    assert batches == [[1, 2], [3]]


# =============================================================================
# Outbox protection / PUT_SOME
# =============================================================================

def _sds_in_outbox(doc, pid=0, gid=0):
    outbox = outbox_of(doc)
    sds = c4d.BaseObject(c4d.Osds)
    sds.SetName("Cage")
    doc.InsertObject(sds, parent=outbox)
    if pid:          # as stamped by a PUT_SOME response
        sds.GetDataInstance().SetInt32(H.BC_PLASTICITY_ID, pid)
        sds.GetDataInstance().SetString(H.BC_PLASTICITY_PNS_ID, f"pns-{pid}")
    if gid:
        null = c4d.BaseObject(c4d.Onull)
        null.GetDataInstance().SetInt32(H.BC_PLASTICITY_GROUP_ID, gid)
        null.GetDataInstance().SetString(H.BC_PLASTICITY_COLLECTION_ID, f"coll-{gid}")
        doc.InsertObject(null, parent=outbox)
    return sds


def test_outbox_ids_are_not_imported(env):
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    _sds_in_outbox(doc, pid=42)
    up, _ = tri_obj(42, "Uploaded")
    other, _ = tri_obj(43, "Other")
    send(bridge, EventType.LIST_RESPONSE, list_data([up, other]))
    assert [c.GetName() for c in children(inbox_of(doc))] == ["Other"]


def test_outbox_item_id_does_not_hide_inbox_group(env):
    """F19: item and group IDs overlap; an Outbox item with ID 7 must not
    swallow the Inbox group with ID 7 (and vice versa)."""
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    _sds_in_outbox(doc, pid=7, gid=8)
    grp = W.group_object(7, "Group 7")
    item8, _ = tri_obj(8, "Item 8", parent=7)
    send(bridge, EventType.LIST_RESPONSE, list_data([grp, item8]))
    inbox = inbox_of(doc)
    assert [c.GetName() for c in children(inbox)] == ["Group 7"]
    assert [c.GetName() for c in children(children(inbox)[0])] == ["Item 8"]


def test_inbox_mirror_of_uploaded_object_is_removed(env):
    """F20: a transaction can deliver an uploaded object before the
    PUT_SOME response; once the Outbox owns the ID the mirror goes away."""
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    sds = _sds_in_outbox(doc)
    sds.GetDataInstance().SetString(H.BC_PLASTICITY_PNS_ID, "pns-1")
    cage = c4d.PolygonObject(3, 1)
    cage.set_geometry([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)])
    cage.InsertUnder(sds)
    assert h.get_outbox_data(FILE)["items"][0]["c4d_id"] == "pns-1"   # upload
    mirror, _ = tri_obj(55, "Cage")
    send(bridge, EventType.TRANSACTION, tx_data([W.item(1, [mirror])]))
    assert [c.GetName() for c in children(inbox_of(doc))] == ["Cage"]
    send(bridge, EventType.PUT_SOME_RESPONSE, parser.parse_message(
        W.put_some_response(1, 200, [], [("pns-1", 55, 3)])))
    assert sds.GetDataInstance().GetInt32(H.BC_PLASTICITY_ID) == 55
    send(bridge, EventType.LIST_RESPONSE, list_data([mirror]))
    assert children(inbox_of(doc)) == []


def test_put_some_response_stamps_ids_and_groups(env):
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    sds = _sds_in_outbox(doc)
    sds.GetDataInstance().SetString(H.BC_PLASTICITY_PNS_ID, "abc")
    null = c4d.BaseObject(c4d.Onull)
    null.GetDataInstance().SetString(H.BC_PLASTICITY_COLLECTION_ID, "coll")
    doc.InsertObject(null, parent=outbox_of(doc))
    send(bridge, EventType.PUT_SOME_RESPONSE, parser.parse_message(
        W.put_some_response(1, 200, [("coll", 900)], [("abc", 901, 4)])))
    assert sds.GetDataInstance().GetInt32(H.BC_PLASTICITY_ID) == 901
    assert sds.GetDataInstance().GetInt32(H.BC_PLASTICITY_VERSION) == 4
    assert null.GetDataInstance().GetInt32(H.BC_PLASTICITY_GROUP_ID) == 900
    assert h._uploaded_for("") == ({901}, {900})    # no upload filename known
    send(bridge, EventType.PUT_SOME_RESPONSE, parser.parse_message(
        W.u32(31) + W.u32(2) + W.u32(500)))
    assert "Upload failed" in bridge.status_message


def test_deleting_outbox_does_not_duplicate_inbox(env):
    """F44: 1.1.x recreated both nulls when only the Outbox was missing."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    outbox_of(doc).Remove()
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    names = [c.GetName() for c in children(root_of(doc))]
    assert names == ["Outbox", "Inbox"]


# =============================================================================
# Scene compatibility / unit scale
# =============================================================================

def _scene_116(doc, legacy_flat=False):
    """A scene as 1.1.6 saved it (JSON poly-face map)."""
    root = c4d.BaseObject(c4d.Onull)
    root.SetName(f"Plasticity: {FILE}")
    root.GetDataInstance().SetBool(H.BC_PLASTICITY_ROOT, True)
    root.GetDataInstance().SetString(H.BC_PLASTICITY_FILENAME, FILE)
    doc.InsertObject(root)
    parent = root
    if not legacy_flat:
        outbox = c4d.BaseObject(c4d.Onull)
        outbox.SetName("Outbox")
        outbox.GetDataInstance().SetBool(H.BC_PLASTICITY_OUTBOX, True)
        inbox = c4d.BaseObject(c4d.Onull)
        inbox.SetName("Inbox")
        inbox.GetDataInstance().SetBool(H.BC_PLASTICITY_INBOX, True)
        doc.InsertObject(inbox, parent=root)
        doc.InsertObject(outbox, parent=root)
        parent = inbox
    mesh = g.build_tri_mesh([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0],
                            [0, 1, 2, 0, 2, 3], make_point=c4d.Vector)
    obj = c4d.PolygonObject(len(mesh.points), len(mesh.polys))
    obj.set_geometry(mesh.points, [c4d.CPolygon(*p) for p in mesh.polys])
    bc = obj.GetDataInstance()
    bc.SetInt32(H.BC_PLASTICITY_ID, 1)
    bc.SetString(H.BC_PLASTICITY_FILENAME, FILE)
    bc.SetString(H.BC_PLASTICITY_GROUPS, json.dumps([0, 3, 3, 3]))
    bc.SetString(H.BC_PLASTICITY_FACE_IDS, json.dumps([10, 11]))
    bc.SetString(H.BC_PLASTICITY_POLY_FACE_MAP, json.dumps([0, 1]))
    obj.SetName("Old Solid")
    doc.InsertObject(obj, parent=parent)
    return root, obj


def test_scene_saved_with_1_1_6_utilities_work(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc)
    doc._active = [obj]
    assert h.apply_face_selection_tags(doc)
    names = [t.GetName() for t in obj.GetTags()]
    assert "Plasticity Face 0" in names and "Plasticity Face 1" in names
    assert h.apply_edge_selection_tags(doc)
    obj.GetPolygonS().Select(0)
    assert h.select_plasticity_faces(doc)
    assert sorted(obj.GetPolygonS()._sel) == [0]
    assert h.select_plasticity_edges(doc)
    assert doc.GetMode() == c4d.Medges and obj.GetEdgeS().GetCount() == 3
    assert h.select_sharp_edges(doc, 0.0)


def test_scene_saved_with_1_1_6_syncs_in_place(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc)
    a, _ = tri_obj(1, "Old Solid", nx=3, ny=3)
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    assert children(inbox_of(doc)) == [obj]
    assert obj.GetPolygonCount() == 18
    s = obj.GetDataInstance().GetString(H.BC_PLASTICITY_POLY_FACE_MAP)
    assert s.startswith('{"v":1')


def test_legacy_flat_scene_is_migrated_into_inbox(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc, legacy_flat=True)
    a, _ = tri_obj(1, "Old Solid")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    assert obj.GetUp() is inbox_of(doc)
    assert [c.GetName() for c in children(root)] == ["Outbox", "Inbox"]


def test_unit_scale_updates_all_roots(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc)          # handler never saw this root
    h.update_unit_scale(2.5)
    assert root[c4d.ID_BASEOBJECT_SCALE] == c4d.Vector(2.5, 2.5, 2.5)
    assert len(doc.undo_steps) == 1


def test_corrupt_poly_face_map_does_not_raise(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc)
    obj.GetDataInstance().SetString(H.BC_PLASTICITY_POLY_FACE_MAP, "{broken")
    doc._active = [obj]
    assert h.apply_face_selection_tags(doc) is False
    assert h.select_sharp_edges(doc) is False


def test_selection_utilities_are_undoable(env):
    h, bridge, client, doc = env
    root, obj = _scene_116(doc)
    doc._active = [obj]
    obj.GetPolygonS().Select(1)
    h.select_plasticity_faces(doc)
    h.select_plasticity_edges(doc)
    h.select_sharp_edges(doc)
    kinds = [k for step in doc.undo_steps for k, _ in step]
    assert kinds.count(c4d.UNDOTYPE_CHANGE_SELECTION) == 3


def test_resize_falls_back_when_ncnt_is_rejected(env):
    """F31: the Python docs only list ResizeObject(pcnt[, vcnt])."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    real = obj.ResizeObject
    calls = []

    def two_args_only(pcnt, vcnt=-1, *extra):
        calls.append(len(extra))
        if extra:
            raise TypeError("ResizeObject() takes at most 2 arguments")
        return real(pcnt, vcnt)
    obj.ResizeObject = two_args_only
    a2, _ = tri_obj(1, "A", nx=4, ny=4)
    send(bridge, EventType.LIST_RESPONSE, list_data([a2]))
    assert calls == [1, 0] and obj.GetPolygonCount() == 32


def test_visibility_and_rename_are_recorded_in_undo(env):
    """F23: flag/name edits happen inside the update's undo step."""
    h, bridge, client, doc = env
    grp = W.group_object(5, "G")
    a, _ = tri_obj(1, "A", parent=5)
    send(bridge, EventType.LIST_RESPONSE, list_data([grp, a]))
    obj = children(children(inbox_of(doc))[0])[0]
    grp_obj = children(inbox_of(doc))[0]
    hidden, _ = tri_obj(1, "A", parent=5, flags=7)
    send(bridge, EventType.LIST_RESPONSE, list_data([W.group_object(5, "G2"), hidden]))
    step = doc.undo_steps[-1]
    assert (c4d.UNDOTYPE_CHANGE_SMALL, grp_obj) in step       # rename
    assert any(o is obj for _, o in step)                      # visibility
    assert grp_obj.GetName() == "G2"


def _sds_with_stamp(doc, pid, pns="pns"):
    sds = _sds_in_outbox(doc)
    bc = sds.GetDataInstance()
    bc.SetInt32(H.BC_PLASTICITY_ID, pid)
    bc.SetString(H.BC_PLASTICITY_PNS_ID, pns)
    return sds


def test_copied_inbox_mesh_used_as_cage_does_not_hide_original(env):
    """Review finding: a Ctrl+drag copy of an Inbox mesh used as an Outbox
    cage keeps the mesh's ID; it must neither hide nor delete the original."""
    h, bridge, client, doc = env
    body, _ = tri_obj(5, "Body")
    send(bridge, EventType.LIST_RESPONSE, list_data([body]))
    original = children(inbox_of(doc))[0]
    sds = _sds_in_outbox(doc)
    original.GetClone().InsertUnder(sds)          # cage copy, carries ID 5
    body2, _ = tri_obj(5, "Body", nx=3)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [body2])]))
    send(bridge, EventType.LIST_RESPONSE, list_data([body2]))
    assert children(inbox_of(doc)) == [original]
    assert original.GetPolygonCount() == 3 * 2 * 2


def test_uploaded_ids_are_tracked_per_plasticity_file(env):
    """Review finding: an upload to file A must not hide or delete object 7
    of file B (IDs are small per-file integers)."""
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    h._upload_filename = FILE
    send(bridge, EventType.PUT_SOME_RESPONSE, parser.parse_message(
        W.put_some_response(1, 200, [], [("x", 7, 1)])))
    other, _ = tri_obj(7, "B's object")
    send(bridge, EventType.LIST_RESPONSE, list_data([other], filename="b.plasticity"))
    roots = [o for o in doc.GetObjects()
             if o.GetDataInstance().GetString(H.BC_PLASTICITY_FILENAME) == "b.plasticity"]
    inbox_b = [c for c in roots[0].GetChildren()
               if c.GetDataInstance().GetBool(H.BC_PLASTICITY_INBOX)][0]
    assert [c.GetName() for c in inbox_b.GetChildren()] == ["B's object"]


def test_stamped_sds_id_is_protected_but_not_deleted(env):
    """IDs stamped on an Outbox SDS (e.g. from a previous session) keep the
    object out of the Inbox; only this session's uploads delete mirrors."""
    h, bridge, client, doc = env
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
    _sds_with_stamp(doc, 42)
    up, _ = tri_obj(42, "Uploaded")
    send(bridge, EventType.LIST_RESPONSE, list_data([up]))
    assert children(inbox_of(doc)) == []


def _tagged_pending(env, nx=5, ny=4):
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    obj = children(inbox_of(doc))[0]
    _tag(obj)
    a2, _ = tri_obj(1, "A", nx=nx, ny=ny, seed=3)
    send(bridge, EventType.TRANSACTION, tx_data([W.item(2, [a2])]))
    assert obj.GetPolygonCount() == 8 and h._pending_auto_refacet
    return obj


def test_refacet_send_failure_event_applies_stash(env):
    """Review finding 2a: an async refacet send failure / unreadable
    response arrives as OPERATION_FAILED and must not leave objects stale."""
    h, bridge, client, doc = env
    obj = _tagged_pending(env)
    from core.protocol import MessageType
    send(bridge, EventType.OPERATION_FAILED,
         {"operation": MessageType.REFACET_SOME_1})
    assert obj.GetPolygonCount() == 40 and not h._pending_auto_refacet


def test_disconnect_applies_stash(env):
    """Review finding 2b."""
    h, bridge, client, doc = env
    obj = _tagged_pending(env)
    send(bridge, EventType.DISCONNECTED, None)
    assert obj.GetPolygonCount() == 40 and not h._pending_auto_refacet


def test_unusable_refacet_item_falls_back_to_stash(env):
    """Review finding 2c: an empty refacet item must not discard the
    kept-aside update."""
    h, bridge, client, doc = env
    obj = _tagged_pending(env)
    empty = dict(id=1, faces=[], vertices=[], indices=[], normals=[],
                 groups=[], face_ids=[])
    send(bridge, EventType.REFACET_RESPONSE, parser.parse_message(
        W.refacet_response(FILE, [empty])))
    assert obj.GetPolygonCount() == 40 and not h._pending_auto_refacet


def test_upload_callback_error_does_not_block_auto_refacet(env):
    """Review finding 2d: refacet requests go out before the Outbox upload,
    and an upload exception is reported, not propagated."""
    h, bridge, client, doc = env
    a, _ = tri_obj(1, "A")
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    _tag(children(inbox_of(doc))[0])

    def boom(fn):
        raise RuntimeError("upload exploded")
    client.on_list_complete = boom
    client.refacets.clear()
    send(bridge, EventType.LIST_RESPONSE, list_data([a]))
    assert client.refacets and client.on_list_complete is None
    assert "upload exploded" in bridge.status_message


def test_list_operation_failed_clears_refresh_callback(env):
    """Review finding 4: a failed Refresh must not upload on a later list."""
    h, bridge, client, doc = env
    from core.protocol import MessageType
    client.on_list_complete = lambda fn: pytest.fail("stale upload")
    send(bridge, EventType.OPERATION_FAILED, {"operation": MessageType.LIST_ALL_1})
    assert client.on_list_complete is None
    send(bridge, EventType.LIST_RESPONSE, list_data([]))
