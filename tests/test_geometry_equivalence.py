"""
Before/after equivalence: the optimised c4d-free geometry code must produce
exactly the same output as the frozen 1.1.6 implementation
(tests/reference/handler_v116.py) on valid input.
"""

import array
import random

import c4d
import pytest

from core import geometry as g
from reference import handler_v116 as ref
from meshgen import ngon_grid, tri_grid

REF = ref.SceneHandler()


def _vec(v):
    return (v.x, v.y, v.z)


def _poly(cp):
    return (cp.a, cp.b, cp.c, cp.d)


@pytest.mark.parametrize("nx,ny,rows", [(1, 1, 1), (7, 5, 2), (40, 30, 4)])
def test_tri_mesh_matches_1_1_6(nx, ny, rows):
    m = tri_grid(nx, ny, rows_per_group=rows, seed=nx)
    pts, polys, nmap, pg, cn, pfv = REF._compute_tri_geometry(
        m["vertices"], m["faces"], m["normals"], m["groups"])
    new = g.build_tri_mesh(m["vertices"], m["faces"])
    assert [_vec(p) for p in pts] == new.points
    assert [_poly(p) for p in polys] == [c4d_poly(p) for p in new.polys]
    made = g.build_tri_mesh(m["vertices"], m["faces"], make_point=c4d.Vector,
                            make_poly=c4d.CPolygon)
    assert [_poly(p) for p in polys] == [_poly(p) for p in made.polys]
    assert [_vec(p) for p in pts] == [_vec(p) for p in made.points]
    assert [v for t in nmap for v in t] == new.corners
    assert (pg, cn, pfv) == ([], {}, [])
    assert new.dropped == 0


def c4d_poly(p):
    cp = c4d.CPolygon(*p)
    return (cp.a, cp.b, cp.c, cp.d)


@pytest.mark.parametrize("nx,ny,seed", [(3, 2, 0), (9, 7, 1), (25, 18, 2)])
def test_ngon_mesh_matches_1_1_6(nx, ny, seed):
    m = ngon_grid(nx, ny, seed=seed)
    pts, polys, nmap, pg, cn, pfv = REF._compute_ngon_geometry(
        m["vertices"], m["indices"], m["faces"], m["normals"], m["groups"])
    new = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                            m["normals"], m["groups"])
    assert [_vec(p) for p in pts] == new.points
    assert [_poly(p) for p in polys] == new.polys
    assert [v for t in nmap for v in t] == new.corners
    assert pg == new.poly_groups
    assert cn == new.corner_normals
    assert list(cn) == list(new.corner_normals)      # same insertion order
    assert pfv == new.poly_face_val


def test_ngon_mesh_matches_with_array_inputs():
    """Decoder output is array-based now; results must not depend on it."""
    m = ngon_grid(6, 5, seed=3)
    as_arr = dict(
        vertices=array.array("f", m["vertices"]).tolist(),
        indices=array.array("i", m["indices"]),
        faces=array.array("i", m["faces"]),
        normals=array.array("f", m["normals"]),
    )
    a = g.build_ngon_mesh(as_arr["vertices"], as_arr["indices"],
                          as_arr["faces"], as_arr["normals"], m["groups"])
    pts, polys, nmap, pg, cn, pfv = REF._compute_ngon_geometry(
        as_arr["vertices"], list(as_arr["indices"]), list(as_arr["faces"]),
        list(as_arr["normals"]), m["groups"])
    assert [_poly(p) for p in polys] == a.polys
    assert pfv == a.poly_face_val


def test_weld_rounding_semantics_preserved():
    # Two positions that differ but round to the same 7-digit key must weld
    # to the FIRST occurrence's exact coordinates, like 1.1.6.
    verts = [0.12345671, 0.0, 0.0,
             0.12345674, 0.0, 0.0,
             0.5, 0.5, 0.5,
             0.12345671, 0.0, 0.0]
    unique, o2n = g.weld_vertices(verts)
    assert unique == [(0.12345671, 0.0, 0.0), (0.5, 0.5, 0.5)]
    assert o2n == [0, 0, 1, 0]


def _random_groups(rnd, n_loops):
    groups = []
    pos = 0
    while pos < n_loops:
        cnt = rnd.randint(1, 40)
        groups += [pos + rnd.choice([0, 0, 1, 2]), cnt]
        pos += cnt
    if rnd.random() < 0.5:                       # unsorted boundaries
        pairs = [groups[i:i + 2] for i in range(0, len(groups), 2)]
        rnd.shuffle(pairs)
        groups = [v for p in pairs for v in p]
    if groups and rnd.random() < 0.3:            # duplicate start
        groups += [groups[0], 5]
    return groups


@pytest.mark.parametrize("seed", range(40))
def test_tri_face_map_matches_1_1_6(seed):
    rnd = random.Random(seed)
    poly_count = rnd.randint(0, 300)
    groups = _random_groups(rnd, poly_count * 3)
    assert (g.tri_face_map(poly_count, groups)
            == REF._compute_tri_face_map(poly_count, groups))


def test_tri_face_map_edge_cases():
    for pc, groups in [(5, []), (0, [0, 3]), (4, [7, 3]), (4, [-3, 6, 3, 3])]:
        assert g.tri_face_map(pc, groups) == REF._compute_tri_face_map(pc, groups)


class _FakeTag:
    """Capture what the 1.1.6 low-level normal path writes."""

    def __init__(self, n):
        self.buf = bytearray(n * 24)

    def GetLowlevelDataAddressW(self):
        return memoryview(self.buf)


def _ref_lowlevel_normals(normals, normal_map):
    """Run the 1.1.6 fallback (low-level) branch of _apply_normals."""
    data = array.array("h")

    def pack_n(v):
        return int(max(-32767.0, min(32767.0, v * 32767.0)))
    for ids in normal_map:
        for v_id in ids:
            if v_id * 3 + 2 < len(normals):
                data.extend([pack_n(normals[v_id * 3]),
                             pack_n(normals[v_id * 3 + 2]),
                             pack_n(normals[v_id * 3 + 1])])
            else:
                data.extend([0, 0, 0])
    return data.tobytes()


def test_normal_packing_matches_1_1_6_lowlevel_layout():
    m = tri_grid(12, 9, seed=4)
    new = g.build_tri_mesh(m["vertices"], m["faces"])
    nmap = [tuple(new.corners[i:i + 4]) for i in range(0, len(new.corners), 4)]
    # Same layout and quantisation as 1.1.6 when given 1.1.6's scale ...
    assert (g.pack_vertex_normals(m["normals"], new.corners, scale=32767.0)
            == _ref_lowlevel_normals(m["normals"], nmap))


def test_normal_packing_short_normals_fall_back_to_zero():
    normals = [0.0, 1.0, 0.0]               # only vertex 0 has a normal
    corners = [0, 1, 2, 2]
    assert (g.pack_vertex_normals(normals, corners, scale=32767.0)
            == _ref_lowlevel_normals(normals, [tuple(corners)]))


def test_normal_packing_clamps_and_truncates_like_1_1_6():
    normals = [1.5, -2.0, 0.49999, -0.333, 1e-9, float("inf")]
    corners = [0, 1, 1, 0]
    assert (g.pack_vertex_normals(normals, corners, scale=32767.0)
            == _ref_lowlevel_normals(normals, [tuple(corners)]))


def test_normal_scale_is_32000_per_maxon():
    # ... but the default is the documented 32000 (Maxon forum, topic 11584):
    # 1.1.x's 32767 stored every normal ~2.4 % too long (F14).
    raw = g.pack_vertex_normals([0.0, 0.0, 1.0], [0, 0, 0, 0])
    vals = array.array("h", raw)
    assert list(vals[:3]) == [0, 32000, 0]          # (x, z, y) order
    back = g.unpack_normals(raw)
    assert back[0][0] == (0.0, 1.0, 0.0)


def _ref_corner_pack(post_polys, face_map, corner_normals):
    data = array.array("h")

    def pack_n(v):
        return int(max(-32767.0, min(32767.0, v * 32767.0)))
    for i, (a, b, c, d) in enumerate(post_polys):
        gi = face_map[i] if i < len(face_map) else 0
        is_tri = c == d
        for v in (a, b, c, d if not is_tri else c):
            t = corner_normals.get((v, gi), (0.0, 1.0, 0.0))
            data.extend([pack_n(t[0]), pack_n(t[1]), pack_n(t[2])])
    return data.tobytes()


def test_corner_normal_packing_matches_1_1_6():
    m = ngon_grid(8, 6, seed=5)
    mesh = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                             m["normals"], m["groups"])
    fm = mesh.poly_face_val[:-3]            # exercise "past end of map" → 0
    assert (g.pack_corner_normals(mesh.polys, fm, mesh.corner_normals,
                                  scale=32767.0)
            == _ref_corner_pack(mesh.polys, fm, mesh.corner_normals))


def _ref_batches(groups_to_melt, polys):
    """1.1.6 colouring, extracted from _create_ngon_groups."""
    polygon_identity = dict(enumerate(polys))
    group_edges = []
    for group in groups_to_melt:
        edges = set()
        for pid in group:
            identity = polygon_identity.get(pid)
            if identity is None:
                continue
            a, b, c, d = identity
            verts = (a, b, c) if c == d else (a, b, c, d)
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
    return batches


@pytest.mark.parametrize("seed", range(4))
def test_melt_batches_match_1_1_6(seed):
    m = ngon_grid(15 + seed, 11, seed=seed, ngon_every=2)
    mesh = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                             m["normals"], m["groups"])
    assert mesh.poly_groups
    assert (g.melt_batches(mesh.poly_groups, mesh.polys)
            == _ref_batches(mesh.poly_groups, mesh.polys))


def test_melt_batches_never_put_adjacent_groups_together():
    m = ngon_grid(20, 14, seed=9, ngon_every=2)
    mesh = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                             m["normals"], m["groups"])
    edges = [set(e for p in grp for e in g.polygon_edges(mesh.polys[p]))
             for grp in mesh.poly_groups]
    for batch in g.melt_batches(mesh.poly_groups, mesh.polys):
        for i, a in enumerate(batch):
            for b in batch[i + 1:]:
                assert not (edges[a] & edges[b])


def test_edge_map_matches_1_1_6():
    m = ngon_grid(10, 8, seed=2)
    mesh = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                             m["normals"], m["groups"])
    obj = c4d.PolygonObject(len(mesh.points), len(mesh.polys))
    obj.set_geometry(mesh.points, mesh.polys)
    pfm = mesh.poly_face_val
    assert (g.build_edge_map(mesh.polys, pfm, len(mesh.polys))
            == ref._build_edge_map(obj, pfm, len(mesh.polys)))


@pytest.mark.parametrize("angle", [0.0, 10.0, 45.0])
def test_sharp_edges_match_1_1_6(angle):
    import json
    import math
    m = tri_grid(14, 10, rows_per_group=3, seed=1)
    mesh = g.build_tri_mesh(m["vertices"], m["faces"])
    obj = c4d.PolygonObject(len(mesh.points), len(mesh.polys))
    obj.set_geometry(mesh.points, [c4d.CPolygon(*p) for p in mesh.polys])
    pfm = g.tri_face_map(len(mesh.polys), m["groups"])
    obj.GetDataInstance().SetString(ref.BC_PLASTICITY_POLY_FACE_MAP,
                                    json.dumps(pfm))
    expected = REF._get_sharp_edge_addresses(obj, angle)
    polys = [c4d_poly(p) for p in mesh.polys]
    em = g.build_edge_map(polys, pfm)
    cos_t = math.cos(math.radians(angle)) if angle > 0 else None
    assert g.sharp_edge_addresses(em, polys, mesh.points, cos_t) == expected
