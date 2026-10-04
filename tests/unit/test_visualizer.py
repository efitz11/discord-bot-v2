import itertools

from core.visualizer import (
    TEAM_COLORS, _colors_distinct, _distinct_team_pair, _team_colors, _readable,
    generate_game_spray_chart, generate_spray_chart,
    _park_wall_outline, _spray_wall_profile,
)


def test_similar_team_colors_are_separated():
    away, home = _distinct_team_pair("WSH", "PHI")  # both red
    assert _colors_distinct(away, home)


def test_distinct_primaries_are_kept():
    away, home = _distinct_team_pair("NYY", "BOS")
    assert away == _readable(_team_colors("NYY")[0])
    assert home == _readable(_team_colors("BOS")[0])


def test_every_team_pairing_is_distinct():
    for a, h in itertools.permutations(TEAM_COLORS, 2):
        assert _colors_distinct(*_distinct_team_pair(a, h)), (a, h)


def _events():
    return [
        {'hc_x_ft': 0, 'hc_y_ft': 200, 'events': 'home_run', 'team_batting': 'WSH'},
        {'hc_x_ft': 50, 'hc_y_ft': 120, 'events': 'single', 'team_batting': 'PHI'},
        {'hc_x_ft': -80, 'hc_y_ft': 150, 'events': 'field_out', 'team_batting': 'WSH'},
        {'hc_x_ft': 'bad', 'hc_y_ft': None, 'events': 'double', 'team_batting': 'PHI'},
    ]


def test_game_spray_chart_renders_both_modes():
    base = {'events': _events(), 'away': 'WSH', 'home': 'PHI'}
    assert generate_game_spray_chart({**base, 'color_by': 'team'}).read(8) == b'\x89PNG\r\n\x1a\n'
    outcome = {**base, 'color_by': 'outcome', 'player_name': 'James Wood', 'is_pitcher': False}
    assert generate_game_spray_chart(outcome).read(8) == b'\x89PNG\r\n\x1a\n'


def test_season_spray_chart_renders():
    data = {'events': _events(), 'player_name': 'James Wood', 'year': 2026}
    assert generate_spray_chart(data).read(8) == b'\x89PNG\r\n\x1a\n'


FENWAY = 3
FENWAY_POSTED = [(-45, 310.0), (-22.5, 390.0), (0, 420.0), (22.5, 380.0), (45, 302.0)]


def test_traced_wall_is_scaled_to_posted_foul_lines():
    wall = _spray_wall_profile(FENWAY_POSTED, _park_wall_outline(FENWAY), 400.0)
    assert len(wall) == 181
    # One uniform scale, so the shape is kept: the two foul lines together match the posted pair.
    assert abs(wall[0][1] + wall[-1][1] - (310 + 302)) < 0.5
    # The Green Monster: left field stays shallow well off the line, unlike an interpolated wall.
    left_field = dict(wall)[-30.0]
    assert left_field < 340


def test_park_without_outline_interpolates_posted_distances():
    assert _park_wall_outline(999999) is None
    wall = dict(_spray_wall_profile(FENWAY_POSTED, None, 400.0))
    assert wall[-45.0] == 310 and wall[0.0] == 420 and wall[22.5] == 380
    assert abs(wall[-11.0] - (420 - 30 * 11 / 22.5)) < 1e-9


def test_park_with_no_data_uses_generic_wall():
    assert all(d == 400.0 for _, d in _spray_wall_profile(None, None, 400.0))
