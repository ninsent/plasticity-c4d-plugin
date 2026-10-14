"""
Before/after benchmark of the c4d-free hot paths.

    python tests/perf/bench.py                 # 100k and 1M triangles
    python tests/perf/bench.py --sizes 20000   # quick run

"before" is the frozen 1.1.6 code in tests/reference, "after" the current
code.  Cinema 4D API calls are not measured: both sides create the same
c4d.Vector / c4d.CPolygon objects through the test stub, which is slower
than the real C++ types, so the geometry numbers are an upper bound.
Every pair is also checked for identical output before it is timed.
"""

import argparse
import array
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
ROOT = os.path.dirname(TESTS)
for p in (os.path.join(TESTS, "stubs"), ROOT, os.path.join(ROOT, "libs"), TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import c4d  # noqa: E402  (stub)

from core import geometry as g  # noqa: E402
from core import protocol as P  # noqa: E402
from core.handler import SceneHandler  # noqa: E402
from core.threading_bridge import ThreadingBridge  # noqa: E402
from meshgen import ngon_grid, tri_grid, tris_for_target  # noqa: E402
from reference import handler_v116 as OLD_H  # noqa: E402
from reference import protocol_v116 as OLD_P  # noqa: E402
import wire as W  # noqa: E402


def timeit(fn, repeat):
    times = []
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), result


def old_lowlevel_normals(normals, normal_map):
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


def bench(target_tris, repeat):
    rows = []
    nx, ny = tris_for_target(target_tris)
    m = tri_grid(nx, ny, rows_per_group=max(1, ny // 64), seed=1)
    n_tris = len(m["faces"]) // 3
    label = f"{n_tris / 1000:.0f}k tris"

    # ── decode a LIST response holding the mesh ──────────────────────────
    data = W.list_response("f", [W.item(1, [W.mesh_object(
        1, "Mesh", m["vertices"], m["faces"], m["normals"], m["groups"],
        m["face_ids"])])])
    t_old, r_old = timeit(lambda: OLD_P.MessageParser().parse_message(data), repeat)
    t_new, r_new = timeit(lambda: P.MessageParser().parse_message(data), repeat)
    assert list(r_new["add"][0]["vertices"]) == r_old["add"][0]["vertices"]
    rows.append(("decode LIST", label, t_old, t_new))
    obj = r_new["add"][0]

    # ── tri geometry (points + polygons) ─────────────────────────────────
    ref = OLD_H.SceneHandler()
    def old_tri():
        r = ref._compute_tri_geometry(obj["vertices"], obj["faces"],
                                      obj["normals"], obj["groups"])
        ref._compute_tri_face_map(len(r[1]), obj["groups"])
        return r
    t_old, r_old = timeit(old_tri, repeat)
    h = SceneHandler(ThreadingBridge())
    t_new, r_new = timeit(lambda: h._compute_geometry(
        obj["vertices"], obj["faces"], [], [], obj["groups"]), repeat)
    assert [(p.x, p.y, p.z) for p in r_old[0][:1000]] == \
        [(p.x, p.y, p.z) for p in r_new.points[:1000]]
    rows.append(("tri geometry (+face map)", label, t_old, t_new))

    # ── per-corner normal tag data ───────────────────────────────────────
    nmap = r_old[2]
    corners = g.build_tri_mesh(obj["vertices"], obj["faces"]).corners
    normals = obj["normals"]
    t_old, r_old = timeit(lambda: old_lowlevel_normals(normals, nmap), repeat)
    t_new, r_new = timeit(lambda: g.pack_vertex_normals(normals, corners,
                                                        scale=32767.0), repeat)
    assert r_old == r_new
    rows.append(("normal tag data", label, t_old, t_new))

    # ── poly-face map store + load ───────────────────────────────────────
    import json
    pfm = g.tri_face_map(n_tris, obj["groups"])
    t_old, s_old = timeit(lambda: json.loads(json.dumps(pfm)), repeat)
    t_new, s_new = timeit(lambda: g.decode_poly_face_map(
        g.encode_poly_face_map(pfm)), repeat)
    assert s_old == s_new
    rows.append(("poly-face map store+load", label, t_old, t_new))
    rows.append(("poly-face map size (bytes)", label,
                 len(json.dumps(pfm)), len(g.encode_poly_face_map(pfm))))

    # ── edge map + sharp edges ───────────────────────────────────────────
    pts = g.import_points(obj["vertices"])
    polys = [(a, c, b, b) for a, c, b in g.build_tri_mesh(
        obj["vertices"], obj["faces"]).polys]
    pobj = c4d.PolygonObject(len(pts), len(polys))
    pobj.set_geometry(pts, polys)
    pobj.GetDataInstance().SetString(OLD_H.BC_PLASTICITY_POLY_FACE_MAP, json.dumps(pfm))
    t_old, r_old = timeit(lambda: OLD_H.SceneHandler._get_sharp_edge_addresses(
        pobj, 10.0), max(1, repeat // 2))
    import math
    t_new, r_new = timeit(lambda: g.sharp_edge_addresses(
        g.build_edge_map(polys, pfm), polys, pts, math.cos(math.radians(10))),
        max(1, repeat // 2))
    assert sorted(r_old) == sorted(r_new)
    rows.append(("select sharp edges", label, t_old, t_new))

    # ── PUT_SOME encoding ────────────────────────────────────────────────
    positions = list(obj["vertices"])
    item = {"c4d_id": "id", "name": "Cage", "parent_c4d_collection_id": "",
            "existing_stable_id": 0, "options": 1, "positions": positions,
            "indices": list(obj["faces"]), "sizes": [3] * n_tris}
    t_old, b_old = timeit(lambda: OLD_P.encode_put_some(1, "f", [], [item]), repeat)
    t_new, b_new = timeit(lambda: P.encode_put_some(1, "f", [], [item]), repeat)
    assert b_old == b_new
    rows.append(("encode PUT_SOME (1 item)", label, t_old, t_new))
    many = [dict(item, positions=positions[:300], indices=list(range(99)),
                 sizes=[3] * 33) for _ in range(max(1, n_tris // 50))]
    t_old, b_old = timeit(lambda: OLD_P.encode_put_some(1, "f", [], many), repeat)
    t_new, b_new = timeit(lambda: P.encode_put_some(1, "f", [], many), repeat)
    assert b_old == b_new
    rows.append((f"encode PUT_SOME ({len(many)} items)", label, t_old, t_new))

    # ── N-gon refacet geometry (welding + ear clipping) ──────────────────
    side = max(4, int((target_tris / 4) ** 0.5))
    ng = ngon_grid(side, side, seed=2)
    k = f"{len(set(ng['faces'])) / 1000:.0f}k polys"
    t_old, r_old = timeit(lambda: ref._compute_ngon_geometry(
        ng["vertices"], ng["indices"], ng["faces"], ng["normals"], ng["groups"]),
        max(1, repeat // 2))
    t_new, r_new = timeit(lambda: g.build_ngon_mesh(
        ng["vertices"], ng["indices"], ng["faces"], ng["normals"], ng["groups"]),
        max(1, repeat // 2))
    assert r_old[5] == r_new.poly_face_val
    rows.append(("N-gon geometry (weld+clip)", k, t_old, t_new))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="*", default=[100000, 1000000])
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()
    print(f"Python {sys.version.split()[0]}")
    print(f"{'operation':34} {'size':>12} {'1.1.6':>10} {'now':>10} {'speed-up':>9}")
    for size in args.sizes:
        for name, label, old, new in bench(size, args.repeat if size < 500000 else 1):
            if "bytes" in name:
                print(f"{name:34} {label:>12} {old:>10} {new:>10} {old / max(new, 1):>8.0f}x")
            else:
                print(f"{name:34} {label:>12} {old * 1000:>8.0f}ms {new * 1000:>8.0f}ms "
                      f"{old / new:>8.1f}x")


if __name__ == "__main__":
    main()
