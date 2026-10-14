"""
Minimal stand-in for Cinema 4D's ``c4d`` module, used only by the test suite.

It implements just enough of the API surface that the plugin touches to
import the modules and exercise handler logic (object tree, BaseContainer,
polygon data incl. N-gon bookkeeping, tags, selections, documents with an
undo recorder, matrices).  It does NOT try to emulate Cinema 4D; behaviour
that matters for correctness and cannot be reproduced here has to be
checked by hand in Cinema 4D.
"""

import math

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTOK = -1

# Object types (values are arbitrary but distinct)
Opoint = 5100
Opolygon = 5100 + 0x1000  # treat polygon as a point subtype via _BASE_TYPES
Onull = 5140
Osds = 1007455
Oconnector = 1011010
Ocube = 5159
Osymmetry = 5142
Osplineobject = 5101
Obend = 5128
Oinstance = 5126

_BASE_TYPES = {Opolygon: {Opoint}}

# Tag types
Tphong = 5612
Tnormal = 5711
Tpolygonselection = 5673
Tedgeselection = 5701
Tpointselection = 5674

# Undo types
UNDOTYPE_CHANGE = 40
UNDOTYPE_CHANGE_SMALL = 41
UNDOTYPE_CHANGE_NOCHILDREN = 42
UNDOTYPE_CHANGE_SELECTION = 43
UNDOTYPE_NEWOBJ = 44
UNDOTYPE_DELETEOBJ = 45
UNDOTYPE_BITS = 46

# Visibility
MODE_ON = 0
MODE_OFF = 1
MODE_UNDEF = 2
OBJECT_ON = 0
OBJECT_OFF = 1
OBJECT_UNDEF = 2

# Description / parameter IDs
ID_BASEOBJECT_VISIBILITY_EDITOR = 901
ID_BASEOBJECT_VISIBILITY_RENDER = 902
ID_BASEOBJECT_GENERATOR_FLAG = 906
ID_BASEOBJECT_REL_SCALE = 1100
ID_BASEOBJECT_SCALE = 1100
PHONGTAG_PHONG_ANGLE = 1001
PHONGTAG_PHONG_ANGLELIMIT = 1002
DESC_NAME = 1

# Messages / commands
MSG_UPDATE = 1
MCOMMAND_MELT = 300
MCOMMAND_JOIN = 301
MCOMMAND_CURRENTSTATETOOBJECT = 302
MODELINGCOMMANDMODE_ALL = 0
MODELINGCOMMANDMODE_POINTSELECTION = 1
MODELINGCOMMANDMODE_POLYGONSELECTION = 2
MODELINGCOMMANDMODE_EDGESELECTION = 3
MODELINGCOMMANDFLAGS_NONE = 0
MODELINGCOMMANDFLAGS_CREATEUNDO = 1
COPYFLAGS_NONE = 0
COPYFLAGS_NO_HIERARCHY = 4
GETACTIVEOBJECTFLAGS_NONE = 0
GETACTIVEOBJECTFLAGS_CHILDREN = 1
GETACTIVEOBJECTFLAGS_SELECTIONORDER = 2
BIT_CONTROLOBJECT = 1 << 2
BIT_ACTIVE = 1 << 1

Mpoints = 200000
Medges = 200001
Mpolygons = 200002
Mmodel = 200003

OBJECT_GENERATOR = 1 << 2
OBJECT_INPUT = 1 << 3
OBJECT_MODIFIER = 1 << 4
# Generators that consume their children as input.
_INPUT_TYPES = {Osds, Osymmetry, Oconnector}

C4DPL_ENDACTIVITY = 1
C4DPL_STARTACTIVITY = 2
CMD_ENABLED = 1
DLG_TYPE_ASYNC = 1
IMAGERESULT_OK = 1
TAG_VISIBLE = 1
TAG_EXPRESSION = 2
EXECUTIONRESULT_OK = 0
DESCFLAGS_DESC_LOADED = 1
BFH_SCALEFIT = BFV_SCALEFIT = BFH_LEFT = BFV_TOP = BFV_FIT = 0
QUICKTAB_BAR = QUICKTAB_BARTITLE = QUICKTAB_SHOWSINGLE = 0
QUICKTAB_NOMULTISELECT = CUSTOMGUI_QUICKTAB = 0
FORMAT_FLOAT = FORMAT_DEGREE = 0


def EventAdd(*_args, **_kwargs):
    _event_counter[0] += 1


_event_counter = [0]


# ---------------------------------------------------------------------------
# Vector / Matrix
# ---------------------------------------------------------------------------

class Vector:
    __slots__ = ("x", "y", "z")

    def __init__(self, x=0.0, y=None, z=None):
        if y is None and z is None:
            if isinstance(x, Vector):
                self.x, self.y, self.z = x.x, x.y, x.z
                return
            y = z = x
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __add__(self, o):
        return Vector(self.x + o.x, self.y + o.y, self.z + o.z)

    def __sub__(self, o):
        return Vector(self.x - o.x, self.y - o.y, self.z - o.z)

    def __neg__(self):
        return Vector(-self.x, -self.y, -self.z)

    def __mul__(self, o):
        if isinstance(o, Vector):
            return self.Dot(o)
        return Vector(self.x * o, self.y * o, self.z * o)

    __rmul__ = __mul__

    def __truediv__(self, s):
        return Vector(self.x / s, self.y / s, self.z / s)

    def __eq__(self, o):
        return (isinstance(o, Vector)
                and self.x == o.x and self.y == o.y and self.z == o.z)

    def __hash__(self):
        return hash((self.x, self.y, self.z))

    def __repr__(self):
        return f"Vector({self.x!r}, {self.y!r}, {self.z!r})"

    def Dot(self, o):
        return self.x * o.x + self.y * o.y + self.z * o.z

    def Cross(self, o):
        return Vector(self.y * o.z - self.z * o.y,
                      self.z * o.x - self.x * o.z,
                      self.x * o.y - self.y * o.x)

    def GetLength(self):
        return math.sqrt(self.Dot(self))

    def GetNormalized(self):
        n = self.GetLength()
        return self / n if n else Vector(0.0)


class Matrix:
    """Affine matrix with C4D's column layout: p' = off + v1*x + v2*y + v3*z."""

    __slots__ = ("off", "v1", "v2", "v3")

    def __init__(self, off=None, v1=None, v2=None, v3=None):
        self.off = Vector(off) if off is not None else Vector(0.0)
        self.v1 = Vector(v1) if v1 is not None else Vector(1.0, 0.0, 0.0)
        self.v2 = Vector(v2) if v2 is not None else Vector(0.0, 1.0, 0.0)
        self.v3 = Vector(v3) if v3 is not None else Vector(0.0, 0.0, 1.0)

    def __mul__(self, o):
        if isinstance(o, Vector):
            return Vector(
                self.off.x + self.v1.x * o.x + self.v2.x * o.y + self.v3.x * o.z,
                self.off.y + self.v1.y * o.x + self.v2.y * o.y + self.v3.y * o.z,
                self.off.z + self.v1.z * o.x + self.v2.z * o.y + self.v3.z * o.z,
            )
        if isinstance(o, Matrix):
            return Matrix(self * o.off, self.MulV(o.v1), self.MulV(o.v2),
                          self.MulV(o.v3))
        raise TypeError(f"unsupported operand {type(o)!r}")

    def MulV(self, v):
        return Vector(
            self.v1.x * v.x + self.v2.x * v.y + self.v3.x * v.z,
            self.v1.y * v.x + self.v2.y * v.y + self.v3.y * v.z,
            self.v1.z * v.x + self.v2.z * v.y + self.v3.z * v.z,
        )

    def __invert__(self):
        a, b, c = self.v1, self.v2, self.v3
        # Columns a, b, c → rows of the inverse via cross products.
        det = a.Dot(b.Cross(c))
        if abs(det) < 1e-300:
            return Matrix()
        r1 = b.Cross(c) / det
        r2 = c.Cross(a) / det
        r3 = a.Cross(b) / det
        inv = Matrix(Vector(0.0),
                     Vector(r1.x, r2.x, r3.x),
                     Vector(r1.y, r2.y, r3.y),
                     Vector(r1.z, r2.z, r3.z))
        inv.off = -inv.MulV(self.off)
        return inv

    def __eq__(self, o):
        return (isinstance(o, Matrix) and self.off == o.off and self.v1 == o.v1
                and self.v2 == o.v2 and self.v3 == o.v3)

    def __repr__(self):
        return f"Matrix({self.off!r}, {self.v1!r}, {self.v2!r}, {self.v3!r})"


class CPolygon:
    __slots__ = ("a", "b", "c", "d")

    def __init__(self, a, b, c, d=None):
        self.a, self.b, self.c = int(a), int(b), int(c)
        self.d = int(c if d is None else d)

    def IsTriangle(self):
        return self.c == self.d

    def __eq__(self, o):
        return (isinstance(o, CPolygon) and (self.a, self.b, self.c, self.d)
                == (o.a, o.b, o.c, o.d))

    def __repr__(self):
        return f"CPolygon({self.a}, {self.b}, {self.c}, {self.d})"


# ---------------------------------------------------------------------------
# BaseContainer / BaseSelect
# ---------------------------------------------------------------------------

class BaseContainer:
    def __init__(self, *_args):
        self._data = {}

    def _get(self, key, default):
        return self._data.get(key, default)

    def GetInt32(self, key, default=0):
        v = self._data.get(key, default)
        return v if isinstance(v, int) and not isinstance(v, bool) else default

    def SetInt32(self, key, value):
        self._data[key] = int(value)

    def GetBool(self, key, default=False):
        v = self._data.get(key, default)
        return bool(v) if isinstance(v, (bool, int)) else default

    def SetBool(self, key, value):
        self._data[key] = bool(value)

    def GetFloat(self, key, default=0.0):
        v = self._data.get(key, default)
        return float(v) if isinstance(v, (int, float)) else default

    def SetFloat(self, key, value):
        self._data[key] = float(value)

    def GetString(self, key, default=""):
        v = self._data.get(key, default)
        return v if isinstance(v, str) else default

    def SetString(self, key, value):
        if not isinstance(value, str):
            raise TypeError("SetString expects str")
        self._data[key] = value

    def RemoveData(self, key):
        return self._data.pop(key, None) is not None

    def __contains__(self, key):
        return key in self._data

    def __iter__(self):
        return iter(self._data.items())


class BaseSelect:
    def __init__(self):
        self._sel = set()

    def Select(self, i):
        self._sel.add(int(i))
        return True

    def Deselect(self, i):
        self._sel.discard(int(i))
        return True

    def DeselectAll(self):
        self._sel.clear()
        return True

    def IsSelected(self, i):
        return int(i) in self._sel

    def GetCount(self):
        return len(self._sel)

    def GetAll(self, max_elements):
        return [i in self._sel for i in range(max_elements)]

    def SetAll(self, states):
        self._sel = {i for i, s in enumerate(states) if s}
        return True

    def CopyTo(self, dest):
        dest._sel = set(self._sel)
        return True

    def GetClone(self):
        c = BaseSelect()
        c._sel = set(self._sel)
        return c


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

class BaseList2D:
    def __init__(self):
        self._name = ""
        self._params = {}
        self._bc = BaseContainer()
        self._bits = 0

    def GetName(self):
        return self._name

    def SetName(self, name):
        self._name = str(name)

    def GetDataInstance(self):
        return self._bc

    def GetData(self):
        return self._bc

    def __getitem__(self, key):
        return self._params.get(key)

    def __setitem__(self, key, value):
        self._params[key] = value

    def GetBit(self, bit):
        return bool(self._bits & bit)

    def SetBit(self, bit):
        self._bits |= bit

    def DelBit(self, bit):
        self._bits &= ~bit


class BaseTag(BaseList2D):
    def __init__(self, tag_type=0):
        super().__init__()
        self._type = tag_type
        self._host = None

    def GetType(self):
        return self._type

    def CheckType(self, t):
        return self._type == t

    def IsInstanceOf(self, t):
        return self._type == t

    def GetObject(self):
        return self._host

    def GetNext(self):
        if self._host is None:
            return None
        tags = self._host._tags
        i = tags.index(self)
        return tags[i + 1] if i + 1 < len(tags) else None

    def GetPred(self):
        if self._host is None:
            return None
        tags = self._host._tags
        i = tags.index(self)
        return tags[i - 1] if i > 0 else None

    def Remove(self):
        if self._host is not None:
            self._host._tags.remove(self)
            self._host = None


class SelectionTag(BaseTag):
    def __init__(self, tag_type):
        super().__init__(tag_type)
        self._sel = BaseSelect()

    def GetBaseSelect(self):
        return self._sel


class VariableTag(BaseTag):
    def __init__(self, tag_type, count, bytes_per_element):
        super().__init__(tag_type)
        self._count = count
        self._buf = bytearray(count * bytes_per_element)

    def GetDataCount(self):
        return self._count

    def GetLowlevelDataAddressW(self):
        return memoryview(self._buf)

    def GetLowlevelDataAddressR(self):
        return memoryview(bytes(self._buf))


class NormalTag(VariableTag):
    """12 int16 per polygon (a, b, c, d × xyz)."""

    def __init__(self, count=0):
        super().__init__(Tnormal, count, 24)


def _make_tag(tag_type):
    if tag_type in (Tpolygonselection, Tedgeselection, Tpointselection):
        return SelectionTag(tag_type)
    if tag_type == Tnormal:
        return NormalTag(0)
    return BaseTag(tag_type)


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------

class BaseObject(BaseList2D):
    def __init__(self, obj_type=Onull):
        super().__init__()
        self._init_base(obj_type)

    def _init_base(self, obj_type):
        self._type = obj_type
        self._up = None
        self._down = None
        self._next = None
        self._pred = None
        self._doc = None
        self._tags = []
        self._ml = Matrix()
        self._cache = None
        self._deform_cache = None
        self._userdata = []
        self._params.setdefault(ID_BASEOBJECT_VISIBILITY_EDITOR, OBJECT_UNDEF)
        self._params.setdefault(ID_BASEOBJECT_VISIBILITY_RENDER, OBJECT_UNDEF)

    # ── typing ────────────────────────────────────────────────────────────
    def GetType(self):
        return self._type

    def GetInfo(self):
        info = 0
        if self._type in _INPUT_TYPES:
            info |= OBJECT_GENERATOR | OBJECT_INPUT
        elif self._type == Ocube:
            info |= OBJECT_GENERATOR
        return info

    # Scale is a parameter in C4D but also part of the matrix: keep both
    # views consistent like Cinema 4D does.
    def __getitem__(self, key):
        if key == ID_BASEOBJECT_SCALE:
            m = self._ml
            return Vector(m.v1.GetLength(), m.v2.GetLength(), m.v3.GetLength())
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == ID_BASEOBJECT_SCALE:
            m = self._ml
            self._ml = Matrix(m.off, m.v1.GetNormalized() * value.x,
                              m.v2.GetNormalized() * value.y,
                              m.v3.GetNormalized() * value.z)
            return
        super().__setitem__(key, value)

    def CheckType(self, t):
        return t == self._type or t in _BASE_TYPES.get(self._type, ())

    IsInstanceOf = CheckType

    # ── hierarchy ─────────────────────────────────────────────────────────
    def GetUp(self):
        return self._up

    def GetDown(self):
        return self._down

    def GetNext(self):
        return self._next

    def GetPred(self):
        return self._pred

    def GetChildren(self):
        out = []
        c = self._down
        while c:
            out.append(c)
            c = c._next
        return out

    def _top(self):
        o = self
        while o._up is not None:
            o = o._up
        return o

    def GetDocument(self):
        return self._top()._doc

    def Remove(self):
        doc = self._doc
        if self._pred is not None:
            self._pred._next = self._next
        elif self._up is not None:
            self._up._down = self._next
        elif doc is not None and doc._first is self:
            doc._first = self._next
        if self._next is not None:
            self._next._pred = self._pred
        self._up = self._next = self._pred = None
        self._doc = None

    def InsertUnder(self, parent):
        self.Remove()
        self._up = parent
        self._next = parent._down
        if parent._down is not None:
            parent._down._pred = self
        parent._down = self

    def InsertUnderLast(self, parent):
        last = parent._down
        if last is None:
            self.InsertUnder(parent)
            return
        while last._next is not None:
            last = last._next
        self.InsertAfter(last)

    def InsertAfter(self, pred):
        self.Remove()
        self._up = pred._up
        self._doc = pred._doc
        self._pred = pred
        self._next = pred._next
        if pred._next is not None:
            pred._next._pred = self
        pred._next = self

    def InsertBefore(self, nxt):
        self.Remove()
        self._up = nxt._up
        self._doc = nxt._doc
        self._next = nxt
        self._pred = nxt._pred
        if nxt._pred is not None:
            nxt._pred._next = self
        elif nxt._up is not None:
            nxt._up._down = self
        elif nxt._doc is not None:
            nxt._doc._first = self
        nxt._pred = self

    # ── matrices ──────────────────────────────────────────────────────────
    def GetMl(self):
        m = self._ml
        return Matrix(m.off, m.v1, m.v2, m.v3)

    def SetMl(self, m):
        self._ml = Matrix(m.off, m.v1, m.v2, m.v3)

    def GetMg(self):
        if self._up is not None:
            return self._up.GetMg() * self._ml
        return self.GetMl()

    def SetMg(self, m):
        if self._up is not None:
            self._ml = ~self._up.GetMg() * m
        else:
            self.SetMl(m)

    def GetUpMg(self):
        return self._up.GetMg() if self._up is not None else Matrix()

    # ── visibility ────────────────────────────────────────────────────────
    def GetEditorMode(self):
        return self._params.get(ID_BASEOBJECT_VISIBILITY_EDITOR, MODE_UNDEF)

    def SetEditorMode(self, mode):
        self._params[ID_BASEOBJECT_VISIBILITY_EDITOR] = mode

    def GetRenderMode(self):
        return self._params.get(ID_BASEOBJECT_VISIBILITY_RENDER, MODE_UNDEF)

    # ── caches ────────────────────────────────────────────────────────────
    def GetCache(self, *_a):
        return self._cache

    def GetDeformCache(self):
        return self._deform_cache

    # ── tags ──────────────────────────────────────────────────────────────
    def GetFirstTag(self):
        return self._tags[0] if self._tags else None

    def GetTags(self):
        return list(self._tags)

    def GetTag(self, tag_type, nr=0):
        found = [t for t in self._tags if t.CheckType(tag_type)]
        return found[nr] if nr < len(found) else None

    def InsertTag(self, tag, pred=None):
        if tag._host is not None:
            tag.Remove()
        tag._host = self
        if pred is None:
            self._tags.insert(0, tag)
        else:
            self._tags.insert(self._tags.index(pred) + 1, tag)

    def MakeTag(self, tag_type, pred=None):
        tag = _make_tag(tag_type)
        self.InsertTag(tag, pred)
        return tag

    def KillTag(self, tag_type, nr=0):
        t = self.GetTag(tag_type, nr)
        if t:
            t.Remove()

    # ── misc ──────────────────────────────────────────────────────────────
    def Message(self, *_a, **_k):
        return True

    def GetUserDataContainer(self):
        return list(self._userdata)

    def AddUserDataBool(self, name, value):
        bc = BaseContainer()
        bc.SetString(DESC_NAME, name)
        desc_id = ("ud", len(self._userdata) + 1)
        self._userdata.append((desc_id, bc))
        self._params[desc_id] = value

    def GetClone(self, flags=COPYFLAGS_NONE):
        clone = self._clone_self()
        if not flags & COPYFLAGS_NO_HIERARCHY:
            prev = None
            for child in self.GetChildren():
                cc = child.GetClone(flags)
                if prev is None:
                    cc.InsertUnder(clone)
                else:
                    cc.InsertAfter(prev)
                prev = cc
        return clone

    def _clone_self(self):
        c = BaseObject(self._type)
        self._copy_base_into(c)
        return c

    def _copy_base_into(self, c):
        c._name = self._name
        c._params = dict(self._params)
        c._bc._data = dict(self._bc._data)
        c._ml = self.GetMl()
        c._bits = self._bits
        c._cache = self._cache
        c._deform_cache = self._deform_cache
        c._userdata = list(self._userdata)

    def __repr__(self):
        return f"<{type(self).__name__} {self._name!r}>"


class PointObject(BaseObject):
    pass


class PolygonObject(PointObject):
    def __init__(self, pcnt=0, vcnt=0):
        BaseList2D.__init__(self)
        self._init_base(Opolygon)
        self._points = [Vector(0.0) for _ in range(pcnt)]
        self._polys = [CPolygon(0, 0, 0) for _ in range(vcnt)]
        self._ngons = []          # list[list[int]] of polygon indices per N-gon
        self._polyS = BaseSelect()
        self._edgeS = BaseSelect()
        self._pointS = BaseSelect()
        self._polyH = BaseSelect()
        self.set_point_calls = 0
        self.set_polygon_calls = 0

    # points
    def GetPointCount(self):
        return len(self._points)

    def GetPoint(self, i):
        return Vector(self._points[i])

    def SetPoint(self, i, v):
        self.set_point_calls += 1
        self._points[i] = Vector(v)

    def GetAllPoints(self):
        return [Vector(p) for p in self._points]

    def SetAllPoints(self, pts):
        if len(pts) != len(self._points):
            raise ValueError("SetAllPoints: point count mismatch")
        self._points = [Vector(p) for p in pts]
        return True

    # polygons
    def GetPolygonCount(self):
        return len(self._polys)

    def GetPolygon(self, i):
        p = self._polys[i]
        return CPolygon(p.a, p.b, p.c, p.d)

    def SetPolygon(self, i, cp):
        self.set_polygon_calls += 1
        self._polys[i] = CPolygon(cp.a, cp.b, cp.c, cp.d)

    def GetAllPolygons(self):
        return [CPolygon(p.a, p.b, p.c, p.d) for p in self._polys]

    def ResizeObject(self, pcnt, vcnt, ncnt=-1):
        self._points = (self._points + [Vector(0.0)] * pcnt)[:pcnt]
        self._polys = (self._polys + [CPolygon(0, 0, 0)] * vcnt)[:vcnt]
        if ncnt == 0:
            self._ngons = []
        else:
            self._ngons = [g for g in self._ngons if all(i < vcnt for i in g)]
        return True

    # selections
    def GetPolygonS(self):
        return self._polyS

    def GetEdgeS(self):
        return self._edgeS

    def GetPointS(self):
        return self._pointS

    def GetPolygonH(self):
        return self._polyH

    # N-gons
    def GetNgonCount(self):
        return len(self._ngons)

    def GetPolygonTranslationMap(self):
        """(ngoncnt, polymap): polymap[i] = index of the visible face that
        polygon i belongs to; visible faces are regular polygons and N-gons."""
        ngon_of = {}
        for gi, g in enumerate(self._ngons):
            for p in g:
                ngon_of[p] = gi
        polymap = []
        face_index_of_ngon = {}
        next_face = 0
        for i in range(len(self._polys)):
            gi = ngon_of.get(i)
            if gi is None:
                polymap.append(next_face)
                next_face += 1
            else:
                if gi not in face_index_of_ngon:
                    face_index_of_ngon[gi] = next_face
                    next_face += 1
                polymap.append(face_index_of_ngon[gi])
        return next_face, polymap

    def GetNGonTranslationMap(self, ngoncnt, polymap):
        out = [[] for _ in range(ngoncnt)]
        for poly_index, face_index in enumerate(polymap):
            out[face_index].append(poly_index)
        return out

    def GetNgonEdgesCompact(self):
        """One int per polygon; bit i set = edge i is an N-gon internal edge."""
        bits = [0] * len(self._polys)
        for g in self._ngons:
            edge_count = {}
            for p in g:
                for e in _poly_edges(self._polys[p]):
                    edge_count[e] = edge_count.get(e, 0) + 1
            for p in g:
                cp = self._polys[p]
                for slot, (v1, v2) in _poly_edge_slots(cp):
                    key = (min(v1, v2), max(v1, v2))
                    if edge_count.get(key, 0) > 1:
                        bits[p] |= 1 << slot
        return bits

    def _clone_self(self):
        c = PolygonObject(0, 0)
        self._copy_base_into(c)
        c._points = self.GetAllPoints()
        c._polys = self.GetAllPolygons()
        c._ngons = [list(g) for g in self._ngons]
        return c

    # test helper
    def set_geometry(self, points, polys, ngons=None):
        self._points = [Vector(*p) if not isinstance(p, Vector) else Vector(p)
                        for p in points]
        self._polys = [p if isinstance(p, CPolygon) else CPolygon(*p)
                       for p in polys]
        self._ngons = [list(g) for g in (ngons or [])]


def _poly_edge_slots(cp):
    if cp.c == cp.d:
        return [(0, (cp.a, cp.b)), (1, (cp.b, cp.c)), (3, (cp.c, cp.a))]
    return [(0, (cp.a, cp.b)), (1, (cp.b, cp.c)), (2, (cp.c, cp.d)),
            (3, (cp.d, cp.a))]


def _poly_edges(cp):
    return [(min(v1, v2), max(v1, v2)) for _, (v1, v2) in _poly_edge_slots(cp)]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class _Documents:
    pass


class BaseDocument(BaseList2D):
    def __init__(self):
        super().__init__()
        self._first = None
        self._undo_depth = 0
        self._undo_open = None
        self.undo_steps = []          # list of lists of (undotype, obj)
        self._active = []
        self._mode = Mmodel
        self._name = "Untitled 1"

    def GetFirstObject(self):
        return self._first

    def GetObjects(self):
        out = []
        o = self._first
        while o:
            out.append(o)
            o = o._next
        return out

    def InsertObject(self, op, parent=None, pred=None, checknames=False):
        op.Remove()
        if pred is not None:
            op.InsertAfter(pred)
        elif parent is not None:
            op.InsertUnder(parent)
        else:
            op._doc = self
            op._next = self._first
            if self._first is not None:
                self._first._pred = op
            self._first = op
        if op._up is None:
            op._doc = self

    # undo recording
    def StartUndo(self):
        if self._undo_depth == 0:
            self._undo_open = []
        self._undo_depth += 1
        return True

    def AddUndo(self, undotype, data, *_a):
        if self._undo_depth <= 0:
            raise RuntimeError("AddUndo called outside StartUndo/EndUndo")
        self._undo_open.append((undotype, data))
        return True

    def EndUndo(self):
        if self._undo_depth <= 0:
            raise RuntimeError("EndUndo without StartUndo")
        self._undo_depth -= 1
        if self._undo_depth == 0:
            if self._undo_open:          # empty groups are discarded
                self.undo_steps.append(self._undo_open)
            self._undo_open = None
        return True

    def FlushUndoBuffer(self):
        self.undo_steps.clear()

    # selection / modes
    def GetActiveObjects(self, flags=GETACTIVEOBJECTFLAGS_NONE):
        if not flags & GETACTIVEOBJECTFLAGS_CHILDREN:
            return list(self._active)
        out = []

        def walk(o):
            out.append(o)
            for c in o.GetChildren():
                walk(c)
        for o in self._active:
            walk(o)
        # C4D returns each object once
        seen, uniq = set(), []
        for o in out:
            if id(o) not in seen:
                seen.add(id(o))
                uniq.append(o)
        return uniq

    def SetActiveObject(self, op, mode=0):
        self._active = [op] if op is not None else []

    def GetMode(self):
        return self._mode

    def SetMode(self, mode):
        self._mode = mode

    def GetDocumentName(self):
        return self._name

    def SetDocumentName(self, name):
        self._name = name

    def ExecutePasses(self, *_a, **_k):
        return True


documents = _Documents()
documents.BaseDocument = BaseDocument
_active_doc = [None]


def _get_active_document():
    return _active_doc[0]


def _set_active_document(doc):
    _active_doc[0] = doc


documents.GetActiveDocument = _get_active_document
documents.SetActiveDocument = _set_active_document


# ---------------------------------------------------------------------------
# utils (incl. a small MCOMMAND_MELT emulation)
# ---------------------------------------------------------------------------

class _Utils:
    pass


utils = _Utils()
utils.DegToRad = math.radians
utils.RadToDeg = math.degrees
utils.modeling_calls = []


def _melt(obj):
    """Emulate MCOMMAND_MELT on the polygon selection.

    Each edge-connected component of selected polygons becomes one N-gon.
    Like Cinema 4D, the component's polygons are replaced by a new internal
    triangulation (a fan over the outline), appended at the end, and the
    new polygons are registered as an N-gon and left selected.
    """
    sel = sorted(obj._polyS._sel)
    if len(sel) < 2:
        return False
    polys = obj._polys
    # union-find over shared edges
    parent = {p: p for p in sel}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    edge_owner = {}
    for p in sel:
        for e in _poly_edges(polys[p]):
            if e in edge_owner:
                ra, rb = find(edge_owner[e]), find(p)
                if ra != rb:
                    parent[ra] = rb
            else:
                edge_owner[e] = p
    comps = {}
    for p in sel:
        comps.setdefault(find(p), []).append(p)

    new_polys_all = []
    for comp in comps.values():
        if len(comp) < 2:
            continue
        # directed boundary edges
        und = {}
        for p in comp:
            for _, (v1, v2) in _poly_edge_slots(polys[p]):
                k = (min(v1, v2), max(v1, v2))
                und[k] = und.get(k, 0) + 1
        nxt = {}
        for p in comp:
            for _, (v1, v2) in _poly_edge_slots(polys[p]):
                if und[(min(v1, v2), max(v1, v2))] == 1:
                    nxt[v1] = v2
        start = polys[min(comp)].a if polys[min(comp)].a in nxt else next(iter(nxt))
        loop = [start]
        v = nxt[start]
        while v != start and len(loop) <= len(nxt):
            loop.append(v)
            v = nxt[v]
        fan = [CPolygon(loop[0], loop[i], loop[i + 1])
               for i in range(1, len(loop) - 1)]
        new_polys_all.append((comp, fan))

    removed = set(p for comp, _ in new_polys_all for p in comp)
    if not removed:
        return False
    keep = [i for i in range(len(polys)) if i not in removed]
    remap = {old: new for new, old in enumerate(keep)}
    new_list = [polys[i] for i in keep]
    new_ngons = []
    for g in obj._ngons:
        if all(p in remap for p in g):
            new_ngons.append([remap[p] for p in g])
    new_sel = set()
    for _, fan in new_polys_all:
        start = len(new_list)
        new_list.extend(fan)
        idx = list(range(start, start + len(fan)))
        new_ngons.append(idx)
        new_sel.update(idx)
    obj._polys = new_list
    obj._ngons = new_ngons
    obj._polyS._sel = new_sel
    return True


def _send_modeling_command(command=None, list=None, mode=0, bc=None, doc=None,
                           flags=0):
    utils.modeling_calls.append((command, mode, flags))
    if command == MCOMMAND_MELT:
        ok = False
        for o in list or []:
            if isinstance(o, PolygonObject):
                ok = _melt(o) or ok
        return ok
    return False


utils.SendModelingCommand = _send_modeling_command


def _matrix_scale(v):
    return Matrix(Vector(0.0), Vector(v.x, 0.0, 0.0), Vector(0.0, v.y, 0.0),
                  Vector(0.0, 0.0, v.z))


def _matrix_move(v):
    return Matrix(Vector(v))


def _matrix_rot_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    return Matrix(Vector(0.0), Vector(c, 0.0, -s), Vector(0.0, 1.0, 0.0),
                  Vector(s, 0.0, c))


utils.MatrixScale = _matrix_scale
utils.MatrixMove = _matrix_move
utils.MatrixRotY = _matrix_rot_y


# ---------------------------------------------------------------------------
# gui / plugins / bitmaps
# ---------------------------------------------------------------------------

class _Gui:
    pass


gui = _Gui()
gui.messages = []


def _message_dialog(text, *_a):
    gui.messages.append(text)
    return True


gui.MessageDialog = _message_dialog


class GeDialog:
    def __init__(self):
        self._values = {}
        self._enabled = {}
        self._hidden = {}
        self._timer = 0
        self._open = False

    def __getattr__(self, name):
        # Layout calls (AddButton, GroupBegin, ...) are accepted and ignored.
        if name[:1].isupper():
            return lambda *a, **k: True
        raise AttributeError(name)

    def SetString(self, id_, value, *a, **k):
        self._values[id_] = value
        return True

    def GetString(self, id_):
        return self._values.get(id_, "")

    def SetBool(self, id_, value):
        self._values[id_] = bool(value)
        return True

    def GetBool(self, id_):
        return bool(self._values.get(id_, False))

    def SetFloat(self, id_, value, *a, **k):
        self._values[id_] = float(value)
        return True

    def GetFloat(self, id_):
        return float(self._values.get(id_, 0.0))

    def Enable(self, id_, state):
        self._enabled[id_] = bool(state)
        return True

    def IsEnabled(self, id_):
        return self._enabled.get(id_, True)

    def HideElement(self, id_, hide):
        self._hidden[id_] = bool(hide)
        return True

    def SetTimer(self, ms):
        self._timer = ms

    def IsOpen(self):
        return self._open

    def DestroyWindow(self):
        return None

    def AskClose(self):
        return False

    def AddCustomGui(self, *a, **k):
        return None


gui.GeDialog = GeDialog


class _Plugins:
    pass


plugins = _Plugins()


class TagData:
    def InitAttr(self, node, typ, desc_id):
        return True


class CommandData:
    pass


class GeResource:
    def Init(self, path):
        return True


plugins.TagData = TagData
plugins.CommandData = CommandData
plugins.GeResource = GeResource
plugins.registered = []


def _register(kind):
    def reg(*a, **k):
        plugins.registered.append((kind, k.get("id")))
        return True
    return reg


plugins.RegisterTagPlugin = _register("tag")
plugins.RegisterCommandPlugin = _register("command")


class _Bitmaps:
    pass


bitmaps = _Bitmaps()


class BaseBitmap:
    def InitWith(self, path):
        return (IMAGERESULT_OK, False)


bitmaps.BaseBitmap = BaseBitmap


class DescID:
    def __init__(self, *levels):
        self.levels = levels


class DescLevel:
    def __init__(self, id_, *a):
        self.id = id_


def _install_submodules():
    import sys
    for name in ("utils", "gui", "plugins", "documents", "bitmaps"):
        sys.modules[f"{__name__}.{name}"] = globals()[name]


_install_submodules()
