"""
Outbox export (PUT_SOME payload building) against the c4d stub.
"""

import array

import c4d
import pytest

from plasticity_bridge.core import geometry as g
from plasticity_bridge.core import handler as H
from plasticity_bridge.core.protocol import MessageParser, encode_put_some
from plasticity_bridge.core.threading_bridge import BridgeEvent, EventType, ThreadingBridge
from meshgen import ngon_grid, tri_grid
from reference import handler_v116 as ref
import wire as W

FILE = "part.plasticity"
V = c4d.Vector
parser = MessageParser()


@pytest.fixture
def env(doc):
    bridge = ThreadingBridge()
    h = H.SceneHandler(bridge)
    bridge.push_event(BridgeEvent(EventType.LIST_RESPONSE, data=parser.parse_message(
        W.list_response(FILE, []))))
    bridge.process_pending_events(time_budget=None)
    root = next(o for o in doc.GetObjects()
                if o.GetDataInstance().GetBool(H.BC_PLASTICITY_ROOT))
    outbox = [c for c in root.GetChildren()
              if c.GetDataInstance().GetBool(H.BC_PLASTICITY_OUTBOX)][0]
    return h, bridge, doc, root, outbox


def cube_poly(size=100.0, offset=(0.0, 0.0, 0.0)):
    """A closed quad cube (C4D winding: outward normals with CPolygon a,b,c,d)."""
    s = size / 2.0
    ox, oy, oz = offset
    pts = [(-s, -s, -s), (s, -s, -s), (s, s, -s), (-s, s, -s),
           (-s, -s, s), (s, -s, s), (s, s, s), (-s, s, s)]
    pts = [(x + ox, y + oy, z + oz) for x, y, z in pts]
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6),
             (1, 2, 6, 5), (0, 4, 7, 3)]
    obj = c4d.PolygonObject(8, 6)
    obj.set_geometry(pts, quads)
    return obj


def add_sds(doc, parent, name="Cage"):
    sds = c4d.BaseObject(c4d.Osds)
    sds.SetName(name)
    sds.InsertUnderLast(parent)
    return sds


def put(obj, parent, ml=None):
    obj.InsertUnderLast(parent)
    if ml is not None:
        obj.SetMl(ml)
    return obj


def positions3(item):
    p = item["positions"]
    return [tuple(p[i:i + 3]) for i in range(0, len(p), 3)]


def faces_of(item):
    out, k = [], 0
    for s in item["sizes"]:
        out.append(item["indices"][k:k + s])
        k += s
    return out


# =============================================================================
# Basic export / equivalence with 1.1.6 for the simple case
# =============================================================================

def test_single_polygon_child_matches_1_1_6(env):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    sds.SetMl(c4d.utils.MatrixMove(V(10, 20, 30)))
    cube = put(cube_poly(), sds, c4d.utils.MatrixMove(V(5, 0, 0)))
    data = h.get_outbox_data(FILE)
    item = data["items"][0]
    old = ref.SceneHandler()._extract_sds_item(sds, cube, "", False)
    assert item["indices"] == old["indices"] and item["sizes"] == old["sizes"]
    assert item["positions"] == pytest.approx(old["positions"], abs=1e-12)
    assert item["options"] == old["options"] == 1 | (1 << 9)
    assert item["c4d_id"] == sds.GetDataInstance().GetString(H.BC_PLASTICITY_PNS_ID)
    encode_put_some(1, "doc.c4d", data["groups"], data["items"])   # valid payload


def test_join_is_never_used(env):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    null = put(c4d.BaseObject(c4d.Onull), sds)
    put(cube_poly(), null)
    put(cube_poly(), null, c4d.utils.MatrixMove(V(300, 0, 0)))
    h.get_outbox_data(FILE)
    assert not [c for c in c4d.utils.modeling_calls if c[0] == c4d.MCOMMAND_JOIN]


# =============================================================================
# F01/F03/F04/F12 — multi-part cages, transforms, first child, deformers
# =============================================================================

def test_multi_part_cage_uploads_all_parts(env):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    null = put(c4d.BaseObject(c4d.Onull), sds)
    put(cube_poly(), null)
    put(cube_poly(), null, c4d.utils.MatrixMove(V(300, 0, 0)))
    item = h.get_outbox_data(FILE)["items"][0]
    assert len(item["sizes"]) == 12 and len(item["positions"]) == 16 * 3
    xs = [p[0] for p in positions3(item)]
    assert min(xs) == pytest.approx(-0.5) and max(xs) == pytest.approx(3.5)
    assert max(item["indices"]) == 15


def test_offset_child_is_baked(env):
    """F03: SDS > Cube generator offset by 200 cm uploads 2 m off-centre."""
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    gen = put(c4d.BaseObject(c4d.Ocube), sds, c4d.utils.MatrixMove(V(200, 0, 0)))
    gen._cache = cube_poly()              # primitive cache, identity Ml
    item = h.get_outbox_data(FILE)["items"][0]
    xs = [p[0] for p in positions3(item)]
    assert min(xs) == pytest.approx(1.5) and max(xs) == pytest.approx(2.5)


def test_cache_hierarchy_matrices(env):
    """Cache root relative to the generator, nested cache nodes relative to
    their cache parent (Symmetry/Cloner-style null + clones)."""
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    gen = put(c4d.BaseObject(c4d.Osymmetry), sds, c4d.utils.MatrixMove(V(0, 100, 0)))
    cache_root = c4d.BaseObject(c4d.Onull)
    cache_root.SetMl(c4d.utils.MatrixMove(V(0, 0, 50)))
    a = cube_poly(10)
    a.SetMl(c4d.utils.MatrixMove(V(100, 0, 0)))
    a.InsertUnder(cache_root)
    gen._cache = cache_root
    put(cube_poly(), gen)                 # consumed input: must not be exported
    item = h.get_outbox_data(FILE)["items"][0]
    pts = positions3(item)
    assert len(pts) == 8
    cx = sum(p[0] for p in pts) / 8
    cy = sum(p[1] for p in pts) / 8      # Plasticity Y = C4D Z
    cz = sum(p[2] for p in pts) / 8      # Plasticity Z = C4D Y
    assert (cx, cy, cz) == pytest.approx((1.0, 0.5, 1.0))


def test_primitive_children_are_walked(env):
    """A Cube primitive does not consume its children: SDS > Cube > Cube
    subdivides both, so both are exported."""
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    gen = put(c4d.BaseObject(c4d.Ocube), sds)
    gen._cache = cube_poly()
    put(cube_poly(), gen, c4d.utils.MatrixMove(V(0, 300, 0)))
    item = h.get_outbox_data(FILE)["items"][0]
    assert len(item["sizes"]) == 12


def test_deform_cache_uses_host_space(env):
    """F12: the single-polygon case honours deformers, and the deform cache's
    points are interpreted in the host object's space."""
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    poly = put(cube_poly(), sds, c4d.utils.MatrixMove(V(100, 0, 0)))
    bent = cube_poly(200)                    # deformed result: twice as big
    bent.SetMl(c4d.utils.MatrixMove(V(9999, 0, 0)))   # must be ignored
    poly._deform_cache = bent
    item = h.get_outbox_data(FILE)["items"][0]
    xs = [p[0] for p in positions3(item)]
    assert min(xs) == pytest.approx(0.0) and max(xs) == pytest.approx(2.0)


def test_only_first_sds_child_is_exported(env, capsys):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    put(cube_poly(), sds)
    put(cube_poly(), sds, c4d.utils.MatrixMove(V(500, 0, 0)))
    item = h.get_outbox_data(FILE)["items"][0]
    assert len(item["sizes"]) == 6
    assert "only subdivides the first one" in capsys.readouterr().out


def test_csto_result_list_is_used(env, monkeypatch):
    """F02: without caches the CSTO fallback must use the returned list."""
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    gen = put(c4d.BaseObject(c4d.Ocube), sds, c4d.utils.MatrixMove(V(100, 0, 0)))
    calls = []

    def smc(command=None, list=None, mode=0, bc=None, doc=None, flags=0):
        calls.append(command)
        assert command == c4d.MCOMMAND_CURRENTSTATETOOBJECT
        assert list[0].GetDocument() is doc          # input inside the doc
        res = cube_poly()
        res.SetMl(list[0].GetMl())                   # carries input matrix
        return [res]
    monkeypatch.setattr(c4d.utils, "SendModelingCommand", smc)
    item = h.get_outbox_data(FILE)["items"][0]
    assert calls == [c4d.MCOMMAND_CURRENTSTATETOOBJECT]
    xs = [p[0] for p in positions3(item)]
    assert min(xs) == pytest.approx(0.5) and max(xs) == pytest.approx(1.5)


def test_unresolvable_sds_is_skipped(env, monkeypatch):
    h, bridge, doc, root, outbox = env
    add_sds(doc, outbox, "Empty")
    sds = add_sds(doc, outbox, "NoCache")
    put(c4d.BaseObject(c4d.Ocube), sds)
    monkeypatch.setattr(c4d.utils, "SendModelingCommand", lambda **k: False)
    data = h.get_outbox_data(FILE)
    assert data["items"] == []
    assert "2 Outbox object(s) skipped" in bridge.status_message


# =============================================================================
# F05 — N-gons
# =============================================================================

def test_ngon_export_single_face(env):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    # hexagon split into a fan of 4 triangles, registered as one N-gon
    loop = [0, 1, 2, 3, 4, 5]
    import math
    pts = [(100 * math.cos(i * math.pi / 3), 0.0, 100 * math.sin(i * math.pi / 3))
           for i in range(6)]
    fan = [(0, i, i + 1, i + 1) for i in range(1, 5)]
    obj = c4d.PolygonObject(6, 4)
    obj.set_geometry(pts, fan, ngons=[[0, 1, 2, 3]])
    put(obj, sds)
    item = h.get_outbox_data(FILE)["items"][0]
    assert item["sizes"] == [6]
    assert item["indices"] == g.to_plasticity_loop(loop)


def test_ngon_with_hole_falls_back_to_fragments(env, capsys):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    outer = [(0, 0, 0), (300, 0, 0), (300, 0, 300), (0, 0, 300)]
    inner = [(100, 0, 100), (200, 0, 100), (200, 0, 200), (100, 0, 200)]
    ring = [(i, (i + 1) % 4, 4 + (i + 1) % 4, 4 + i) for i in range(4)]
    obj = c4d.PolygonObject(8, 4)
    obj.set_geometry(outer + inner, ring, ngons=[[0, 1, 2, 3]])
    put(obj, sds)
    item = h.get_outbox_data(FILE)["items"][0]
    assert item["sizes"] == [4, 4, 4, 4]
    assert "have holes" in capsys.readouterr().out


def _import_list(h, bridge, objs):
    bridge.push_event(BridgeEvent(EventType.LIST_RESPONSE, data=parser.parse_message(
        W.list_response(FILE, [W.item(1, objs)]))))
    bridge.process_pending_events(time_budget=None)


def _inbox(root):
    return [c for c in root.GetChildren()
            if c.GetDataInstance().GetBool(H.BC_PLASTICITY_INBOX)][0]


def test_import_export_round_trip_tris(env):
    """Import → export gives back the original indices and positions."""
    h, bridge, doc, root, outbox = env
    m = tri_grid(6, 4, seed=3)
    _import_list(h, bridge, [W.mesh_object(1, "Solid", m["vertices"], m["faces"],
                                           m["normals"], m["groups"], m["face_ids"])])
    obj = _inbox(root).GetChildren()[0]
    sds = add_sds(doc, outbox)
    obj.Remove()
    put(obj, sds)
    item = h.get_outbox_data(FILE)["items"][0]
    assert item["indices"] == m["faces"]
    assert set(item["sizes"]) == {3}
    f32 = array.array("f", m["vertices"]).tolist()
    assert item["positions"] == pytest.approx(f32, abs=1e-6)


def test_import_export_round_trip_ngons(env):
    """N-gon refacet → melt → export yields the original polygons."""
    h, bridge, doc, root, outbox = env
    m = ngon_grid(7, 5, seed=6)
    _import_list(h, bridge, [W.mesh_object(1, "S", [0.0] * 9, [0, 1, 2])])
    bridge.push_event(BridgeEvent(EventType.REFACET_RESPONSE, data=parser.parse_message(
        W.refacet_response(FILE, [dict(id=1, faces=m["faces"], vertices=m["vertices"],
                                       indices=m["indices"], normals=m["normals"],
                                       groups=m["groups"], face_ids=m["face_ids"])]))))
    bridge.process_pending_events(time_budget=None)
    obj = _inbox(root).GetChildren()[0]
    assert obj.GetNgonCount() > 0
    sds = add_sds(doc, outbox)
    obj.Remove()
    put(obj, sds)
    item = h.get_outbox_data(FILE)["items"][0]

    def key(pos):
        return tuple(round(c, 4) for c in pos)

    # original polygons as cyclic position loops (Plasticity winding)
    verts = array.array("f", m["vertices"]).tolist()
    orig = {}
    for k, f in enumerate(m["faces"]):
        orig.setdefault(f, []).append(key(verts[m["indices"][k] * 3:m["indices"][k] * 3 + 3]))
    pts = positions3(item)
    got = [[key(pts[i]) for i in face] for face in faces_of(item)]

    def canon(loop):
        i = loop.index(min(loop))
        return tuple(loop[i:] + loop[:i])
    assert sorted(canon(f) for f in got) == sorted(canon(l) for l in orig.values())


# =============================================================================
# Root transform / unit scale
# =============================================================================

def test_root_transform_and_unit_scale_inverted(env):
    h, bridge, doc, root, outbox = env
    h.update_unit_scale(2.0)
    assert root[c4d.ID_BASEOBJECT_SCALE] == V(2, 2, 2)
    m = root.GetMl()
    m.off = V(1000, -50, 30)                  # user moved the root
    root.SetMl(m)
    sds = add_sds(doc, outbox)
    put(cube_poly(), sds, c4d.utils.MatrixMove(V(100, 0, 0)))
    item = h.get_outbox_data(FILE)["items"][0]
    xs = [p[0] for p in positions3(item)]
    # root-local: centre at x = 100 cm → 1 m regardless of root scale/offset
    assert min(xs) == pytest.approx(0.5) and max(xs) == pytest.approx(1.5)


# =============================================================================
# Outbox walk: visibility, skipped objects, ids, groups
# =============================================================================

def test_only_visible_respects_hidden_parents(env):
    h, bridge, doc, root, outbox = env
    grp = put(c4d.BaseObject(c4d.Onull), outbox)
    grp.SetName("Hidden group")
    grp.SetEditorMode(c4d.MODE_OFF)
    a = add_sds(doc, grp, "Inherits hidden")
    put(cube_poly(), a)
    b = add_sds(doc, grp, "Explicitly on")
    b.SetEditorMode(c4d.MODE_ON)
    put(cube_poly(), b)
    c = add_sds(doc, outbox, "Visible")
    put(cube_poly(), c)
    names = sorted(i["name"] for i in h.get_outbox_data(FILE, only_visible=True)["items"])
    assert names == ["Visible"]                 # hidden group itself skipped
    grp.SetEditorMode(c4d.MODE_UNDEF)
    names = sorted(i["name"] for i in h.get_outbox_data(FILE, only_visible=True)["items"])
    assert names == ["Explicitly on", "Inherits hidden", "Visible"]


def test_only_visible_explicit_on_under_hidden_parent(env):
    h, bridge, doc, root, outbox = env
    outbox.SetEditorMode(c4d.MODE_OFF)
    a = add_sds(doc, outbox, "On")
    a.SetEditorMode(c4d.MODE_ON)
    put(cube_poly(), a)
    b = add_sds(doc, outbox, "Undef")
    put(cube_poly(), b)
    names = [i["name"] for i in h.get_outbox_data(FILE, only_visible=True)["items"]]
    assert names == ["On"]
    names = sorted(i["name"] for i in h.get_outbox_data(FILE)["items"])
    assert names == ["On", "Undef"]


def test_skipped_objects_are_reported(env, capsys):
    """F16: anything that is not an SDS or Null is reported, not dropped
    silently."""
    h, bridge, doc, root, outbox = env
    put(c4d.BaseObject(c4d.Ocube), outbox).SetName("Loose cube")
    put(cube_poly(), outbox).SetName("Loose mesh")
    put(c4d.BaseObject(c4d.Osplineobject), outbox).SetName("Spline")
    data = h.get_outbox_data(FILE)
    assert data["items"] == []
    out = capsys.readouterr().out
    for n in ("Loose cube", "Loose mesh", "Spline"):
        assert f"'{n}'" in out
    assert "3 Outbox object(s) skipped" in bridge.status_message


def test_groups_and_parent_ids(env):
    h, bridge, doc, root, outbox = env
    g1 = put(c4d.BaseObject(c4d.Onull), outbox)
    g1.SetName("G1")
    g2 = put(c4d.BaseObject(c4d.Onull), g1)
    g2.SetName("G2")
    sds = add_sds(doc, g2)
    put(cube_poly(), sds)
    data = h.get_outbox_data(FILE)
    (a, b) = data["groups"]
    assert (a["name"], a["parent_c4d_collection_id"]) == ("G1", "")
    assert b["parent_c4d_collection_id"] == a["c4d_collection_id"]
    assert data["items"][0]["parent_c4d_collection_id"] == b["c4d_collection_id"]
    # ids are persistent across exports
    again = h.get_outbox_data(FILE)
    assert again["groups"] == data["groups"]
    assert again["items"][0]["c4d_id"] == data["items"][0]["c4d_id"]


def test_duplicated_objects_get_fresh_ids(env):
    """F18: a copy of an uploaded SDS/null carries the original's IDs."""
    h, bridge, doc, root, outbox = env
    grp = put(c4d.BaseObject(c4d.Onull), outbox)
    sds = add_sds(doc, grp)
    put(cube_poly(), sds)
    h.get_outbox_data(FILE)
    sds.GetDataInstance().SetInt32(H.BC_PLASTICITY_ID, 77)
    grp.GetDataInstance().SetInt32(H.BC_PLASTICITY_GROUP_ID, 88)
    dup = grp.GetClone()                       # copies BC data and children
    put(dup, outbox)
    data = h.get_outbox_data(FILE)
    ids = [i["c4d_id"] for i in data["items"]]
    cids = [gr["c4d_collection_id"] for gr in data["groups"]]
    assert len(set(ids)) == 2 and len(set(cids)) == 2
    stable = sorted(i["existing_stable_id"] for i in data["items"])
    assert stable == [0, 77]
    assert sorted(gr["existing_group_id"] for gr in data["groups"]) == [0, 88]


def test_user_data_option_bits(env):
    h, bridge, doc, root, outbox = env
    sds = add_sds(doc, outbox)
    put(cube_poly(), sds)
    sds.AddUserDataBool("pns_rounded_corners", True)
    sds.AddUserDataBool("pns_merge_patches", False)
    sds.AddUserDataBool("pns_interpolate_boundary", True)
    item = h.get_outbox_data(FILE)["items"][0]
    assert item["options"] == 1 | (1 << 8) | (1 << 10)
