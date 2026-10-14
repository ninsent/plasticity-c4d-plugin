"""
Wire-protocol tests: golden bytes from the Blender addon layouts, round
trips for every message type, malformed/truncated input, error codes.
"""

import random
import struct
from array import array

import pytest

from core import protocol as P
from core.protocol import FacetShapeType, MessageParser, MessageType, ProtocolError
from reference import blender_v221 as B
from reference import protocol_v116 as OLD
import wire as W

parser = MessageParser()


def _lists(obj):
    """Normalise arrays to lists for comparisons."""
    if isinstance(obj, array):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: _lists(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_lists(v) for v in obj]
    return obj


# =============================================================================
# Client -> server: golden bytes (Blender layouts)
# =============================================================================

@pytest.mark.parametrize("enc,mt", [
    (P.encode_list_all, B.MessageType.LIST_ALL_1),
    (P.encode_list_visible, B.MessageType.LIST_VISIBLE_1),
    (P.encode_subscribe_all, B.MessageType.SUBSCRIBE_ALL_1),
    (P.encode_unsubscribe, B.MessageType.UNSUBSCRIBE_ALL_1),
])
def test_simple_messages_golden(enc, mt):
    for mid in (0, 1, 7, 2 ** 32 - 1):
        assert enc(mid) == B.simple(mt, mid)


def test_handshake_golden():
    assert P.encode_handshake(42) == B.handshake(42)
    assert P.encode_handshake(1) == bytes.fromhex("6400000001000000")


@pytest.mark.parametrize("filename", ["", "a", "ab", "abc", "abcd", "part.plasticity"])
def test_subscribe_some_golden(filename):
    ids = [1, 2, 300000, 2 ** 32 - 1]
    assert P.encode_subscribe_some(9, filename, ids) == B.subscribe_some(9, filename, ids)
    assert P.encode_subscribe_some(9, filename, []) == B.subscribe_some(9, filename, [])


@pytest.mark.parametrize("kwargs", [
    {},
    dict(relative_to_bbox=False, curve_chord_tolerance=0.5, curve_chord_angle=0.1,
         surface_plane_tolerance=0.25, surface_plane_angle=0.75,
         match_topology=False, max_sides=128, plane_angle=0.7853981633974483,
         min_width=0.01, max_width=10.0, curve_chord_max=7.07,
         shape=FacetShapeType.CONVEX),
    dict(max_sides=3, shape=FacetShapeType.ANY),
])
def test_refacet_some_golden(kwargs):
    bshape = B.FacetShapeType[kwargs.get("shape", FacetShapeType.CUT).name]
    bk = dict(kwargs, shape=bshape)
    for filename in ("box", "pad me.x", ""):
        assert (P.encode_refacet_some(3, filename, [5, 6], **kwargs)
                == B.refacet_some(3, filename, [5, 6], **bk))


def test_refacet_shape_accepts_plain_int():
    a = P.encode_refacet_some(1, "f", [1], shape=FacetShapeType.CUT)
    b = P.encode_refacet_some(1, "f", [1], shape=20501)
    assert a == b


def test_non_ascii_filename_uses_utf8_byte_length():
    # The Blender addon prefixes subscribe_some/refacet_some filenames with
    # len(str); the server reads a byte count (as in PUT_SOME, where Blender
    # does use bytes).  We always send the byte length, padded to 4.
    msg = P.encode_subscribe_some(1, "é", [])
    assert msg[8:12] == struct.pack("<I", 2)
    assert msg[12:16] == "é".encode() + b"\x00\x00"
    assert len(msg) % 4 == 0


def _put_some_fixture(rnd=None, unicode=False):
    rnd = rnd or random.Random(1)
    name = "Würfel ✓" if unicode else "Cube"
    groups = [
        {"c4d_collection_id": "g1", "name": "Group", "parent_c4d_collection_id": "",
         "existing_group_id": 0},
        {"c4d_collection_id": "g2abc", "name": "日本", "parent_c4d_collection_id": "g1",
         "existing_group_id": 77},
    ]
    nv = 9
    positions = [rnd.uniform(-5, 5) for _ in range(nv * 3)]
    items = [
        {"c4d_id": "abcdef0123", "name": name, "parent_c4d_collection_id": "g2abc",
         "existing_stable_id": 0, "options": 1 | (1 << 9),
         "positions": positions, "indices": [0, 1, 2, 3, 4, 5, 6, 7, 8, 0, 4, 8],
         "sizes": [3, 4, 5]},
        {"c4d_id": "x", "name": "", "parent_c4d_collection_id": "",
         "existing_stable_id": 12345, "options": 2 ** 40 + 1,
         "positions": [], "indices": [], "sizes": []},
    ]
    return groups, items


@pytest.mark.parametrize("unicode", [False, True])
def test_put_some_golden(unicode):
    groups, items = _put_some_fixture(unicode=unicode)
    assert P.encode_put_some(5, "scene.c4d", groups, items) == \
        B.put_some(5, "scene.c4d", groups, items)
    assert P.encode_put_some(5, "", [], []) == B.put_some(5, "", [], [])


def test_put_some_matches_1_1_6_on_large_random_mesh():
    rnd = random.Random(3)
    nv = 5000
    positions = [rnd.uniform(-100, 100) for _ in range(nv * 3)]
    sizes, indices = [], []
    while len(indices) < 20000:
        k = rnd.choice([3, 3, 4, 5, 7])
        sizes.append(k)
        indices += [rnd.randrange(nv) for _ in range(k)]
    items = [{"c4d_id": "id", "name": "n", "parent_c4d_collection_id": "",
              "existing_stable_id": 1, "options": 1, "positions": positions,
              "indices": indices, "sizes": sizes}]
    assert P.encode_put_some(2, "f", [], items) == OLD.encode_put_some(2, "f", [], items)


@pytest.mark.parametrize("bad", [
    dict(positions=[0.0, 1.0]),
    dict(sizes=[3, 4]),
    dict(indices=[0, 1, 5]),
    dict(indices=[0, -1, 2]),
])
def test_put_some_rejects_inconsistent_mesh(bad):
    item = {"c4d_id": "a", "name": "a", "parent_c4d_collection_id": "",
            "options": 1, "positions": [0.0] * 9, "indices": [0, 1, 2],
            "sizes": [3]}
    item.update(bad)
    with pytest.raises(ValueError):
        P.encode_put_some(1, "f", [], [item])


# =============================================================================
# Client -> server: round trips (decode what we encode)
# =============================================================================

def _decode_client(data):
    """Minimal decoder for client->server messages (test-only)."""
    v = memoryview(data)

    def u(o):
        return struct.unpack_from("<I", v, o)[0], o + 4

    def s(o):
        n, o = u(o)
        txt = bytes(v[o:o + n]).decode()
        return txt, o + n + (4 - n % 4) % 4
    t, o = u(0)
    mid, o = u(o)
    out = {"type": t, "message_id": mid}
    if t in (24, 26):
        out["filename"], o = s(o)
        n, o = u(o)
        out["ids"] = list(struct.unpack_from(f"<{n}I", v, o))
        o += 4 * n
        if t == 26:
            out["params"] = struct.unpack_from("<IffffIIffffI", v, o)
            o += 48
    elif t == 31:
        out["filename"], o = s(o)
        ng, o = u(o)
        out["groups"] = []
        for _ in range(ng):
            a, o = s(o)
            b, o = s(o)
            c, o = s(o)
            d, o = u(o)
            out["groups"].append((a, b, c, d))
        ni, o = u(o)
        out["items"] = []
        for _ in range(ni):
            a, o = s(o)
            b, o = s(o)
            c, o = s(o)
            sid, o = u(o)
            opts = struct.unpack_from("<Q", v, o)[0]
            o += 8
            nv, o = u(o)
            pos = list(struct.unpack_from(f"<{nv * 3}f", v, o))
            o += 12 * nv
            fc, o = u(o)
            ic, o = u(o)
            idx = list(struct.unpack_from(f"<{ic}I", v, o))
            o += 4 * ic
            sz = list(struct.unpack_from(f"<{fc}I", v, o))
            o += 4 * fc
            out["items"].append((a, b, c, sid, opts, pos, idx, sz))
    assert o == len(data), "trailing bytes"
    return out


def test_client_round_trips_every_type():
    for enc, t in ((P.encode_list_all, 20), (P.encode_list_visible, 22),
                   (P.encode_subscribe_all, 23), (P.encode_unsubscribe, 25),
                   (P.encode_handshake, 100)):
        assert _decode_client(enc(11)) == {"type": t, "message_id": 11}
    d = _decode_client(P.encode_subscribe_some(4, "file é", [1, 2]))
    assert (d["filename"], d["ids"]) == ("file é", [1, 2])
    d = _decode_client(P.encode_refacet_some(4, "f", [3], max_sides=128))
    assert d["ids"] == [3] and d["params"][6] == 128
    groups, items = _put_some_fixture(unicode=True)
    d = _decode_client(P.encode_put_some(8, "s.c4d", groups, items))
    assert d["groups"][1] == ("g2abc", "日本", "g1", 77)
    a = d["items"][0]
    assert a[1] == "Würfel ✓" and a[6] == items[0]["indices"] and a[7] == [3, 4, 5]
    assert a[5] == pytest.approx(items[0]["positions"], rel=1e-6)
    assert a[4] == items[0]["options"]
    assert d["items"][1][4] == 2 ** 40 + 1


# =============================================================================
# Server -> client: parsing
# =============================================================================

def _sample_objects():
    verts = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.5]
    return [
        W.group_object(10, "Grp"),
        W.mesh_object(11, "Solid é", verts, [0, 1, 2, 1, 3, 2], [0.0, 0.0, 1.0] * 4,
                      [0, 6], [99], parent_id=10, flags=7),
        W.mesh_object(12, "", obj_type=1),                # empty sheet
        W.mesh_object(13, "wire", obj_type=2),            # no geometry payload
    ]


def test_transaction_parse_matches_blender_reader():
    objs = _sample_objects()
    data = W.transaction("file", [W.item(1, objs[:2]), W.delete_item([4, 5]),
                                  W.item(2, objs[2:])], version=3)
    got = _lists(parser.parse_message(data))
    ref = B.decode_transaction(data, has_list_header=False)
    assert got["type"] == MessageType.TRANSACTION_1
    assert (got["filename"], got["version"], got["delete"]) == ("file", 3, [4, 5])
    for key in ("add", "update"):
        assert len(got[key]) == len(ref[key])
        for a, b in zip(got[key], ref[key]):
            b = {k: (_lists(v) if v is not None else []) for k, v in b.items()}
            assert a == b
    solid = got["add"][1]
    assert solid["name"] == "Solid é" and solid["parent_id"] == 10
    assert solid["faces"] == [0, 1, 2, 1, 3, 2] and solid["groups"] == [0, 6]


def test_parse_matches_1_1_6_values():
    objs = _sample_objects()
    data = W.list_response("f", [W.item(1, objs)], message_id=7)
    new = _lists(parser.parse_message(data))
    old = OLD.MessageParser().parse_message(data)
    assert new.pop("message_id") == 7
    assert new == old


def test_list_response_ok_and_error():
    ok = parser.parse_message(W.list_response("f", [], message_id=9))
    assert ok["type"] == MessageType.LIST_ALL_1 and ok["message_id"] == 9
    assert "error" not in ok
    err = parser.parse_message(W.list_response("f", [], message_id=9, code=500))
    assert err["error"] == 500 and err["message_id"] == 9
    assert err["add"] == [] and err["update"] == [] and err["delete"] == []
    vis = parser.parse_message(W.list_response("f", [], msg_type=22, code=404))
    assert vis["type"] == MessageType.LIST_VISIBLE_1 and vis["error"] == 404


def test_refacet_response_ok_and_error():
    obj = dict(id=5, faces=[0, 0, 0, 0, 1, 1, 1], vertices=[0.0] * 21,
               indices=[0, 1, 2, 3, 4, 5, 6], normals=[0.0, 0.0, 1.0] * 7,
               groups=[0, 7], face_ids=[42])
    r = _lists(parser.parse_message(W.refacet_response("box", [obj], message_id=4)))
    assert r["message_id"] == 4 and r["filename"] == "box"
    item = r["refaceted_objects"][0]
    assert item["plasticity_id"] == 5 and item["faces"] == obj["faces"]
    assert item["groups"] == [0, 7] and item["face_ids"] == [42]
    old = OLD.MessageParser().parse_message(W.refacet_response("box", [obj], message_id=4))
    r.pop("message_id")
    assert r == old
    e = parser.parse_message(W.refacet_response("box", [], code=500, message_id=4))
    assert e["error"] == 500 and e["refaceted_objects"] == [] and e["filename"] == ""


def test_new_version_new_file_handshake_put_some():
    assert parser.parse_message(W.new_version("a.p", 12)) == {
        "type": MessageType.NEW_VERSION_1, "filename": "a.p", "version": 12}
    assert parser.parse_message(W.new_file("日本.p")) == {
        "type": MessageType.NEW_FILE_1, "filename": "日本.p"}
    h = parser.parse_message(W.handshake_response(1, [20, 22, 26, 31, 100]))
    assert h["supported_messages"] == {20, 22, 26, 31, 100}
    assert parser.parse_message(W.handshake_response(1, []))["supported_messages"] == set()
    ps = parser.parse_message(W.put_some_response(3, 200, [("g1", 8)], [("abc", 9, 2)]))
    assert ps["code"] == 200
    assert ps["group_results"] == [{"c4d_collection_id": "g1", "group_id": 8}]
    assert ps["item_results"] == [{"c4d_id": "abc", "stable_id": 9, "version_id": 2}]


def test_put_some_error_response_without_payload():
    data = W.u32(31) + W.u32(3) + W.u32(500)            # no arrays at all
    r = parser.parse_message(data)
    assert r["code"] == 500 and r["item_results"] == [] and r["group_results"] == []


def test_unknown_types_and_items():
    assert parser.parse_message(W.u32(999) + W.u32(0)) is None
    assert parser.parse_message(b"") is None
    assert parser.parse_message(b"\x00\x00") is None
    # client->server-only types arriving from the server are ignored
    assert parser.parse_message(W.u32(23) + W.u32(1)) is None
    # unknown item types inside a transaction are skipped
    junk = W.u32(8) + W.u32(77) + W.u32(0)
    data = W.transaction("f", [junk, W.delete_item([3])])
    assert parser.parse_message(data)["delete"] == [3]
    # zero-length items are skipped
    data = W.transaction("f", [W.u32(0), W.delete_item([4])])
    assert parser.parse_message(data)["delete"] == [4]


@pytest.mark.parametrize("name", ["", "a", "ab", "abc", "abcd", "é", "日本", "😀", "x😀"])
def test_strings_padding(name):
    data = W.transaction(name, [W.item(1, [W.mesh_object(1, name, [0.0] * 9,
                                                         [0, 1, 2])])])
    r = parser.parse_message(data)
    assert r["filename"] == name and r["add"][0]["name"] == name
    assert list(r["add"][0]["faces"]) == [0, 1, 2]


def test_invalid_utf8_is_replaced_not_fatal():
    bad = W.u32(11) + W.u32(2) + b"\xff\xfe\x00\x00"
    assert parser.parse_message(bad)["filename"] == "��"


def _all_messages():
    objs = _sample_objects()
    return [
        W.transaction("file", [W.item(1, objs), W.delete_item([1, 2])]),
        W.list_response("file", [W.item(1, objs)]),
        W.refacet_response("f", [dict(id=1, faces=[0, 0, 0], vertices=[0.0] * 9,
                                      indices=[0, 1, 2], normals=[0.0] * 9,
                                      groups=[0, 3], face_ids=[1])]),
        W.new_version("abc", 1),
        W.new_file("abcde"),
        W.handshake_response(1, [1, 2, 3]),
        W.put_some_response(1, 200, [("g", 1)], [("i", 2, 3)]),
    ]


@pytest.mark.parametrize("idx", range(7))
def test_truncation_never_yields_silent_garbage(idx):
    data = _all_messages()[idx]
    full = parser.parse_message(data)
    assert full is not None
    for cut in range(len(data)):
        prefix = data[:cut]
        try:
            res = parser.parse_message(prefix)
        except ProtocolError:
            continue
        # A prefix may only parse if it is < 4 bytes (None) or if the cut
        # removed nothing but trailing string padding.
        assert res is None or _lists(res) == _lists(full), (cut, res)


def test_huge_counts_are_rejected_before_allocation():
    data = (W.u32(0) + W.string("f") + W.u32(1) + W.u32(1)
            + W.u32(40) + W.u32(1) + W.u32(1)
            + W.u32(0) + W.u32(1) + W.u32(1) + W.i32(0) + W.i32(0) + W.u32(0)
            + W.string("n") + W.u32(0xFFFFFFFF))
    with pytest.raises(ProtocolError):
        parser.parse_message(data)


def test_item_length_beyond_buffer_is_an_error():
    data = W.u32(0) + W.string("f") + W.u32(1) + W.u32(1) + W.u32(100) + W.u32(3)
    with pytest.raises(ProtocolError):
        parser.parse_message(data)


def test_peek_message_type():
    assert P.peek_message_type(W.new_file("a")) == 11
    assert P.peek_message_type(b"\x01") is None


# =============================================================================
# Endianness guard
# =============================================================================

def test_byteswap_guard_round_trips(monkeypatch):
    """Simulate a big-endian host: encode and decode both swap, so a value
    encoded with the guard decodes back to itself."""
    monkeypatch.setattr(P, "_SWAP", True)
    raw = P._array_bytes("f", [1.5, -2.0])
    native = array("f", [1.5, -2.0])
    swapped = array("f", native)
    swapped.byteswap()
    assert raw == swapped.tobytes()
    back, _ = P._read_float_array(memoryview(raw), 0, 2)
    assert back.tolist() == [1.5, -2.0]


def test_wire_is_little_endian_on_this_host():
    assert P._array_bytes(P._U32, [1]) == b"\x01\x00\x00\x00"
