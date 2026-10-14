"""
Protocol definitions and binary message parsing for Plasticity WebSocket communication.

All message types and binary layouts match the Plasticity Blender Bridge
(https://github.com/nkallen/plasticity-blender-addon, client.py) exactly:
little-endian, uint32 length-prefixed UTF-8 strings padded to 4 bytes.

Bulk arrays are read with ``array.frombytes`` (one memcpy) instead of
per-element unpacking, and written with ``array.tobytes``; on big-endian
hosts the data is byte-swapped so the wire format stays little-endian.
Every read is bounds-checked: a truncated or corrupt message raises
``ProtocolError`` instead of yielding silently short data.
"""

import struct
import sys
from array import array
from enum import IntEnum
from typing import Any, Dict, List, Optional


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


class ProtocolError(ValueError):
    """Raised for truncated or malformed messages."""


LIST_TYPES = frozenset({MessageType.LIST_ALL_1, MessageType.LIST_SOME_1,
                        MessageType.LIST_VISIBLE_1})

_SWAP = sys.byteorder != "little"


def _typecode(signed):
    """Array typecode for a 4-byte (u)int on this platform."""
    for tc in (("i", "l") if signed else ("I", "L")):
        if array(tc).itemsize == 4:
            return tc
    raise RuntimeError("no 4-byte integer array type on this platform")


_I32 = _typecode(True)
_U32 = _typecode(False)
_F32 = "f"

_HDR = struct.Struct("<II")
_U32S = struct.Struct("<I")
_U64S = struct.Struct("<Q")


# =============================================================================
# Encoding helpers
# =============================================================================

def _encode_string(s: str) -> bytes:
    """Encode a UTF-8 string with a uint32 byte-length prefix and 4-byte
    alignment padding.  (The Blender addon uses ``len(str)`` for the prefix
    in subscribe_some/refacet_some, which is only correct for ASCII; the
    byte length is what the server reads.)"""
    encoded = s.encode('utf-8')
    padding = b'\x00' * ((4 - len(encoded) % 4) % 4)
    return _U32S.pack(len(encoded)) + encoded + padding


def _array_bytes(typecode, values) -> bytes:
    a = values if (isinstance(values, array) and values.typecode == typecode) \
        else array(typecode, values)
    if _SWAP:
        a = array(typecode, a)
        a.byteswap()
    return a.tobytes()


def _encode_id_array(ids: List[int]) -> bytes:
    """Encode a list of uint32 IDs with a uint32 count prefix."""
    return _U32S.pack(len(ids)) + _array_bytes(_U32, ids)


# =============================================================================
# Encoding (client -> server)
# =============================================================================

def encode_list_all(message_id: int) -> bytes:
    return _HDR.pack(MessageType.LIST_ALL_1, message_id)


def encode_list_visible(message_id: int) -> bytes:
    return _HDR.pack(MessageType.LIST_VISIBLE_1, message_id)


def encode_subscribe_all(message_id: int) -> bytes:
    return _HDR.pack(MessageType.SUBSCRIBE_ALL_1, message_id)


def encode_unsubscribe(message_id: int) -> bytes:
    return _HDR.pack(MessageType.UNSUBSCRIBE_ALL_1, message_id)


def encode_subscribe_some(message_id: int, filename: str,
                          plasticity_ids: List[int]) -> bytes:
    return (_HDR.pack(MessageType.SUBSCRIBE_SOME_1, message_id)
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
    return (_HDR.pack(MessageType.REFACET_SOME_1, message_id)
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
                int(shape),
            ))


def encode_handshake(message_id: int) -> bytes:
    """Encode a HANDSHAKE_1 message (client -> server)."""
    return _HDR.pack(MessageType.HANDSHAKE_1, message_id)


def encode_put_some(message_id: int, filename: str,
                    groups: List[Dict], items: List[Dict]) -> bytes:
    """Encode a PUT_SOME_1 message (client -> server).

    groups: list of dicts with keys:
        c4d_collection_id, name, parent_c4d_collection_id, existing_group_id
    items: list of dicts with keys:
        c4d_id, name, parent_c4d_collection_id, existing_stable_id,
        options (uint64), positions (flat float list),
        indices (flat uint32 list), sizes (uint32 list of face vertex counts)

    The payload is assembled as a list of parts joined once (the 1.1.x
    ``bytes +=`` loop was quadratic in message size).  Raises ``ValueError``
    for inconsistent mesh data rather than sending a malformed message.
    """
    parts = [_HDR.pack(MessageType.PUT_SOME_1, message_id),
             _encode_string(filename),
             _U32S.pack(len(groups))]
    for g in groups:
        parts.append(_encode_string(g["c4d_collection_id"]))
        parts.append(_encode_string(g["name"]))
        parts.append(_encode_string(g["parent_c4d_collection_id"]))
        parts.append(_U32S.pack(g.get("existing_group_id", 0)))

    parts.append(_U32S.pack(len(items)))
    for item in items:
        positions = item["positions"]
        indices = item["indices"]
        sizes = item["sizes"]
        if len(positions) % 3:
            raise ValueError(f"PUT_SOME item {item['name']!r}: position "
                             f"count {len(positions)} is not a multiple of 3")
        vertex_count = len(positions) // 3
        if sum(sizes) != len(indices):
            raise ValueError(f"PUT_SOME item {item['name']!r}: face sizes "
                             f"sum to {sum(sizes)} but there are "
                             f"{len(indices)} indices")
        # (negative indices are rejected by the uint32 array below)
        if indices and max(indices) >= vertex_count:
            raise ValueError(f"PUT_SOME item {item['name']!r}: index out of "
                             f"range for {vertex_count} vertices")

        parts.append(_encode_string(item["c4d_id"]))
        parts.append(_encode_string(item["name"]))
        parts.append(_encode_string(item["parent_c4d_collection_id"]))
        parts.append(_U32S.pack(item.get("existing_stable_id", 0)))
        parts.append(_U64S.pack(item["options"]))
        parts.append(_U32S.pack(vertex_count))
        parts.append(_array_bytes(_F32, positions))
        parts.append(_HDR.pack(len(sizes), len(indices)))
        try:
            parts.append(_array_bytes(_U32, indices))
            parts.append(_array_bytes(_U32, sizes))
        except OverflowError as e:
            raise ValueError(f"PUT_SOME item {item['name']!r}: negative or "
                             f"oversized index/size ({e})") from None

    return b"".join(parts)


# =============================================================================
# Decoding helpers (bounds-checked bulk reads)
# =============================================================================

def _need(view, offset, nbytes, what):
    if offset < 0 or nbytes < 0 or offset + nbytes > len(view):
        raise ProtocolError(
            f"truncated message: need {nbytes} bytes for {what} at offset "
            f"{offset}, have {len(view) - offset}")


def _read_u32(view, offset):
    _need(view, offset, 4, "uint32")
    return _U32S.unpack_from(view, offset)[0], offset + 4


def _read_i32(view, offset):
    _need(view, offset, 4, "int32")
    return struct.unpack_from('<i', view, offset)[0], offset + 4


def _read_f32(view, offset):
    _need(view, offset, 4, "float32")
    return struct.unpack_from('<f', view, offset)[0], offset + 4


def _read_string(view, offset):
    length, offset = _read_u32(view, offset)
    if length == 0:
        return "", offset
    padded = length + (4 - length % 4) % 4
    _need(view, offset, length, "string")
    raw = bytes(view[offset:offset + length])
    try:
        s = raw.decode('utf-8')
    except UnicodeDecodeError:
        s = raw.decode('utf-8', errors='replace')
    # Tolerate a missing final pad only at the very end of the buffer.
    return s, min(offset + padded, len(view))


def _read_array(view, offset, count, typecode, what):
    nbytes = count * 4
    _need(view, offset, nbytes, what)
    a = array(typecode)
    if count:
        a.frombytes(view[offset:offset + nbytes])
        if _SWAP:
            a.byteswap()
    return a, offset + nbytes


def _read_float_array(view, offset, count):
    """Read ``count`` float32 values as an ``array('f')``."""
    return _read_array(view, offset, count, _F32, "float32 array")


def _read_int_array(view, offset, count):
    """Read ``count`` int32 values as an ``array`` of signed ints."""
    return _read_array(view, offset, count, _I32, "int32 array")


def _read_uint_array(view, offset, count):
    """Read ``count`` uint32 values as an ``array`` of unsigned ints."""
    return _read_array(view, offset, count, _U32, "uint32 array")


# =============================================================================
# Object decoding (matches Blender decode_object_data exactly)
# =============================================================================

def decode_object_data(view, offset):
    """
    Decode one object from binary data.

    Returns tuple:
        (object_type, object_id, version_id, parent_id, material_id, flags,
         name, vertices, faces, normals, new_offset, groups, face_ids)

    ``vertices``, ``faces`` and ``normals`` are arrays; ``groups`` and
    ``face_ids`` are plain lists (they are small and get JSON-serialised).
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
        groups = groups.tolist()

        # Face IDs: count then int32 * count
        num_face_ids, offset = _read_u32(view, offset)
        face_ids, offset = _read_int_array(view, offset, num_face_ids)
        face_ids = face_ids.tolist()

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

def peek_message_type(data) -> Optional[int]:
    """The raw message type of ``data`` (first uint32), or None."""
    if len(data) < 4:
        return None
    return _U32S.unpack_from(data, 0)[0]


class MessageParser:
    def parse_message(self, data: bytes) -> Optional[Dict[str, Any]]:
        """Parse one server message.

        Returns a dict with at least ``'type'``, or ``None`` for empty or
        unknown messages.  Server-side failures (code != 200) are returned
        as dicts with an ``'error'`` key so callers can react to them.
        Raises ``ProtocolError`` for truncated/malformed input.
        """
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

        elif message_type in LIST_TYPES:
            message_id, offset = _read_u32(view, offset)
            code, offset = _read_u32(view, offset)
            if code != 200:
                print(f"[Protocol] List failed with code: {code}")
                return {'type': message_type, 'message_id': message_id,
                        'error': code, 'filename': '',
                        'delete': [], 'add': [], 'update': []}
            result = self._parse_transaction(view, offset, message_type)
            result['message_id'] = message_id
            return result

        elif message_type == MessageType.REFACET_SOME_1:
            message_id, offset = _read_u32(view, offset)
            result = self._parse_refacet(view, offset, message_type)
            result['message_id'] = message_id
            return result

        elif message_type == MessageType.NEW_VERSION_1:
            return self._parse_new_version(view, offset)

        elif message_type == MessageType.NEW_FILE_1:
            return self._parse_new_file(view, offset)

        elif message_type == MessageType.HANDSHAKE_1:
            return self._parse_handshake(view, offset)

        elif message_type == MessageType.PUT_SOME_1:
            return self._parse_put_some(view, offset)

        else:
            print(f"[Protocol] Unhandled message type: {message_type!r}")
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
            _need(view, offset, item_length, "transaction item")
            item_view = view[offset:offset + item_length]
            item_type_raw, _ = _read_u32(item_view, 0)

            try:
                item_type = MessageType(item_type_raw)
            except ValueError:
                offset += item_length
                continue

            if item_type == MessageType.DELETE_1:
                # [type][count][ids...]; the Blender addon reads the count
                # from offset 0 (the type field) — a bug there — but takes
                # the IDs from offset 8, matching this layout.
                num_deleted, _ = _read_u32(item_view, 4)
                ids, _ = _read_uint_array(item_view, 8, num_deleted)
                transaction['delete'].extend(ids.tolist())

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
            return {'type': msg_type, 'error': code, 'filename': '',
                    'refaceted_objects': []}

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
                'normals': normals, 'groups': groups.tolist(),
                'face_ids': face_ids.tolist(),
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
        types, offset = _read_uint_array(view, offset, num_messages)

        return {
            'type': MessageType.HANDSHAKE_1,
            'message_id': message_id,
            'supported_messages': set(types.tolist()),
        }

    def _parse_put_some(self, view, offset):
        """Parse PUT_SOME_1 response (server -> client).

        On a non-200 code the result arrays may be absent; whatever cannot
        be read is reported as empty instead of failing the whole message.
        """
        message_id, offset = _read_u32(view, offset)
        code, offset = _read_u32(view, offset)

        group_results = []
        item_results = []
        try:
            num_groups, offset = _read_u32(view, offset)
            for _ in range(num_groups):
                collection_id, offset = _read_string(view, offset)
                group_id, offset = _read_u32(view, offset)
                group_results.append({
                    "c4d_collection_id": collection_id,
                    "group_id": group_id,
                })

            num_items, offset = _read_u32(view, offset)
            for _ in range(num_items):
                c4d_id, offset = _read_string(view, offset)
                stable_id, offset = _read_u32(view, offset)
                version_id, offset = _read_u32(view, offset)
                item_results.append({
                    "c4d_id": c4d_id,
                    "stable_id": stable_id,
                    "version_id": version_id,
                })
        except ProtocolError:
            if code == 200:
                raise

        return {
            'type': MessageType.PUT_SOME_1,
            'message_id': message_id,
            'code': code,
            'group_results': group_results,
            'item_results': item_results,
        }
