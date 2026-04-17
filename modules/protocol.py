"""
Protocol definitions and binary message parsing for Plasticity WebSocket communication.

All message types and binary formats match the Blender addon exactly.
Uses struct.unpack_from for fast bulk decoding instead of Python loops.
"""

import struct
from enum import IntEnum
from typing import Tuple, List, Dict, Any, Optional


class MessageType(IntEnum):
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


class ObjectType(IntEnum):
    SOLID = 0
    SHEET = 1
    WIRE = 2
    GROUP = 5
    EMPTY = 6


class FacetShapeType(IntEnum):
    ANY = 20500
    CUT = 20501
    CONVEX = 20502


# =============================================================================
# Encoding helpers
# =============================================================================

def _encode_string(s: str) -> bytes:
    """Encode a UTF-8 string with a uint32 length prefix and 4-byte alignment padding."""
    encoded = s.encode('utf-8')
    padding = b'\x00' * ((4 - len(encoded) % 4) % 4)
    return struct.pack("<I", len(encoded)) + encoded + padding


def _encode_id_array(ids: List[int]) -> bytes:
    """Encode a list of uint32 IDs with a uint32 count prefix."""
    return struct.pack(f"<{len(ids) + 1}I", len(ids), *ids)


# =============================================================================
# Encoding (client -> server)
# =============================================================================

def encode_list_all(message_id: int) -> bytes:
    return struct.pack("<II", MessageType.LIST_ALL_1, message_id)


def encode_list_visible(message_id: int) -> bytes:
    return struct.pack("<II", MessageType.LIST_VISIBLE_1, message_id)


def encode_subscribe_all(message_id: int) -> bytes:
    return struct.pack("<II", MessageType.SUBSCRIBE_ALL_1, message_id)


def encode_unsubscribe(message_id: int) -> bytes:
    return struct.pack("<II", MessageType.UNSUBSCRIBE_ALL_1, message_id)


def encode_subscribe_some(message_id: int, filename: str,
                          plasticity_ids: List[int]) -> bytes:
    return (struct.pack("<II", MessageType.SUBSCRIBE_SOME_1, message_id)
            + _encode_string(filename)
            + _encode_id_array(plasticity_ids))


def encode_refacet_some(
    message_id, filename, plasticity_ids,
    relative_to_bbox=True, curve_chord_tolerance=0.01, curve_chord_angle=0.35,
    surface_plane_tolerance=0.01, surface_plane_angle=0.35,
    match_topology=True, max_sides=3, plane_angle=0.0,
    min_width=0.0, max_width=0.0, curve_chord_max=0.0,
    shape=FacetShapeType.CUT
) -> bytes:
    return (struct.pack("<II", MessageType.REFACET_SOME_1, message_id)
            + _encode_string(filename)
            + _encode_id_array(plasticity_ids)
            + struct.pack(
                "<IffffIIffffI",
                1 if relative_to_bbox else 0,
                curve_chord_tolerance, curve_chord_angle,
                surface_plane_tolerance, surface_plane_angle,
                1 if match_topology else 0,
                max_sides, plane_angle,
                min_width, max_width, curve_chord_max,
                shape.value,
            ))


def encode_handshake(message_id: int) -> bytes:
    """Encode a HANDSHAKE_1 message (client -> server)."""
    return struct.pack("<II", MessageType.HANDSHAKE_1, message_id)


def encode_put_some(message_id: int, filename: str,
                    groups: List[Dict], items: List[Dict]) -> bytes:
    """Encode a PUT_SOME_1 message (client -> server).

    groups: list of dicts with keys:
        c4d_collection_id, name, parent_c4d_collection_id, existing_group_id
    items: list of dicts with keys:
        c4d_id, name, parent_c4d_collection_id, existing_stable_id,
        options (uint64), positions (flat float list),
        indices (flat uint32 list), sizes (uint32 list of face vertex counts)
    """
    msg = struct.pack("<II", MessageType.PUT_SOME_1, message_id)

    # Filename
    msg += _encode_string(filename)

    # Groups
    msg += struct.pack("<I", len(groups))
    for g in groups:
        msg += _encode_string(g["c4d_collection_id"])
        msg += _encode_string(g["name"])
        msg += _encode_string(g["parent_c4d_collection_id"])
        msg += struct.pack("<I", g.get("existing_group_id", 0))

    # Items
    msg += struct.pack("<I", len(items))
    for item in items:
        msg += _encode_string(item["c4d_id"])
        msg += _encode_string(item["name"])
        msg += _encode_string(item["parent_c4d_collection_id"])
        msg += struct.pack("<I", item.get("existing_stable_id", 0))
        msg += struct.pack("<Q", item["options"])  # uint64

        positions = item["positions"]
        vertex_count = len(positions) // 3
        msg += struct.pack("<I", vertex_count)
        msg += struct.pack(f"<{len(positions)}f", *positions)

        indices = item["indices"]
        sizes = item["sizes"]
        face_count = len(sizes)
        index_count = len(indices)
        msg += struct.pack("<I", face_count)
        msg += struct.pack("<I", index_count)
        msg += struct.pack(f"<{index_count}I", *indices)
        msg += struct.pack(f"<{face_count}I", *sizes)

    return msg


# =============================================================================
# Decoding helpers (fast bulk reads)
# =============================================================================

def _read_u32(view, offset):
    return struct.unpack_from('<I', view, offset)[0], offset + 4


def _read_i32(view, offset):
    return struct.unpack_from('<i', view, offset)[0], offset + 4


def _read_f32(view, offset):
    return struct.unpack_from('<f', view, offset)[0], offset + 4


def _read_string(view, offset):
    length, offset = _read_u32(view, offset)
    if length == 0:
        return "", offset
    raw = bytes(view[offset:offset + length])
    try:
        s = raw.decode('utf-8')
    except UnicodeDecodeError:
        s = raw.decode('utf-8', errors='replace')
    offset += length
    offset += (4 - length % 4) % 4  # padding
    return s, offset


def _read_float_array(view, offset, count):
    """Read `count` float32 values as a flat list."""
    if count == 0:
        return [], offset
    byte_len = count * 4
    data = struct.unpack_from(f'<{count}f', view, offset)
    return list(data), offset + byte_len


def _read_int_array(view, offset, count):
    """Read `count` int32 values as a flat list."""
    if count == 0:
        return [], offset
    byte_len = count * 4
    data = struct.unpack_from(f'<{count}i', view, offset)
    return list(data), offset + byte_len


def _read_uint_array(view, offset, count):
    """Read `count` uint32 values as a flat list."""
    if count == 0:
        return [], offset
    byte_len = count * 4
    data = struct.unpack_from(f'<{count}I', view, offset)
    return list(data), offset + byte_len


# =============================================================================
# Object decoding (matches Blender decode_object_data exactly)
# =============================================================================

def decode_object_data(view, offset):
    """
    Decode one object from binary data.

    Returns tuple:
        (object_type, object_id, version_id, parent_id, material_id, flags,
         name, vertices, faces, normals, new_offset, groups, face_ids)
    """
    object_type, offset = _read_u32(view, offset)
    object_id, offset = _read_u32(view, offset)
    version_id, offset = _read_u32(view, offset)
    parent_id, offset = _read_i32(view, offset)
    material_id, offset = _read_i32(view, offset)
    flags, offset = _read_u32(view, offset)
    name, offset = _read_string(view, offset)

    vertices = []
    faces = []
    normals = []
    groups = []
    face_ids = []

    if object_type in (ObjectType.SOLID, ObjectType.SHEET):
        # Vertices: count then float32 * count * 3 (12 bytes per vertex)
        num_vertices, offset = _read_u32(view, offset)
        vertices, offset = _read_float_array(view, offset, num_vertices * 3)

        # Faces/indices: count then int32 * count * 3 (12 bytes per face)
        num_faces, offset = _read_u32(view, offset)
        faces, offset = _read_int_array(view, offset, num_faces * 3)

        # Normals: count then float32 * count * 3
        num_normals, offset = _read_u32(view, offset)
        normals, offset = _read_float_array(view, offset, num_normals * 3)

        # Groups: count then int32 * count
        num_groups, offset = _read_u32(view, offset)
        groups, offset = _read_int_array(view, offset, num_groups)

        # Face IDs: count then int32 * count
        num_face_ids, offset = _read_u32(view, offset)
        face_ids, offset = _read_int_array(view, offset, num_face_ids)

    elif object_type == ObjectType.GROUP:
        pass  # Groups have no geometry

    return (object_type, object_id, version_id, parent_id, material_id, flags,
            name, vertices, faces, normals, offset, groups, face_ids)


def decode_objects(view, offset):
    """Decode multiple objects from a buffer (after skipping message sub-type)."""
    num_objects, offset = _read_u32(view, offset)
    objects = []
    for _ in range(num_objects):
        (obj_type, obj_id, ver, parent, mat, flags, name,
         verts, faces, normals, offset, groups, face_ids) = decode_object_data(view, offset)
        objects.append({
            'type': obj_type, 'id': obj_id, 'version': ver,
            'parent_id': parent, 'material_id': mat, 'flags': flags,
            'name': name, 'vertices': verts, 'faces': faces,
            'normals': normals, 'groups': groups, 'face_ids': face_ids,
        })
    return objects, offset


# =============================================================================
# Main message parser
# =============================================================================

class MessageParser:
    def parse_message(self, data: bytes) -> Optional[Dict[str, Any]]:
        if len(data) < 4:
            return None

        view = memoryview(data)
        offset = 0

        msg_type_raw, offset = _read_u32(view, offset)
        try:
            message_type = MessageType(msg_type_raw)
        except ValueError:
            print(f"[Protocol] Unknown message type: {msg_type_raw}")
            return None

        if message_type == MessageType.TRANSACTION_1:
            return self._parse_transaction(view, offset, message_type)

        elif message_type in (MessageType.LIST_ALL_1, MessageType.LIST_SOME_1,
                              MessageType.LIST_VISIBLE_1):
            message_id, offset = _read_u32(view, offset)
            code, offset = _read_u32(view, offset)
            if code != 200:
                print(f"[Protocol] List failed with code: {code}")
                return None
            return self._parse_transaction(view, offset, message_type)

        elif message_type == MessageType.REFACET_SOME_1:
            message_id, offset = _read_u32(view, offset)
            return self._parse_refacet(view, offset, message_type)

        elif message_type == MessageType.NEW_VERSION_1:
            return self._parse_new_version(view, offset)

        elif message_type == MessageType.NEW_FILE_1:
            return self._parse_new_file(view, offset)

        elif message_type == MessageType.HANDSHAKE_1:
            return self._parse_handshake(view, offset)

        elif message_type == MessageType.PUT_SOME_1:
            return self._parse_put_some(view, offset)

        else:
            print(f"[Protocol] Unhandled message type: {message_type}")
            return None

    def _parse_transaction(self, view, offset, msg_type):
        """Parse TRANSACTION or LIST response (same binary layout)."""
        filename, offset = _read_string(view, offset)
        version, offset = _read_u32(view, offset)
        num_messages, offset = _read_u32(view, offset)

        transaction = {
            'type': msg_type,
            'filename': filename,
            'version': version,
            'delete': [],
            'add': [],
            'update': [],
        }

        for _ in range(num_messages):
            item_length, offset = _read_u32(view, offset)
            if item_length == 0:
                continue

            item_view = view[offset:offset + item_length]
            item_type_raw = struct.unpack_from('<I', item_view, 0)[0]

            try:
                item_type = MessageType(item_type_raw)
            except ValueError:
                offset += item_length
                continue

            if item_type == MessageType.DELETE_1:
                num_deleted = struct.unpack_from('<I', item_view, 4)[0]
                for i in range(num_deleted):
                    del_id = struct.unpack_from('<I', item_view, 8 + i * 4)[0]
                    transaction['delete'].append(del_id)

            elif item_type in (MessageType.ADD_1, MessageType.UPDATE_1):
                objects, _ = decode_objects(item_view, 4)
                key = 'add' if item_type == MessageType.ADD_1 else 'update'
                transaction[key].extend(objects)

            offset += item_length

        return transaction

    def _parse_refacet(self, view, offset, msg_type):
        """Parse REFACET_SOME response."""
        code, offset = _read_u32(view, offset)
        if code != 200:
            print(f"[Protocol] Refacet failed with code {code}")
            return {'type': msg_type, 'error': code, 'refaceted_objects': []}

        filename, offset = _read_string(view, offset)
        file_version, offset = _read_u32(view, offset)
        num_items, offset = _read_u32(view, offset)

        items = []
        for _ in range(num_items):
            plasticity_id, offset = _read_u32(view, offset)
            version, offset = _read_u32(view, offset)

            num_faces, offset = _read_u32(view, offset)
            faces, offset = _read_int_array(view, offset, num_faces)

            num_positions, offset = _read_u32(view, offset)
            vertices, offset = _read_float_array(view, offset, num_positions)

            num_indices, offset = _read_u32(view, offset)
            indices, offset = _read_int_array(view, offset, num_indices)

            num_normals, offset = _read_u32(view, offset)
            normals, offset = _read_float_array(view, offset, num_normals)

            num_groups, offset = _read_u32(view, offset)
            groups, offset = _read_int_array(view, offset, num_groups)

            num_face_ids, offset = _read_u32(view, offset)
            face_ids, offset = _read_int_array(view, offset, num_face_ids)

            items.append({
                'plasticity_id': plasticity_id, 'version': version,
                'faces': faces, 'vertices': vertices, 'indices': indices,
                'normals': normals, 'groups': groups, 'face_ids': face_ids,
            })

        return {
            'type': msg_type, 'filename': filename,
            'file_version': file_version, 'refaceted_objects': items,
        }

    def _parse_new_version(self, view, offset):
        filename, offset = _read_string(view, offset)
        version, offset = _read_u32(view, offset)
        return {
            'type': MessageType.NEW_VERSION_1,
            'filename': filename, 'version': version,
        }

    def _parse_new_file(self, view, offset):
        filename, offset = _read_string(view, offset)
        return {
            'type': MessageType.NEW_FILE_1,
            'filename': filename,
        }

    def _parse_handshake(self, view, offset):
        """Parse HANDSHAKE_1 response (server -> client)."""
        message_id, offset = _read_u32(view, offset)
        num_messages, offset = _read_u32(view, offset)

        supported = set()
        for _ in range(num_messages):
            msg_type, offset = _read_u32(view, offset)
            supported.add(msg_type)

        return {
            'type': MessageType.HANDSHAKE_1,
            'message_id': message_id,
            'supported_messages': supported,
        }

    def _parse_put_some(self, view, offset):
        """Parse PUT_SOME_1 response (server -> client)."""
        message_id, offset = _read_u32(view, offset)
        code, offset = _read_u32(view, offset)

        num_groups, offset = _read_u32(view, offset)
        group_results = []
        for _ in range(num_groups):
            collection_id, offset = _read_string(view, offset)
            group_id, offset = _read_u32(view, offset)
            group_results.append({
                "c4d_collection_id": collection_id,
                "group_id": group_id,
            })

        num_items, offset = _read_u32(view, offset)
        item_results = []
        for _ in range(num_items):
            c4d_id, offset = _read_string(view, offset)
            stable_id, offset = _read_u32(view, offset)
            version_id, offset = _read_u32(view, offset)
            item_results.append({
                "c4d_id": c4d_id,
                "stable_id": stable_id,
                "version_id": version_id,
            })

        return {
            'type': MessageType.PUT_SOME_1,
            'message_id': message_id,
            'code': code,
            'group_results': group_results,
            'item_results': item_results,
        }
