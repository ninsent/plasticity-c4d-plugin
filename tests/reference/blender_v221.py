"""
REFERENCE — wire layouts of the Plasticity Blender Bridge.

Ported from https://github.com/nkallen/plasticity-blender-addon, client.py
(addon version 2.2.1, commit 6f96a0f9058005ddfb51937b4c61736d5e8ac9eb),
used only as a golden reference by the protocol tests.  Changes from the
original: the async/websocket plumbing is removed (functions return bytes or
parsed dicts), numpy is replaced by ``array``, and the dict keys
``blender_*`` read the C4D bridge's ``c4d_*`` keys.  Packing order and
formats are untouched.

Copyright 2023 Plastic Software, LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
"""

import struct
from array import array
from enum import Enum


class MessageType(Enum):
    TRANSACTION_1 = 0
    ADD_1 = 1
    UPDATE_1 = 2
    DELETE_1 = 3
    MOVE_1 = 4
    ATTRIBUTE_1 = 5
    NEW_VERSION_1 = 10
    NEW_FILE_1 = 11
    LIST_ALL_1 = 20
    LIST_SOME_1 = 21
    LIST_VISIBLE_1 = 22
    SUBSCRIBE_ALL_1 = 23
    SUBSCRIBE_SOME_1 = 24
    UNSUBSCRIBE_ALL_1 = 25
    REFACET_SOME_1 = 26
    PUT_SOME_1 = 31
    HANDSHAKE_1 = 100


class FacetShapeType(Enum):
    ANY = 20500
    CUT = 20501
    CONVEX = 20502


# --- client -> server (verbatim packing sequences) --------------------------

def handshake(message_id):
    message = struct.pack("<I", MessageType.HANDSHAKE_1.value)
    message += struct.pack("<I", message_id)
    return message


def simple(msg_type, message_id):
    m = struct.pack("<I", msg_type.value)
    m += struct.pack("<I", message_id)
    return m


def subscribe_some(message_id, filename, plasticity_ids):
    subscribe_message = struct.pack(
        "<I", MessageType.SUBSCRIBE_SOME_1.value)
    subscribe_message += struct.pack(
        "<I", message_id)
    subscribe_message += struct.pack(
        "<I", len(filename))
    subscribe_message += struct.pack(
        f"<{len(filename)}s", filename.encode('utf-8'))
    padding = (4 - (len(filename) % 4)) % 4
    subscribe_message += struct.pack(
        f"<{padding}x")
    subscribe_message += struct.pack(
        "<I", len(plasticity_ids))
    for plasticity_id in plasticity_ids:
        subscribe_message += struct.pack(
            "<I", plasticity_id)
    return subscribe_message


def refacet_some(message_id, filename, plasticity_ids, relative_to_bbox=True,
                 curve_chord_tolerance=0.01, curve_chord_angle=0.35,
                 surface_plane_tolerance=0.01, surface_plane_angle=0.35,
                 match_topology=True, max_sides=3, plane_angle=0, min_width=0,
                 max_width=0, curve_chord_max=0, shape=FacetShapeType.CUT):
    refacet_message = struct.pack(
        "<I", MessageType.REFACET_SOME_1.value)
    refacet_message += struct.pack(
        "<I", message_id)
    refacet_message += struct.pack(
        "<I", len(filename))
    refacet_message += struct.pack(
        f"<{len(filename)}s", filename.encode('utf-8'))
    padding = (4 - (len(filename) % 4)) % 4
    refacet_message += struct.pack(
        f"<{padding}x")
    refacet_message += struct.pack(
        "<I", len(plasticity_ids))
    for plasticity_id in plasticity_ids:
        refacet_message += struct.pack(
            "<I", plasticity_id)
    refacet_message += struct.pack(
        "<I", relative_to_bbox)
    refacet_message += struct.pack(
        "<f", curve_chord_tolerance)
    refacet_message += struct.pack(
        "<f", curve_chord_angle)
    refacet_message += struct.pack(
        "<f", surface_plane_tolerance)
    refacet_message += struct.pack(
        "<f", surface_plane_angle)
    refacet_message += struct.pack(
        "<I", 1 if match_topology else 0)
    refacet_message += struct.pack(
        "<I", max_sides)
    refacet_message += struct.pack(
        "<f", plane_angle)
    refacet_message += struct.pack(
        "<f", min_width)
    refacet_message += struct.pack(
        "<f", max_width)
    refacet_message += struct.pack(
        "<f", curve_chord_max)
    refacet_message += struct.pack(
        "<I", shape.value)
    return refacet_message


def put_some(message_id, filename, groups, items):
    message = struct.pack("<I", MessageType.PUT_SOME_1.value)
    message += struct.pack("<I", message_id)

    filename_bytes = filename.encode('utf-8')
    filename_padding = (4 - (len(filename_bytes) % 4)) % 4
    message += struct.pack("<I", len(filename_bytes))
    message += filename_bytes
    message += b'\x00' * filename_padding

    message += struct.pack("<I", len(groups))
    for group in groups:
        cid = group["c4d_collection_id"].encode('utf-8')
        cid_pad = (4 - (len(cid) % 4)) % 4
        name_bytes = group["name"].encode('utf-8')
        name_padding = (4 - (len(name_bytes) % 4)) % 4
        pid = group["parent_c4d_collection_id"].encode('utf-8')
        pid_pad = (4 - (len(pid) % 4)) % 4

        message += struct.pack("<I", len(cid))
        message += cid
        message += b'\x00' * cid_pad
        message += struct.pack("<I", len(name_bytes))
        message += name_bytes
        message += b'\x00' * name_padding
        message += struct.pack("<I", len(pid))
        message += pid
        message += b'\x00' * pid_pad
        message += struct.pack("<I", group.get("existing_group_id", 0))

    message += struct.pack("<I", len(items))
    for item in items:
        bid = item["c4d_id"].encode('utf-8')
        bid_pad = (4 - (len(bid) % 4)) % 4
        name_bytes = item["name"].encode('utf-8')
        name_padding = (4 - (len(name_bytes) % 4)) % 4
        pid = item["parent_c4d_collection_id"].encode('utf-8')
        pid_pad = (4 - (len(pid) % 4)) % 4

        message += struct.pack("<I", len(bid))
        message += bid
        message += b'\x00' * bid_pad
        message += struct.pack("<I", len(name_bytes))
        message += name_bytes
        message += b'\x00' * name_padding
        message += struct.pack("<I", len(pid))
        message += pid
        message += b'\x00' * pid_pad

        message += struct.pack("<I", item.get("existing_stable_id", 0))
        message += struct.pack("<Q", item["options"])

        positions = item["positions"]
        vertex_count = len(positions) // 3
        message += struct.pack("<I", vertex_count)
        message += struct.pack(f"<{len(positions)}f", *positions)

        indices = item["indices"]
        sizes = item["sizes"]
        face_count = len(sizes)
        index_count = len(indices)

        message += struct.pack("<I", face_count)
        message += struct.pack("<I", index_count)
        message += struct.pack(f"<{index_count}I", *indices)
        message += struct.pack(f"<{face_count}I", *sizes)
    return message


# --- server -> client decoding (Blender's reader, numpy → array) -----------

def _i(view, off, signed=False):
    return int.from_bytes(view[off:off + 4], 'little', signed=signed)


def _arr(view, off, n, tc):
    a = array(tc)
    a.frombytes(view[off:off + n * 4].tobytes())
    return a


def decode_object_data(view, offset):
    object_type = _i(view, offset); offset += 4
    object_id = _i(view, offset); offset += 4
    version_id = _i(view, offset); offset += 4
    parent_id = _i(view, offset, True); offset += 4
    material_id = _i(view, offset, True); offset += 4
    flags = _i(view, offset); offset += 4
    name_length = _i(view, offset); offset += 4
    name = view[offset:offset + name_length].tobytes().decode('utf-8')
    offset += name_length
    offset += (4 - (name_length % 4)) % 4
    vertices = faces = normals = groups = face_ids = None
    if object_type in (0, 1):
        n = _i(view, offset); offset += 4
        vertices = _arr(view, offset, n * 3, 'f'); offset += n * 12
        n = _i(view, offset); offset += 4
        faces = _arr(view, offset, n * 3, 'i'); offset += n * 12
        n = _i(view, offset); offset += 4
        normals = _arr(view, offset, n * 3, 'f'); offset += n * 12
        n = _i(view, offset); offset += 4
        groups = _arr(view, offset, n, 'i').tolist(); offset += n * 4
        n = _i(view, offset); offset += 4
        face_ids = _arr(view, offset, n, 'i').tolist(); offset += n * 4
    return dict(type=object_type, id=object_id, version=version_id,
                parent_id=parent_id, material_id=material_id, flags=flags,
                name=name, vertices=vertices, faces=faces, normals=normals,
                groups=groups, face_ids=face_ids), offset


def decode_objects(view):
    num_objects = _i(view, 0)
    offset = 4
    out = []
    for _ in range(num_objects):
        obj, offset = decode_object_data(view, offset)
        out.append(obj)
    return out


def decode_transaction(data, has_list_header):
    view = memoryview(data)
    offset = 4
    if has_list_header:
        offset += 8
    flen = _i(view, offset); offset += 4
    filename = view[offset:offset + flen].tobytes().decode('utf-8')
    offset += flen + (4 - (flen % 4)) % 4
    version = _i(view, offset); offset += 4
    num_messages = _i(view, offset); offset += 4
    tx = {"filename": filename, "version": version, "delete": [], "add": [],
          "update": []}
    for _ in range(num_messages):
        item_length = _i(view, offset); offset += 4
        item = view[offset:offset + item_length]
        t = _i(item, 0)
        if t == 3:
            # NOTE: Blender reads the count from item[:4] (the type field);
            # the IDs start at offset 8.  We read the count at offset 4.
            n = _i(item, 4)
            tx["delete"].extend(_arr(item, 8, n, 'i').tolist())
        elif t == 1:
            tx["add"].extend(decode_objects(item[4:]))
        elif t == 2:
            tx["update"].extend(decode_objects(item[4:]))
        offset += item_length
    return tx
