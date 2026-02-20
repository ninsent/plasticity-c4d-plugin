"""
Protocol definitions and binary message parsing for Plasticity WebSocket communication.

All message types and binary formats match the Blender addon exactly.
Uses struct.unpack_from for fast bulk decoding instead of Python loops.
"""

import struct
import array
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


def encode_subscribe_some(message_id: int, filename: str, plasticity_ids: List[int]) -> bytes:
    msg = struct.pack("<II", MessageType.SUBSCRIBE_SOME_1, message_id)
    fn = filename.encode('utf-8')
    msg += struct.pack("<I", len(fn))
    msg += fn
    msg += b'\x00' * ((4 - len(fn) % 4) % 4)
    msg += struct.pack("<I", len(plasticity_ids))
    for pid in plasticity_ids:
        msg += struct.pack("<I", pid)
    return msg


def encode_refacet_some(
    message_id, filename, plasticity_ids,
    relative_to_bbox=True, curve_chord_tolerance=0.01, curve_chord_angle=0.35,
    surface_plane_tolerance=0.01, surface_plane_angle=0.35,
    match_topology=True, max_sides=3, plane_angle=0.0,
    min_width=0.0, max_width=0.0, curve_chord_max=0.0,
    shape=FacetShapeType.CUT
) -> bytes:
    msg = struct.pack("<II", MessageType.REFACET_SOME_1, message_id)
    fn = filename.encode('utf-8')
    msg += struct.pack("<I", len(fn))
    msg += fn
    msg += b'\x00' * ((4 - len(fn) % 4) % 4)
    msg += struct.pack("<I", len(plasticity_ids))
    for pid in plasticity_ids:
        msg += struct.pack("<I", pid)
    msg += struct.pack("<I", 1 if relative_to_bbox else 0)
    msg += struct.pack("<f", curve_chord_tolerance)
    msg += struct.pack("<f", curve_chord_angle)
    msg += struct.pack("<f", surface_plane_tolerance)
    msg += struct.pack("<f", surface_plane_angle)
    msg += struct.pack("<I", 1 if match_topology else 0)
    msg += struct.pack("<I", max_sides)
    msg += struct.pack("<f", plane_angle)
    msg += struct.pack("<f", min_width)
    msg += struct.pack("<f", max_width)
    msg += struct.pack("<f", curve_chord_max)
    msg += struct.pack("<I", shape.value)
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
            # Transaction has no message_id — goes straight to filename
            return self._parse_transaction(view, offset, message_type)

        elif message_type in (MessageType.LIST_ALL_1, MessageType.LIST_SOME_1,
                              MessageType.LIST_VISIBLE_1):
            message_id, offset = _read_u32(view, offset)
            code, offset = _read_u32(view, offset)
            if code != 200:
                print(f"[Protocol] List failed with code: {code}")
                return None
            # LIST response wraps a transaction-like structure
            return self._parse_transaction(view, offset, message_type)

        elif message_type == MessageType.REFACET_SOME_1:
            message_id, offset = _read_u32(view, offset)
            return self._parse_refacet(view, offset, message_type)

        elif message_type == MessageType.NEW_VERSION_1:
            return self._parse_new_version(view, offset)

        elif message_type == MessageType.NEW_FILE_1:
            return self._parse_new_file(view, offset)

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

            # Face facets (ngon face assignments)
            num_faces, offset = _read_u32(view, offset)
            faces, offset = _read_int_array(view, offset, num_faces)

            # Positions (vertices as flat float array)
            num_positions, offset = _read_u32(view, offset)
            vertices, offset = _read_float_array(view, offset, num_positions)

            # Indices
            num_indices, offset = _read_u32(view, offset)
            indices, offset = _read_int_array(view, offset, num_indices)

            # Normals
            num_normals, offset = _read_u32(view, offset)
            normals, offset = _read_float_array(view, offset, num_normals)

            # Groups
            num_groups, offset = _read_u32(view, offset)
            groups, offset = _read_int_array(view, offset, num_groups)

            # Face IDs
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
        """Parse NEW_VERSION_1 message."""
        filename, offset = _read_string(view, offset)
        version, offset = _read_u32(view, offset)
        return {
            'type': MessageType.NEW_VERSION_1,
            'filename': filename, 'version': version,
        }

    def _parse_new_file(self, view, offset):
        """Parse NEW_FILE_1 message."""
        filename, offset = _read_string(view, offset)
        return {
            'type': MessageType.NEW_FILE_1,
            'filename': filename,
        }
