"""
Plasticity Auto-Refacet Tag

A custom TagData plugin that stores refacet settings on individual objects.
When attached to a Plasticity mesh, the SceneHandler will automatically
re-request tessellation with these settings after every refresh or
live-link geometry update.

The tag is purely a data holder — Execute() is a no-op.  The handler
reads its parameters on demand via read_tag_refacet_kwargs().
"""

import c4d
import math
from c4d import plugins

# ---------------------------------------------------------------------------
# Plugin ID — replace with a properly registered ID from plugincafe.maxon.net
# ---------------------------------------------------------------------------
TAG_PLUGIN_ID = 1068209

# ---------------------------------------------------------------------------
# Parameter IDs  (must match res/description/tplasticityrefacet.h)
# ---------------------------------------------------------------------------
PREF_TOPOLOGY         = 1000
PREF_TOPOLOGY_TRIS    = 0
PREF_TOPOLOGY_NGONS   = 1

PREF_OPTIONS_MODE     = 1001
PREF_OPTIONS_SIMPLE   = 0
PREF_OPTIONS_ADVANCED = 1

PREF_TOLERANCE        = 1010
PREF_ANGLE            = 1011

PREF_MIN_WIDTH        = 1020
PREF_MAX_WIDTH        = 1021
PREF_CURVE_CHORD_TOL  = 1022
PREF_CURVE_CHORD_ANG  = 1023
PREF_SURF_PLANE_TOL   = 1024
PREF_SURF_ANGLE_TOL   = 1025

PREF_GRP_SIMPLE       = 2000
PREF_GRP_ADVANCED     = 2001


# ===================================================================
# Helper: read tag settings → refacet_some kwargs
# ===================================================================

def read_tag_refacet_kwargs(tag):
    """Convert the tag's parameters into keyword arguments for
    ``PlasticityClient.refacet_some()``.

    Returns a dict ready to be unpacked with ``**kwargs``.
    """
    tri_mode = tag[PREF_TOPOLOGY] == PREF_TOPOLOGY_TRIS
    advanced = tag[PREF_OPTIONS_MODE] == PREF_OPTIONS_ADVANCED

    max_sides   = 3   if tri_mode else 128
    plane_angle = 0.0 if tri_mode else math.pi / 4.0

    if advanced:
        cct = tag[PREF_CURVE_CHORD_TOL]
        cca = tag[PREF_CURVE_CHORD_ANG]
        spt = tag[PREF_SURF_PLANE_TOL]
        spa = tag[PREF_SURF_ANGLE_TOL]
    else:
        tolerance = tag[PREF_TOLERANCE]
        angle     = tag[PREF_ANGLE]
        cct = tolerance
        cca = angle
        spt = tolerance
        spa = angle

    min_width       = tag[PREF_MIN_WIDTH]
    max_width       = tag[PREF_MAX_WIDTH]
    curve_chord_max = max_width * math.sqrt(0.5)

    return {
        "relative_to_bbox":       True,
        "curve_chord_tolerance":  cct,
        "curve_chord_angle":      cca,
        "surface_plane_tolerance": spt,
        "surface_plane_angle":    spa,
        "match_topology":         True,
        "max_sides":              max_sides,
        "plane_angle":            plane_angle,
        "min_width":              min_width,
        "max_width":              max_width,
        "curve_chord_max":        curve_chord_max,
    }


# ===================================================================
# TagData plugin
# ===================================================================

class PlasticityRefacetTag(plugins.TagData):
    """Custom tag that stores auto-refacet settings."""

    # ----- Init --------------------------------------------------------
    def Init(self, node):
        self.InitAttr(node, int,   PREF_TOPOLOGY)
        self.InitAttr(node, int,   PREF_OPTIONS_MODE)
        self.InitAttr(node, float, PREF_TOLERANCE)
        self.InitAttr(node, float, PREF_ANGLE)
        self.InitAttr(node, float, PREF_MIN_WIDTH)
        self.InitAttr(node, float, PREF_MAX_WIDTH)
        self.InitAttr(node, float, PREF_CURVE_CHORD_TOL)
        self.InitAttr(node, float, PREF_CURVE_CHORD_ANG)
        self.InitAttr(node, float, PREF_SURF_PLANE_TOL)
        self.InitAttr(node, float, PREF_SURF_ANGLE_TOL)

        node[PREF_TOPOLOGY]        = PREF_TOPOLOGY_TRIS
        node[PREF_OPTIONS_MODE]    = PREF_OPTIONS_SIMPLE
        node[PREF_TOLERANCE]       = 0.01
        node[PREF_ANGLE]           = math.radians(25.0)
        node[PREF_MIN_WIDTH]       = 0.0
        node[PREF_MAX_WIDTH]       = 0.0
        node[PREF_CURVE_CHORD_TOL] = 0.01
        node[PREF_CURVE_CHORD_ANG] = math.radians(20.0)
        node[PREF_SURF_PLANE_TOL]  = 0.01
        node[PREF_SURF_ANGLE_TOL]  = math.radians(20.0)
        return True

    # ----- Execute (no-op) ---------------------------------------------
    def Execute(self, tag, doc, op, bt, priority, flags):
        return c4d.EXECUTIONRESULT_OK

    # ----- Description: hide Simple / Advanced groups -------------------
    def GetDDescription(self, node, description, flags):
        if not description.LoadDescription(node.GetType()):
            return False

        mode = node[PREF_OPTIONS_MODE]
        singleID = description.GetSingleDescID()

        # Hide the Simple group when in Advanced mode
        simple_id = c4d.DescID(c4d.DescLevel(PREF_GRP_SIMPLE))
        if singleID is None or simple_id.IsPartOf(singleID)[0]:
            bc = description.GetParameterI(simple_id)
            if bc is not None:
                bc.SetBool(c4d.DESC_HIDE, mode != PREF_OPTIONS_SIMPLE)

        # Hide the Advanced group when in Simple mode
        adv_id = c4d.DescID(c4d.DescLevel(PREF_GRP_ADVANCED))
        if singleID is None or adv_id.IsPartOf(singleID)[0]:
            bc = description.GetParameterI(adv_id)
            if bc is not None:
                bc.SetBool(c4d.DESC_HIDE, mode != PREF_OPTIONS_ADVANCED)

        return (True, flags | c4d.DESCFLAGS_DESC_LOADED)


# ===================================================================
# Registration
# ===================================================================

def register_refacet_tag(tag_icon, res):
    """Register the auto-refacet tag plugin.  Called from plasticity_c4d.pyp.

    Args:
        tag_icon: A loaded BaseBitmap for the tag icon, or None.
        res: The GeResource instance (``__res__``) from the main .pyp module.
    """
    result = plugins.RegisterTagPlugin(
        id=TAG_PLUGIN_ID,
        str="Plasticity Auto-Refacet",
        info=c4d.TAG_VISIBLE | c4d.TAG_EXPRESSION,
        g=PlasticityRefacetTag,
        description="Tplasticityrefacet",
        icon=tag_icon,
        res=res,
    )

    if result:
        print(f"[Plasticity Bridge] Auto-Refacet tag registered (ID: {TAG_PLUGIN_ID})")
    else:
        print("[Plasticity Bridge] Failed to register Auto-Refacet tag")

    return result