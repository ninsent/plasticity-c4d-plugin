"""
Pure geometry logic for the Plasticity Bridge — no ``c4d`` import.

Everything in here works on plain Python data (flat number sequences,
tuples, lists) so it can be unit-tested and profiled outside Cinema 4D.
``core/handler.py`` is the thin C4D-facing layer that turns these results
into ``c4d.Vector`` / ``c4d.CPolygon`` / tag data.

Conventions
-----------
* Plasticity is Z-up, Cinema 4D is Y-up: C4D (x, y, z) = Plasticity (x, z, y).
  The same swap applies to normals.
* Positions are scaled by ``IMPORT_SCALE`` (metres → centimetres) on import
  and by ``1 / IMPORT_SCALE`` on export.
* Winding is reversed between the two applications.  A Plasticity face
  ``(v0, v1, ..., vn-1)`` becomes the C4D loop ``(v0, vn-1, ..., v1)``;
  for triangles that is ``CPolygon(a, c, b)``.  ``to_plasticity_loop`` is the
  exact inverse.
* Polygons are handled as 4-tuples ``(a, b, c, d)`` exactly like C4D's
  ``CPolygon``: a triangle has ``c == d``.
"""

import bisect
import json
import math
import sys
from array import array

IMPORT_SCALE = 100.0
EXPORT_SCALE = 1.0 / IMPORT_SCALE

# Normal tag storage: 12 int16 per polygon (corners a, b, c, d × x, y, z),
# each component scaled by 32000 — Maxon developer forum, topic 11584
# ("you have to divide them by 32000.0").  1.1.x used 32767, which made
# every stored normal ~2.4 % too long.
NORMAL_INT16_SCALE = 32000.0

_LITTLE_ENDIAN = sys.byteorder == "little"


# =============================================================================
# Coordinate conversion
# =============================================================================

def import_points(vertices, scale=IMPORT_SCALE, make=tuple):
    """Convert flat Plasticity positions to C4D points.

    ``vertices`` is a flat ``x, y, z, x, y, z, ...`` sequence.  ``make`` is
    called as ``make((x, y, z))`` when it is ``tuple``, otherwise as
    ``make(x, y, z)`` (pass ``c4d.Vector`` from the handler).  A trailing
    partial vertex is ignored, like 1.1.x did.
    """
    xs = vertices[0::3]
    ys = vertices[1::3]
    zs = vertices[2::3]
    s = scale
    if make is tuple:
        return [(x * s, z * s, y * s) for x, y, z in zip(xs, ys, zs)]
    return [make(x * s, z * s, y * s) for x, y, z in zip(xs, ys, zs)]


def export_positions(points, scale=EXPORT_SCALE):
    """C4D points (objects with ``.x/.y/.z``, already in root-local space)
    → flat Plasticity position list ``[x, y, z, ...]``."""
    s = scale
    return [c for p in points for c in (p.x * s, p.z * s, p.y * s)]


def to_plasticity_loop(loop):
    """Reverse a C4D polygon loop back to Plasticity winding, keeping the
    first vertex: ``(a, b, c, d) → (a, d, c, b)``, ``(a, b, c) → (a, c, b)``."""
    return [loop[0]] + list(loop[:0:-1])


def polygon_loop(p):
    """Vertex loop of a polygon tuple ``(a, b, c, d)`` (triangle if c == d)."""
    a, b, c, d = p
    return [a, b, c] if c == d else [a, b, c, d]


# =============================================================================
# Ear-clipping triangulation for concave N-gons (unchanged from 1.1.x)
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


def polygon_normal(pts):
    """Newell's method — robust unit normal of an ordered 3D point ring
    (``(x, y, z)`` tuples).  Works for non-planar and concave polygons.
    Degenerate rings return ``(0, 1, 0)``."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        cx, cy, cz = pts[i]
        qx, qy, qz = pts[(i + 1) % n]
        nx += (cy - qy) * (cz + qz)
        ny += (cz - qz) * (cx + qx)
        nz += (cx - qx) * (cy + qy)
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-12:
        return (0.0, 1.0, 0.0)
    return (nx / length, ny / length, nz / length)


def project_to_2d(pts, normal):
    """Flatten 3D points to 2D by dropping the axis most aligned with the
    polygon normal.  Returns a list of ``(u, v)`` tuples."""
    ax = abs(normal[0])
    ay = abs(normal[1])
    az = abs(normal[2])
    if ax >= ay and ax >= az:
        return [(p[1], p[2]) for p in pts]
    elif ay >= az:
        return [(p[0], p[2]) for p in pts]
    else:
        return [(p[0], p[1]) for p in pts]


def is_convex(pts_2d):
    """Quick O(n) convexity test.  True if all turns have the same sign."""
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


def ear_clip(pts_2d):
    """Ear-clipping triangulation for a simple (possibly concave) polygon.

    Returns a list of ``(i, j, k)`` index triples into ``pts_2d``, or ``[]``
    on degenerate input.  Convex polygons take a fan fast path.
    """
    n = len(pts_2d)
    if n < 3:
        return []
    if n == 3:
        return [(0, 1, 2)]

    if is_convex(pts_2d):
        return [(0, i + 1, i + 2) for i in range(n - 2)]

    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += pts_2d[i][0] * pts_2d[j][1]
        area -= pts_2d[j][0] * pts_2d[i][1]
    if abs(area) < 1e-12:
        return []
    ccw = area > 0.0

    xs = [p[0] for p in pts_2d]
    ys = [p[1] for p in pts_2d]

    remaining = list(range(n))
    triangles = []

    reflex = set()
    for i in range(n):
        pi = remaining[(i - 1) % n]
        ci = remaining[i]
        ni = remaining[(i + 1) % n]
        cross = _cross_2d(xs[pi], ys[pi], xs[ci], ys[ci], xs[ni], ys[ni])
        if (ccw and cross <= 0) or (not ccw and cross >= 0):
            reflex.add(ci)

    max_iter = n * n

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
                continue

            pi = remaining[(i - 1) % m]
            ni = remaining[(i + 1) % m]

            pix, piy = xs[pi], ys[pi]
            cix, ciy = xs[ci], ys[ci]
            nix, niy = xs[ni], ys[ni]

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
            break

    return triangles


# =============================================================================
# Group lookup helpers
# =============================================================================

def _group_lookup(groups):
    """Sorted ``(start, group_index)`` boundaries for a flat
    ``[start0, count0, start1, count1, ...]`` groups array."""
    n_groups = len(groups) // 2
    boundaries = sorted(
        ((groups[gi * 2], gi) for gi in range(n_groups)),
        key=lambda x: x[0],
    )
    return [b[0] for b in boundaries], [b[1] for b in boundaries]


def tri_face_map(poly_count, groups):
    """Poly-face map for tri-mode geometry: polygon ``i`` covers loop
    positions ``3i .. 3i+2``; its group is the last group starting at or
    before ``3i`` (0 if none).  Identical results to 1.1.x, computed with
    run filling instead of a bisect per polygon."""
    if not groups or poly_count == 0:
        return [0] * poly_count
    keys, gidx = _group_lookup(groups)
    if not keys:
        return [0] * poly_count
    result = [0] * poly_count
    # Polygon pi belongs to boundary k when keys[k] <= 3*pi < keys[k+1].
    # The first polygon at/after boundary k is ceil(keys[k] / 3).
    for k in range(len(keys)):
        start = max(0, -(-keys[k] // 3))
        end = poly_count if k + 1 == len(keys) else max(0, -(-keys[k + 1] // 3))
        if start >= poly_count:
            break
        if end > start:
            result[start:min(end, poly_count)] = [gidx[k]] * (min(end, poly_count) - start)
    return result


# =============================================================================
# Tri-mode import
# =============================================================================

class TriMesh:
    """Result of ``build_tri_mesh``.

    ``polys`` are ``(a, c, b)`` triples (winding already reversed) or
    whatever ``make_poly(a, c, b)`` returned; ``tris`` the same triangles
    as plain ``(a, c, b)`` tuples; ``dropped`` counts triangles skipped
    because they referenced vertices that do not exist.
    """

    __slots__ = ("points", "polys", "tris", "dropped")

    def __init__(self, points, polys, tris, dropped):
        self.points = points
        self.polys = polys
        self.tris = tris
        self.dropped = dropped

    @property
    def corners(self):
        """Flat original vertex ids per C4D corner a, b, c, d (4 per
        polygon; d repeats c) — the lookup order for per-vertex normals."""
        return [v for a, c, b in self.tris for v in (a, c, b, b)]


def build_tri_mesh(vertices, indices, scale=IMPORT_SCALE, make_point=tuple,
                   make_poly=None):
    """Pure-triangle geometry for ADD/UPDATE objects and tri-mode refacet.

    ``make_point``/``make_poly`` build the C4D objects directly
    (``c4d.Vector`` / ``c4d.CPolygon`` from the handler) so no
    intermediate lists are needed.
    """
    points = import_points(vertices, scale, make_point)
    vert_count = len(points)
    ia = indices[0::3]
    ib = indices[1::3]
    ic = indices[2::3]
    n = min(len(ia), len(ib), len(ic))
    dropped = 0
    if n and (min(indices) < 0 or max(indices) >= vert_count):
        tris = [(a, c, b) for a, b, c in zip(ia, ib, ic)
                if 0 <= a < vert_count and 0 <= b < vert_count
                and 0 <= c < vert_count]
        dropped = n - len(tris)
    else:
        tris = list(zip(ia, ic, ib))
    if make_poly is None:
        polys = tris
    else:
        polys = [make_poly(a, c, b) for a, c, b in tris]
    return TriMesh(points, polys, tris, dropped)


# =============================================================================
# N-gon (refacet) import
# =============================================================================

class NgonMesh:
    """Result of ``build_ngon_mesh``.

    points          C4D-space point tuples (welded, scaled, axis-swapped)
    polys           (a, b, c, d) tuples in C4D winding
    corners         flat original-vertex ids, 4 per polygon (for normals)
    poly_groups     lists of polygon indices to melt into one N-gon
    corner_normals  {(welded_vertex, group_index): (nx, ny, nz) C4D space}
    poly_face_val   group index per polygon (the poly-face map pre-melt)
    dropped         polygons skipped (fewer than 3 distinct corners or
                    out-of-range vertex references)
    """

    __slots__ = ("points", "polys", "corners", "poly_groups",
                 "corner_normals", "poly_face_val", "dropped")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw[k])


def weld_vertices(vertices, ndigits=7):
    """Weld positions whose coordinates agree after ``round(.., ndigits)``.

    Returns ``(unique, old_to_new)`` where ``unique`` holds the exact
    ``(x, y, z)`` of the first occurrence of each welded key.  Identical to
    1.1.x; exact duplicates (the common case — Plasticity repeats shared
    vertices per face) are resolved by a cheap exact-tuple lookup first, so
    ``round()`` only runs once per distinct position.
    """
    exact = {}
    rounded = {}
    unique = []
    old_to_new = []
    append = old_to_new.append
    for t in zip(vertices[0::3], vertices[1::3], vertices[2::3]):
        idx = exact.get(t)
        if idx is None:
            key = (round(t[0], ndigits), round(t[1], ndigits),
                   round(t[2], ndigits))
            idx = rounded.get(key)
            if idx is None:
                idx = len(unique)
                rounded[key] = idx
                unique.append(t)
            exact[t] = idx
        append(idx)
    return unique, old_to_new


def build_ngon_mesh(vertices, indices, faces, normals, groups,
                    scale=IMPORT_SCALE):
    """Geometry for N-gon refacet data (faces = per-loop polygon membership).

    Same algorithm and output as 1.1.x ``_compute_ngon_geometry``: weld,
    split ``faces`` into polygon runs, drop consecutive duplicate corners,
    emit tris/quads directly and ear-clip larger polygons (to be melted back
    into N-gons in C4D).  Polygons referencing out-of-range vertices are
    dropped instead of raising.
    """
    unique, old_to_new = weld_vertices(vertices)
    s = scale
    points = [(x * s, z * s, y * s) for x, y, z in unique]
    vert_count = len(old_to_new)

    gs_keys, gs_idx = _group_lookup(groups)

    def face_val_to_group_idx(loop_pos):
        idx = bisect.bisect_right(gs_keys, loop_pos) - 1
        return gs_idx[idx] if idx >= 0 else 0

    n_normals = len(normals)
    has_normals = n_normals >= 3

    def ntuple(orig_vi):
        base = orig_vi * 3
        if 0 <= base and base + 2 < n_normals:
            return (normals[base], normals[base + 2], normals[base + 1])
        return (0.0, 1.0, 0.0)

    polys = []
    corners = []
    poly_groups = []
    poly_face_val = []
    corner_normals = {}
    dropped = 0

    n_loops = len(faces)
    poly_starts = [0]
    for i in range(1, n_loops):
        if faces[i] != faces[i - 1]:
            poly_starts.append(i)
    poly_starts.append(n_loops)

    for p in range(len(poly_starts) - 1):
        start = poly_starts[p]
        end = poly_starts[p + 1]
        if start >= end:
            continue

        raw_orig_vi = indices[start:end]
        if any(v < 0 or v >= vert_count for v in raw_orig_vi):
            dropped += 1
            continue
        raw_face_vi = [old_to_new[v] for v in raw_orig_vi]
        group_idx = face_val_to_group_idx(start)

        if has_normals:
            for k in range(len(raw_face_vi)):
                cn_key = (raw_face_vi[k], group_idx)
                if cn_key not in corner_normals:
                    corner_normals[cn_key] = ntuple(raw_orig_vi[k])

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
        if count < 3 or len(set(face_vindices)) < 3:
            # Fewer than 3 distinct corners after welding: zero area.
            dropped += 1
            continue

        first_poly = len(polys)

        if count == 3:
            ia, ib, ic = face_vindices
            polys.append((ia, ic, ib, ib))
            o = orig_vindices
            corners.extend((o[0], o[2], o[1], o[1]))
            poly_face_val.append(group_idx)

        elif count == 4:
            ia, ib, ic, id_ = face_vindices
            polys.append((ia, id_, ic, ib))
            o = orig_vindices
            corners.extend((o[0], o[3], o[2], o[1]))
            poly_face_val.append(group_idx)

        else:
            face_pts_3d = [points[vi] for vi in face_vindices]
            face_normal = polygon_normal(face_pts_3d)
            pts_2d = project_to_2d(face_pts_3d, face_normal)
            ear_tris = ear_clip(pts_2d)

            if not ear_tris:
                print(f"[Plasticity] Ear-clip failed for {count}-gon, "
                      f"falling back to fan triangulation")
                ear_tris = [(0, t + 1, t + 2) for t in range(count - 2)]

            for li_a, li_b, li_c in ear_tris:
                ia = face_vindices[li_a]
                ib = face_vindices[li_b]
                ic = face_vindices[li_c]
                polys.append((ia, ic, ib, ib))
                o = orig_vindices
                corners.extend((o[li_a], o[li_c], o[li_b], o[li_b]))
                poly_face_val.append(group_idx)

        if len(polys) - first_poly > 1:
            poly_groups.append(list(range(first_poly, len(polys))))

    return NgonMesh(points=points, polys=polys, corners=corners,
                    poly_groups=poly_groups, corner_normals=corner_normals,
                    poly_face_val=poly_face_val, dropped=dropped)


# =============================================================================
# N-gon melt support
# =============================================================================

def polygon_edges(p):
    """Undirected edges of a polygon tuple as ``(min, max)`` pairs."""
    loop = polygon_loop(p)
    n = len(loop)
    out = []
    for i in range(n):
        v1, v2 = loop[i], loop[(i + 1) % n]
        out.append((v1, v2) if v1 < v2 else (v2, v1))
    return out


def melt_batches(groups_to_melt, polys):
    """Greedy graph colouring of melt groups.

    Two groups that share an edge must not be melted in the same
    ``MCOMMAND_MELT`` call (they would fuse into one N-gon), so groups are
    coloured such that adjacent groups get different colours.  Returns a
    list of batches (lists of group indices), identical to 1.1.x.
    """
    group_edges = []
    for group in groups_to_melt:
        edges = set()
        for pid in group:
            if 0 <= pid < len(polys):
                edges.update(polygon_edges(polys[pid]))
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
        used = {colors[a] for a in adjacent[gi] if colors[a] >= 0}
        color = 0
        while color in used:
            color += 1
        colors[gi] = color
        while len(batches) <= color:
            batches.append([])
        batches[color].append(gi)
    return batches


def vertex_groups_from_corner_normals(corner_normals):
    """``{vertex: {group indices}}`` from ``corner_normals`` keys, built in
    the same insertion order as 1.1.x so set iteration order matches."""
    out = {}
    for vi, gi in corner_normals:
        s = out.get(vi)
        if s is None:
            out[vi] = {gi}
        else:
            s.add(gi)
    return out


def vertex_groups(polys, poly_face_val):
    """``{vertex: {group indices}}`` for the pre-melt polygons.  Used when
    no normals were sent (1.1.x then mapped every melted polygon to 0)."""
    out = {}
    for p, gi in zip(polys, poly_face_val):
        for v in polygon_loop(p):
            s = out.get(v)
            if s is None:
                out[v] = {gi}
            else:
                s.add(gi)
    return out


def post_melt_face_map(post_polys, identity_to_group, vert_groups):
    """Map each post-melt polygon back to its Plasticity group.

    Polygons untouched by the melt are found by identity; the new polygons
    an N-gon melt creates are assigned the group that all their corners
    share (the first one if several do, 0 if none).
    """
    result = []
    empty = frozenset()
    for p in post_polys:
        gi = identity_to_group.get(p)
        if gi is not None:
            result.append(gi)
            continue
        loop = polygon_loop(p)
        common = set(vert_groups.get(loop[0], empty))
        for v in loop[1:]:
            common &= vert_groups.get(v, empty)
            if len(common) <= 1:
                break
        result.append(next(iter(common)) if common else 0)
    return result


# =============================================================================
# Normal tag data
# =============================================================================

def _quantize(normals, scale=NORMAL_INT16_SCALE):
    lo, hi = -scale, scale
    return [int(max(lo, min(hi, v * scale))) for v in normals]


def _pack_vertex_normals(normals, scale=NORMAL_INT16_SCALE):
    """Per-vertex 6-byte blobs (int16 x, z, y in native byte order)."""
    nv = len(normals) // 3
    q = _quantize(normals[:nv * 3], scale)
    inter = array("h", bytes(2 * 3 * nv))
    inter[0::3] = array("h", q[0::3])
    inter[1::3] = array("h", q[2::3])
    inter[2::3] = array("h", q[1::3])
    raw = inter.tobytes()
    return [raw[i * 6:i * 6 + 6] for i in range(nv)]


_ZERO_NORMAL = bytes(6)


def pack_vertex_normals(normals, corners, scale=NORMAL_INT16_SCALE):
    """Low-level NormalTag data: 12 int16 per polygon.

    ``normals`` is the flat per-vertex Plasticity normal array, ``corners``
    the flat original vertex ids (4 per polygon: corners a, b, c, d).  Each
    corner gets ``(nx, nz, ny)`` quantised (clamped, truncated) like 1.1.x;
    ids without a normal get ``(0, 0, 0)``.  Bytes are in native order
    because the tag buffer is raw in-memory data.
    """
    pv = _pack_vertex_normals(normals, scale)
    nv = len(pv)
    if not corners:
        return b""
    if 0 <= min(corners) and max(corners) < nv:
        return b"".join([pv[i] for i in corners])
    z = _ZERO_NORMAL
    return b"".join([pv[i] if 0 <= i < nv else z for i in corners])


def pack_corner_normals(polys, face_map, corner_normals,
                        fallback=(0.0, 1.0, 0.0), scale=NORMAL_INT16_SCALE):
    """Low-level NormalTag data after an N-gon melt.

    Each corner of polygon ``i`` gets ``corner_normals[(vertex, group)]``
    where ``group = face_map[i]`` (0 past the end of the map).  Triangles
    repeat corner c for d.  Values are already in C4D axis order.
    """
    cache = {}
    out = []
    n_map = len(face_map)
    lo, hi = -scale, scale
    for i, (a, b, c, d) in enumerate(polys):
        gi = face_map[i] if i < n_map else 0
        for v in (a, b, c, c if c == d else d):
            key = (v, gi)
            blob = cache.get(key)
            if blob is None:
                t = corner_normals.get(key, fallback)
                blob = array("h", [int(max(lo, min(hi, t[0] * scale))),
                                   int(max(lo, min(hi, t[1] * scale))),
                                   int(max(lo, min(hi, t[2] * scale)))]
                             ).tobytes()
                cache[key] = blob
            out.append(blob)
    return b"".join(out)


def unpack_normals(raw, scale=NORMAL_INT16_SCALE):
    """Inverse of the packers: per polygon a 4-tuple of ``(x, y, z)``
    normals.  Used for the high-level ``NormalTag.Set`` fallback."""
    a = array("h")
    a.frombytes(raw)
    inv = 1.0 / scale
    vals = [v * inv for v in a]
    out = []
    for i in range(0, len(vals) - 11, 12):
        out.append(((vals[i], vals[i + 1], vals[i + 2]),
                    (vals[i + 3], vals[i + 4], vals[i + 5]),
                    (vals[i + 6], vals[i + 7], vals[i + 8]),
                    (vals[i + 9], vals[i + 10], vals[i + 11])))
    return out


# =============================================================================
# Poly-face map storage
# =============================================================================

# 1.1.x stored the map as a JSON list with one int per polygon.  1.2 stores
# run-length pairs in a small versioned JSON object, which is orders of
# magnitude smaller for real meshes (polygons of one CAD face are contiguous)
# and still parses with the C json module.  Both formats are read.
_PFM_RLE_PREFIX = '{"v":1,"rle":'


def encode_poly_face_map(pfm):
    """Encode a poly-face map as ``{"v":1,"rle":[value, count, ...]}``."""
    runs = []
    prev = None
    count = 0
    for v in pfm:
        if v == prev:
            count += 1
        else:
            if count:
                runs.append(prev)
                runs.append(count)
            prev = v
            count = 1
    if count:
        runs.append(prev)
        runs.append(count)
    return _PFM_RLE_PREFIX + json.dumps(runs, separators=(",", ":")) + "}"


def decode_poly_face_map(s):
    """Decode either the 1.2 run-length format or the 1.1.x JSON list.

    Returns ``None`` for empty or unreadable data (callers treat that as
    "no map") instead of raising.
    """
    if not s:
        return None
    try:
        data = json.loads(s)
    except ValueError:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get("v") == 1:
        runs = data.get("rle") or []
        out = []
        for i in range(0, len(runs) - 1, 2):
            out.extend([runs[i]] * runs[i + 1])
        return out
    return None


# =============================================================================
# Edge maps / utilities on plain index data
# =============================================================================

def build_edge_map(polys, poly_face_map, poly_count=None):
    """Global edge map: ``(min_v, max_v) → [(poly, slot, group), ...]``.

    Slots follow C4D's edge numbering (edge index = poly * 4 + slot):
    0 = a-b, 1 = b-c, 2 = c-d (quads only), 3 = d-a (= c-a for triangles).
    """
    if poly_count is None:
        poly_count = len(polys)
    edge_map = {}
    map_len = min(len(poly_face_map), poly_count, len(polys))
    for poly_idx in range(map_len):
        group_idx = poly_face_map[poly_idx]
        a, b, c, d = polys[poly_idx]
        if c == d:
            slots = ((0, a, b), (1, b, c), (3, d, a))
        else:
            slots = ((0, a, b), (1, b, c), (3, d, a), (2, c, d))
        for slot, v1, v2 in slots:
            key = (v1, v2) if v1 < v2 else (v2, v1)
            lst = edge_map.get(key)
            if lst is None:
                edge_map[key] = [(poly_idx, slot, group_idx)]
            else:
                lst.append((poly_idx, slot, group_idx))
    return edge_map


def boundary_edge_addresses(edge_map):
    """Edges on an open border or between two different CAD faces —
    ``(poly, slot)`` pairs (the "Store Edges" set)."""
    out = []
    for occ in edge_map.values():
        if len(occ) == 1:
            out.append((occ[0][0], occ[0][1]))
        elif len(occ) == 2 and occ[0][2] != occ[1][2]:
            out.append((occ[0][0], occ[0][1]))
    return out


def triangle_normal(pa, pb, pc):
    """Unit normal of triangle (pa, pb, pc) — points as ``(x, y, z)``."""
    ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
    vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length > 1e-12:
        return (nx / length, ny / length, nz / length)
    return (0.0, 1.0, 0.0)


def sharp_edge_addresses(edge_map, polys, points, cos_threshold=None):
    """Plasticity boundary edges, optionally only those whose two adjacent
    polygons meet at more than the threshold angle.

    ``cos_threshold`` is ``cos(angle)``; ``None`` disables the filter.
    Polygon normals use corners a, b, c, like 1.1.x.
    """
    normals = None
    if cos_threshold is not None:
        normals = {}

    def normal_of(pi):
        n = normals.get(pi)
        if n is None:
            a, b, c, _ = polys[pi]
            n = triangle_normal(points[a], points[b], points[c])
            normals[pi] = n
        return n

    out = []
    for occ in edge_map.values():
        if len(occ) == 1:
            out.append((occ[0][0], occ[0][1]))
        elif len(occ) == 2:
            if occ[0][2] == occ[1][2]:
                continue
            if normals is not None:
                n0 = normal_of(occ[0][0])
                n1 = normal_of(occ[1][0])
                if n0[0] * n1[0] + n0[1] * n1[1] + n0[2] * n1[2] >= cos_threshold:
                    continue
            out.append((occ[0][0], occ[0][1]))
    return out


def perimeter_edge_addresses(edge_map, selected_groups):
    """Edges on the outline of the selected CAD face groups."""
    out = []
    for occ in edge_map.values():
        if len(occ) == 1:
            if occ[0][2] in selected_groups:
                out.append((occ[0][0], occ[0][1]))
        elif len(occ) == 2:
            if (occ[0][2] in selected_groups) != (occ[1][2] in selected_groups):
                out.append((occ[0][0], occ[0][1]))
    return out


def expand_to_groups(poly_face_map, selected_polys):
    """All polygons whose group contains at least one selected polygon."""
    groups = {poly_face_map[i] for i in selected_polys
              if 0 <= i < len(poly_face_map)}
    return groups, [i for i, g in enumerate(poly_face_map) if g in groups]


# =============================================================================
# Export: N-gon reconstruction and PUT_SOME payload
# =============================================================================

def ngon_outline(polys, members):
    """Ordered outer loop (C4D winding) of the N-gon made of ``members``.

    Uses the directed boundary edges of the member polygons.  Returns
    ``None`` when the boundary is not a single simple loop (holes, pinched
    vertices, inconsistent winding) — callers then fall back to exporting
    the member polygons individually.
    """
    count = {}
    loops = []
    for pi in members:
        loop = polygon_loop(polys[pi])
        loops.append(loop)
        n = len(loop)
        for i in range(n):
            v1, v2 = loop[i], loop[(i + 1) % n]
            if v1 == v2:
                continue
            key = (v1, v2) if v1 < v2 else (v2, v1)
            count[key] = count.get(key, 0) + 1
    nxt = {}
    order = []
    for loop in loops:
        n = len(loop)
        for i in range(n):
            v1, v2 = loop[i], loop[(i + 1) % n]
            if v1 == v2:
                continue
            key = (v1, v2) if v1 < v2 else (v2, v1)
            if count[key] == 1:
                if v1 in nxt:
                    return None
                nxt[v1] = v2
                order.append(v1)
    if len(nxt) < 3:
        return None
    start = order[0]
    out = [start]
    v = nxt[start]
    while v != start:
        if v not in nxt or len(out) > len(nxt):
            return None
        out.append(v)
        v = nxt[v]
    if len(out) != len(nxt):
        return None
    return out


def groups_from_polymap(polymap, poly_count):
    """Visible faces from ``PolygonObject.GetPolygonTranslationMap()``.

    ``polymap[i]`` is the N-gon (visible face) index of polygon ``i``.
    Returns lists of polygon indices, one per visible face, ordered by each
    face's lowest polygon index — so a mesh without N-gons yields
    ``[[0], [1], ...]`` and exports in the original polygon order.  A map of
    the wrong length is ignored (every polygon becomes its own face).

    ``GetNGonTranslationMap`` is deliberately not used: its documented
    return layout is known to be wrong (Maxon forum, topic 12819), and the
    per-polygon map already carries all the information.
    """
    if polymap is None or len(polymap) != poly_count:
        return [[i] for i in range(poly_count)]
    groups = {}
    for i, face in enumerate(polymap):
        lst = groups.get(face)
        if lst is None:
            groups[face] = [i]
        else:
            lst.append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def faces_from_groups(polys, groups):
    """Visible faces (C4D-winding vertex loops) from polygon groups.

    Returns ``(faces, fragmented)``: ``fragmented`` counts N-gons whose
    outline could not be rebuilt and were exported as their fragments.
    """
    faces = []
    fragmented = 0
    for members in groups:
        if len(members) == 1:
            faces.append(polygon_loop(polys[members[0]]))
            continue
        outline = ngon_outline(polys, members)
        if outline is None:
            fragmented += 1
            faces.extend(polygon_loop(polys[p]) for p in members)
        else:
            faces.append(outline)
    return faces, fragmented


def faces_to_plasticity(faces, vertex_offset=0):
    """C4D loops → Plasticity ``(indices, sizes)`` with reversed winding."""
    indices = []
    sizes = []
    for loop in faces:
        sizes.append(len(loop))
        indices.append(loop[0] + vertex_offset)
        indices.extend(v + vertex_offset for v in reversed(loop[1:]))
    return indices, sizes


def merge_mesh_parts(parts):
    """Concatenate ``(positions, faces)`` parts into one PUT_SOME mesh.

    ``positions`` are flat Plasticity-space floats, ``faces`` C4D-winding
    loops indexing that part's vertices.  Returns
    ``(positions, indices, sizes)``.
    """
    positions = []
    indices = []
    sizes = []
    offset = 0
    for part_positions, part_faces in parts:
        positions.extend(part_positions)
        idx, sz = faces_to_plasticity(part_faces, offset)
        indices.extend(idx)
        sizes.extend(sz)
        offset += len(part_positions) // 3
    return positions, indices, sizes


# =============================================================================
# Outbox helpers
# =============================================================================

def effective_editor_visibility(modes, on, off):
    """Effective editor visibility of an object.

    ``modes`` lists editor modes from the object up to the top-level
    ancestor.  The nearest explicit mode wins (``on``/``off``); undefined
    modes defer to the parent; all undefined means visible.
    """
    for m in modes:
        if m == on:
            return True
        if m == off:
            return False
    return True


def dedupe_ids(ids):
    """Indices of entries whose id already appeared earlier in ``ids`` —
    i.e. copies that must get a fresh id (duplicated objects carry the
    original's persistent IDs)."""
    seen = set()
    dupes = []
    for i, v in enumerate(ids):
        if v and v in seen:
            dupes.append(i)
        elif v:
            seen.add(v)
    return dupes
