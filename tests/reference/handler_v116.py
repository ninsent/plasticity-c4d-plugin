"""
FROZEN REFERENCE — do not edit.

Verbatim copies of the geometry functions from Plasticity Bridge 1.1.6
(commit 13563f2, core/handler.py), extracted with ``ast`` so the test suite
can prove that the refactored / optimised implementations in ``core``
produce identical output on the same inputs.  Only the module header below
was added; function bodies are unchanged.
"""

import array
import bisect
import json
import math
import uuid

import c4d

BC_PLASTICITY_ID = 1066930
BC_PLASTICITY_POLY_FACE_MAP = 1066935
BC_PLASTICITY_PNS_ID = 1066938
_IMPORT_SCALE = 100.0
_KIND_SUBD = 1
MANAGED_NORMAL_TAG_NAME = "__plasticity_normals__"


def _get_poly_face_map_only(obj):
    bc = obj.GetDataInstance()
    pfm = json.loads(bc.GetString(BC_PLASTICITY_POLY_FACE_MAP, "") or "[]")
    if not pfm or obj.GetPolygonCount() == 0:
        return None
    return pfm, obj.GetPolygonCount()


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
    unit_scale = 1.0

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

    @staticmethod
    def _store_poly_face_map(obj, poly_face_map):
        bc = obj.GetDataInstance()
        bc.SetString(BC_PLASTICITY_POLY_FACE_MAP, json.dumps(poly_face_map))

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
