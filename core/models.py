"""Data models and shared formatting helpers for the MLB Discord bot.

Pure data/presentation code only — no HTTP. The API client lives in
core/mlb_client.py, which re-exports everything here for backwards
compatibility.
"""
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime

from core.utils import utc_to_et

__all__ = [
    "resolve_team_alias",
    "player_headshot_url",
    "extract_highlight_videos",
    "parse_hr_number",
    "format_table",
    "score_hitter_line",
    "score_pitcher_line",
    "aggregate_game_log_stats",
    "collect_team_performances",
    "build_player_info_line",
    "stat_groups_for",
    "Team",
    "Game",
    "Pitch",
    "AtBat",
    "ScoringPlay",
    "PlayerGameStats",
    "PaceData",
    "PlayerPercentiles",
    "StandingsGroup",
    "PitchArsenal",
    "SavantLeaderboard",
    "BatterVsPitcher",
    "HighlightItem",
    "PlayerSeasonStats",
    "CompareStats",
    "BoxScoreData",
    "parse_box_score_side",
    "PlayerGameLogData",
    "BullpenData",
    "Leader",
    "TEAM_ALIASES",
    "LEVEL_ABBREVS",
    "PERCENTILE_DISPLAY_NAMES",
    "BATTER_PERCENTILE_CATEGORIES",
    "PITCHER_PERCENTILE_CATEGORIES",
]


# Common team nicknames accepted anywhere a team query is typed
TEAM_ALIASES = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals",
                "dbacks": "diamondbacks", "barves": "braves"}

# MiLB sport names → short level tags
LEVEL_ABBREVS = {"Triple-A": "AAA", "Double-A": "AA", "High-A": "A+",
                 "Single-A": "A", "Rookie": "Rk", "Complex League": "CPX"}

# Savant percentile stat keys → compact display labels
PERCENTILE_DISPLAY_NAMES = {
    "exit_velocity_avg":        "Avg EV",
    "barrel_batted_rate":       "Barrel %",
    "hard_hit_percent":         "Hard-Hit %",
    "xwoba":                    "xwOBA",
    "xba":                      "xBA",
    "xslg":                     "xSLG",
    "sweet_spot_percent":       "Sweet-Spot %",
    "k_percent":                "K %",
    "bb_percent":               "BB %",
    "whiff_percent":            "Whiff %",
    "chase_percent":            "Chase %",
    "sprint_speed":             "Sprint",
    "oaa":                      "OAA",
    "framing":                  "Framing",
    "runner_run_value":         "BsR",
    "fielding_run_value":       "Fielding",
    "batting_run_value":        "Batting",
    "launch_angle_avg":         "Avg LA",
    "groundballs_percent":      "GB %",
    "xera":                     "xERA",
    "pitch_run_value_fastball": "Fastball",
    "pitch_run_value_breaking": "Breaking Ball",
    "pitch_run_value_offspeed": "Offspeed",
}

# Savant percentile section layouts
BATTER_PERCENTILE_CATEGORIES = [
    ("Run Value",            ["batting_run_value", "runner_run_value", "fielding_run_value"]),
    ("Batting",              ["xwoba", "xba", "xslg", "exit_velocity_avg", "barrel_batted_rate",
                              "hard_hit_percent", "sweet_spot_percent", "whiff_percent",
                              "chase_percent", "k_percent", "bb_percent"]),
    ("Fielding and Running", ["oaa", "framing", "sprint_speed"]),
]
PITCHER_PERCENTILE_CATEGORIES = [
    ("Pitch Values",     ["pitch_run_value_fastball", "pitch_run_value_breaking", "pitch_run_value_offspeed"]),
    ("Contact Quality",  ["barrel_batted_rate", "exit_velocity_avg", "launch_angle_avg", "groundballs_percent", "xwoba", "xera"]),
    ("Plate Discipline", ["k_percent", "bb_percent", "whiff_percent", "chase_percent"]),
]


def resolve_team_alias(query: str) -> str:
    q = query.lower()
    return TEAM_ALIASES.get(q, q)


def player_headshot_url(player_id) -> str:
    return f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}@3x.jpg"


def extract_highlight_videos(content_data: dict) -> dict:
    """Map playId guid → {'url', 'blurb'} for mp4 highlights in a /game/{pk}/content payload."""
    videos = {}
    if not isinstance(content_data, dict):
        return videos
    items = (((content_data.get('highlights') or {}).get('highlights') or {}).get('items')) or []
    for item in items:
        if 'guid' not in item:
            continue
        for pb in item.get('playbacks', []):
            if pb.get('name') == 'mp4Avc':
                videos[item['guid']] = {'url': pb['url'], 'blurb': item.get('headline', item.get('blurb', ''))}
                break
    return videos


def parse_hr_number(desc: str) -> int:
    """Extract the season HR count from a play description, e.g. '... homers (12) ...'."""
    for keyword in ('grand slam', 'home run', 'homers'):
        if keyword in desc:
            m = re.search(r'\((\d+)\)', desc[desc.index(keyword):])
            return int(m.group(1)) if m else 0
    return 0


def format_table(labels: list, rows: list, headers: dict, left_cols: set) -> str:
    """Fixed-width plain-text table. `headers` maps label → display name."""
    if not rows:
        return "No data available."
    widths = {}
    for label in labels:
        widths[label] = len(headers.get(label, str(label).upper()))
        for row in rows:
            widths[label] = max(widths[label], len(str(row.get(label, ''))))

    def fmt(label, val):
        return val.ljust(widths[label]) if label in left_cols else val.rjust(widths[label])

    lines = [' '.join(fmt(l, headers.get(l, str(l).upper())) for l in labels).rstrip()]
    for row in rows:
        lines.append(' '.join(fmt(l, str(row.get(l, ''))) for l in labels).rstrip())
    return '\n'.join(lines)


def score_hitter_line(b: dict):
    """Weighted fantasy-style score + summary for a boxscore batting dict.
    Returns (score, summary) or None if the player had no at-bats."""
    if not b or b.get("atBats", 0) == 0:
        return None
    ab      = b.get("atBats", 0)
    hits    = b.get("hits", 0)
    doubles = b.get("doubles", 0)
    triples = b.get("triples", 0)
    hr      = b.get("homeRuns", 0)
    singles = max(0, hits - doubles - triples - hr)
    rbi     = b.get("rbi", 0)
    runs    = b.get("runs", 0)
    bb      = b.get("baseOnBalls", 0)
    sb      = b.get("stolenBases", 0)

    score = hr*4 + triples*2 + doubles*1.5 + singles*0.5 + rbi*1 + runs*0.5 + bb*0.25 + sb*1

    parts = [f"{hits}-{ab}"]
    if hr:      parts.append(f"{hr} HR")
    if triples: parts.append(f"{triples} 3B")
    if doubles: parts.append(f"{doubles} 2B")
    if rbi:     parts.append(f"{rbi} RBI")
    if runs:    parts.append(f"{runs} R")
    if bb:      parts.append(f"{bb} BB")
    if sb:      parts.append(f"{sb} SB")
    return score, ", ".join(parts)


def score_pitcher_line(p: dict):
    """Bill James game score + summary for a boxscore pitching dict.
    Returns (game_score, outs, summary) or None if empty."""
    if not p:
        return None
    ip_str = str(p.get("inningsPitched", "0"))
    try:
        ip_parts = ip_str.split(".")
        outs = int(ip_parts[0]) * 3 + (int(ip_parts[1]) if len(ip_parts) > 1 else 0)
    except (ValueError, IndexError):
        outs = 0

    h  = p.get("hits", 0)
    er = p.get("earnedRuns", 0)
    ur = max(0, p.get("runs", 0) - er)
    bb = p.get("baseOnBalls", 0)
    k  = p.get("strikeOuts", 0)
    full_innings = outs // 3

    game_score = 50 + outs + k - 2*h - 4*er - 2*ur - bb + 2*max(0, full_innings - 4)
    return game_score, outs, f"{ip_str} IP, {er} ER, {k} K, {bb} BB"


def _outs_from_ip(ip_str) -> int:
    """Convert an MLB 'innings pitched' string (e.g. '5.2') to total outs recorded."""
    try:
        whole, _, thirds = str(ip_str).partition(".")
        return int(whole or 0) * 3 + int(thirds or 0)
    except ValueError:
        return 0


def _ip_from_outs(outs: int) -> str:
    return f"{outs // 3}.{outs % 3}"


def aggregate_game_log_stats(splits: List[dict], stat_type: str) -> dict:
    """Sum a list of gameLog 'stat' dicts (one per game) into a single combined
    stat dict, recomputing the rate stats that the API only provides per-game."""
    stats = [sp.get('stat', {}) for sp in splits]
    if not stats:
        return {}

    def total(key):
        return sum(s.get(key, 0) or 0 for s in stats)

    if stat_type == "hitting":
        ab = total('atBats')
        h = total('hits')
        bb = total('baseOnBalls')
        hbp = total('hitByPitch')
        sf = total('sacFlies')
        doubles = total('doubles')
        triples = total('triples')
        hr = total('homeRuns')
        singles = h - doubles - triples - hr
        total_bases = singles + doubles * 2 + triples * 3 + hr * 4
        obp_denom = ab + bb + hbp + sf
        avg = h / ab if ab else 0.0
        obp = (h + bb + hbp) / obp_denom if obp_denom else 0.0
        slg = total_bases / ab if ab else 0.0
        agg = {
            'gamesPlayed': total('gamesPlayed') or len(stats),
            'plateAppearances': total('plateAppearances'),
            'atBats': ab, 'runs': total('runs'), 'hits': h,
            'doubles': doubles, 'triples': triples, 'homeRuns': hr,
            'rbi': total('rbi'), 'baseOnBalls': bb, 'strikeOuts': total('strikeOuts'),
            'stolenBases': total('stolenBases'), 'caughtStealing': total('caughtStealing'),
            'intentionalWalks': total('intentionalWalks'), 'hitByPitch': hbp,
            'avg': f"{avg:.3f}".lstrip('0') if avg < 1 else f"{avg:.3f}",
            'obp': f"{obp:.3f}".lstrip('0') if obp < 1 else f"{obp:.3f}",
            'slg': f"{slg:.3f}".lstrip('0') if slg < 1 else f"{slg:.3f}",
            'ops': f"{(obp + slg):.3f}".lstrip('0') if (obp + slg) < 1 else f"{(obp + slg):.3f}",
        }
        return agg

    # pitching
    outs = sum(_outs_from_ip(s.get('inningsPitched', '0.0')) for s in stats)
    ip = outs / 3
    er = total('earnedRuns')
    h = total('hits')
    bb = total('baseOnBalls')
    k = total('strikeOuts')
    ab_against = total('atBats')
    hits_against = h
    era = (er * 9 / ip) if ip else 0.0
    whip = ((bb + h) / ip) if ip else 0.0
    k9 = (k * 9 / ip) if ip else 0.0
    bb9 = (bb * 9 / ip) if ip else 0.0
    kbb = (k / bb) if bb else float(k)
    avg_against = (hits_against / ab_against) if ab_against else 0.0
    return {
        'wins': total('wins'), 'losses': total('losses'),
        'gamesPlayed': total('gamesPitched') or len(stats),
        'gamesStarted': total('gamesStarted'),
        'completeGames': total('completeGames'), 'shutouts': total('shutouts'),
        'saveOpportunities': total('saveOpportunities'), 'saves': total('saves'),
        'holds': total('holds'),
        'inningsPitched': _ip_from_outs(outs),
        'hits': h, 'runs': total('runs'), 'earnedRuns': er, 'homeRuns': total('homeRuns'),
        'baseOnBalls': bb, 'strikeOuts': k,
        'era': f"{era:.2f}", 'whip': f"{whip:.2f}",
        'strikeoutsPer9Inn': f"{k9:.2f}", 'walksPer9Inn': f"{bb9:.2f}",
        'strikeoutWalkRatio': f"{kbb:.2f}",
        'avg': f"{avg_against:.3f}".lstrip('0') if avg_against < 1 else f"{avg_against:.3f}",
    }


def collect_team_performances(team_data: dict) -> tuple:
    """Score every batter and pitcher on one side of a boxscore.
    Returns (hitters, pitchers): hitters = [{'name', 'score', 'summary'}],
    pitchers = [{'name', 'score', 'outs', 'summary'}]."""
    players = team_data.get("players", {})
    hitters, pitchers = [], []
    for batter_id in team_data.get("batters", []):
        p_data = players.get(f"ID{batter_id}", {})
        scored = score_hitter_line(p_data.get("stats", {}).get("batting", {}))
        if scored:
            hitters.append({
                "name":    p_data.get("person", {}).get("fullName", "Unknown"),
                "score":   scored[0],
                "summary": scored[1],
            })
    for pitcher_id in team_data.get("pitchers", []):
        p_data = players.get(f"ID{pitcher_id}", {})
        scored = score_pitcher_line(p_data.get("stats", {}).get("pitching", {}))
        if scored:
            pitchers.append({
                "name":    p_data.get("person", {}).get("fullName", "Unknown"),
                "score":   scored[0],
                "outs":    scored[1],
                "summary": scored[2],
            })
    return hitters, pitchers


def build_player_info_line(person: dict, bt_label: bool = False) -> str:
    """Standard 'POS | B/T | height | weight | age' line for player embeds."""
    pos = person.get('primaryPosition', {}).get('abbreviation', '')
    age_str = ""
    try:
        b_dt = datetime.strptime(person.get('birthDate', '1900-01-01')[:10], "%Y-%m-%d")
        now = datetime.now()
        age = now.year - b_dt.year - ((now.month, now.day) < (b_dt.month, b_dt.day))
        age_str = f"Age: {age}"
    except ValueError:
        pass
    bt = f"{person.get('batSide', {}).get('code', '')}/{person.get('pitchHand', {}).get('code', '')}"
    label = "B/T: " if bt_label else ""
    line = f"{pos}  |  {label}{bt}  |  {person.get('height', '')}  |  {person.get('weight', '')} lbs  |  {age_str}"
    if person.get('nickName'):
        line += f"  |  \"{person['nickName']}\""
    return line


def stat_groups_for(pos: str, stat_type: Optional[str]) -> List[str]:
    """Which stat groups to fetch for a player: explicit override, both for two-way players,
    else pitching for pitchers / hitting for everyone else."""
    if stat_type:
        return [stat_type]
    if pos == "TWP":
        return ["hitting", "pitching"]
    return ["pitching"] if pos == "P" else ["hitting"]

def _bold_play_description(desc: str, play: dict) -> str:
    if not desc or not play:
        return desc
        
    names = set()
    matchup = play.get('matchup', {})
    if matchup.get('batter'): names.add(matchup['batter'].get('fullName'))
    if matchup.get('pitcher'): names.add(matchup['pitcher'].get('fullName'))
    
    for runner in play.get('runners', []):
        if runner.get('details', {}).get('runner'):
            names.add(runner['details']['runner'].get('fullName'))
            
    names = {n for n in names if n}
    for name in sorted(names, key=len, reverse=True):
        # Idempotent replacement to prevent double-bolding if we run this twice
        desc = desc.replace(f"**{name}**", name)
        desc = desc.replace(name, f"**{name}**")
        
    return desc

@dataclass
class Team:
    id: int
    name: str
    abbreviation: str
    score: int
    hits: int = 0
    errors: int = 0
    record: str = ""

@dataclass
class Game:
    game_pk: int
    status: str
    abstract_state: str
    away: Team
    home: Team
    inning: int = 0
    is_top_inning: bool = True
    outs: int = 0
    strikes: int = 0
    balls: int = 0
    bases: str = "---"
    pitcher: str = ""
    pitch_count: int = 0
    batter: str = ""
    lineup_pos_batter: str = ""
    on_deck: str = ""
    lineup_pos_on_deck: str = ""
    last_play_desc: str = ""
    last_play_pitcher: str = ""
    last_pitch_type: str = ""
    last_pitch_speed: float = 0.0
    statcast_dist: float = 0.0
    statcast_speed: float = 0.0
    statcast_angle: float = 0.0
    away_probable: str = ""
    home_probable: str = ""
    away_probable_stats: str = ""
    home_probable_stats: str = ""
    win_pitcher: str = ""
    loss_pitcher: str = ""
    save_pitcher: str = ""
    win_pitcher_note: str = ""
    loss_pitcher_note: str = ""
    save_pitcher_note: str = ""
    game_time_str: str = ""
    game_date_str: str = ""
    scoring_plays: List["ScoringPlay"] = None
    no_hitter: bool = False
    perfect_game: bool = False
    no_hitter_pitchers: List[dict] = None
    level: str = ""
    venue_name: str = ""
    venue_id: int = 0
    home_venue_id: int = 0
    venue_city: str = ""
    venue_state: str = ""

    def is_neutral_site(self) -> bool:
        """True when the game is at neither team's home park (e.g. Las Vegas).

        Requires the schedule to be hydrated with team venue info; if the home
        park is unknown we return False to avoid false positives.
        """
        return bool(self.venue_id and self.home_venue_id and self.venue_id != self.home_venue_id)

    def venue_label(self) -> str:
        """'Park Name — City, ST' when location is known, else just the park name."""
        if self.venue_city:
            loc = f"{self.venue_city}, {self.venue_state}" if self.venue_state else self.venue_city
            return f"{self.venue_name} — {loc}"
        return self.venue_name

    @classmethod
    def from_api_json(cls, data: dict):
        """Parses the raw MLB Stats API JSON into a clean Python object."""
        away_data = data['teams']['away']
        home_data = data['teams']['home']
        ls = data.get('linescore', {})
        
        away_record = f"({away_data.get('leagueRecord', {}).get('wins', 0)}-{away_data.get('leagueRecord', {}).get('losses', 0)})"
        home_record = f"({home_data.get('leagueRecord', {}).get('wins', 0)}-{home_data.get('leagueRecord', {}).get('losses', 0)})"
        
        away_team = Team(
            id=away_data['team']['id'],
            name=away_data['team']['name'],
            abbreviation=away_data['team'].get('abbreviation', away_data['team']['name'][:3].upper()),
            score=away_data.get('score', 0),
            hits=ls.get('teams', {}).get('away', {}).get('hits', 0),
            errors=ls.get('teams', {}).get('away', {}).get('errors', 0),
            record=away_record
        )
        
        home_team = Team(
            id=home_data['team']['id'],
            name=home_data['team']['name'],
            abbreviation=home_data['team'].get('abbreviation', home_data['team']['name'][:3].upper()),
            score=home_data.get('score', 0),
            hits=ls.get('teams', {}).get('home', {}).get('hits', 0),
            errors=ls.get('teams', {}).get('home', {}).get('errors', 0),
            record=home_record
        )
        
        game = cls(
            game_pk=data['gamePk'],
            status=data['status']['detailedState'],
            abstract_state=data['status']['abstractGameState'],
            away=away_team,
            home=home_team,
            inning=ls.get('currentInning', 0),
            is_top_inning=ls.get('isTopInning', True),
            outs=ls.get('outs', 0),
            strikes=ls.get('strikes', 0),
            balls=ls.get('balls', 0)
        )

        sport_name = home_data.get('team', {}).get('sport', {}).get('name', '')
        game.level = LEVEL_ABBREVS.get(sport_name, sport_name)

        game_venue = data.get('venue', {})
        game.venue_name = game_venue.get('name', '')
        game.venue_id = game_venue.get('id', 0)
        game.home_venue_id = home_data.get('team', {}).get('venue', {}).get('id', 0)
        venue_loc = game_venue.get('location', {})
        game.venue_city = venue_loc.get('city', '')
        game.venue_state = venue_loc.get('stateAbbrev', '')

        offense = ls.get('offense', {})
        defense = ls.get('defense', {})
        
        bases = "---"
        if 'first' in offense: bases = "1" + bases[1:]
        if 'second' in offense: bases = bases[:1] + "2" + bases[2:]
        if 'third' in offense: bases = bases[:2] + "3"
        game.bases = bases
        
        def _last_name(d: dict) -> str:
            if d.get('lastName'):
                return d['lastName']
            full = d.get('fullName', '')
            return full.split()[-1] if full else ''

        pitcher_data = defense.get('pitcher', {})
        game.pitcher = _last_name(pitcher_data)

        if 'stats' in pitcher_data:
            for st in pitcher_data['stats']:
                if st.get('type', {}).get('displayName') == 'gameLog' and st.get('group', {}).get('displayName') == 'pitching':
                    game.pitch_count = st.get('stats', {}).get('pitchesThrown', 0)
                    break

        batter_data = offense.get('batter', {})
        game.batter = _last_name(batter_data)
        on_deck_data = offense.get('onDeck', {})
        game.on_deck = _last_name(on_deck_data)
        
        def find_lineup_pos(player_id, lineups):
            if not lineups: return ""
            for _, players in lineups.items():
                for i, p in enumerate(players):
                    if p.get('id') == player_id:
                        return str(i + 1)
            return ""
            
        lineups = data.get('lineups', {})
        if game.batter:
            game.lineup_pos_batter = find_lineup_pos(batter_data.get('id'), lineups)
        if game.on_deck:
            game.lineup_pos_on_deck = find_lineup_pos(on_deck_data.get('id'), lineups)
            
        last_play = data.get('previousPlay', {})
        if last_play and 'result' in last_play:
            desc = last_play['result'].get('description', '')
            game.last_play_desc = _bold_play_description(desc, last_play)
            game.last_play_pitcher = last_play.get('matchup', {}).get('pitcher', {}).get('fullName', '')
            
            play_events = last_play.get('playEvents', [])
            for event in play_events:
                if 'pitchData' in event:
                    game.last_pitch_speed = event['pitchData'].get('startSpeed') or 0.0
                    if 'details' in event and 'type' in event['details']:
                        game.last_pitch_type = event['details']['type'].get('description', '')
                if 'hitData' in event:
                    hd = event['hitData']
                    game.statcast_dist = hd.get('totalDistance') or 0.0
                    game.statcast_speed = hd.get('launchSpeed') or 0.0
                    game.statcast_angle = hd.get('launchAngle') or 0.0
        
        game.away_probable = away_data.get('probablePitcher', {}).get('lastName', '')
        game.home_probable = home_data.get('probablePitcher', {}).get('lastName', '')
        
        if 'stats' in away_data.get('probablePitcher', {}):
            for st in away_data['probablePitcher']['stats']:
                if st.get('type', {}).get('displayName') == 'statsSingleSeason' and st.get('group', {}).get('displayName') == 'pitching':
                    s = st.get('stats', {})
                    game.away_probable_stats = f"({s.get('wins', 0)}-{s.get('losses', 0)}) {s.get('era', '-.--')}"
                    break
                    
        if 'stats' in home_data.get('probablePitcher', {}):
            for st in home_data['probablePitcher']['stats']:
                if st.get('type', {}).get('displayName') == 'statsSingleSeason' and st.get('group', {}).get('displayName') == 'pitching':
                    s = st.get('stats', {})
                    game.home_probable_stats = f"({s.get('wins', 0)}-{s.get('losses', 0)}) {s.get('era', '-.--')}"
                    break
        
        decisions = data.get('decisions', {})
        
        winner = decisions.get('winner', {})
        game.win_pitcher = winner.get('lastName', '')
        for st in winner.get('stats', []):
            if st.get('type', {}).get('displayName') == 'gameLog' and 'note' in st.get('stats', {}):
                game.win_pitcher_note = st['stats']['note']
            elif st.get('type', {}).get('displayName') == 'statsSingleSeason' and not game.win_pitcher_note:
                game.win_pitcher_note = f"(W, {st.get('stats', {}).get('wins', 0)}-{st.get('stats', {}).get('losses', 0)})"
                
        loser = decisions.get('loser', {})
        game.loss_pitcher = loser.get('lastName', '')
        for st in loser.get('stats', []):
            if st.get('type', {}).get('displayName') == 'gameLog' and 'note' in st.get('stats', {}):
                game.loss_pitcher_note = st['stats']['note']
            elif st.get('type', {}).get('displayName') == 'statsSingleSeason' and not game.loss_pitcher_note:
                game.loss_pitcher_note = f"(L, {st.get('stats', {}).get('wins', 0)}-{st.get('stats', {}).get('losses', 0)})"
                
        save = decisions.get('save', {})
        game.save_pitcher = save.get('lastName', '')
        for st in save.get('stats', []):
            if st.get('type', {}).get('displayName') == 'gameLog' and 'note' in st.get('stats', {}):
                game.save_pitcher_note = st['stats']['note']
            elif st.get('type', {}).get('displayName') == 'statsSingleSeason' and not game.save_pitcher_note:
                game.save_pitcher_note = f"(SV, {st.get('stats', {}).get('saves', 0)})"
        
        if 'gameDate' in data:
            try:
                dt = utc_to_et(datetime.strptime(data['gameDate'], "%Y-%m-%dT%H:%M:%SZ"))
                game.game_time_str = dt.strftime("%I:%M").lstrip('0') + " ET"
                fmt = "%A, %b %d, %Y" if dt.year != datetime.now().year else "%A, %b %d"
                game.game_date_str = dt.strftime(fmt).replace(" 0", " ")
            except ValueError:
                pass

        flags = data.get('flags', {})
        game.no_hitter = flags.get('noHitter', False)
        game.perfect_game = flags.get('perfectGame', False)

        return game

    def format_score_line(self) -> str:
        """A simple formatter to output the game score for Discord."""
        away_base = f"{self.away.abbreviation.ljust(3)} {str(self.away.score).rjust(2)} {str(self.away.hits).rjust(2)} {self.away.errors}"
        home_base = f"{self.home.abbreviation.ljust(3)} {str(self.home.score).rjust(2)} {str(self.home.hits).rjust(2)} {self.home.errors}"

        if self.abstract_state == "Live" and self.status not in ["Delayed", "Warmup"]:
            outs_str = (int(self.outs) * '●') + ((3 - int(self.outs)) * '○')
            inning_half_str = "▲" if self.is_top_inning else "▼"
            
            pitcher_str = f"P: {self.pitcher}"
            if self.pitch_count > 0:
                pitcher_str += f" ({self.pitch_count} P)"
                
            away_line = f"{away_base} | {inning_half_str} {self.inning} | {self.bases.center(5)} | {pitcher_str}"
            
            count_str = f"({self.balls}-{self.strikes})"
            batter_str = f"{self.lineup_pos_batter}: {self.batter}" if self.lineup_pos_batter else f"B: {self.batter}"
            on_deck_str = f"{self.lineup_pos_on_deck}: {self.on_deck}" if self.lineup_pos_on_deck else f"OD: {self.on_deck}"
            home_line = f"{home_base} | {outs_str} | {count_str.center(5)} | {batter_str} {on_deck_str}"
            
            output = f"{away_line}\n{home_line}"
            
            if self.no_hitter or self.perfect_game:
                alert = "P*RFECT GAME" if self.perfect_game else "NO H*TTER"
                side_name = self.home.name.upper() if self.away.hits == 0 else self.away.name.upper()
                output += f"\n\n##############################\n{side_name} THROWING A {alert}\n"
                if self.no_hitter_pitchers:
                    output += self._format_pitcher_table()
                output += "##############################"
            return output
        elif self.abstract_state == "Final":
            final_str = f"F/{self.inning}" if self.inning != 9 and self.inning > 0 else "F"
            
            away_p, home_p, sv_p = "", "", ""
            if self.win_pitcher:
                w_str = f"{self.win_pitcher} {self.win_pitcher_note}".strip()
                l_str = f"{self.loss_pitcher} {self.loss_pitcher_note}".strip()
                if self.save_pitcher:
                    sv_p = f"{self.save_pitcher} {self.save_pitcher_note}".strip()
                
                if self.away.score > self.home.score:
                    away_p = w_str
                    home_p = l_str
                elif self.home.score > self.away.score:
                    away_p = l_str
                    home_p = w_str
                    
            away_p_str = f" | {away_p}" if away_p else ""
            home_p_str = f" | {home_p}" if home_p else ""
            
            result = f"{away_base}  {self.away.record.center(7)} | {final_str.ljust(4)}{away_p_str}\n{home_base}  {self.home.record.center(7)} | {' ' * 4}{home_p_str}"
            if sv_p:
                spacer = " " * len(f"{home_base}  {self.home.record.center(7)}")
                result += f"\n{spacer} | {' ' * 4} | {sv_p}"

            if self.no_hitter or self.perfect_game:
                alert = "PERFECT GAME" if self.perfect_game else "NO HITTER"
                side_name = self.home.name.upper() if self.away.hits == 0 else self.away.name.upper()
                result += f"\n\n##############################\n{side_name} THREW A {alert}!\n"
                if self.no_hitter_pitchers:
                    result += self._format_pitcher_table()
                result += "##############################"
            return result
        else:
            time_str = self.game_time_str if self.status in ["Scheduled", "Pre-Game", "Warmup"] and self.game_time_str else self.status
            
            away_prob = f"{self.away_probable.ljust(10)} {self.away_probable_stats}".strip() if self.away_probable else ""
            home_prob = f"{self.home_probable.ljust(10)} {self.home_probable_stats}".strip() if self.home_probable else ""
            
            away_prob_str = f" | {away_prob}" if away_prob else ""
            home_prob_str = f" | {home_prob}" if home_prob else ""
            
            return f"{self.away.abbreviation.ljust(3)} {self.away.record.center(7)} | {time_str.ljust(9)}{away_prob_str}\n{self.home.abbreviation.ljust(3)} {self.home.record.center(7)} | {' ' * 9}{home_prob_str}"

    def format_last_play(self) -> str:
        """Format the last play description and statcast info as markdown (outside code block)."""
        if not self.last_play_desc or self.abstract_state != "Live":
            return ""
        output = f"**Last Play:** With **{self.last_play_pitcher}** pitching, {self.last_play_desc}\n\n"
        if self.last_pitch_type:
            output += f"**Pitch:** {self.last_pitch_type}, {self.last_pitch_speed:.2f} mph\n"
        if self.statcast_dist > 0 or self.statcast_speed > 0:
            output += f"**Statcast:** {self.statcast_dist:.0f} ft, {self.statcast_speed:.1f} mph, {self.statcast_angle:.0f}°\n"
        return output.rstrip()

    def _format_pitcher_table(self) -> str:
        """Format no-hitter pitcher details into a table matching the old bot's display."""
        if not self.no_hitter_pitchers:
            return ""
        labels = ['pitcher', 'ip', 'bb', 'so', 'np']
        header_map = {'pitcher': 'PITCHER', 'ip': 'IP', 'bb': 'BB', 'so': 'SO', 'np': 'NP'}
        return '\n' + format_table(labels, self.no_hitter_pitchers, header_map, {'pitcher'}) + '\n'

@dataclass
class Pitch:
    number: int
    count: str
    description: str
    speed: float
    type: str
    px: float
    pz: float
    sz_top: float
    sz_bot: float

@dataclass
class AtBat:
    inning: str
    pitcher_name: str
    description: str
    pitch_data: str
    statcast_data: str
    video_url: str
    video_blurb: str
    is_scoring: bool
    is_complete: bool
    pitches: List[Pitch] = None
    stand: str = "R" # 'L' or 'R'



@dataclass
class ScoringPlay:
    inning: str
    description: str
    video_url: str
    video_blurb: str
    pitcher_name: str = ""

@dataclass
class PlayerGameStats:
    player_id: str
    player_name: str
    team_abbrev: str
    opp_abbrev: str
    is_home: bool
    date: str
    batting_stats: Optional[dict] = None
    pitching_stats: Optional[dict] = None
    pitching_dec: str = ""
    info_message: str = ""
    headshot_url: str = ""
    at_bats: List[AtBat] = None

    def format_discord_code_block(self) -> str:
        if self.info_message:
            return self.info_message
            
        output = ""
        dec = self.pitching_dec if self.pitching_dec else ""

        if self.pitching_stats:
            s = self.pitching_stats
            ip = s.get('inningsPitched', '0.0')
            output += " IP  H  R ER HR BB SO  P-S\n"
            output += f"{ip} {s.get('hits', 0):2d} {s.get('runs', 0):2d} {s.get('earnedRuns', 0):2d} {s.get('homeRuns', 0):2d} {s.get('baseOnBalls', 0):2d} {s.get('strikeOuts', 0):2d} {s.get('pitchesThrown', 0):2d}-{s.get('strikes', 0)} {dec}\n\n"

        if self.batting_stats:
            s = self.batting_stats
            output += "AB H 2B 3B HR R RBI BB SO SB CS\n"
            output += f"{s.get('atBats', 0):2d} {s.get('hits', 0):1d} {s.get('doubles', 0):2d} {s.get('triples', 0):2d} {s.get('homeRuns', 0):2d} {s.get('runs', 0):1d} {s.get('rbi', 0):3d} {s.get('baseOnBalls', 0):2d} {s.get('strikeOuts', 0):2d} {s.get('stolenBases', 0):2d} {s.get('caughtStealing', 0):2d}\n\n"

        return output.strip('\n')

@dataclass
class PaceData:
    player_id: int
    player_name: str
    team_abbrev: str
    team_gp: int
    is_pitcher: bool
    current_stats: dict
    projected_stats: dict
    year: int
    player_url: str = ""

    def format_discord_code_block(self) -> str:
        output = ""
        if self.is_pitcher:
            # Row 1: G GS W L SV HLD IP SO BB
            h1 = "       G  GS   W   L  SV HLD    IP   SO  BB\n"
            c = self.current_stats
            p = self.projected_stats
            
            c1 = "CURR " + f"{c.get('gamesPitched',0):3d} {c.get('gamesStarted',0):3d} {c.get('wins',0):3d} {c.get('losses',0):3d} {c.get('saves',0):3d} {c.get('holds',0):3d} {str(c.get('inningsPitched','0.0')):5s} {c.get('strikeOuts',0):4d} {c.get('baseOnBalls',0):3d}\n"
            p1 = "PROJ " + f"{p.get('gamesPitched',0):3d} {p.get('gamesStarted',0):3d} {p.get('wins',0):3d} {p.get('losses',0):3d} {p.get('saves',0):3d} {p.get('holds',0):3d} {str(p.get('inningsPitched','0.0')):5s} {p.get('strikeOuts',0):4d} {p.get('baseOnBalls',0):3d}\n"
            
            # Row 2: H R ER HR ERA WHIP
            h2 = "       H   R  ER  HR    ERA    WHIP\n"
            c2 = "CURR " + f"{c.get('hits',0):3d} {c.get('runs',0):3d} {c.get('earnedRuns',0):3d} {c.get('homeRuns',0):3d} {c.get('era','-.--'):>7s} {c.get('whip','-.--'):>6s}\n"
            p2 = "PROJ " + f"{p.get('hits',0):3d} {p.get('runs',0):3d} {p.get('earnedRuns',0):3d} {p.get('homeRuns',0):3d} {p.get('era','-.--'):>7s} {p.get('whip','-.--'):>6s}"
            
            output = h1 + c1 + p1 + "\n" + h2 + c2 + p2
        else:
            # Row 1: G PA AB R H 2B 3B HR RBI BB SO
            h1 = "       G  PA  AB   R   H  2B  3B  HR RBI  BB  SO\n"
            c = self.current_stats
            p = self.projected_stats
            
            c1 = "CURR " + f"{c.get('gamesPlayed',0):3d} {c.get('plateAppearances',0):3d} {c.get('atBats',0):3d} {c.get('runs',0):3d} {c.get('hits',0):3d} {c.get('doubles',0):3d} {c.get('triples',0):3d} {c.get('homeRuns',0):3d} {c.get('rbi',0):3d} {c.get('baseOnBalls',0):3d} {c.get('strikeOuts',0):3d}\n"
            p1 = "PROJ " + f"{p.get('gamesPlayed',0):3d} {p.get('plateAppearances',0):3d} {p.get('atBats',0):3d} {p.get('runs',0):3d} {p.get('hits',0):3d} {p.get('doubles',0):3d} {p.get('triples',0):3d} {p.get('homeRuns',0):3d} {p.get('rbi',0):3d} {p.get('baseOnBalls',0):3d} {p.get('strikeOuts',0):3d}\n"
            
            # Row 2: SB CS IBB HBP AVG OBP SLG OPS
            h2 = "      SB  CS IBB HBP    AVG    OBP    SLG    OPS\n"
            c2 = "CURR " + f"{c.get('stolenBases',0):3d} {c.get('caughtStealing',0):3d} {c.get('intentionalWalks',0):3d} {c.get('hitByPitch',0):3d} {c.get('avg','-.--'):>6s} {c.get('obp','-.--'):>6s} {c.get('slg','-.--'):>6s} {c.get('ops','-.--'):>6s}\n"
            p2 = "PROJ " + f"{p.get('stolenBases',0):3d} {p.get('caughtStealing',0):3d} {p.get('intentionalWalks',0):3d} {p.get('hitByPitch',0):3d} {p.get('avg','-.--'):>6s} {p.get('obp','-.--'):>6s} {p.get('slg','-.--'):>6s} {p.get('ops','-.--'):>6s}"
            
            output = h1 + c1 + p1 + "\n" + h2 + c2 + p2
            
        return output

@dataclass
class PlayerPercentiles:
    player_name: str
    team_abbrev: str
    year: str
    stat_type: str
    percentiles: List[dict]
    player_id: Optional[str] = None
    def apply_to_embed(self, embed) -> None:
        if not self.percentiles:
            embed.description = "No percentiles found for this year."
            return

        def get_bar(val):
            filled = round(val / 10)
            return "█" * filled + "░" * (10 - filled)

        display_names = PERCENTILE_DISPLAY_NAMES
        category_list = BATTER_PERCENTILE_CATEGORIES if self.stat_type == "Batter" else PITCHER_PERCENTILE_CATEGORIES
        stat_lookup = {row['stat']: row for row in self.percentiles}
        assigned_stats = set()

        # collect all sections first so we can compute a shared name width
        sections = []
        for cat_name, targets in category_list:
            rows = []
            for stat_name in targets:
                if stat_name in stat_lookup:
                    row = stat_lookup[stat_name]
                    name = display_names.get(stat_name, stat_name.replace("_", " ").title())
                    rows.append((name, row['value'], row['raw']))
                    assigned_stats.add(stat_name)
            if rows:
                sections.append((cat_name, rows))

        other_rows = []
        for row in self.percentiles:
            if row['stat'] not in assigned_stats:
                stat_name = row['stat']
                name = display_names.get(stat_name, stat_name.replace("_", " ").title())
                other_rows.append((name, row['value'], row['raw']))
        if other_rows:
            sections.append(("Other", other_rows))

        all_rows = [r for _, rows in sections for r in rows]
        padding = max(len(r[0]) for r in all_rows) if all_rows else 0

        def build_section(rows):
            lines = []
            for name, val, raw in rows:
                lines.append(f"{name.rjust(padding)}  {get_bar(val)}  {val:>2}  ({raw})")
            return "```\n" + "\n".join(lines) + "\n```"

        for cat_name, rows in sections:
            embed.add_field(name=cat_name, value=build_section(rows), inline=False)

@dataclass
class StandingsGroup:
    title: str
    records: List[dict]

    def format_discord_code_block(self, is_wc: bool = False) -> str:
        lines = []
        if is_wc:
            h_team = "TEAM".ljust(11)
            h_w = "W".rjust(3)
            h_l = "L".rjust(3)
            h_pct = "PCT".rjust(5)
            h_wcgb = "WCGB".rjust(5)
            h_strk = "STRK".rjust(4)
            h_diff = "RunDiff".rjust(7)
            lines.append(f"{h_team} {h_w} {h_l} {h_pct} {h_wcgb}  {h_strk} {h_diff}")
        else:
            h_team = "TEAM".ljust(11)
            h_w = "W".rjust(3)
            h_l = "L".rjust(3)
            h_pct = "PCT".rjust(5)
            h_gb = "GB".rjust(6)
            h_wcgb = "WCGB".rjust(5)
            h_strk = "STRK".rjust(5)
            h_diff = "Diff".rjust(5)
            lines.append(f"{h_team} {h_w} {h_l} {h_pct} {h_gb} {h_wcgb} {h_strk} {h_diff}")
            
        for r in self.records:
            team = r['team'][:11].ljust(11)
            w = str(r['w']).rjust(3)
            l = str(r['l']).rjust(3)
            pct = r['pct'].lstrip("0").rjust(5)
            
            if is_wc:
                gb_val = r['wc_gb']
                gb = gb_val.rjust(5)
                strk = r['streak'].rjust(4)
                diff = str(r['diff']).rjust(7)
                lines.append(f"{team} {w} {l} {pct} {gb}  {strk} {diff}")
            else:
                gb_val = r['gb']
                gb = str(gb_val).rjust(6)
                wc_gb_val = r['wc_gb']
                wc_gb = str(wc_gb_val).rjust(5)
                strk = r['streak'].rjust(5)
                diff = str(r['diff']).rjust(5)
                lines.append(f"{team} {w} {l} {pct} {gb} {wc_gb} {strk} {diff}")
            
        return "\n".join(lines)



@dataclass
class PitchArsenal:
    player_name: str
    team: str
    year: str
    pitches: List[dict]

    def format_discord_code_block(self) -> str:
        if not self.pitches:
            return "No pitch arsenal data found."

        lines = []
        lines.append("PITCH        SPEED USE% WHIFF%  K%     BA   xBA")

        for p in self.pitches:
            name = p['name'][:12].ljust(12)
            usage = str(int(float(p['usage'] or 0))).rjust(3) + '%'
            whiff = str(int(float(p['whiff'] or 0))).rjust(5) + '%'
            k_pct = str(int(float(p['k_pct'] or 0))).rjust(2) + '%'
            speed = f"{float(p['avg_speed'] or 0):.1f}".rjust(5)
            ba = f"{float(p['ba'] or 0):.3f}".lstrip('0').rjust(5)
            xba = f"{float(p['xba'] or 0):.3f}".lstrip('0').rjust(5)
            lines.append(f"{name} {speed} {usage} {whiff} {k_pct}  {ba} {xba}")

        return "\n".join(lines)

@dataclass
class SavantLeaderboard:
    title: str
    stat_key: str
    year: str
    rows: List[dict]

    def format_discord_code_block(self) -> str:
        if not self.rows:
            return "No data found."
        lines = []
        lines.append("RK  PLAYER          TEAM  VALUE")
        for i, r in enumerate(self.rows, 1):
            rank = str(i).rjust(2)
            name = r['name'][:14].ljust(14)
            team = r.get('team', '').rjust(4)
            val = str(r['value']).rjust(6)
            lines.append(f"{rank}  {name} {team} {val}")
        return "\n".join(lines)


@dataclass
class BatterVsPitcher:
    batter_name: str
    pa: int
    ab: int
    h: int
    d: int
    t: int
    hr: int
    bb: int
    so: int
    avg: str
    ops: str

@dataclass
class HighlightItem:
    title: str
    description: str
    url: str
    duration: str
    date: str

@dataclass
class PlayerSeasonStats:
    player_name: str
    team_abbrev: str
    stat_type: str
    years: str
    is_career: bool
    info_line: str
    stats: List[dict]
    info_message: str = ""
    headshot_url: str = ""
    parent_org_abbrev: str = ""
    level_abbrev: str = ""
    birth_date: str = ""

    def format_discord_code_block(self) -> str:
        if self.info_message:
            return self.info_message

        if self.stat_type == "hitting":
            labels_list = [
                ['season', 'team', 'gamesPlayed', 'plateAppearances', 'atBats', 'runs', 'hits', 'doubles', 'triples', 'homeRuns'],
                ['season', 'team', 'rbi', 'baseOnBalls', 'strikeOuts', 'stolenBases', 'caughtStealing', 'intentionalWalks', 'hitByPitch'],
                ['season', 'team', 'avg', 'obp', 'slg', 'ops']
            ]
            repl = {'season':'YEAR', 'team':'TM', 'gamesPlayed':'G', 'plateAppearances':'PA', 'atBats':'AB', 'hits':'H', 'doubles':'2B', 'triples':'3B', 'homeRuns':'HR', 'runs':'R', 'rbi':'RBI', 'baseOnBalls':'BB', 'strikeOuts':'SO', 'stolenBases':'SB', 'caughtStealing':'CS', 'totalBases':'TB', 'intentionalWalks':'IBB', 'hitByPitch':'HBP', 'avg':'AVG', 'obp':'OBP', 'slg':'SLG', 'ops':'OPS'}
        else:
            labels_list = [
                ['season', 'team', 'wins', 'losses', 'gamesPlayed', 'gamesStarted', 'completeGames', 'shutouts', 'saveOpportunities', 'saves', 'holds'],
                ['season', 'team', 'inningsPitched', 'hits', 'runs', 'earnedRuns', 'homeRuns', 'baseOnBalls', 'strikeOuts', 'era', 'whip'],
                ['season', 'team', 'strikeoutsPer9Inn', 'walksPer9Inn', 'strikeoutWalkRatio', 'avg']
            ]
            repl = {'season':'YEAR', 'team':'TM', 'wins':'W', 'losses':'L', 'gamesPlayed':'G', 'gamesStarted':'GS', 'completeGames':'CG', 'shutouts':'SHO', 'saves':'SV', 'saveOpportunities':'SVO', 'holds':'HLD',
                    'gamesFinished':'GF', 'inningsPitched':'IP', 'strikeOuts':'SO', 'baseOnBalls':'BB', 'homeRuns':'HR', 'era':'ERA', 'whip':'WHIP', 'hits':'H', 'runs':'R', 'earnedRuns':'ER', 
                    'strikeoutsPer9Inn':'K/9', 'walksPer9Inn':'BB/9', 'strikeoutWalkRatio':'K/BB', 'avg':'AVG'}

        has_split_col = any('split' in s for s in self.stats)
        if has_split_col:
            for labels in labels_list:
                labels.insert(0, 'split')
            repl['split'] = ''

        if len(self.stats) == 1:
            for labels in labels_list:
                if 'season' in labels: labels.remove('season')
                if 'team' in labels: labels.remove('team')
        elif len(self.stats) > 1:
            all_seasons_same = all(s.get('season') == self.stats[0].get('season') for s in self.stats)
            for labels in labels_list:
                if all_seasons_same and 'season' in labels:
                    labels.remove('season')
                if has_split_col and 'team' in labels:
                    labels.remove('team')

        blocks = []
        for labels in labels_list:
            lines = [''] * (len(self.stats) + 1)
            for label in labels:
                display_label = repl.get(label, label.upper())
                width = len(display_label)
                for row in self.stats:
                    width = max(width, len(str(row.get(label, ""))))
                
                lines[0] += display_label.rjust(width) + " "
                for i, row in enumerate(self.stats):
                    lines[i+1] += str(row.get(label, "")).rjust(width) + " "
            # Use .strip('\n') to prevent Python from deleting the leading spaces on your headers!
            blocks.append("\n".join([line.rstrip() for line in lines]).strip('\n'))

        return "\n\n".join(blocks)

    def card_headline_and_grid(self) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        """Headline (big-number) and detail-grid (label, value) pairs for the most recent
        stat row, used by the /stats `image` baseball-card renderer."""
        if not self.stats:
            return [], []
        s = self.stats[-1]

        def fmt(key):
            v = s.get(key)
            return str(v) if v not in (None, "") else "—"

        if self.stat_type == "hitting":
            headline = [('AVG', fmt('avg')), ('HR', fmt('homeRuns')), ('RBI', fmt('rbi')), ('OPS', fmt('ops'))]
            grid_keys = [
                ('G', 'gamesPlayed'), ('PA', 'plateAppearances'), ('AB', 'atBats'), ('R', 'runs'),
                ('H', 'hits'), ('2B', 'doubles'), ('3B', 'triples'), ('BB', 'baseOnBalls'),
                ('SO', 'strikeOuts'), ('SB', 'stolenBases'), ('CS', 'caughtStealing'),
                ('IBB', 'intentionalWalks'), ('HBP', 'hitByPitch'), ('OBP', 'obp'), ('SLG', 'slg'),
            ]
        else:
            headline = [('W-L', f"{s.get('wins', '—')}-{s.get('losses', '—')}"), ('ERA', fmt('era')),
                        ('SO', fmt('strikeOuts')), ('WHIP', fmt('whip'))]
            grid_keys = [
                ('G', 'gamesPlayed'), ('GS', 'gamesStarted'), ('CG', 'completeGames'), ('SHO', 'shutouts'),
                ('SVO', 'saveOpportunities'), ('SV', 'saves'), ('HLD', 'holds'), ('IP', 'inningsPitched'),
                ('H', 'hits'), ('R', 'runs'), ('ER', 'earnedRuns'), ('HR', 'homeRuns'), ('BB', 'baseOnBalls'),
                ('K/9', 'strikeoutsPer9Inn'), ('BB/9', 'walksPer9Inn'), ('K/BB', 'strikeoutWalkRatio'), ('AVG', 'avg'),
            ]

        grid = [(label, fmt(key)) for label, key in grid_keys]
        return headline, grid

    def card_multi_rows(self) -> Optional[List[Tuple[str, ...]]]:
        """One row per season for the four headline stats, used when the card covers more
        than one season (e.g. a year range). Returns None for a single-season card."""
        if len(self.stats) <= 1:
            return None

        headline_keys = ('avg', 'homeRuns', 'rbi', 'ops') if self.stat_type == "hitting" else None

        def fmt(row, key):
            v = row.get(key)
            return str(v) if v not in (None, "") else "—"

        rows = []
        for row in self.stats:
            if self.stat_type == "hitting":
                vals = tuple(fmt(row, k) for k in headline_keys)
            else:
                vals = (f"{row.get('wins', '—')}-{row.get('losses', '—')}", fmt(row, 'era'), fmt(row, 'strikeOuts'), fmt(row, 'whip'))
            rows.append((str(row.get('season', '')), str(row.get('team', '')), *vals))
        return rows

# (stat key, display label, direction): direction is 1 if a higher value is
# better, -1 if lower is better, 0 if the stat isn't a head-to-head comparison
# (e.g. counting stats like games played).
_HITTING_STAT_DEFS = [
    ('gamesPlayed', 'G', 0), ('atBats', 'AB', 0),
    ('hits', 'H', 1), ('doubles', '2B', 1), ('triples', '3B', 1), ('homeRuns', 'HR', 1),
    ('runs', 'R', 1), ('rbi', 'RBI', 1), ('baseOnBalls', 'BB', 1), ('strikeOuts', 'SO', -1),
    ('stolenBases', 'SB', 1), ('caughtStealing', 'CS', -1),
    ('avg', 'AVG', 1), ('obp', 'OBP', 1), ('slg', 'SLG', 1), ('ops', 'OPS', 1),
]
_PITCHING_STAT_DEFS = [
    ('wins', 'W', 1), ('losses', 'L', -1),
    ('gamesPlayed', 'G', 0), ('gamesStarted', 'GS', 0),
    ('completeGames', 'CG', 1), ('shutouts', 'SHO', 1),
    ('saveOpportunities', 'SVO', 0), ('saves', 'SV', 1), ('holds', 'HLD', 1),
    ('inningsPitched', 'IP', 0),
    ('hits', 'H', -1), ('runs', 'R', -1), ('earnedRuns', 'ER', -1), ('homeRuns', 'HR', -1),
    ('baseOnBalls', 'BB', -1), ('strikeOuts', 'SO', 1),
    ('era', 'ERA', -1), ('whip', 'WHIP', -1),
    ('strikeoutsPer9Inn', 'K/9', 1), ('walksPer9Inn', 'BB/9', -1),
    ('strikeoutWalkRatio', 'K/BB', 1), ('avg', 'AVG', -1),
]

@dataclass
class CompareStats:
    title: str
    stat_type: str
    rows: List[dict]
    errors: List[str] = None

    def image_rows(self) -> list:
        """Flat (label, v1, v2, direction) rows for a 2-player image comparison."""
        if len(self.rows) != 2:
            return []
        defs = _HITTING_STAT_DEFS if self.stat_type == "hitting" else _PITCHING_STAT_DEFS
        r1, r2 = self.rows
        out = []
        for key, label, direction in defs:
            v1, v2 = r1.get(key), r2.get(key)
            if v1 is None and v2 is None:
                continue
            out.append((label, v1, v2, direction))
        return out

    def format_discord_code_block(self) -> str:
        if not self.rows:
            return "No stats to compare."

        if self.stat_type == "hitting":
            labels_list = [
                ['name', 'team', 'gamesPlayed', 'atBats', 'hits', 'doubles', 'triples', 'homeRuns', 'runs', 'rbi', 'baseOnBalls', 'strikeOuts'],
                ['name', 'team', 'stolenBases', 'caughtStealing', 'avg', 'obp', 'slg', 'ops']
            ]
            repl = {'name':'NAME', 'team':'TM', 'gamesPlayed':'G', 'atBats':'AB', 'hits':'H', 'doubles':'2B', 'triples':'3B', 'homeRuns':'HR', 'runs':'R', 'rbi':'RBI', 'baseOnBalls':'BB', 'strikeOuts':'SO', 'stolenBases':'SB', 'caughtStealing':'CS', 'avg':'AVG', 'obp':'OBP', 'slg':'SLG', 'ops':'OPS'}
            left_justify = {'name', 'team'}
        else:
            labels_list = [
                ['name', 'team', 'wins', 'losses', 'gamesPlayed', 'gamesStarted', 'completeGames', 'shutouts', 'saveOpportunities', 'saves', 'holds'],
                ['name', 'team', 'inningsPitched', 'hits', 'runs', 'earnedRuns', 'homeRuns', 'baseOnBalls', 'strikeOuts', 'era', 'whip'],
                ['name', 'team', 'strikeoutsPer9Inn', 'walksPer9Inn', 'strikeoutWalkRatio', 'avg']
            ]
            repl = {'name':'NAME', 'team':'TM', 'wins':'W', 'losses':'L', 'gamesPlayed':'G', 'gamesStarted':'GS', 'completeGames':'CG', 'shutouts':'SHO', 'saveOpportunities':'SVO', 'saves':'SV', 'holds':'HLD',
                    'inningsPitched':'IP', 'strikeOuts':'SO', 'baseOnBalls':'BB', 'homeRuns':'HR', 'era':'ERA', 'whip':'WHIP', 'hits':'H', 'runs':'R', 'earnedRuns':'ER',
                    'strikeoutsPer9Inn':'K/9', 'walksPer9Inn':'BB/9', 'strikeoutWalkRatio':'K/BB', 'avg':'AVG'}
            left_justify = {'name', 'team'}

        blocks = []
        for labels in labels_list:
            lines = [''] * (len(self.rows) + 1)
            for label in labels:
                display_label = repl.get(label, label.upper())
                width = len(display_label)
                for row in self.rows:
                    width = max(width, len(str(row.get(label, ""))))

                if label in left_justify:
                    lines[0] += display_label.ljust(width) + " "
                    for i, row in enumerate(self.rows):
                        lines[i+1] += str(row.get(label, "")).ljust(width) + " "
                else:
                    lines[0] += display_label.rjust(width) + " "
                    for i, row in enumerate(self.rows):
                        lines[i+1] += str(row.get(label, "")).rjust(width) + " "

            blocks.append("\n".join([line.rstrip() for line in lines]).strip('\n'))

        return "\n\n".join(blocks)

@dataclass
class BoxScoreData:
    title: str
    team_name: str
    team_abbrev: str
    batting_rows: List[dict]
    pitching_rows: List[dict]
    pitching_notes: List[str] = None
    team_notes: List[dict] = None
    game_info: List[dict] = None
    abs_info: List[dict] = None
    bench_rows: List[dict] = None
    game_status: str = ""
    game_abstract_state: str = ""
    game_date: str = ""

    def format_batting(self) -> str:
        labels = ['name', 'pos', 'ab', 'r', 'h', 'rbi', 'bb', 'so', 'lob', 'avg', 'obp', 'slg']
        repl = {'name': '', 'pos': '', 'ab': 'AB', 'r': 'R', 'h': 'H', 'rbi': 'RBI', 'bb': 'BB', 'so': 'SO', 'lob': 'LOB', 'avg': 'AVG', 'obp': 'OBP', 'slg': 'SLG'}
        return format_table(labels, self.batting_rows, repl, {'name', 'pos'})

    def format_bench(self) -> str:
        """Season lines for position players who haven't entered the game yet."""
        if not self.bench_rows:
            return ""
        labels = ['name', 'pos', 'bat', 'ab', 'hr', 'avg', 'obp', 'ops']
        repl = {'name': '', 'pos': '', 'bat': 'B', 'ab': 'AB', 'hr': 'HR', 'avg': 'AVG', 'obp': 'OBP', 'ops': 'OPS'}
        return format_table(labels, self.bench_rows, repl, {'name', 'pos', 'bat'})

    def format_pitching(self) -> str:
        labels = ['name', 'ip', 'h', 'r', 'er', 'bb', 'so', 'hr', 'era', 'p', 's', 'dec']
        repl = {'name': '', 'ip': 'IP', 'h': 'H', 'r': 'R', 'er': 'ER', 'bb': 'BB', 'so': 'SO', 'hr': 'HR', 'era': 'ERA', 'p': 'P', 's': 'S', 'dec': 'DEC'}
        return format_table(labels, self.pitching_rows, repl, {'name', 'dec'})

    def format_notes(self) -> str:
        """Format team batting/fielding/baserunning notes."""
        if not self.team_notes:
            return ""
        output = ""
        for section in self.team_notes:
            output += f"\n**{section.get('title', '')}:**\n"
            for field in section.get('fieldList', []):
                output += f"**{field.get('label', '')}:** {field.get('value', '')}\n"
        return output.strip()

    def format_game_info(self) -> str:
        """Format game info (venue, umpires, weather, etc.)."""
        if not self.game_info:
            return ""
        output = "\n**Game Info:**\n"
        for info in self.game_info:
            label = info.get('label', '')
            value = info.get('value', '')
            if value:
                output += f"**{label}:** {value}\n"
            else:
                output += f"**{label}**\n"
        return output.strip()

    def format_abs_info(self) -> str:
        """Format ABS Challenges info."""
        if not self.abs_info:
            return ""
        output = ""
        for info in self.abs_info:
            label = info.get('label', '')
            value = info.get('value', '')
            if value:
                output += f"**{label}:** {value}\n"
            else:
                output += f"**{label}**\n"
        return output.strip()

def parse_box_score_side(box_data: dict, side: str) -> "BoxScoreData":
    """Parse one team's side of a /game/{pk}/boxscore payload into a BoxScoreData.
    Game status/date fields are left blank for the caller to fill in."""
    team_data = box_data.get('teams', {}).get(side, {})
    players = team_data.get('players', {})
    team_name = team_data.get('team', {}).get('name', '')
    team_abbrev = team_data.get('team', {}).get('abbreviation', '')

    def batting_row(p_data: dict, b_stats: dict, indent: bool) -> dict:
        season = p_data.get('seasonStats', {}).get('batting', {})
        name = p_data.get('person', {}).get('boxscoreName', 'Unknown')
        positions = p_data.get('allPositions', [])
        pos = "-".join(p.get('abbreviation', '') for p in positions) if positions else p_data.get('position', {}).get('abbreviation', '')
        return {
            'name': (" " if indent else "") + name,  # indent substitutes
            'pos': pos,
            'ab': str(b_stats.get('atBats', 0)),
            'r': str(b_stats.get('runs', 0)),
            'h': str(b_stats.get('hits', 0)),
            'rbi': str(b_stats.get('rbi', 0)),
            'bb': str(b_stats.get('baseOnBalls', 0)),
            'so': str(b_stats.get('strikeOuts', 0)),
            'lob': str(b_stats.get('leftOnBase', 0)),
            'avg': season.get('avg', '.000'),
            'obp': season.get('obp', '.000'),
            'slg': season.get('slg', '.000'),
            'is_starter': not indent,
        }

    # ones digit of battingOrder == '0' → original starter; anything else → substitute
    all_batters = team_data.get('batters', [])
    starter_ids = {bid for bid in all_batters
                   if str(players.get(f'ID{bid}', {}).get('battingOrder', '')).endswith('0')}

    batting_rows = []
    order_pos = 0  # tracks position in the lineup (1-9)
    for idx, batter_id in enumerate(all_batters):
        p_data = players.get(f'ID{batter_id}', {})
        b_stats = p_data.get('stats', {}).get('batting', {})
        if not b_stats:
            continue
        is_starter = batter_id in starter_ids
        if is_starter:
            order_pos += 1
        batting_rows.append(batting_row(p_data, b_stats, indent=not is_starter))
        # After the 9th starter, append the trailing substitutes then stop
        if is_starter and order_pos >= 9:
            for rem_id in all_batters[idx + 1:]:
                if rem_id in starter_ids:
                    break
                rem_data = players.get(f'ID{rem_id}', {})
                rem_stats = rem_data.get('stats', {}).get('batting', {})
                if not rem_stats:
                    continue
                batting_rows.append(batting_row(rem_data, rem_stats, indent=True))
            break

    pitching_rows = []
    for pitcher_id in team_data.get('pitchers', []):
        p_data = players.get(f'ID{pitcher_id}', {})
        p_stats = p_data.get('stats', {}).get('pitching', {})
        if not p_stats:
            continue
        season = p_data.get('seasonStats', {}).get('pitching', {})
        pitching_rows.append({
            'name': p_data.get('person', {}).get('boxscoreName', 'Unknown'),
            'ip': str(p_stats.get('inningsPitched', '0')),
            'h': str(p_stats.get('hits', 0)),
            'r': str(p_stats.get('runs', 0)),
            'er': str(p_stats.get('earnedRuns', 0)),
            'bb': str(p_stats.get('baseOnBalls', 0)),
            'so': str(p_stats.get('strikeOuts', 0)),
            'hr': str(p_stats.get('homeRuns', 0)),
            'era': season.get('era', '-.--'),
            'p': str(p_stats.get('pitchesThrown', 0)),
            's': str(p_stats.get('strikes', 0)),
            'dec': p_stats.get('note', ''),
        })

    # Bench: position players still available (not yet in the game), with their
    # season line so you can see who's left to pinch hit.
    bench_rows = []
    for bench_id in team_data.get('bench', []):
        p_data = players.get(f'ID{bench_id}', {})
        season = p_data.get('seasonStats', {}).get('batting', {})
        bench_rows.append({
            'id': bench_id,
            'name': p_data.get('person', {}).get('boxscoreName', 'Unknown'),
            'pos': p_data.get('position', {}).get('abbreviation', ''),
            'bat': '',  # batting hand (L/R/S) — filled in by the client
            'ab': str(season.get('atBats', 0)),
            'hr': str(season.get('homeRuns', 0)),
            'avg': season.get('avg', '.000'),
            'obp': season.get('obp', '.000'),
            'ops': season.get('ops', '.000'),
        })

    game_info, abs_info = [], []
    for info in box_data.get('info', []):
        label = info.get('label', '').upper()
        (abs_info if 'ABS' in label or 'CHALLENGE' in label else game_info).append(info)

    opp_side = 'away' if side == 'home' else 'home'
    opp_abbrev = box_data.get('teams', {}).get(opp_side, {}).get('team', {}).get('abbreviation', '??')
    title = f"{opp_abbrev} @ {team_abbrev}" if side == 'home' else f"{team_abbrev} @ {opp_abbrev}"

    return BoxScoreData(
        title=title,
        team_name=team_name,
        team_abbrev=team_abbrev,
        batting_rows=batting_rows,
        pitching_rows=pitching_rows,
        pitching_notes=box_data.get('pitchingNotes', []),
        team_notes=team_data.get('info', []),
        game_info=game_info,
        abs_info=abs_info,
        bench_rows=bench_rows,
    )


@dataclass
class PlayerGameLogData:
    player_id: str
    player_name: str
    team_abbrev: str
    headshot_url: str
    position_type: str  # 'hitting' or 'pitching'
    rows: List[dict]

    def format_log(self) -> str:
        if self.position_type == 'pitching':
            labels = ['date', 'tm', 'opp', 'ip', 'h', 'r', 'er', 'bb', 'so', 'hr', 'p', 's', 'dec']
            repl = {'date': 'DATE', 'tm': 'TM', 'opp': 'OPP', 'ip': 'IP', 'h': 'H', 'r': 'R', 'er': 'ER', 'bb': 'BB', 'so': 'SO', 'hr': 'HR', 'p': 'P', 's': 'S', 'dec': 'DEC'}
            left_cols = {'date', 'tm', 'opp', 'dec'}
        else:
            labels = ['date', 'tm', 'opp', 'ab', 'r', 'h', '2b', '3b', 'hr', 'rbi', 'bb', 'so', 'lob', 'avg', 'obp', 'slg', 'ops']
            repl = {'date': 'DATE', 'tm': 'TM', 'opp': 'OPP', 'ab': 'AB', 'r': 'R', 'h': 'H', '2b': '2B', '3b': '3B', 'hr': 'HR', 'rbi': 'RBI', 'bb': 'BB', 'so': 'SO', 'lob': 'LOB', 'avg': 'AVG', 'obp': 'OBP', 'slg': 'SLG', 'ops': 'OPS'}
            left_cols = {'date', 'tm', 'opp'}

        if len({row.get('tm') for row in self.rows}) <= 1:
            labels = [l for l in labels if l != 'tm']

        return format_table(labels, self.rows, repl, left_cols)


@dataclass
class BullpenData:
    team_name: str
    team_abbrev: str
    past_dates: List[str]
    bullpen: List[dict]
    starters: List[dict]

    def _get_status(self, row: dict) -> str:
        """Determines freshness status based on recent usage."""
        # past_dates are ordered oldest to newest: e.g. [4/8, 4/9, 4/10, 4/11]
        counts = []
        for pd in self.past_dates:
            val = row.get(pd, '')
            counts.append(int(val) if val and val.isdigit() else 0)
            
        # Analyze last 3 days (indices -1, -2, -3)
        yest = counts[-1]
        day_before = counts[-2]
        day_3 = counts[-3]
        total_3 = yest + day_before + day_3

        # 3 in a row
        if yest > 0 and day_before > 0 and day_3 > 0:
            return "💀"
        # Back to back OR Heavy yesterday
        if (yest > 0 and day_before > 0) or yest > 28:
            return "🔴"
        # Moderate usage
        if yest > 15 or total_3 > 40:
            return "🟡"
        # Fresh
        return "🟢"

    def format_table(self) -> str:
        labels = ['status', 'name', 't', 'era'] + self.past_dates
        repl = {'status': 'S', 'name': 'NAME', 't': 'T', 'era': 'ERA'}
        for pd in self.past_dates:
            repl[pd] = pd
            
        all_rows = self.bullpen + self.starters
        # Attach status to data
        for r in all_rows:
            r['status'] = self._get_status(r)
            
        # Group by status (Fresh -> Used -> Tired -> Gassed -> Bad)
        status_order = {"🟢": 0, "🟡": 1, "🔴": 2, "💀": 3}
        self.bullpen.sort(key=lambda x: status_order.get(x.get('status', ''), 99))
        self.starters.sort(key=lambda x: status_order.get(x.get('status', ''), 99))


        left_cols = {'name'}
        widths = {}
        for label in labels:
            widths[label] = len(repl.get(label, str(label)))
            for row in all_rows:
                val = str(row.get(label, ''))
                # Handle emoji width manually - they are usually wide
                actual_len = 2 if label == 'status' else len(val)
                widths[label] = max(widths[label], actual_len)
                
        header = ''
        for label in labels:
            display = repl.get(label, str(label))
            if label in left_cols:
                header += display.ljust(widths[label]) + ' '
            else:
                header += display.rjust(widths[label]) + ' '
                
        output = [header.rstrip()]
        
        # Helper to format a row
        def format_row(r):
            line = ''
            for label in labels:
                val = str(r.get(label, ''))
                if label in left_cols:
                    line += val.ljust(widths[label]) + ' '
                elif label == 'status':
                    # Status is special due to emoji
                    line += val + ' ' 
                else:
                    line += val.rjust(widths[label]) + ' '
            return line.rstrip()

        output.append("-" * len(header))
        for row in self.bullpen:
            output.append(format_row(row))
            
        if self.starters:
            output.append("\nPROBABLE / RECENT STARTERS")
            output.append("-" * len(header))
            for row in self.starters:
                output.append(format_row(row))

        legend = "\nLegend: 🟢 Fresh | 🟡 Used | 🔴 Tired | 💀 Gassed"
        output.append(legend)

        if not self.bullpen and not self.starters:
            return "No bullpen data found."
            
        return "\n".join(output)

@dataclass
class Leader:
    rank: int
    name: str
    team_abbrev: str
    value: str
    games_played: str = ""
    innings_pitched: str = ""
    plate_appearances: str = ""
    stat_group: str = "hitting"

    def format(self, max_name_len=18, is_team=False) -> str:
        if is_team:
            return f"{self.name:<{max_name_len + 8}} {self.value}"
        extra = self.innings_pitched if self.stat_group == "pitching" else self.plate_appearances
        return f"{self.team_abbrev:<4} {self.name:<{max_name_len}} {self.games_played:>3} {extra:>5} {self.value}"

    @staticmethod
    def header(max_name_len=18, stat_group="hitting", stat_label="STAT") -> str:
        extra_label = "IP" if stat_group == "pitching" else "PA"
        return f"{'TM':<4} {'NAME':<{max_name_len}} {'GP':>3} {extra_label:>5} {stat_label}"
