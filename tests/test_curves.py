# -*- coding: utf-8 -*-
"""Кривые разогрева/остывания: направление, выдержки, длительность цикла."""
import pr8_cathode_app as A

HEAT = [tuple(map(float, p)) for p in A.DEFAULTS["heat_profile"]]
COOL = [tuple(map(float, p)) for p in A.DEFAULTS["cool_profile"]]


def duration_min(I0, segs):
    return A.curve_points(0.0, I0, segs)[-1][0] / 60


def test_full_heat_up_takes_about_54_min():
    assert 53.5 < duration_min(0, A.up_segments(HEAT, 0, 88)) < 54.0


def test_full_cool_down_takes_about_46_min():
    assert 46.0 < duration_min(88, A.down_segments(COOL, 88, 0)) < 46.6


def test_up_never_goes_down_and_down_never_goes_up():
    assert A.up_segments(HEAT, 85, 80) == []
    assert A.up_segments(HEAT, 0, 0) == []
    assert A.up_segments(HEAT, 50, -5) == []
    assert A.down_segments(COOL, 50, 60) == []
    assert A.down_segments(COOL, 0.0, 0) == []


def test_rate_follows_segment_of_current_point():
    # с 45 А вверх — участок 40→60 (2,0 А/мин); с 45 А вниз — участок 70→40 (2,0 А/мин)
    assert A.up_segments(HEAT, 45, 88)[0] == (60.0, 2.0 / 60, 0.0)
    assert A.down_segments(COOL, 45, 0)[0] == (40.0, 2.0 / 60, 0.0)


def test_hold_at_70_not_skipped_just_below_the_point():
    assert A.up_segments(HEAT, 69.97, 88)[0] == (70.0, 2.0 / 60, 180.0)


def test_limit_between_points_ends_exactly_at_limit():
    segs = A.up_segments(HEAT, 0, 50)
    assert segs[-1][0] == 50 and all(t <= 50 for t, _, _ in segs)
    segs = A.down_segments(COOL, 88, 30)
    assert segs[-1][0] == 30 and all(t >= 30 for t, _, _ in segs)


def test_curve_points_time_is_monotonic():
    pts = A.curve_points(0.0, 0.0, A.up_segments(HEAT, 0, 88))
    assert all(b[0] >= a[0] for a, b in zip(pts, pts[1:]))
