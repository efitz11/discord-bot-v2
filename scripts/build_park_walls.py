"""Build core/data/park_walls.json — the outfield wall shape of each MLB park.

Source: GeomMLBStadiums (https://github.com/bdilday/GeomMLBStadiums), MIT licensed,
Copyright (c) 2018 Ben Dilday. Its traced park outlines are in Statcast's hc_x/hc_y
coordinate space, the same space our spray-chart dots come from.

For each park we cast a ray from home plate every half degree across fair territory
(-45° left field line .. +45° right field line) and record where it meets the outfield
wall, in feet. Run from the repo root:

    python3 scripts/build_park_walls.py
"""
import csv
import io
import json
import math
import urllib.request

SOURCE_URL = "https://raw.githubusercontent.com/bdilday/GeomMLBStadiums/master/inst/extdata/mlb_stadia_paths.csv"
OUT_PATH = "core/data/park_walls.json"

# Back tip of home plate in the dataset's coordinates (where both foul lines start).
PLATE_X, PLATE_Y = 125.18, 204.53
# Dataset units → feet. Median fit of every park's foul lines and center field against
# the MLB Stats API's posted distances.
FT_PER_UNIT = 2.4

# Dataset park name → MLB Stats API venue id.
VENUES = {
    'angels': 1, 'astros': 2392, 'blue_jays': 14, 'braves': 4705,
    'brewers': 32, 'cardinals': 2889, 'cubs': 17, 'diamondbacks': 15, 'dodgers': 22,
    'giants': 2395, 'guardians': 5, 'mariners': 680, 'marlins': 4169, 'mets': 3289,
    'nationals': 3309, 'padres': 2680, 'phillies': 2681, 'pirates': 31, 'rangers': 5325,
    'rays': 12, 'red_sox': 3, 'reds': 2602, 'rockies': 19, 'tigers': 2394, 'twins': 3312,
    'white_sox': 4, 'yankees': 3313,
    # Left out:
    #   athletics — the Oakland Coliseum, which the A's no longer play in.
    #   orioles   — traced before Camden Yards' 2025 left-field wall change.
    #   royals    — traced before Kauffman Stadium's fence change (the API's foul lines
    #               now run 10+ ft longer than the outline's).
}

# The traced wall gets unreliable right at the foul poles, where it doubles back into
# the stands. Past this angle the wall is blended into the foul line's measured length.
POLE_BLEND_DEG = 43.0


def ray_to_wall(poly, angle_deg):
    """Farthest point where a ray from the plate at angle_deg crosses the outline."""
    dx, dy = math.sin(math.radians(angle_deg)), math.cos(math.radians(angle_deg))
    best = None
    for (x0, y0), (x1, y1) in zip(poly, poly[1:] + poly[:1]):
        ex, ey = x1 - x0, y1 - y0
        den = dx * ey - dy * ex
        if abs(den) < 1e-12:
            continue
        t = (x0 * ey - y0 * ex) / den
        u = (x0 * dy - y0 * dx) / den
        if t > 0 and 0 <= u <= 1:
            best = t if best is None else max(best, t)
    return best


def main():
    with urllib.request.urlopen(SOURCE_URL) as resp:
        rows = list(csv.DictReader(io.StringIO(resp.read().decode())))

    def segment(team, name):
        return [(float(r['x']) - PLATE_X, PLATE_Y - float(r['y']))
                for r in rows if r['team'] == team and r['segment'] == name]

    angles = [a / 2 for a in range(-90, 91)]
    parks = {}
    for team, venue_id in sorted(VENUES.items()):
        wall = segment(team, 'outfield_outer')
        fouls = segment(team, 'foul_lines')
        left_line = max(math.hypot(x, y) for x, y in fouls if x < 0)
        right_line = max(math.hypot(x, y) for x, y in fouls if x > 0)

        inner = {a: ray_to_wall(wall, a) for a in angles if abs(a) <= POLE_BLEND_DEG}
        dists = []
        for a in angles:
            if abs(a) <= POLE_BLEND_DEG:
                d = inner[a]
            else:
                edge = inner[math.copysign(POLE_BLEND_DEG, a)]
                line = left_line if a < 0 else right_line
                t = (abs(a) - POLE_BLEND_DEG) / (45 - POLE_BLEND_DEG)
                d = edge + t * (line - edge)
            dists.append(round(d * FT_PER_UNIT, 1))
        parks[str(venue_id)] = {'name': team, 'wall_ft': dists}

    out = {
        '_source': "GeomMLBStadiums by Ben Dilday — https://github.com/bdilday/GeomMLBStadiums",
        '_license': "MIT License. Copyright (c) 2018 Ben Dilday. Permission is hereby granted, free of charge, "
                    "to any person obtaining a copy of this software and associated documentation files (the "
                    "\"Software\"), to deal in the Software without restriction, including without limitation the "
                    "rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the "
                    "Software, and to permit persons to whom the Software is furnished to do so, subject to the "
                    "following conditions: The above copyright notice and this permission notice shall be included "
                    "in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED \"AS IS\", "
                    "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES "
                    "OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL "
                    "THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER "
                    "IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE "
                    "SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.",
        'angles_deg': {'start': -45, 'step': 0.5},
        'parks': parks,
    }
    with open(OUT_PATH, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
        f.write('\n')
    print(f"Wrote {len(parks)} parks to {OUT_PATH}")


if __name__ == '__main__':
    main()
