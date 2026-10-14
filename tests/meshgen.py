"""
Synthetic Plasticity-style mesh data for tests and benchmarks.

Plasticity tessellates each CAD face separately, so vertices are shared
inside a face group but duplicated along group borders, and normals are sent
per vertex.  ``groups`` is a flat ``[start, count, ...]`` array in loop
(index) positions and ``face_ids`` has one entry per group.
"""

import math
import random


def tri_grid(nx, ny, rows_per_group=4, seed=0, wavy=True):
    """A height-field grid split into horizontal face groups.

    Returns a dict with ``vertices``, ``faces`` (tri indices), ``normals``,
    ``groups``, ``face_ids`` — the layout of an ADD/UPDATE object.
    Triangle count = 2 * nx * ny.
    """
    rnd = random.Random(seed)
    vertices, normals, indices, groups, face_ids = [], [], [], [], []
    row = 0
    while row < ny:
        rows = min(rows_per_group, ny - row)
        base = len(vertices) // 3
        start = len(indices)
        for j in range(row, row + rows + 1):
            for i in range(nx + 1):
                x = i / nx
                y = j / ny
                z = 0.05 * math.sin(6.0 * x) * math.cos(4.0 * y) if wavy else 0.0
                vertices += [x, y, z + rnd.uniform(-1e-4, 1e-4)]
                n = (-0.3 * math.cos(6.0 * x), 0.2 * math.sin(4.0 * y), 1.0)
                ln = math.sqrt(sum(c * c for c in n))
                normals += [n[0] / ln, n[1] / ln, n[2] / ln]
        w = nx + 1
        for jj in range(rows):
            for i in range(nx):
                a = base + jj * w + i
                b = a + 1
                c = a + w
                d = c + 1
                indices += [a, b, d, a, d, c]
        groups += [start, len(indices) - start]
        face_ids.append(1000 + len(face_ids))
        row += rows
    return {"vertices": vertices, "faces": indices, "normals": normals,
            "groups": groups, "face_ids": face_ids}


def ngon_grid(nx, ny, seed=0, concave_every=7, ngon_every=3):
    """Refacet-style N-gon payload over a grid of cells.

    Most cells are quads; every ``ngon_every``-th cell pair is merged into a
    hexagon (with a collinear midpoint), and every ``concave_every``-th
    L-shaped triple becomes a concave octagon.  Every polygon gets its own
    (unwelded) vertices so welding has work to do.  Returns ``vertices``,
    ``indices``, ``faces`` (per-loop polygon id), ``normals``, ``groups``,
    ``face_ids``.
    """
    rnd = random.Random(seed)
    vertices, normals, indices, faces = [], [], [], []
    groups, face_ids = [], []
    poly_id = 0

    def P(i, j):
        return (i / nx, j / ny, 0.0)

    def emit(loop_pts):
        nonlocal poly_id
        for (x, y, z) in loop_pts:
            vertices.extend((x, y, z))
            normals.extend((0.0, 0.0, 1.0))
            indices.append(len(vertices) // 3 - 1)
            faces.append(poly_id)
        poly_id += 1

    used = set()
    group_start = 0
    cells_in_group = 0
    for j in range(ny):
        for i in range(nx):
            if (i, j) in used:
                continue
            k = j * nx + i
            if (k % concave_every == 0 and i + 1 < nx and j + 1 < ny
                    and (i + 1, j) not in used and (i, j + 1) not in used):
                # L-shape over (i,j), (i+1,j), (i,j+1): concave octagon-ish
                loop = [P(i, j), P(i + 1, j), P(i + 2, j), P(i + 2, j + 1),
                        P(i + 1, j + 1), P(i + 1, j + 2), P(i, j + 2),
                        P(i, j + 1)]
                used.update({(i, j), (i + 1, j), (i, j + 1)})
                emit(loop)
            elif k % ngon_every == 0 and i + 1 < nx and (i + 1, j) not in used:
                loop = [P(i, j), P(i + 1, j), P(i + 2, j), P(i + 2, j + 1),
                        P(i + 1, j + 1), P(i, j + 1)]
                used.update({(i, j), (i + 1, j)})
                emit(loop)
            else:
                loop = [P(i, j), P(i + 1, j), P(i + 1, j + 1), P(i, j + 1)]
                used.add((i, j))
                emit(loop)
            cells_in_group += 1
            if cells_in_group >= 12 + rnd.randint(0, 6):
                groups += [group_start, len(indices) - group_start]
                face_ids.append(500 + len(face_ids))
                group_start = len(indices)
                cells_in_group = 0
    if len(indices) > group_start:
        groups += [group_start, len(indices) - group_start]
        face_ids.append(500 + len(face_ids))
    # tiny jitter that survives round(.., 7) welding identically
    return {"vertices": vertices, "indices": indices, "faces": faces,
            "normals": normals, "groups": groups, "face_ids": face_ids}


def tris_for_target(target_tris):
    """(nx, ny) for a tri_grid of roughly ``target_tris`` triangles."""
    side = int(math.sqrt(target_tris / 2.0))
    return side, max(1, int(target_tris / 2.0 / side))
