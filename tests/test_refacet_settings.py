"""Refacet kwargs: one shared implementation for the dialog and the tag."""

import math

import pytest

from plasticity_bridge.core.protocol import FacetShapeType, encode_refacet_some
from plasticity_bridge.core.refacet_settings import build_refacet_kwargs
from plasticity_bridge.core import refacet_tag as T


def _dialog_1_1_6(tri_mode, advanced, tol, ang, minw, maxw, a, b, c, d):
    """The computation 1.1.6's dialog._do_refacet performed inline."""
    max_sides = 3 if tri_mode else 128
    plane_angle = 0.0 if tri_mode else math.pi / 4.0
    curve_chord_max = maxw * math.sqrt(0.5)
    if advanced:
        cct, cca, spt, spa = a, b, c, d
    else:
        cct, cca, spt, spa = tol, ang, tol, ang
    return dict(relative_to_bbox=True, curve_chord_tolerance=cct,
                curve_chord_angle=cca, surface_plane_tolerance=spt,
                surface_plane_angle=spa, match_topology=True,
                max_sides=max_sides, plane_angle=plane_angle, min_width=minw,
                max_width=maxw, curve_chord_max=curve_chord_max,
                shape=FacetShapeType.CUT)


@pytest.mark.parametrize("tri_mode", [True, False])
@pytest.mark.parametrize("advanced", [True, False])
def test_same_bytes_as_1_1_6_dialog(tri_mode, advanced):
    args = (0.02, 0.4, 0.1, 7.5, 0.03, 0.5, 0.04, 0.6)
    new = build_refacet_kwargs(tri_mode, advanced, *args)
    old = _dialog_1_1_6(tri_mode, advanced, *args)
    assert (encode_refacet_some(3, "f", [1, 2], **new)
            == encode_refacet_some(3, "f", [1, 2], **old))


class FakeTag(dict):
    pass


def test_tag_reader_uses_shared_builder():
    tag = FakeTag({T.PREF_TOPOLOGY: T.PREF_TOPOLOGY_NGONS,
                   T.PREF_OPTIONS_MODE: T.PREF_OPTIONS_ADVANCED,
                   T.PREF_TOLERANCE: 0.02, T.PREF_ANGLE: 0.4,
                   T.PREF_MIN_WIDTH: 0.1, T.PREF_MAX_WIDTH: 2.0,
                   T.PREF_CURVE_CHORD_TOL: 0.03, T.PREF_CURVE_CHORD_ANG: 0.5,
                   T.PREF_SURF_PLANE_TOL: 0.04, T.PREF_SURF_ANGLE_TOL: 0.6})
    kw = T.read_tag_refacet_kwargs(tag)
    assert kw == build_refacet_kwargs(False, True, 0.02, 0.4, 0.1, 2.0,
                                      0.03, 0.5, 0.04, 0.6)
    assert kw["max_sides"] == 128 and kw["plane_angle"] == pytest.approx(math.pi / 4)
    assert kw["curve_chord_max"] == pytest.approx(2.0 * math.sqrt(0.5))
    tag[T.PREF_OPTIONS_MODE] = T.PREF_OPTIONS_SIMPLE
    tag[T.PREF_TOPOLOGY] = T.PREF_TOPOLOGY_TRIS
    kw = T.read_tag_refacet_kwargs(tag)
    assert (kw["curve_chord_tolerance"], kw["surface_plane_angle"]) == (0.02, 0.4)
    assert kw["max_sides"] == 3 and kw["plane_angle"] == 0.0


def test_kwargs_are_hashable_for_batching():
    kw = build_refacet_kwargs(True, False, 0.01, 0.3)
    assert hash(tuple(sorted(kw.items())))


def test_tag_ids_unchanged():
    # Must match res/description/tplasticityrefacet.h and saved scenes.
    assert T.TAG_PLUGIN_ID == 1068209
    assert (T.PREF_TOPOLOGY, T.PREF_OPTIONS_MODE, T.PREF_TOLERANCE, T.PREF_ANGLE,
            T.PREF_MIN_WIDTH, T.PREF_MAX_WIDTH, T.PREF_CURVE_CHORD_TOL,
            T.PREF_CURVE_CHORD_ANG, T.PREF_SURF_PLANE_TOL, T.PREF_SURF_ANGLE_TOL,
            T.PREF_GRP_SIMPLE, T.PREF_GRP_ADVANCED) == (
        1000, 1001, 1010, 1011, 1020, 1021, 1022, 1023, 1024, 1025, 2000, 2001)
