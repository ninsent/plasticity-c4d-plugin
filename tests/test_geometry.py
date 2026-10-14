"""Unit tests for the c4d-free geometry helpers (new behaviour and fixes)."""

import json
import random

import pytest

from core import geometry as g
from meshgen import ngon_grid, tri_grid


# ── coordinate conversion / winding ─────────────────────────────────────────

def test_axis_swap_and_scale_round_trip():
    verts = [1.0, 2.0, 3.0, -0.5, 0.25, 8.0]
    pts = g.import_points(verts)
    assert pts == [(100.0, 300.0, 200.0), (-50.0, 800.0, 25.0)]

    class V:
        def __init__(self, t):
            self.x, self.y, self.z = t
    assert g.export_positions([V(p) for p in pts]) == pytest.approx(verts)


@pytest.mark.parametrize("face", [[0, 1, 2], [4, 5, 6, 7], [9, 3, 8, 1, 0, 2]])
def test_winding_round_trip(face):
    # import: Plasticity (v0, v1, ..., vn-1) → C4D (v0, vn-1, ..., v1)
    c4d_loop = [face[0]] + face[:0:-1]
    assert g.to_plasticity_loop(c4d_loop) == face
    idx, sizes = g.faces_to_plasticity([c4d_loop])
    assert idx == face and sizes == [len(face)]


def test_tri_mesh_winding_matches_cpolygon_a_c_b():
    m = g.build_tri_mesh([0.0] * 9, [0, 1, 2])
    assert m.polys == [(0, 2, 1)]
    assert m.corners == [0, 2, 1, 1]


# ── invalid input ────────────────────────────────────────────────────────────

def test_invalid_indices_are_dropped():
    verts = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]   # 3 vertices
    m = g.build_tri_mesh(verts, [0, 1, 2, 0, 1, 7, -1, 0, 1])
    assert m.polys == [(0, 2, 1)] and m.dropped == 2
    n = g.build_ngon_mesh(verts, [0, 1, 2, 0, 1, 9], [0, 0, 0, 1, 1, 1],
                          [], [0, 6])
    assert n.polys == [(0, 2, 1, 1)] and n.dropped == 1


def test_degenerate_ngon_faces_are_dropped():
    # all corners weld to two positions → fewer than 3 distinct corners
    verts = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    n = g.build_ngon_mesh(verts, [0, 1, 2, 3], [0, 0, 0, 0], [], [0, 4])
    assert n.polys == [] and n.dropped == 1


def test_duplicate_vertices_are_welded_in_ngon_mode():
    m = ngon_grid(4, 3, seed=1)
    n = g.build_ngon_mesh(m["vertices"], m["indices"], m["faces"],
                          m["normals"], m["groups"])
    assert len(n.points) == 5 * 4 + (0)          # (nx+1)*(ny+1) grid points
    assert len(m["vertices"]) // 3 > len(n.points)


def test_concave_ngon_is_triangulated_inside_its_outline():
    # L-shape (concave): fan triangulation would leave the shape
    pts2d = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    tris = g.ear_clip(pts2d)
    assert len(tris) == 4
    area = sum(abs((pts2d[b][0] - pts2d[a][0]) * (pts2d[c][1] - pts2d[a][1])
                   - (pts2d[c][0] - pts2d[a][0]) * (pts2d[b][1] - pts2d[a][1])) / 2
               for a, b, c in tris)
    assert area == pytest.approx(3.0)


def test_ear_clip_degenerate_input():
    assert g.ear_clip([(0, 0), (1, 1)]) == []
    assert g.ear_clip([(0, 0), (1, 0), (2, 0), (3, 0)]) == []   # zero area


# ── poly-face map storage ────────────────────────────────────────────────────

@pytest.mark.parametrize("pfm", [[], [0], [0, 0, 0, 1, 1, 2, 0, 0], list(range(50)),
                                 [3] * 1000 + [1] * 7])
def test_poly_face_map_codec_round_trip(pfm):
    s = g.encode_poly_face_map(pfm)
    assert g.decode_poly_face_map(s) == (pfm or None) or (not pfm and
                                                          g.decode_poly_face_map(s) == [])


def test_poly_face_map_reads_1_1_x_json_lists():
    legacy = json.dumps([0, 0, 1, 2, 2])
    assert g.decode_poly_face_map(legacy) == [0, 0, 1, 2, 2]


def test_poly_face_map_is_compact():
    pfm = [i // 500 for i in range(100000)]
    assert len(g.encode_poly_face_map(pfm)) < 2000
    assert len(json.dumps(pfm)) > 300000


@pytest.mark.parametrize("bad", ["", "not json", "{\"v\": 9}", "42", "{"])
def test_poly_face_map_corrupt_data_is_none(bad):
    assert g.decode_poly_face_map(bad) is None


# ── N-gon export ─────────────────────────────────────────────────────────────

def _fan(loop):
    return [(loop[0], loop[i], loop[i + 1], loop[i + 1])
            for i in range(1, len(loop) - 1)]


def _same_cycle(a, b):
    if len(a) != len(b):
        return False
    i = b.index(a[0]) if a[0] in b else -1
    return i >= 0 and b[i:] + b[:i] == a


@pytest.mark.parametrize("loop", [[0, 1, 2, 3, 4], [5, 9, 7, 6, 8, 4, 3],
                                  list(range(12))])
def test_ngon_outline_recovers_loop(loop):
    polys = _fan(loop)
    out = g.ngon_outline(polys, list(range(len(polys))))
    assert _same_cycle(out, loop)


def test_ngon_outline_with_quads_and_concave_shape():
    # L-shaped hexagon split into two quads
    polys = [(0, 1, 2, 3), (0, 3, 4, 5)]
    assert _same_cycle(g.ngon_outline(polys, [0, 1]), [0, 1, 2, 3, 4, 5])


def test_ngon_outline_rejects_holes_and_pinches():
    # ring of 4 quads around a hole → two boundary loops
    outer = [0, 1, 2, 3]
    inner = [4, 5, 6, 7]
    ring = [(outer[i], outer[(i + 1) % 4], inner[(i + 1) % 4], inner[i])
            for i in range(4)]
    assert g.ngon_outline(ring, [0, 1, 2, 3]) is None
    # two triangles touching at one vertex
    assert g.ngon_outline([(0, 1, 2, 2), (0, 3, 4, 4)], [0, 1]) is None


def test_faces_from_groups_falls_back_to_fragments():
    outer = [0, 1, 2, 3]
    inner = [4, 5, 6, 7]
    ring = [(outer[i], outer[(i + 1) % 4], inner[(i + 1) % 4], inner[i])
            for i in range(4)]
    faces, frag = g.faces_from_groups(ring, [[0, 1, 2, 3]])
    assert frag == 1 and len(faces) == 4


def test_groups_from_polymap():
    assert g.groups_from_polymap([0, 1, 1, 2, 1], 5) == [[0], [1, 2, 4], [3]]
    assert g.groups_from_polymap([0, 1], 3) == [[0], [1], [2]]   # wrong length
    assert g.groups_from_polymap(None, 2) == [[0], [1]]


def test_merge_mesh_parts_offsets_indices():
    p1 = ([0.0] * 9, [[0, 2, 1]])
    p2 = ([1.0] * 12, [[0, 3, 2, 1]])
    pos, idx, sizes = g.merge_mesh_parts([p1, p2])
    assert len(pos) == 21
    assert idx == [0, 1, 2, 3, 4, 5, 6] and sizes == [3, 4]


# ── visibility / ids ─────────────────────────────────────────────────────────

ON, OFF, UNDEF = 0, 1, 2


@pytest.mark.parametrize("modes,visible", [
    ([UNDEF], True),
    ([UNDEF, OFF], False),            # hidden parent
    ([UNDEF, UNDEF, OFF], False),     # hidden grandparent
    ([ON, OFF], True),                # explicit ON overrides the parent
    ([OFF, ON], False),
    ([UNDEF, ON, OFF], True),
])
def test_effective_editor_visibility(modes, visible):
    assert g.effective_editor_visibility(modes, ON, OFF) is visible


def test_dedupe_ids():
    assert g.dedupe_ids(["a", "b", "a", "", "", "b", "c"]) == [2, 5]


# ── melt support ─────────────────────────────────────────────────────────────

def test_post_melt_face_map_identity_and_shared_groups():
    pre = [(0, 1, 2, 2), (2, 3, 0, 0)]
    identity = {pre[0]: 4}
    vg = {0: {4, 7}, 1: {4}, 2: {4, 7}, 3: {7}}
    post = [pre[0], (0, 2, 3, 3), (9, 9, 9, 9)]
    assert g.post_melt_face_map(post, identity, vg) == [4, 7, 0]


def test_vertex_groups_without_normals():
    vg = g.vertex_groups([(0, 1, 2, 2), (2, 3, 4, 4)], [0, 1])
    assert vg[2] == {0, 1} and vg[0] == {0}


# ── sharp / perimeter edges ──────────────────────────────────────────────────

def test_perimeter_edges_of_selected_group():
    # two quads side by side, different groups
    polys = [(0, 1, 4, 3), (1, 2, 5, 4)]
    em = g.build_edge_map(polys, [0, 1])
    per0 = g.perimeter_edge_addresses(em, {0})
    assert len(per0) == 4                       # shared edge counts once
    assert g.perimeter_edge_addresses(em, {0, 1}).__len__() == 6
    assert len(g.boundary_edge_addresses(em)) == 7


def test_expand_to_groups():
    groups, polys = g.expand_to_groups([0, 0, 1, 1, 2], [1, 4, 99])
    assert groups == {0, 2} and polys == [0, 1, 4]


@pytest.mark.parametrize("seed", range(5))
def test_tri_face_map_is_monotone_runs_for_real_meshes(seed):
    m = tri_grid(9, 7, rows_per_group=2, seed=seed)
    n = len(m["faces"]) // 3
    pfm = g.tri_face_map(n, m["groups"])
    assert pfm == sorted(pfm) and set(pfm) == set(range(len(m["face_ids"])))
    assert g.decode_poly_face_map(g.encode_poly_face_map(pfm)) == pfm
