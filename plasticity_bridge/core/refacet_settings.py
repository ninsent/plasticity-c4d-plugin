"""
Refacet settings → ``PlasticityClient.refacet_some`` keyword arguments.

Shared by the dialog's "Refacet Selected" button and the Auto-Refacet tag
so both always send identical requests.  No ``c4d`` import.
"""

import math


def build_refacet_kwargs(tri_mode, advanced, tolerance, angle,
                         min_width=0.0, max_width=0.0,
                         curve_chord_tol=0.01, curve_chord_angle=0.35,
                         surface_plane_tol=0.01, surface_angle_tol=0.35):
    """Translate UI/tag settings into refacet request parameters.

    * Tris: ``max_sides=3``, ``plane_angle=0``; N-gons: ``max_sides=128``,
      ``plane_angle=π/4``.
    * Simple mode uses ``tolerance``/``angle`` for both the curve-chord and
      the surface-plane parameters; Advanced mode uses the four separate
      values.
    * ``curve_chord_max = max_width · √½``.

    Angles are in radians.  The facet shape is left at the encoder default
    (``FacetShapeType.CUT``).
    """
    if advanced:
        cct, cca = curve_chord_tol, curve_chord_angle
        spt, spa = surface_plane_tol, surface_angle_tol
    else:
        cct = spt = tolerance
        cca = spa = angle

    return {
        "relative_to_bbox":        True,
        "curve_chord_tolerance":   cct,
        "curve_chord_angle":       cca,
        "surface_plane_tolerance": spt,
        "surface_plane_angle":     spa,
        "match_topology":          True,
        "max_sides":               3 if tri_mode else 128,
        "plane_angle":             0.0 if tri_mode else math.pi / 4.0,
        "min_width":               min_width,
        "max_width":               max_width,
        "curve_chord_max":         max_width * math.sqrt(0.5),
    }
