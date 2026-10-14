"""
Builders for server -> client messages, following the layouts the
Plasticity Blender Bridge reads (tests/reference/blender_v221.py).  Used to
feed the parser and the fake WebSocket server in the client tests.
"""

import struct
import sys
from array import array

_SWAP = sys.byteorder != "little"


def u32(v):
    return struct.pack("<I", v)


def i32(v):
    return struct.pack("<i", v)


def string(s):
    b = s.encode("utf-8")
    return u32(len(b)) + b + b"\x00" * ((4 - len(b) % 4) % 4)


def _le(a):
    if _SWAP:
        a.byteswap()
    return a.tobytes()


def f32s(values):
    return _le(array("f", values))


def i32s(values):
    return _le(array("i", values))


def mesh_object(obj_id, name, vertices=(), faces=(), normals=(), groups=(),
                face_ids=(), obj_type=0, version=1, parent_id=0,
                material_id=-1, flags=6):
    out = (u32(obj_type) + u32(obj_id) + u32(version) + i32(parent_id)
           + i32(material_id) + u32(flags) + string(name))
    if obj_type in (0, 1):
        out += u32(len(vertices) // 3) + f32s(vertices)
        out += u32(len(faces) // 3) + i32s(faces)
        out += u32(len(normals) // 3) + f32s(normals)
        out += u32(len(groups)) + i32s(groups)
        out += u32(len(face_ids)) + i32s(face_ids)
    return out


def group_object(obj_id, name, parent_id=0, flags=6):
    return mesh_object(obj_id, name, obj_type=5, parent_id=parent_id,
                       flags=flags)


def item(item_type, objects):
    body = u32(item_type) + u32(len(objects)) + b"".join(objects)
    return u32(len(body)) + body


def delete_item(ids):
    body = u32(3) + u32(len(ids)) + b"".join(u32(i) for i in ids)
    return u32(len(body)) + body


def transaction(filename, items, version=1):
    return (u32(0) + string(filename) + u32(version) + u32(len(items))
            + b"".join(items))


def list_response(filename, items, message_id=1, code=200, version=1,
                  msg_type=20):
    head = u32(msg_type) + u32(message_id) + u32(code)
    if code != 200:
        return head
    return head + string(filename) + u32(version) + u32(len(items)) \
        + b"".join(items)


def refacet_response(filename, objs, message_id=1, code=200, version=1):
    out = u32(26) + u32(message_id) + u32(code)
    if code != 200:
        return out
    out += string(filename) + u32(version) + u32(len(objs))
    for o in objs:
        out += u32(o["id"]) + u32(o.get("version", 1))
        out += u32(len(o["faces"])) + i32s(o["faces"])
        out += u32(len(o["vertices"])) + f32s(o["vertices"])
        out += u32(len(o["indices"])) + i32s(o["indices"])
        out += u32(len(o["normals"])) + f32s(o["normals"])
        out += u32(len(o["groups"])) + i32s(o["groups"])
        out += u32(len(o["face_ids"])) + i32s(o["face_ids"])
    return out


def new_version(filename, version):
    return u32(10) + string(filename) + u32(version)


def new_file(filename):
    return u32(11) + string(filename)


def handshake_response(message_id, types):
    return u32(100) + u32(message_id) + u32(len(types)) + \
        b"".join(u32(t) for t in types)


def put_some_response(message_id, code, groups=(), items=()):
    out = u32(31) + u32(message_id) + u32(code) + u32(len(groups))
    for cid, gid in groups:
        out += string(cid) + u32(gid)
    out += u32(len(items))
    for cid, sid, ver in items:
        out += string(cid) + u32(sid) + u32(ver)
    return out
