import aiohttp
import asyncio
import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta

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

        offense = ls.get('offense', {})
        defense = ls.get('defense', {})
        
        bases = "---"
        if 'first' in offense: bases = "1" + bases[1:]
        if 'second' in offense: bases = bases[:1] + "2" + bases[2:]
        if 'third' in offense: bases = bases[:2] + "3"
        game.bases = bases
        
        pitcher_data = defense.get('pitcher', {})
        game.pitcher = pitcher_data.get('lastName', '')
        
        if 'stats' in pitcher_data:
            for st in pitcher_data['stats']:
                if st.get('type', {}).get('displayName') == 'gameLog' and st.get('group', {}).get('displayName') == 'pitching':
                    game.pitch_count = st.get('stats', {}).get('pitchesThrown', 0)
                    break
        
        batter_data = offense.get('batter', {})
        game.batter = batter_data.get('lastName', '')
        on_deck_data = offense.get('onDeck', {})
        game.on_deck = on_deck_data.get('lastName', '')
        
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
                dt = datetime.strptime(data['gameDate'], "%Y-%m-%dT%H:%M:%SZ")
                dt = dt - timedelta(hours=4)  # ET offset for baseball season
                game.game_time_str = dt.strftime("%I:%M").lstrip('0') + " ET"
                game.game_date_str = dt.strftime("%A, %b %d").replace(" 0", " ")
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
        left_cols = {'pitcher'}

        widths = {}
        for label in labels:
            widths[label] = len(header_map[label])
            for row in self.no_hitter_pitchers:
                widths[label] = max(widths[label], len(str(row.get(label, ''))))

        header = ''
        for label in labels:
            display = header_map[label]
            if label in left_cols:
                header += display.ljust(widths[label]) + ' '
            else:
                header += display.rjust(widths[label]) + ' '

        lines = ['\n' + header.rstrip()]
        for row in self.no_hitter_pitchers:
            line = ''
            for label in labels:
                val = str(row.get(label, ''))
                if label in left_cols:
                    line += val.ljust(widths[label]) + ' '
                else:
                    line += val.rjust(widths[label]) + ' '
            lines.append(line.rstrip())

        return '\n'.join(lines) + '\n'

    def format_modern_score_line(self) -> str:
        """A modern Discord markdown formatter for the game score."""
        away_line = f"**{self.away.abbreviation} {self.away.score}** ({self.away.hits}H, {self.away.errors}E)"
        home_line = f"**{self.home.abbreviation} {self.home.score}** ({self.home.hits}H, {self.home.errors}E)"
        
        if self.abstract_state == "Live" and self.status not in ["Delayed", "Warmup"]:
            outs_str = (int(self.outs) * '●') + ((3 - int(self.outs)) * '○')
            inning_half_str = "▲" if self.is_top_inning else "▼"
            
            bases_emojis = ""
            bases_emojis += "⚾" if self.bases[0] == "1" else "♢"
            bases_emojis += "⚾" if self.bases[1] == "2" else "♢"
            bases_emojis += "⚾" if self.bases[2] == "3" else "♢"
            
            pitcher_str = f"**P:** {self.pitcher}" + (f" ({self.pitch_count}P)" if self.pitch_count > 0 else "")
            batter_str = f"**B:** {self.batter}"
            
            output = f"{away_line} @ {home_line}\n"
            output += f"**{inning_half_str} {self.inning}** | **Outs:** {outs_str} | **Count:** {self.balls}-{self.strikes} | **Bases:** {bases_emojis}\n"
            output += f"{pitcher_str} | {batter_str}\n"
            
            if self.last_play_desc:
                output += f"*{self.last_play_desc}*\n"
                
                pitch_info = []
                if self.last_pitch_type:
                    pitch_info.append(f"{self.last_pitch_type} ({self.last_pitch_speed:.1f} mph)")
                if self.statcast_dist > 0 or self.statcast_speed > 0:
                    pitch_info.append(f"{self.statcast_dist:.1f}ft / {self.statcast_speed:.1f}mph / {self.statcast_angle:.1f}°")
                
                if pitch_info:
                    output += f"> {' | '.join(pitch_info)}"
            return output
        elif self.abstract_state == "Final":
            final_str = f"Final/{self.inning}" if self.inning != 9 and self.inning > 0 else "Final"
            return f"{away_line} @ {home_line} | **{final_str}**"
        else:
            return f"{away_line} @ {home_line} | **{self.status}**"

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
    def apply_to_embed(self, embed) -> None:
        if not self.percentiles:
            embed.description = "No percentiles found for this year."
            return

        def get_bar(val):
            filled = round(val / 10)
            return "█" * filled + "░" * (10 - filled)

        display_names = {
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
            "sprint_speed":             "Sprint Speed",
            "oaa":                      "Range (OAA)",
            "framing":                  "Framing",
            "runner_run_value":         "Baserunning",
            "fielding_run_value":       "Fielding",
            "batting_run_value":        "Batting",
            "launch_angle_avg":         "Avg LA",
            "groundballs_percent":      "GB %",
            "xera":                     "xERA",
            "pitch_run_value_fastball": "Fastball",
            "pitch_run_value_breaking": "Breaking Ball",
            "pitch_run_value_offspeed": "Offspeed",
        }

        batter_categories = [
            ("Run Value",    ["batting_run_value", "runner_run_value", "fielding_run_value"]),
            ("Batting",  ["xwoba", "xba", "xslg", "exit_velocity_avg", "barrel_batted_rate",
                          "hard_hit_percent", "sweet_spot_percent", "whiff_percent",
                          "chase_percent", "k_percent", "bb_percent"]),
            ("Fielding and Running", ["oaa", "framing", "sprint_speed"]),
        ]

        pitcher_categories = [
            ("Pitch Values",     ["pitch_run_value_fastball", "pitch_run_value_breaking", "pitch_run_value_offspeed"]),
            ("Contact Quality",  ["barrel_batted_rate", "exit_velocity_avg", "launch_angle_avg", "groundballs_percent", "xwoba", "xera"]),
            ("Plate Discipline", ["k_percent", "bb_percent", "whiff_percent", "chase_percent"]),
        ]

        category_list = batter_categories if self.stat_type == "Batter" else pitcher_categories
        stat_lookup = {row['stat']: row for row in self.percentiles}
        assigned_stats = set()

        # pad stat names to the length of the longest name
        padding = max(len(v) for v in display_names.values())

        def build_section(rows):
            # rows: list of (name, val, raw)
            if not rows:
                return None
            max_len = max(len(r[0]) for r in rows)
            lines = []
            for name, val, raw in rows:
                padded = name.ljust(max_len)
                lines.append(f"{padded}  {get_bar(val)}  {val:>2}  ({raw})")
            return "```\n" + "\n".join(lines) + "\n```"

        for cat_name, targets in category_list:
            rows = []
            for stat_name in targets:
                if stat_name in stat_lookup:
                    row = stat_lookup[stat_name]
                    name = display_names.get(stat_name, stat_name.replace("_", " ").title()).rjust(padding)
                    rows.append((name, row['value'], row['raw']))
                    assigned_stats.add(stat_name)
            content = build_section(rows)
            if content:
                embed.add_field(name=cat_name, value=content, inline=False)

        other_rows = []
        for row in self.percentiles:
            if row['stat'] not in assigned_stats:
                stat_name = row['stat']
                name = display_names.get(stat_name, stat_name.replace("_", " ").title()).rjust(padding)
                other_rows.append((name, row['value'], row['raw']))
        content = build_section(other_rows)
        if content:
            embed.add_field(name="Other", value=content, inline=False)

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
            rv = str(p['rv100']).rjust(6)
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

    @property
    def score(self) -> float:
        """Heuristic to determine who 'owns' who."""
        try:
            ops_val = float(self.ops)
        except:
            ops_val = 0.0
        
        # Factor in volume - ownership needs at least a few PAs to be meaningful
        if self.pa < 3:
            return 0.0
            
        return ops_val

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

        if len(self.stats) == 1:
            for labels in labels_list:
                if 'season' in labels: labels.remove('season')
                if 'team' in labels: labels.remove('team')
        elif len(self.stats) > 1:
            all_seasons_same = all(s.get('season') == self.stats[0].get('season') for s in self.stats)
            for labels in labels_list:
                if all_seasons_same and 'season' in labels: 
                    labels.remove('season')

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

@dataclass
class CompareStats:
    title: str
    stat_type: str
    rows: List[dict]
    errors: List[str] = None

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
    game_status: str = ""
    game_abstract_state: str = ""

    def _format_table(self, labels: List[str], rows: List[dict], repl: dict, left_cols: set) -> str:
        """Generic fixed-width table formatter."""
        if not rows:
            return "No data available."

        widths = {}
        for label in labels:
            widths[label] = len(repl.get(label, label.upper()))
            for row in rows:
                widths[label] = max(widths[label], len(str(row.get(label, ''))))

        header = ''
        for label in labels:
            display = repl.get(label, label.upper())
            if label in left_cols:
                header += display.ljust(widths[label]) + ' '
            else:
                header += display.rjust(widths[label]) + ' '

        lines = [header.rstrip()]
        for row in rows:
            line = ''
            for label in labels:
                val = str(row.get(label, ''))
                if label in left_cols:
                    line += val.ljust(widths[label]) + ' '
                else:
                    line += val.rjust(widths[label]) + ' '
            lines.append(line.rstrip())

        return '\n'.join(lines)

    def format_batting(self) -> str:
        labels = ['name', 'pos', 'ab', 'r', 'h', 'rbi', 'bb', 'so', 'lob', 'avg', 'obp', 'slg']
        repl = {'name': '', 'pos': '', 'ab': 'AB', 'r': 'R', 'h': 'H', 'rbi': 'RBI', 'bb': 'BB', 'so': 'SO', 'lob': 'LOB', 'avg': 'AVG', 'obp': 'OBP', 'slg': 'SLG'}
        left_cols = {'name', 'pos'}
        return self._format_table(labels, self.batting_rows, repl, left_cols)

    def format_pitching(self) -> str:
        labels = ['name', 'ip', 'h', 'r', 'er', 'bb', 'so', 'hr', 'era', 'p', 's', 'dec']
        repl = {'name': '', 'ip': 'IP', 'h': 'H', 'r': 'R', 'er': 'ER', 'bb': 'BB', 'so': 'SO', 'hr': 'HR', 'era': 'ERA', 'p': 'P', 's': 'S', 'dec': 'DEC'}
        left_cols = {'name', 'dec'}
        return self._format_table(labels, self.pitching_rows, repl, left_cols)

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

@dataclass
class BullpenData:
    team_name: str
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
            
        # Group by status (Fresh -> Used -> Tired -> Gassed)
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
    position: str = ""

    def format(self, max_name_len=18, is_team=False) -> str:
        if is_team:
            return f"{self.rank:<2} {self.name:<{max_name_len + 8}} {self.value}"
        return f"{self.rank:<2} {self.team_abbrev:<4} {self.position:<3} {self.name:<{max_name_len}} {self.value}"

class MLBClient:
    BASE_URL = "https://statsapi.mlb.com/api/v1"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._team_abbrevs: Optional[dict] = None


    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_team_abbrevs(self) -> dict:
        if self._team_abbrevs:
            return self._team_abbrevs
        
        session = await self.get_session()
        async with session.get(f"{self.BASE_URL}/teams?sportId=1") as resp:
            data = await resp.json()
            mapping = {}
            for team in data.get('teams', []):
                tid = team.get('id')
                abbrev = team.get('abbreviation')
                if abbrev == "OAK":
                    abbrev = "ATH" # Per user request
                mapping[tid] = abbrev
            self._team_abbrevs = mapping
        return self._team_abbrevs


    async def close(self):
        """Closes the aiohttp session properly."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def search_players(self, query: str, milb: bool = False) -> List[dict]:
        """Queries the MLB APIs for autocomplete."""
        session = await self.get_session()
        if milb:
            url = f"{self.BASE_URL}/people/search?names={urllib.parse.quote(query)}&sportIds=11,12,13,14,15,5442,16&active=true&hydrate=currentTeam,team"
            try:
                async with session.get(url) as resp:
                    data = await resp.json()
                    results = []
                    for p in data.get('people', []):
                        team_name = p.get('currentTeam', {}).get('name', 'FA')
                        results.append({'id': p['id'], 'name': p['fullName'], 'name_display_club': team_name, 'mlb': 1})
                    return results
            except Exception:
                return []
        else:
            url = f"https://baseballsavant.mlb.com/player/search-all?search={urllib.parse.quote(query)}"
            try:
                async with session.get(url) as resp:
                    results = await resp.json()
            except Exception:
                results = []

            # Fall back to MLB Stats API for players not yet indexed by Savant (e.g. fresh callups)
            if not any(p.get('mlb') == 1 for p in results):
                try:
                    fallback_url = f"{self.BASE_URL}/people/search?names={urllib.parse.quote(query)}&sportId=1&active=true&hydrate=currentTeam"
                    async with session.get(fallback_url) as resp:
                        data = await resp.json()
                    for p in data.get('people', []):
                        team_name = p.get('currentTeam', {}).get('name', 'FA')
                        results.append({
                            'id': str(p['id']),
                            'name': p['fullName'],
                            'name_display_club': team_name,
                            'mlb': 1,
                        })
                except Exception:
                    pass

            return results

    async def resolve_player(self, name_or_id: str, milb: bool = False) -> Optional[dict]:
        """Resolve a player name or ID to {'id': str, 'name': str}.
        Prioritizes Nationals > active MLB > any result, matching the old bot's behavior."""
        session = await self.get_session()
        if name_or_id.isdigit():
            # If it's an ID, we still want the name for display purposes
            url = f"{self.BASE_URL}/people/{name_or_id}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    person = data.get('people', [{}])[0]
                    return {'id': name_or_id, 'name': person.get('fullName', name_or_id)}
            return {'id': name_or_id, 'name': name_or_id}

        players = await self.search_players(name_or_id, milb=milb)
        if not players:
            return None

        if milb:
            # For MiLB, prioritize Nats org affiliates
            nats_affiliates = ['nationals', 'senators', 'red wings', 'blue rocks', 'frednats', 'rochester', 'harrisburg', 'wilmington', 'fredericksburg']
            for p in players:
                team = p.get('name_display_club', '').lower()
                if any(aff in team for aff in nats_affiliates):
                    return {'id': str(p['id']), 'name': p['name']}
            return {'id': str(players[0]['id']), 'name': players[0]['name']}
        else:
            # For MLB: Nationals first, then active MLB, then anyone
            nats_match = None
            mlb_match = None
            for p in players:
                team = p.get('name_display_club', '')
                if not team:
                    continue
                if 'nationals' in team.lower() and nats_match is None:
                    nats_match = p
                elif p.get('mlb') == 1 and mlb_match is None:
                    mlb_match = p

            best = nats_match or mlb_match or players[0]
            return {'id': str(best['id']), 'name': best['name']}

    async def get_team_id(self, team_query: str) -> Optional[int]:
        if not team_query: return None
        query = team_query.lower()
        aliases = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals", "dbacks": "diamondbacks", "barves": "braves"}
        query = aliases.get(query, query)
        
        session = await self.get_session()
        async with session.get(f"{self.BASE_URL}/teams?sportId=1") as resp:
            data = await resp.json()
            for team in data.get('teams', []):
                if (query == team.get('abbreviation', '').lower() or 
                    query in team.get('name', '').lower() or 
                    query in team.get('teamName', '').lower()):
                    return team['id']
        return None

    async def get_team_schedule(self, team_query: str, num_games: int = 3, past: bool = False) -> List[Game]:
        team_id = await self.get_team_id(team_query)
        if not team_id:
            return []

        now = datetime.utcnow() - timedelta(hours=5)
        # Use a wide 45-day window to guarantee we find enough games even with rainouts or the All-Star break
        if past:
            start_date = (now - timedelta(days=45)).strftime("%Y-%m-%d")
            end_date = now.strftime("%Y-%m-%d")
        else:
            start_date = now.strftime("%Y-%m-%d")
            end_date = (now + timedelta(days=45)).strftime("%Y-%m-%d")

        session = await self.get_session()
        url = f"{self.BASE_URL}/schedule?sportId=1&teamId={team_id}&startDate={start_date}&endDate={end_date}&hydrate=team,linescore(matchup,runners),previousPlay,person,stats,lineups,probablePitcher,decisions,flags"
        
        async with session.get(url) as resp:
            data = await resp.json()

        if not data.get('dates'): return []

        games = []
        for date_obj in data['dates']:
            for game_data in date_obj['games']:
                game = Game.from_api_json(game_data)
                if past:
                    # Only include games that have completely finished
                    if game.abstract_state == 'Final' or game.status in ['Suspended', 'Completed Early']:
                        games.append(game)
                else:
                    # Include anything that isn't finished or cancelled (Scheduled, Warmup, Live, Delayed)
                    if game.abstract_state != 'Final' and game.status not in ['Postponed', 'Cancelled']:
                        games.append(game)

        # Take the most recent 'N' games from the end of the list (past), or the first 'N' games (next)
        games = games[-num_games:] if past else games[:num_games]
            
        # Fetch PBP if any of these scheduled games happen to be Live right now
        async def fetch_pbp(g: Game):
            if g.abstract_state == "Live" and g.status not in ["Delayed", "Warmup"]:
                try:
                    async with session.get(f"{self.BASE_URL}/game/{g.game_pk}/playByPlay") as pbp_resp:
                        if pbp_resp.status == 200:
                            # Our existing static parse handles everything else, so just poke the endpoint to wake the API up
                            pass
                except Exception: pass
                
        if games: await asyncio.gather(*(fetch_pbp(g) for g in games))
        return games

    async def get_games_with_scoring_plays(self, team_query: str, date: str = None) -> List[Game]:
        # 1. Find the game(s) for the team
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return []
        
        team_id = await self.get_team_id(team_query)
        if not team_id:
            return games

        session = await self.get_session()

        async def process_game(game: Game):
            # 2. Fetch PBP and Content
            pbp_url = f"{self.BASE_URL}/game/{game.game_pk}/playByPlay"
            content_url = f"{self.BASE_URL}/game/{game.game_pk}/content"
            
            try:
                async with session.get(pbp_url) as resp:
                    pbp_data = await resp.json() if resp.status == 200 else {}
                async with session.get(content_url) as resp:
                    content_data = await resp.json() if resp.status == 200 else {}
            except Exception as e:
                print(f"Error fetching scoring play data for game {game.game_pk}: {e}")
                return

            # 3. Process scoring plays
            game.scoring_plays = []
            
            team_side = 'away' if game.away.id == team_id else 'home'
            team_half = 'top' if team_side == 'away' else 'bottom'

            content_dict = {}
            highlights = content_data.get('highlights', {}).get('highlights', {}).get('items', [])
            for item in highlights:
                if 'guid' in item:
                    for pb in item.get('playbacks', []):
                        if pb.get('name') == 'mp4Avc':
                            content_dict[item['guid']] = {'url': pb['url'], 'blurb': item.get('headline', item.get('blurb', ''))}
                            break

            scoring_play_indices = pbp_data.get('scoringPlays', [])
            if not scoring_play_indices: return
            all_plays = pbp_data.get('allPlays', [])

            for play_index in scoring_play_indices:
                play = all_plays[play_index]
                if play.get('about', {}).get('halfInning') != team_half: continue

                half = play.get('about', {}).get('halfInning', '')
                inning = f"{'bot' if half == 'bottom' else half} {play.get('about', {}).get('inning', '')}"
                desc = play.get('result', {}).get('description', 'Scoring play.')
                desc = _bold_play_description(desc, play)
                if 'awayScore' in play.get('result', {}): desc += f" ({play['result']['awayScore']}-{play['result']['homeScore']})"
                vid_url, vid_blurb = "", ""
                if play.get('playEvents') and (play_id := play['playEvents'][-1].get('playId')) and play_id in content_dict:
                    vid_url, vid_blurb = content_dict[play_id]['url'], content_dict[play_id]['blurb']
                game.scoring_plays.append(ScoringPlay(inning, desc, vid_url, vid_blurb))

        await asyncio.gather(*(process_game(g) for g in games))
        return games

    async def get_recent_home_runs(self, date: str = None) -> List[dict]:
        session = await self.get_session()
        from datetime import timezone
        now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
        date_str = date or now.strftime("%Y-%m-%d")

        sched_url = f"{self.BASE_URL}/schedule?sportId=1&date={date_str}&hydrate=team"
        async with session.get(sched_url) as resp:
            sched = await resp.json() if resp.status == 200 else {}

        dates = sched.get('dates', [])
        if not dates:
            return []
        games = dates[0].get('games', [])

        home_runs = []

        async def fetch_game_hrs(game):
            if game.get('status', {}).get('abstractGameCode') == 'P':
                return
            game_pk = game['gamePk']
            away_abbrev = game['teams']['away']['team']['abbreviation']
            home_abbrev = game['teams']['home']['team']['abbreviation']

            pbp_url = f"{self.BASE_URL}/game/{game_pk}/playByPlay"
            content_url = f"{self.BASE_URL}/game/{game_pk}/content"
            try:
                async with session.get(pbp_url) as resp:
                    pbp = await resp.json() if resp.status == 200 else {}
                async with session.get(content_url) as resp:
                    content_data = await resp.json() if resp.status == 200 else {}
            except Exception:
                return

            content_dict = {}
            for item in content_data.get('highlights', {}).get('highlights', {}).get('items', []):
                if 'guid' in item:
                    for pb in item.get('playbacks', []):
                        if pb.get('name') == 'mp4Avc':
                            content_dict[item['guid']] = {'url': pb['url'], 'blurb': item.get('headline', item.get('blurb', ''))}
                            break

            for play in pbp.get('allPlays', []):
                if play.get('result', {}).get('eventType') != 'home_run':
                    continue

                about = play.get('about', {})
                half = about.get('halfInning', '')
                inning_num = about.get('inning', '')
                end_time = about.get('endTime', '')

                batter = play.get('matchup', {}).get('batter', {}).get('fullName', '')
                pitcher = play.get('matchup', {}).get('pitcher', {}).get('fullName', '')
                rbi = play.get('result', {}).get('rbi', 0)

                if half == 'bottom':
                    batter_team = home_abbrev
                    pitcher_team = away_abbrev
                else:
                    batter_team = away_abbrev
                    pitcher_team = home_abbrev

                dist, ev, la, pitch_type, pitch_speed = 0, 0.0, 0, '', 0.0
                last_play_id = None
                for event in play.get('playEvents', []):
                    if event.get('details', {}).get('isInPlay') and 'hitData' in event:
                        hd = event['hitData']
                        dist = int(hd.get('totalDistance') or 0)
                        ev = hd.get('launchSpeed') or 0.0
                        la = int(hd.get('launchAngle') or 0)
                        pitch_type = event.get('details', {}).get('type', {}).get('description', '')
                        pitch_speed = event.get('pitchData', {}).get('startSpeed') or 0.0
                        last_play_id = event.get('playId')
                        break

                desc = play.get('result', {}).get('description', '')
                hr_num = 0
                for keyword in ('grand slam', 'home run', 'homers'):
                    if keyword in desc:
                        m = re.search(r'\((\d+)\)', desc[desc.index(keyword):])
                        if m:
                            hr_num = int(m.group(1))
                        break

                video_url, video_blurb = '', ''
                if last_play_id and last_play_id in content_dict:
                    video_url = content_dict[last_play_id]['url']
                    video_blurb = content_dict[last_play_id]['blurb']

                home_runs.append({
                    'batter': batter,
                    'batter_team': batter_team,
                    'pitcher': pitcher,
                    'pitcher_team': pitcher_team,
                    'rbi': rbi,
                    'dist': dist,
                    'ev': ev,
                    'la': la,
                    'num': hr_num,
                    'time': end_time,
                    'inning': f"{'bot' if half == 'bottom' else 'top'} {inning_num}",
                    'desc': desc,
                    'pitch_type': pitch_type,
                    'pitch_speed': pitch_speed,
                    'video_url': video_url,
                    'video_blurb': video_blurb,
                })

        await asyncio.gather(*(fetch_game_hrs(g) for g in games))

        return home_runs

    async def get_player_game_stats(self, player_id_or_name: str, date: str = None, milb: bool = False, include_abs: bool = False) -> List[PlayerGameStats]:
        session = await self.get_session()

        resolved = await self.resolve_player(player_id_or_name, milb=milb)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']
        
        headshot_url = f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}@3x.jpg"

        # Fetch player info to find out what team they are currently on
        person_url = f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam,team"
        async with session.get(person_url) as resp:
            person_data = await resp.json()

        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        team_id = person.get('currentTeam', {}).get('id')
        team_abbrev = person.get('currentTeam', {}).get('abbreviation', 'TEAM')

        # For a specific historical date (MLB only), use gameLog to find the correct team+game
        # rather than assuming the player is on their current team
        if date and not milb:
            try:
                parsed = datetime.strptime(date, "%m/%d/%Y") if "/" in date else datetime.strptime(date, "%Y-%m-%d")
                season = str(parsed.year)
                target_date_str = parsed.strftime("%Y-%m-%d")
                game_pks = []
                for grp in ["pitching", "hitting"]:
                    gl_url = f"{self.BASE_URL}/people/{player_id}/stats?stats=gameLog&season={season}&group={grp}"
                    async with session.get(gl_url) as resp:
                        gl_data = await resp.json()
                    for stat_block in gl_data.get("stats", []):
                        for split in stat_block.get("splits", []):
                            if split.get("date") == target_date_str:
                                gpk = split["game"]["gamePk"]
                                if gpk not in game_pks:
                                    game_pks.append(gpk)
                                team_id = split["team"]["id"]
                                team_abbrev = split["team"].get("abbreviation", team_abbrev)
            except Exception:
                game_pks = []
            if game_pks:
                sched_data = {"dates": [{"date": target_date_str, "games": [{"gamePk": gpk, "teams": {}} for gpk in game_pks]}]}
            else:
                return [PlayerGameStats(player_id, player_name, team_abbrev, "N/A", False, date, info_message="No games found for this date.", headshot_url=headshot_url)]
        else:
            if not team_id:
                return [PlayerGameStats(player_id, player_name, "FA", "N/A", False, "Today", info_message="Player is not currently on a team.", headshot_url=headshot_url)]

            # Fetch the team's schedule for today to get the gamePk(s)
            sport_ids = "11,12,13,14,15,5442,16" if milb else "1"
            schedule_url = f"{self.BASE_URL}/schedule?sportId={sport_ids}&teamId={team_id}"
            async with session.get(schedule_url) as resp:
                sched_data = await resp.json()

            if not sched_data.get('dates') or not sched_data['dates'][0].get('games'):
                return [PlayerGameStats(player_id, player_name, team_abbrev, "N/A", False, "Today", info_message="No games scheduled for today.", headshot_url=headshot_url)]


        results = []
        games = sched_data['dates'][0]['games']
        game_date = sched_data['dates'][0]['date']
        game_date_formatted = f"{int(game_date[5:7])}/{int(game_date[8:10])}"

        # Loop through all games that day (handles doubleheaders cleanly)
        for game in games:
            # Fetch the Boxscore for that game
            box_url = f"{self.BASE_URL}/game/{game['gamePk']}/boxscore"
            async with session.get(box_url) as resp:
                box_data = await resp.json()

            if game.get('teams') and game['teams'].get('home', {}).get('team', {}).get('id'):
                is_home = (game['teams']['home']['team']['id'] == team_id)
            else:
                is_home = (box_data['teams']['home']['team']['id'] == team_id)
            side = 'home' if is_home else 'away'
                
            box_away = box_data['teams']['away']['team']
            box_home = box_data['teams']['home']['team']
            team_abbrev = box_home.get('abbreviation', team_abbrev) if is_home else box_away.get('abbreviation', team_abbrev)
            opp_abbrev = box_away.get('abbreviation', "OPP") if is_home else box_home.get('abbreviation', "OPP")

            if game.get('status', {}).get('abstractGameState') == 'Preview':
                results.append(PlayerGameStats(player_id, player_name, team_abbrev, opp_abbrev, is_home, game_date_formatted, info_message="Game has not started yet.", headshot_url=headshot_url))
                continue

            players_dict = box_data['teams'][side]['players']
            player_key = f"ID{player_id}"
            
            if player_key not in players_dict:
                results.append(PlayerGameStats(player_id, player_name, team_abbrev, opp_abbrev, is_home, game_date_formatted, info_message="Player did not play in this game.", headshot_url=headshot_url))
                continue

                
            player_stats = players_dict[player_key]['stats']
            batting = player_stats.get('batting')
            pitching = player_stats.get('pitching')
            
            # Pitchers usually have empty hitting dicts even in the DH era, so we filter them out
            if batting and batting.get('atBats', 0) == 0 and batting.get('plateAppearances', 0) == 0:
                batting = None
            if pitching and pitching.get('inningsPitched', '0.0') == '0.0':
                pitching = None
                
            if not batting and not pitching:
                results.append(PlayerGameStats(player_id, player_name, team_abbrev, opp_abbrev, is_home, game_date_formatted, info_message="Player played but recorded no stats (e.g., pinch runner or defensive sub).", headshot_url=headshot_url))
                continue

                
            at_bats = []
            if include_abs:
                pbp_url = f"{self.BASE_URL}/game/{game['gamePk']}/playByPlay"
                content_url = f"{self.BASE_URL}/game/{game['gamePk']}/content"
                
                try:
                    async with session.get(pbp_url) as resp:
                        pbp_data = await resp.json() if resp.status == 200 else {}
                    async with session.get(content_url) as resp:
                        content_data = await resp.json() if resp.status == 200 else {}
                except Exception as e:
                    print(f"Error fetching AB data: {e}")
                    pbp_data, content_data = {}, {}
                    
                content_dict = {}
                highlights = content_data.get('highlights', {}).get('highlights', {}).get('items', [])
                for item in highlights:
                    if 'guid' in item:
                        for pb in item.get('playbacks', []):
                            if pb.get('name') == 'mp4Avc':
                                content_dict[item['guid']] = {'url': pb['url'], 'blurb': item.get('headline', item.get('blurb', ''))}
                                break
                                
                for play in pbp_data.get('allPlays', []):
                    if play.get('matchup', {}).get('batter', {}).get('id') == int(player_id):
                        half = play.get('about', {}).get('halfInning', '')
                        if half == 'bottom': half = 'bot'
                        inning = f"{half} {play.get('about', {}).get('inning', '')}"
                        is_complete = play.get('about', {}).get('isComplete', False)
                        desc = play.get('result', {}).get('description', 'Currently at bat.')
                        desc = _bold_play_description(desc, play)
                        matchup = play.get('matchup', {})
                        pitcher = matchup.get('pitcher', {}).get('fullName', '')
                        stand = matchup.get('batSide', {}).get('code', 'R')
                        is_scoring = play.get('about', {}).get('isScoringPlay', False)


                        pitch_str, statcast_str, vid_url, vid_blurb = "", "", "", ""
                        pitches_list = []

                        if play.get('playEvents'):
                            for pe in play['playEvents']:
                                if pe.get('isPitch'):
                                    pd = pe.get('pitchData', {})
                                    coord = pd.get('coordinates', {})
                                    details = pe.get('details', {})
                                    cnt = pe.get('count', {})
                                    
                                    p_num = pe.get('pitchNumber', len(pitches_list) + 1)
                                    p_count = f"{cnt.get('balls', 0)}-{cnt.get('strikes', 0)}"
                                    p_desc = details.get('details', {}).get('description') or details.get('description', '')
                                    p_speed = pd.get('startSpeed', 0.0)
                                    p_type = details.get('type', {}).get('description', 'Pitch')
                                    p_px = coord.get('pX')
                                    p_pz = coord.get('pZ')
                                    p_sz_top = pd.get('strikeZoneTop')
                                    p_sz_bot = pd.get('strikeZoneBottom')
                                    
                                    if p_px is not None and p_pz is not None:
                                        pitches_list.append(Pitch(
                                            number=p_num, count=p_count, description=p_desc,
                                            speed=p_speed, type=p_type, px=p_px, pz=p_pz,
                                            sz_top=p_sz_top, sz_bot=p_sz_bot
                                        ))

                            last_event = play['playEvents'][-1]
                            if 'pitchData' in last_event:
                                pspeed = last_event['pitchData'].get('startSpeed')
                                ptype = last_event.get('details', {}).get('type', {}).get('description')
                                if pspeed and ptype:
                                    pitch_str = f"{pspeed:.1f} mph {ptype}"

                            if 'hitData' in last_event:
                                hd = last_event['hitData']
                                dist, ev, la = hd.get('totalDistance'), hd.get('launchSpeed'), hd.get('launchAngle')
                                parts = []
                                if dist: parts.append(f"{dist} ft")
                                if ev: parts.append(f"{ev} mph")
                                if la is not None: parts.append(f"{la} degrees")
                                statcast_str = ", ".join(parts)

                            play_id = last_event.get('playId')
                            if play_id and play_id in content_dict:
                                vid_url = content_dict[play_id]['url']
                                vid_blurb = content_dict[play_id]['blurb']

                        at_bats.append(AtBat(inning, pitcher, desc, pitch_str, statcast_str, vid_url, vid_blurb, is_scoring, is_complete, pitches_list, stand))



            results.append(PlayerGameStats(
                player_id=player_id,
                player_name=player_name, team_abbrev=team_abbrev, opp_abbrev=opp_abbrev, is_home=is_home,
                date=game_date_formatted, batting_stats=batting, pitching_stats=pitching, pitching_dec=pitching.get('note', '') if pitching else "",
                headshot_url=headshot_url, at_bats=at_bats
            ))

            
        return results

    async def get_player_season_stats(self, player_id_or_name: str, stat_type: str = None, year: str = None, career: bool = False, milb: bool = False) -> List[PlayerSeasonStats]:
        session = await self.get_session()
        player_id = None
        player_name = player_id_or_name

        resolved = await self.resolve_player(player_id_or_name, milb=milb)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']

        headshot_url = f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}@3x.jpg"

        league_list_id = "mlb_milb" if milb else "mlb"
        person_url = f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam,team,draft,stats(type=[yearByYear,careerRegularSeason,career](team(league,sport)),leagueListId={league_list_id},group=[hitting,pitching])"
        async with session.get(person_url) as resp:
            person_data = await resp.json()

        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        pos = person.get('primaryPosition', {}).get('abbreviation', '')
        
        birthdate = person.get('birthDate', '1900-01-01')[:10]
        try:
            b_dt = datetime.strptime(birthdate, "%Y-%m-%d")
            now = datetime.now()
            age = now.year - b_dt.year - ((now.month, now.day) < (b_dt.month, b_dt.day))
            age_str = f"Age: {age}"
        except:
            age_str = ""

        info_line = f"{pos}  |  {person.get('batSide', {}).get('code', '')}/{person.get('pitchHand', {}).get('code', '')}  |  {person.get('height', '')}  |  {person.get('weight', '')} lbs  |  {age_str}"
        
        if person.get('nickName'):
            info_line += f"  |  \"{person['nickName']}\""
            
        if milb and 'drafts' in person and person['drafts']:
            draft = person['drafts'][-1]
            d_year = draft.get('year', 'N/A')
            d_round = draft.get('pickRound', 'N/A')
            d_pick = draft.get('roundPickNumber', 'N/A')
            d_school_obj = draft.get('school') or {}
            d_school = d_school_obj.get('name', 'N/A')
            info_line += f"\n  Draft: {d_year} | Round: {d_round} | Pick: {d_pick} | School: {d_school}"
            
        parent_org_abbrev = ""
        if milb:
            parent_org_id = person.get('currentTeam', {}).get('parentOrgId')
            if parent_org_id:
                async with session.get(f"{self.BASE_URL}/teams/{parent_org_id}") as resp:
                    if resp.status == 200:
                        team_data = await resp.json()
                        if team_data.get('teams'):
                            parent_org_abbrev = team_data['teams'][0].get('abbreviation', '')

        if not stat_type:
            if pos == "TWP":
                stat_types_to_fetch = ["hitting", "pitching"]
            else:
                stat_types_to_fetch = ["pitching"] if pos == "P" else ["hitting"]
        else:
            stat_types_to_fetch = [stat_type]

        api_stat_types = ["careerRegularSeason", "career"] if career else ["yearByYear"]
        
        target_year = str(year) if year else None
        target_years = []
        if year:
            year_clean = str(year).strip(' "\'')
            parts = year_clean.split('-')
            if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                start = int(parts[0].strip())
                end = int(parts[1].strip())
                target_years = [str(y) for y in range(start, end + 1)]
            else:
                target_years = [year_clean]
        
        all_stats = person.get('stats', [])
        results = []
        team_abbrev = person.get('currentTeam', {}).get('abbreviation', 'FA')

        career_years_str = ""
        career_teams = []  # List of (team_id, abbrev) tuples in chronological order
        team_id_to_latest_abbrev = {}  # Map of team_id -> most recent abbreviation

        # Extract team chain and career years from yearByYear data.
        # Prefer the yearByYear group matching the player's primary stat type (hitting for
        # position players, pitching for pitchers) so a player with sparse stats in the other
        # group doesn't produce a truncated chain (e.g. a hitter who pitched only for one team).
        primary_stat = stat_types_to_fetch[0] if stat_types_to_fetch else "hitting"
        yby_groups = [sg for sg in all_stats if sg['type']['displayName'] == 'yearByYear']
        preferred = next((sg for sg in yby_groups if sg['group']['displayName'] == primary_stat), None)
        chain_group = preferred or (yby_groups[0] if yby_groups else None)

        if chain_group:
            career_years = []
            for split in chain_group.get('splits', []):
                season = split.get('season')
                if season and season not in career_years:
                    career_years.append(season)
                if milb:
                    t_abbrev = split.get('sport', {}).get('abbreviation')
                    t_id = None
                else:
                    t_abbrev = split.get('team', {}).get('abbreviation')
                    t_id = split.get('team', {}).get('id')
                if t_abbrev and t_abbrev not in ['MLB', 'MiLB']:
                    # Track the most recent abbreviation for this team
                    team_id_to_latest_abbrev[t_id] = t_abbrev
                    # Only add if different from the last team (avoid back-to-back duplicates)
                    if not career_teams or t_id != career_teams[-1][0]:
                        career_teams.append((t_id, t_abbrev))

            if career_years:
                career_years_str = f"{min(career_years)}-{max(career_years)}" if len(career_years) > 1 else min(career_years)

        # If the player's current team isn't the last entry in the chain (e.g., recently traded),
        # append it so the chain reflects where they actually play now
        current_team_id = person.get('currentTeam', {}).get('id')
        current_team_abbrev = person.get('currentTeam', {}).get('abbreviation', '')
        if current_team_id and current_team_abbrev and career_teams and career_teams[-1][0] != current_team_id:
            career_teams.append((current_team_id, current_team_abbrev))
            team_id_to_latest_abbrev[current_team_id] = current_team_abbrev

        if career and career_teams:
            # Use the most recent abbreviation for each team
            team_abbrevs = [team_id_to_latest_abbrev[t_id] for t_id, _ in career_teams]
            unique_team_count = len(set(t_id for t_id, _ in career_teams))
            info_line += f"\n\n{'-'.join(team_abbrevs)} ({unique_team_count} teams)"

        for st in stat_types_to_fetch:
            found_stats = []
            level_abbrev = ""
            current_target_year = target_year

            for stat_group in all_stats:
                if stat_group['group']['displayName'] == st and stat_group['type']['displayName'] in api_stat_types:
                    splits = stat_group.get('splits', [])
                    if not splits:
                        continue
                        
                    if career:
                        career_split = splits[-1]
                        for sp in splits:
                            if 'team' not in sp:
                                career_split = sp
                                break
                                
                        s = career_split.get('stat', {})
                        s['season'] = "Career"
                        s['team'] = "MLB"
                        found_stats.append(s)
                        current_target_year = career_years_str or "Career"
                        break
                    else:
                        if not target_years:
                            now = datetime.now()
                            # Default to current year if season has started, otherwise use most recent stats
                            if now.month >= 4:
                                target_years = [str(now.year)]
                            else:
                                target_years = [splits[-1].get('season', str(now.year))]

                        for split in splits:
                            season = split.get('season', '')
                            if season in target_years:
                                s = split.get('stat', {})
                                s['season'] = season
                                if milb:
                                    s['team'] = split.get('team', {}).get('abbreviation') or split.get('sport', {}).get('abbreviation', 'MiLB')
                                    level_abbrev = split.get('sport', {}).get('abbreviation', '')
                                else:
                                    s['team'] = split.get('team', {}).get('abbreviation', 'MLB')
                                found_stats.append(s)

            display_years = current_target_year
            if not career and target_years:
                display_years = f"{target_years[0]}-{target_years[-1]}" if len(target_years) > 1 else target_years[0]

            if found_stats:
                results.append(PlayerSeasonStats(
                    player_name=player_name,
                    team_abbrev=team_abbrev,
                    stat_type=st,
                    years=display_years,
                    is_career=career,
                    info_line=info_line,
                    stats=found_stats,
                    headshot_url=headshot_url,
                    parent_org_abbrev=parent_org_abbrev,
                    level_abbrev=level_abbrev,
                    birth_date=birthdate
                ))
            elif stat_type or len(stat_types_to_fetch) == 1:
                results.append(PlayerSeasonStats(
                    player_name=player_name,
                    team_abbrev=team_abbrev,
                    stat_type=st,
                    years=display_years,
                    is_career=career,
                    info_line=info_line,
                    stats=[],
                    info_message=f"No {st} stats found for this player.",
                    headshot_url=headshot_url,
                    parent_org_abbrev=parent_org_abbrev,
                    level_abbrev=level_abbrev,
                    birth_date=birthdate
                ))

        return results

    async def get_player_last_games(self, player_id_or_name: str, num_games: int = 10, stat_type: str = None) -> List[PlayerSeasonStats]:
        """Fetch a player's aggregated stats over their last N games using the lastXGames stat type."""
        session = await self.get_session()

        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']

        headshot_url = f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}@3x.jpg"

        person_url = (
            f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam,team,"
            f"stats(type=[lastXGames](team(league)),leagueListId=mlb_hist,limit={num_games},group=[hitting,pitching])"
        )
        async with session.get(person_url) as resp:
            person_data = await resp.json()

        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        pos = person.get('primaryPosition', {}).get('abbreviation', '')
        team_abbrev = person.get('currentTeam', {}).get('abbreviation', 'FA')

        birthdate = person.get('birthDate', '1900-01-01')[:10]
        try:
            b_dt = datetime.strptime(birthdate, "%Y-%m-%d")
            now = datetime.now()
            age = now.year - b_dt.year - ((now.month, now.day) < (b_dt.month, b_dt.day))
            age_str = f"Age: {age}"
        except:
            age_str = ""

        info_line = f"{pos}  |  B/T: {person.get('batSide', {}).get('code', '')}/{person.get('pitchHand', {}).get('code', '')}  |  {person.get('height', '')}  |  {person.get('weight', '')} lbs  |  {age_str}"
        if person.get('nickName'):
            info_line += f"  |  \"{person['nickName']}\""

        if not stat_type:
            if pos == "TWP":
                stat_types_to_fetch = ["hitting", "pitching"]
            else:
                stat_types_to_fetch = ["pitching"] if pos == "P" else ["hitting"]
        else:
            stat_types_to_fetch = [stat_type]

        all_stats = person.get('stats', [])
        results = []

        for st in stat_types_to_fetch:
            found_stats = []

            for stat_group in all_stats:
                if stat_group.get('group', {}).get('displayName') == st and stat_group.get('type', {}).get('displayName') == 'lastXGames':
                    splits = stat_group.get('splits', [])
                    if not splits:
                        continue

                    # lastXGames returns per-team splits plus a total; pick the total (no team) or fallback to last
                    agg_split = splits[-1]
                    for sp in splits:
                        if 'team' not in sp:
                            agg_split = sp
                            break

                    s = agg_split.get('stat', {})
                    s['team'] = agg_split.get('team', {}).get('abbreviation', team_abbrev)
                    found_stats.append(s)

            if found_stats:
                results.append(PlayerSeasonStats(
                    player_name=player_name,
                    team_abbrev=team_abbrev,
                    stat_type=st,
                    years=f"Last {num_games} Games",
                    is_career=False,
                    info_line=info_line,
                    stats=found_stats,
                    headshot_url=headshot_url,
                ))
            elif stat_type or len(stat_types_to_fetch) == 1:
                results.append(PlayerSeasonStats(
                    player_name=player_name,
                    team_abbrev=team_abbrev,
                    stat_type=st,
                    years=f"Last {num_games} Games",
                    is_career=False,
                    info_line=info_line,
                    stats=[],
                    info_message=f"No {st} stats found for this player's last {num_games} games.",
                    headshot_url=headshot_url,
                ))

        return results

    async def get_player_splits(self, player_id_or_name: str, sit_code: str, year: str = None, stat_type: str = None) -> List[PlayerSeasonStats]:
        session = await self.get_session()

        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']

        headshot_url = f"https://securea.mlb.com/mlb/images/players/head_shot/{player_id}@3x.jpg"

        async with session.get(f"{self.BASE_URL}/people/{player_id}") as resp:
            person_data = await resp.json()
        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        pos = person.get('primaryPosition', {}).get('abbreviation', '')
        team_abbrev = person.get('currentTeam', {}).get('abbreviation', 'FA')

        info_line = f"{pos}  |  {person.get('batSide', {}).get('code', '')}/{person.get('pitchHand', {}).get('code', '')}  |  {person.get('height', '')}  |  {person.get('weight', '')} lbs"

        if not stat_type:
            stat_type = "pitching" if pos == "P" else "hitting"

        season = year or str(datetime.now().year)

        _month_abbrevs = {'3':'Mar','4':'Apr','5':'May','6':'Jun','7':'Jul','8':'Aug','9':'Sep','10':'Oct'}
        all_months = sit_code == "all_months"
        api_sit_code = "3,4,5,6,7,8,9,10" if all_months else sit_code

        url = (
            f"{self.BASE_URL}/people/{player_id}/stats"
            f"?stats=statSplits&group={stat_type}&season={season}&sportId=1&sitCodes={api_sit_code}"
        )
        async with session.get(url) as resp:
            data = await resp.json()

        api_stats = data.get('stats', [])
        splits = api_stats[0].get('splits', []) if api_stats else []

        # Use the team from the split data (accurate for the queried season)
        split_team_id = splits[0].get('team', {}).get('id') if splits else None
        if split_team_id:
            async with session.get(f"{self.BASE_URL}/teams/{split_team_id}") as tresp:
                tdata = await tresp.json()
            team_abbrev = tdata.get('teams', [{}])[0].get('abbreviation', team_abbrev)

        if not splits:
            return [PlayerSeasonStats(
                player_name=player_name,
                team_abbrev=team_abbrev,
                stat_type=stat_type,
                years=season,
                is_career=False,
                info_line=info_line,
                stats=[],
                info_message=f"No {stat_type} split stats found for {player_name} in {season}.",
                headshot_url=headshot_url,
            )]

        if all_months:
            stat_rows = []
            for sp in splits:
                code = sp.get('split', {}).get('code', '')
                s = sp.get('stat', {})
                s['season'] = _month_abbrevs.get(code, code)
                s['team'] = team_abbrev
                stat_rows.append(s)
        else:
            stat = splits[0].get('stat', {})
            stat['team'] = team_abbrev
            stat_rows = [stat]

        return [PlayerSeasonStats(
            player_name=player_name,
            team_abbrev=team_abbrev,
            stat_type=stat_type,
            years=season,
            is_career=False,
            info_line=info_line,
            stats=stat_rows,
            headshot_url=headshot_url,
        )]

    async def get_compare_stats(self, player_names: List[str], stat_type: str = None, year: str = None, career: bool = False) -> Optional["CompareStats"]:
        """Fetch and compare multiple players' season or career stats side-by-side."""
        session = await self.get_session()

        # Resolve all player IDs concurrently
        async def _resolve(name: str):
            resolved = await self.resolve_player(name.strip())
            return resolved['id'] if resolved else None

        player_ids = await asyncio.gather(*(_resolve(n) for n in player_names))
        player_ids = [pid for pid in player_ids if pid is not None]
        if not player_ids:
            return None

        # Fetch all player data concurrently
        league_list_id = "mlb_hist"
        api_stat_types = "careerRegularSeason,career" if career else "yearByYear"

        async def fetch_person(pid: str):
            url = f"{self.BASE_URL}/people/{pid}?hydrate=currentTeam,team,stats(type=[{api_stat_types}](team(league,sport)),leagueListId={league_list_id},group=[hitting,pitching])"
            async with session.get(url) as resp:
                data = await resp.json()
            return data.get('people', [None])[0]

        persons = await asyncio.gather(*(fetch_person(pid) for pid in player_ids))
        persons = [p for p in persons if p is not None]
        if not persons:
            return None

        # Auto-detect stat type from first player's position
        first_pos = persons[0].get('primaryPosition', {}).get('abbreviation', '')
        if not stat_type:
            stat_type = "pitching" if first_pos == "P" else "hitting"

        now = datetime.now()
        target_year = str(year) if year else str(now.year)

        rows = []
        errors = []
        for person in persons:
            full_name = person.get('fullName', 'Unknown')
            last_name = person.get('lastName', full_name)[:5]
            team_abbrev = person.get('currentTeam', {}).get('abbreviation', 'FA')
            all_stats = person.get('stats', [])

            player_stat = None
            for stat_group in all_stats:
                group_name = stat_group.get('group', {}).get('displayName')
                type_name = stat_group.get('type', {}).get('displayName')

                if group_name != stat_type:
                    continue

                if career and type_name in ['careerRegularSeason', 'career']:
                    splits = stat_group.get('splits', [])
                    if splits:
                        # Prefer the total (no team) split
                        for sp in splits:
                            if 'team' not in sp:
                                player_stat = sp.get('stat', {})
                                break
                        if player_stat is None:
                            player_stat = splits[-1].get('stat', {})
                    break
                elif not career and type_name == 'yearByYear':
                    for split in stat_group.get('splits', []):
                        if split.get('season') == target_year:
                            player_stat = split.get('stat', {})
                    # Only fall back to most recent year if season hasn't started yet (before April)
                    if player_stat is None and now.month < 4:
                        splits = stat_group.get('splits', [])
                        if splits:
                            player_stat = splits[-1].get('stat', {})

            if player_stat:
                player_stat['name'] = last_name
                player_stat['team'] = team_abbrev
                rows.append(player_stat)
            else:
                if now.month >= 4:
                    errors.append(f"No stats yet for {full_name} in {target_year}.")
                else:
                    errors.append(f"No {stat_type} stats found for {full_name}.")

        if not rows:
            return None

        display_names = [p.get('fullName', '?') for p in persons]
        title = " vs ".join(display_names)
        if career:
            title += " (Career)"
        else:
            title += f" ({target_year})"

        return CompareStats(
            title=title,
            stat_type=stat_type,
            rows=rows,
            errors=errors,
        )

    async def get_player_percentiles(self, player_id_or_name: str, year: str = None) -> Optional[PlayerPercentiles]:
        session = await self.get_session()
        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return None

        pid = resolved['id']

        # Fetch player's current team
        team_abbrev = ""
        try:
            async with session.get(f"{self.BASE_URL}/people/{pid}?hydrate=currentTeam") as resp:
                data = await resp.json()
                person = data.get('people', [{}])[0]
                team_info = person.get('currentTeam', {})
                team_abbrev = team_info.get('abbreviation', '')
        except:
            pass

        url = f"https://baseballsavant.mlb.com/savant-player/{pid}"
        
        async with session.get(url) as resp:
            text = await resp.text()
            
        import re, json
        match = re.search(r"statcast:\s*(\[.*?\]),\s*\n", text, re.DOTALL)
        if not match:
            return None
            
        try:
            statcast_data = json.loads(match.group(1))
        except:
            return None
            
        if not statcast_data: return None
        
        target_year = str(year) if year else str(datetime.utcnow().year)
        
        year_stats = None
        for sm_dict in statcast_data:
            if sm_dict.get('aggregate') == "0" and str(sm_dict.get('year')) == target_year:
                year_stats = sm_dict
                break
                
        if not year_stats:
            for sm_dict in reversed(statcast_data):
                if sm_dict.get('aggregate') == "0":
                    year_stats = sm_dict
                    break
        
        if not year_stats: return None
        
        stat_type = year_stats.get('grouping_cat', 'Unknown')
        
        if stat_type == "Pitcher":
            stats_list = [
                "percent_rank_exit_velocity_avg", "percent_rank_launch_angle_avg",
                "percent_rank_barrel_batted_rate", "percent_rank_xwoba", "percent_rank_xera",
                "percent_rank_k_percent", "percent_rank_bb_percent", "percent_rank_chase_percent",
                "percent_rank_groundballs_percent", "percent_rank_whiff_percent",
                "percent_rank_pitch_run_value_fastball", "percent_rank_pitch_run_value_breaking",
                "percent_rank_pitch_run_value_offspeed"
            ]
        elif stat_type == "Batter":
            stats_list = [
                "percent_rank_exit_velocity_avg", "percent_rank_barrel_batted_rate",
                "percent_rank_xwoba", "percent_rank_xba", "percent_rank_k_percent",
                "percent_rank_bb_percent", "percent_rank_chase_percent", "percent_rank_whiff_percent",
                "percent_rank_sprint_speed", "percent_speed_order", "percent_rank_oaa",
                "percent_rank_fielding_run_value", "percent_rank_swing_take_run_value",
                "percent_rank_runner_run_value", "percent_rank_framing"
            ]
        else:
            stats_list = []
            
        raw_replace = {'oaa':'outs_above_average', 'chase_percent':'oz_swing_percent'}
        table_rows = []
        for prop in stats_list:
            if prop in year_stats and year_stats[prop] is not None:
                d = {}
                d['stat'] = prop.replace("percent_rank_", "").replace("percent_speed_order","sprint_speed").replace("swing_take","batting")
                try:
                    d['value'] = int(year_stats[prop])
                except ValueError:
                    d['value'] = 0
                    
                raw_prop = prop.replace('percent_rank_', '')
                raw_prop = raw_replace.get(raw_prop, raw_prop)
                d['raw'] = year_stats.get(raw_prop, "")
                
                if isinstance(d['raw'], float):
                    fmt = f"{d['raw']:.3f}"
                    if fmt.startswith("0.") and "avg" not in prop and "woba" not in prop and "ba" not in prop:
                        d['raw'] = fmt
                    elif fmt.startswith("0."):
                        d['raw'] = fmt.lstrip("0")
                    else:
                        d['raw'] = f"{d['raw']:.1f}"
                        
                table_rows.append(d)
                
        table_rows = sorted(table_rows, key=lambda i: i["value"], reverse=True)
        return PlayerPercentiles(resolved['name'], team_abbrev, str(year_stats.get('year')), stat_type, table_rows)

    async def get_highlights(self, query: str, date: str = None) -> List[HighlightItem]:
        session = await self.get_session()
        game_pk = None
        target_name = None
        is_team = False

        # Attempt to match a team first
        team_id = await self.get_team_id(query)
        if team_id:
            is_team = True

        if is_team:
            games = await self.get_todays_games(team_query=query, date=date)
            if not games: return []
            game_pk = games[0].game_pk
        else:
            resolved = await self.resolve_player(query)
            if not resolved: return []
            pid = resolved['id']
            target_name = resolved['name']
            
            log_url = f"{self.BASE_URL}/people/{pid}/stats?stats=gameLog&group=hitting,pitching"
            
            async with session.get(log_url) as resp:
                data = await resp.json()
                
            splits = []
            for sg in data.get('stats', []):
                splits.extend(sg.get('splits', []))
                
            if not splits: return []
            
            # Sort chronologically just in case, though API usually is
            splits = sorted(splits, key=lambda s: s.get('date', ''))
            
            if date:
                # Find the exact game for the given date if requested
                match = None
                for sp in splits:
                    if sp.get('date') == date: match = sp
                if not match: return []
                game_pk = match.get('game', {}).get('gamePk')
            else:
                last_game = splits[-1]
                game_pk = last_game.get('game', {}).get('gamePk')

        if not game_pk: return []

        content_url = f"{self.BASE_URL}/game/{game_pk}/content"
        async with session.get(content_url) as resp:
            content_data = await resp.json()

        items = content_data.get('highlights', {}).get('highlights', {}).get('items', [])
        results = []
        for item in items:
            blurb = item.get('blurb', '')
            desc = item.get('description', '')
            title = item.get('title', '')
            
            if not is_team and target_name:
                last_name = target_name.split()[-1]
                if last_name.lower() not in blurb.lower() and last_name.lower() not in desc.lower() and last_name.lower() not in title.lower():
                    continue

            url = ""
            # Favor direct high quality mp4
            for pb in item.get('playbacks', []):
                if pb.get('name') == 'mp4Avc':
                    url = pb.get('url')
                    break
            
            if not url:
                for pb in item.get('playbacks', []):
                    if '.mp4' in pb.get('url', ''):
                        url = pb.get('url')
                        break
                        
            if not url: continue
            
            hi = HighlightItem(
                title=title or blurb,
                description=desc,
                url=url,
                duration=item.get('duration', ''),
                date=item.get('date', '')
            )
            results.append(hi)

        return results

    async def get_standings(self, query: str = None) -> List[StandingsGroup]:
        session = await self.get_session()
        
        q = query.lower() if query else ""
        is_wc = "wc" in q or "wildcard" in q or "wild" in q
        
        league_id = "103,104"
        if "al" in q and "nl" not in q: league_id = "103"
        elif "nl" in q and "al" not in q: league_id = "104"
            
        url = f"{self.BASE_URL}/standings?leagueId={league_id}&hydrate=division,league"
        if is_wc:
            url += "&standingsTypes=wildCard"
            
        async with session.get(url) as resp:
            data = await resp.json()
            
        groups = []
        for grp in data.get('records', []):
            if is_wc:
                group_name = grp.get('league', {}).get('name', 'Wildcard') + " Wildcard"
            else:
                group_name = grp.get('division', {}).get('name', 'Division')
                
            if not is_wc and query:
                if "east" in q and "east" not in group_name.lower(): continue
                if "central" in q and "central" not in group_name.lower(): continue
                if "west" in q and "west" not in group_name.lower(): continue
                if "nl" in q and "al" not in q and "national" not in group_name.lower(): continue
                if "al" in q and "nl" not in q and "american" not in group_name.lower(): continue
            
            records = []
            for tr in grp.get('teamRecords', []):
                streak_obj = tr.get('streak')
                if isinstance(streak_obj, dict): streak = streak_obj.get('streakCode', '-')
                else: streak = str(streak_obj) if streak_obj else '-'
                if not streak: streak = "-"
                
                pct = tr.get('leagueRecord', {}).get('pct', '.000')
                if pct == ".000" and tr.get('wins', 0) == 0 and tr.get('losses', 0) == 0:
                    pct = ".---"
                
                records.append({
                    'team': tr.get('team', {}).get('name', 'Unknown'),
                    'w': tr.get('wins', 0),
                    'l': tr.get('losses', 0),
                    'pct': pct,
                    'gb': tr.get('divisionGamesBack', '-'),
                    'wc_gb': tr.get('wildCardGamesBack', '-'),
                    'streak': streak,
                    'diff': tr.get('runDifferential', 0)
                })
            
            groups.append(StandingsGroup(title=group_name, records=records))
            
        return groups

    async def get_matchup(self, team_query: str, pitcher_name: str) -> Optional[dict]:
        """Fetch career stats for all hitters on a team against a specific pitcher."""
        session = await self.get_session()
        
        # 1. Resolve Pitcher
        pitcher = await self.resolve_player(pitcher_name)
        if not pitcher:
            return None
            
        pid = pitcher['id']
        pitcher_display = pitcher['name']
        
        # 2. Get Team ID and Roster
        team_id = await self.get_team_id(team_query)
        if not team_id:
            return None
            
        roster_url = f"{self.BASE_URL}/teams/{team_id}/roster?rosterType=active"
        async with session.get(roster_url) as resp:
            roster_data = await resp.json()
            
        batters = []
        for entry in roster_data.get('roster', []):
            if entry.get('position', {}).get('type') != 'Pitcher':
                batters.append({
                    'id': entry['person']['id'],
                    'name': entry['person']['fullName']
                })
        
        if not batters:
            return None
            
        # 3. Fetch stats for each batter (parallel)
        async def fetch_vs(batter_id, batter_name):
            url = f"{self.BASE_URL}/people/{batter_id}/stats?stats=vsPlayer&opposingPlayerId={pid}&group=hitting"
            async with session.get(url) as resp:
                data = await resp.json()
                
            splits = []
            for sg in data.get('stats', []):
                splits.extend(sg.get('splits', []))
                
            if not splits:
                return BatterVsPitcher(
                    batter_name=batter_name, pa=0, ab=0, h=0, d=0, t=0, hr=0, bb=0, so=0, avg=".000", ops=".000"
                )
                
            # Aggregate raw totals for precise career math
            pa = ab = h = d = t = hr = bb = so = hbp = sf = 0
            for s in splits:
                st = s.get('stat', {})
                pa += st.get('plateAppearances', 0)
                ab += st.get('atBats', 0)
                h += st.get('hits', 0)
                d += st.get('doubles', 0)
                t += st.get('triples', 0)
                hr += st.get('homeRuns', 0)
                bb += st.get('baseOnBalls', 0)
                so += st.get('strikeOuts', 0)
                hbp += st.get('hitByPitch', 0)
                sf += st.get('sacFlies', 0)
            
            if pa == 0:
                return BatterVsPitcher(
                    batter_name=batter_name, pa=0, ab=0, h=0, d=0, t=0, hr=0, bb=0, so=0, avg=".000", ops=".000"
                )
                
            avg_str = f"{(h / ab):.3f}".lstrip('0') if ab > 0 else ".000"
            
            # Precise OBP = (H + BB + HBP) / (AB + BB + HBP + SF)
            obp_denom = (ab + bb + hbp + sf)
            obp = (h + bb + hbp) / obp_denom if obp_denom > 0 else 0.0
            
            # Precise SLG = (Singles + 2*D + 3*T + 4*HR) / AB
            singles = h - (d + t + hr)
            slg = (singles + 2*d + 3*t + 4*hr) / ab if ab > 0 else 0.0

            ops_str = f"{(obp + slg):.3f}".lstrip('0')
            
            return BatterVsPitcher(
                batter_name=batter_name,
                pa=pa, ab=ab, h=h, d=d, t=t, hr=hr, bb=bb, so=so,
                avg=avg_str if avg_str != ".000" else ".000",
                ops=ops_str
            )

        tasks = [fetch_vs(b['id'], b['name']) for b in batters]
        results = await asyncio.gather(*tasks)
        
        # Sort by PA descending
        results.sort(key=lambda x: x.pa, reverse=True)
        
        return {
            'pitcher': pitcher_display,
            'matchups': results
        }

    async def get_pitch_arsenal(self, player_name: str, year: str = None) -> Optional[PitchArsenal]:
        """Fetch pitch arsenal stats for a pitcher from Baseball Savant."""
        import re, json
        session = await self.get_session()
        resolved = await self.resolve_player(player_name)
        if not resolved:
            return None

        pid = str(resolved['id'])
        target_year = year or str(datetime.utcnow().year)

        # Fetch from statcast breakdown endpoint
        pitch_data = None
        for try_year in ([target_year] if year else [target_year, str(int(target_year) - 1)]):
            url = f"https://baseballsavant.mlb.com/player-services/statcast-pitches-breakdown?playerId={pid}&position=1&pitchBreakdown=pitches&timeFrame=yearly&season={try_year}&updatePitches=true"

            try:
                async with session.get(url) as resp:
                    text = await resp.text()
                    match = re.search(r'window\.serverVals\.pitchDetails\s*=\s*(\[.*?\])\s*(?:;|$)', text, re.DOTALL)
                    if match:
                        pitch_data = json.loads(match.group(1))
                        if pitch_data:
                            target_year = try_year
                            break
            except:
                continue

        if not pitch_data:
            return None

        player_display = resolved['name']
        team = 'FA'

        # Fetch player data to get team abbreviation
        try:
            async with session.get(f"{self.BASE_URL}/people/{pid}?hydrate=currentTeam") as resp:
                person_data = await resp.json()
                if person_data.get('people'):
                    team_id = person_data['people'][0].get('currentTeam', {}).get('id')
                    if team_id:
                        async with session.get(f"{self.BASE_URL}/teams/{team_id}") as team_resp:
                            team_data = await team_resp.json()
                            if team_data.get('teams'):
                                team = team_data['teams'][0].get('abbreviation', 'FA')
        except:
            pass

        # Map pitch types to full names
        pitch_name_map = {
            'FF': 'Four-Seam Fastball',
            'SI': 'Sinker',
            'FC': 'Cut Fastball',
            'CH': 'Changeup',
            'CU': 'Curveball',
            'SL': 'Slider',
            'KB': 'Knuckle Curve',
            'FS': 'Splitter',
            'KN': 'Knuckleball',
            'EP': 'Eephus',
            'ST': 'Sweeper',
            'GY': 'Gyroball',
            'SC': 'Screwball',
        }

        pitches = []
        for pitch in pitch_data:
            api_type = pitch.get('api_pitch_type', '')
            pitch_name = pitch_name_map.get(api_type)
            if not pitch_name:
                pitch_name = pitch.get('pitch_name', api_type if api_type else '?')

            pitches.append({
                'name': pitch_name,
                'type': api_type,
                'usage': pitch.get('pitch_percent', '0'),
                'whiff': pitch.get('whiff_percent', '0'),
                'k_pct': pitch.get('k_percent', '0'),
                'ba': pitch.get('ba', '.000'),
                'xba': pitch.get('xba', '.000'),
                'rv100': pitch.get('run_value', '0'),
                'hard_hit': pitch.get('hard_hit_percent', '0'),
                'avg_speed': pitch.get('release_speed', '0'),
            })

        return PitchArsenal(player_display, team, target_year, pitches)

    async def get_savant_leaderboard(self, stat: str, year: str = None, player_type: str = 'batter', count: int = 10) -> Optional[SavantLeaderboard]:
        """Fetch a Statcast leaderboard from Baseball Savant."""
        import io, csv
        session = await self.get_session()
        target_year = year or str(datetime.utcnow().year)
        
        stat_labels = {
            'exit_velocity_avg': 'Avg Exit Velocity',
            'barrel_batted_rate': 'Barrel %',
            'hard_hit_percent': 'Hard Hit %',
            'xba': 'Expected BA',
            'xslg': 'Expected SLG',
            'xwoba': 'Expected wOBA',
            'xobp': 'Expected OBP',
            'xera': 'Expected ERA',
            'k_percent': 'K %',
            'bb_percent': 'BB %',
            'whiff_percent': 'Whiff %',
            'chase_percent': 'Chase Rate',
            'sprint_speed': 'Sprint Speed',
            'outs_above_average': 'OAA',
            'arm_strength': 'Arm Strength',
            'launch_angle_avg': 'Avg Launch Angle',
            'sweet_spot_percent': 'Sweet Spot %',
            'bat_speed': 'Bat Speed',
            'swing_length': 'Swing Length',
        }
        
        title = stat_labels.get(stat, stat.replace('_', ' ').title())
        
        all_rows = []
        for try_year in ([target_year] if year else [target_year, str(int(target_year) - 1)]):
            url = f"https://baseballsavant.mlb.com/leaderboard/custom?year={try_year}&type={player_type}&min=50&selections={stat}&chart=false&csv=true"
            
            async with session.get(url) as resp:
                raw = await resp.read()
                text = raw.decode('utf-8-sig')
            
            reader = csv.DictReader(io.StringIO(text))
            all_rows = list(reader)
            
            if all_rows:
                target_year = try_year
                break
        
        if not all_rows:
            return None
        
        # Sort descending by stat value (higher = better for most stats)
        reverse = stat not in ['chase_percent', 'swing_length']  # lower is better for these
        all_rows.sort(key=lambda r: float(r.get(stat, 0) or 0), reverse=reverse)
        
        rows = []
        for r in all_rows[:count]:
            rows.append({
                'name': r.get('last_name, first_name', '?'),
                'value': r.get(stat, '0'),
            })
        
        return SavantLeaderboard(f"{target_year} {title} Leaders", stat, target_year, rows)

    async def get_box_score(self, team_query: str, date: str = None) -> Optional["BoxScoreData"]:
        """Fetch the box score for a team's game on a given date."""
        session = await self.get_session()

        # First find the game
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return None

        game = games[0]
        # Determine which side the team is on
        query = team_query.lower()
        aliases = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals", "dbacks": "diamondbacks", "barves": "braves"}
        query = aliases.get(query, query)
        if query in game.home.abbreviation.lower() or query in game.home.name.lower():
            side = "home"
        else:
            side = "away"

        # Fetch boxscore
        box_url = f"{self.BASE_URL}/game/{game.game_pk}/boxscore"
        async with session.get(box_url) as resp:
            box_data = await resp.json()

        team_data = box_data.get('teams', {}).get(side, {})
        players = box_data.get('teams', {}).get(side, {}).get('players', {})
        team_name = team_data.get('team', {}).get('name', '')
        team_abbrev = team_data.get('team', {}).get('abbreviation', '')

        # Parse batters
        batting_order = team_data.get('battingOrder', [])
        all_batters = team_data.get('batters', [])
        batting_rows = []
        order_pos = 0  # tracks position in the lineup (1-9)

        for batter_id in all_batters:
            p_data = players.get(f'ID{batter_id}', {})
            b_stats = p_data.get('stats', {}).get('batting', {})
            if not b_stats and 'atBats' not in b_stats:
                continue
            season_stats = p_data.get('seasonStats', {}).get('batting', {})

            name = p_data.get('person', {}).get('boxscoreName', 'Unknown')
            # Build position string from allPositions
            positions = p_data.get('allPositions', [])
            pos = "-".join(p.get('abbreviation', '') for p in positions) if positions else p_data.get('position', {}).get('abbreviation', '')

            is_starter = batter_id in batting_order
            if is_starter:
                order_pos += 1
                display_name = name
            else:
                display_name = " " + name  # indent substitutes

            batting_rows.append({
                'name': display_name,
                'pos': pos,
                'ab': str(b_stats.get('atBats', 0)),
                'r': str(b_stats.get('runs', 0)),
                'h': str(b_stats.get('hits', 0)),
                'rbi': str(b_stats.get('rbi', 0)),
                'bb': str(b_stats.get('baseOnBalls', 0)),
                'so': str(b_stats.get('strikeOuts', 0)),
                'lob': str(b_stats.get('leftOnBase', 0)),
                'avg': season_stats.get('avg', '.000'),
                'obp': season_stats.get('obp', '.000'),
                'slg': season_stats.get('slg', '.000'),
                'is_starter': is_starter,
            })
            # Stop after 9 starters have been listed (remaining are subs already handled)
            if is_starter and order_pos >= 9:
                # Include remaining non-starters that follow
                remaining_idx = all_batters.index(batter_id) + 1
                for rem_id in all_batters[remaining_idx:]:
                    if rem_id in batting_order:
                        break  # shouldn't happen, but safety
                    rem_data = players.get(f'ID{rem_id}', {})
                    rem_stats = rem_data.get('stats', {}).get('batting', {})
                    if not rem_stats:
                        continue
                    rem_season = rem_data.get('seasonStats', {}).get('batting', {})
                    rem_name = rem_data.get('person', {}).get('boxscoreName', 'Unknown')
                    rem_positions = rem_data.get('allPositions', [])
                    rem_pos = "-".join(p.get('abbreviation', '') for p in rem_positions) if rem_positions else ''
                    batting_rows.append({
                        'name': " " + rem_name,
                        'pos': rem_pos,
                        'ab': str(rem_stats.get('atBats', 0)),
                        'r': str(rem_stats.get('runs', 0)),
                        'h': str(rem_stats.get('hits', 0)),
                        'rbi': str(rem_stats.get('rbi', 0)),
                        'bb': str(rem_stats.get('baseOnBalls', 0)),
                        'so': str(rem_stats.get('strikeOuts', 0)),
                        'lob': str(rem_stats.get('leftOnBase', 0)),
                        'avg': rem_season.get('avg', '.000'),
                        'obp': rem_season.get('obp', '.000'),
                        'slg': rem_season.get('slg', '.000'),
                        'is_starter': False,
                    })
                break

        # Parse pitchers
        pitching_rows = []
        for pitcher_id in team_data.get('pitchers', []):
            p_data = players.get(f'ID{pitcher_id}', {})
            p_stats = p_data.get('stats', {}).get('pitching', {})
            if not p_stats:
                continue
            season_stats = p_data.get('seasonStats', {}).get('pitching', {})
            name = p_data.get('person', {}).get('boxscoreName', 'Unknown')
            dec = p_stats.get('note', '')

            pitching_rows.append({
                'name': name,
                'ip': str(p_stats.get('inningsPitched', '0')),
                'h': str(p_stats.get('hits', 0)),
                'r': str(p_stats.get('runs', 0)),
                'er': str(p_stats.get('earnedRuns', 0)),
                'bb': str(p_stats.get('baseOnBalls', 0)),
                'so': str(p_stats.get('strikeOuts', 0)),
                'hr': str(p_stats.get('homeRuns', 0)),
                'era': season_stats.get('era', '-.--'),
                'p': str(p_stats.get('pitchesThrown', 0)),
                's': str(p_stats.get('strikes', 0)),
                'dec': dec,
            })

        # Parse pitching notes
        pitching_notes = box_data.get('pitchingNotes', [])

        # Parse team notes (batting/fielding/baserunning)
        team_notes = team_data.get('info', [])

        # Parse game info (weather, umpires, etc.)
        game_info_raw = box_data.get('info', [])
        game_info = []
        abs_info = []
        for info in game_info_raw:
            label = info.get('label', '').upper()
            if 'ABS' in label or 'CHALLENGE' in label:
                abs_info.append(info)
            else:
                game_info.append(info)

        # Build title
        opp_side = "away" if side == "home" else "home"
        opp_abbrev = box_data.get('teams', {}).get(opp_side, {}).get('team', {}).get('abbreviation', '??')
        if side == "home":
            title = f"{opp_abbrev} @ {team_abbrev}"
        else:
            title = f"{team_abbrev} @ {box_data.get('teams', {}).get('home', {}).get('team', {}).get('abbreviation', '??')}"

        return BoxScoreData(
            title=title,
            team_name=team_name,
            team_abbrev=team_abbrev,
            batting_rows=batting_rows,
            pitching_rows=pitching_rows,
            pitching_notes=pitching_notes,
            team_notes=team_notes,
            game_info=game_info,
            abs_info=abs_info,
            game_status=game.status,
            game_abstract_state=game.abstract_state,
        )

    async def get_leaders(self, stat: str, stat_group: str = None, team_id: str = None, year: str = None, league: str = None, player_pool: str = None, position: str = None, reverse: bool = False) -> List["Leader"]:
        session = await self.get_session()
        
        stat_group = stat_group or "hitting"
        season = year or datetime.utcnow().year
        if not year and datetime.utcnow().month < 3:
            season -= 1

        # Use the stats/season endpoint which allows fetching a larger pool to sort manually
        params = {
            "stats": "season",
            "group": stat_group,
            "sportId": "1",
            "season": season,
            "limit": 200, # Fetch enough to cover all qualified players
            "playerPool": player_pool.upper() if player_pool else "QUALIFIED"
        }
        
        if team_id:
            params["teamId"] = team_id

        query_string = urllib.parse.urlencode(params)
        
        if league and league.lower() in ["al", "nl"]:
            query_string += f"&leagueId={'103' if league.lower() == 'al' else '104'}"

        if position:
            if position.upper() == "OF":
                query_string += "&position=LF&position=CF&position=RF&position=OF"
            else:
                query_string += f"&position={position.upper()}"
                
        url = f"{self.BASE_URL}/stats?{query_string}"
        
        async with session.get(url) as resp:
            data = await resp.json()
            
        if not data.get("stats") or not data["stats"][0].get("splits"):
            return []
            
        splits = data["stats"][0]["splits"]
        abbrev_map = await self.get_team_abbrevs()
        
        # Mapping for common stat keys

        stat_keys = {
            "battingAverage": "avg",
            "earnedRunAverage": "era",
            "runsBattedIn": "rbi",
            "onBasePercentage": "obp",
            "sluggingPercentage": "slg",
            "onBasePlusSlugging": "ops",
            "walksAndHitsPerInningPitched": "whip",
            "strikeouts": "strikeOuts",
            "stolenBases": "stolenBases",
            "wins": "wins",
            "saves": "saves",
            "homeRuns": "homeRuns",
            "hits": "hits",
            "runs": "runs",
            "walks": "baseOnBalls",
            "gamesPlayed": "gamesPlayed",
            "totalBases": "totalBases",
            "atBats": "atBats",
            "doubles": "doubles",
            "triples": "triples"
        }
        
        api_key = stat_keys.get(stat, stat)
        
        def safe_float(val):
            try:
                txt = str(val).replace(',', '')
                if txt.startswith('.'):
                    txt = "0" + txt
                return float(txt)
            except:
                return -999999.0

        # Determine if higher is better
        lower_is_better = ["era", "earnedRunAverage", "whip", "walksAndHitsPerInningPitched", "runsAllowed", "hitsAllowed", "walksAllowed", "hitsPer9Inn", "walksPer9Inn", "homeRunsPer9Inn"]
        hi_to_lo = api_key not in lower_is_better
        
        if reverse:
            hi_to_lo = not hi_to_lo
            
        splits.sort(key=lambda x: safe_float(x.get("stat", {}).get(api_key, 0)), reverse=hi_to_lo)
        
        # Take top 10 after sort
        leaders = []
        for i, s in enumerate(splits[:10]):
            rank = i + 1
            stat_obj = s.get("stat", {})
            value = str(stat_obj.get(api_key, ""))
            player_name = s.get("player", {}).get("fullName", "Unknown")
            team_id = s.get("team", {}).get("id")
            team_abbrev = abbrev_map.get(team_id, "??")
            pos_abbrev = s.get("position", {}).get("abbreviation", "")
            leaders.append(Leader(rank, player_name, team_abbrev, value, pos_abbrev))

            
        return leaders


    async def get_team_leaders(self, stat: str, stat_group: str, league: str = None, year: str = None, reverse: bool = False) -> List["Leader"]:

        session = await self.get_session()
        
        # Translate player stat key to team stat key
        team_stat_keys = {
            "battingAverage": "avg",
            "runsBattedIn": "rbi",
            "onBasePercentage": "obp",
            "sluggingPercentage": "slg",
            "onBasePlusSlugging": "ops",
            "walks": "baseOnBalls",
            "strikeouts": "strikeOuts",
            "earnedRunAverage": "era",
            "walksAndHitsPerInningPitched": "whip"
        }
        team_stat_key = team_stat_keys.get(stat, stat)
        
        # Fetch data
        if year:
            season = year
        else:
            season = datetime.utcnow().year
            if datetime.utcnow().month < 3:
                season -= 1
            
        url = f"{self.BASE_URL}/teams/stats?season={season}&sportId=1&group={stat_group}&stats=season"

        
        async with session.get(url) as resp:
            data = await resp.json()
            
        if not data.get("stats") or not data["stats"][0].get("splits"):
            return []
            
        teams = data["stats"][0]["splits"]
        
        # Filter by league
        if league:
            teams = [t for t in teams if str(t.get("team", {}).get("league", {}).get("id", "")) == league or str(t.get("league", {}).get("id", "")) == (league)]
            
        # Determine sort direction
        asc_pitching = {"era", "whip", "baseOnBalls", "hits", "runs", "homeRuns", "losses", "doubles", "triples", "avg", "obp", "slg", "ops"}
        reverse_sort = True
        if stat_group == "pitching" and team_stat_key in asc_pitching:
            reverse_sort = False
            
        if reverse:
            reverse_sort = not reverse_sort

            
        def safe_float(val):
            try:
                if isinstance(val, str):
                    val = val.replace(',', '')
                return float(val)
            except:
                return float('-inf') if reverse_sort else float('inf')

        teams.sort(key=lambda t: safe_float(t.get("stat", {}).get(team_stat_key, 0)), reverse=reverse_sort)
        
        leaders = []
        for i, team in enumerate(teams[:10]):
            rank = i + 1
            team_name = team.get("team", {}).get("name", "Unknown")
            value = str(team.get("stat", {}).get(team_stat_key, ""))
            leaders.append(Leader(rank, team_name, "", value, position=""))
            
        return leaders

    async def get_bullpen(self, team_query: str) -> Optional["BullpenData"]:
        """Fetch the bullpen availability and last 4 days of pitch counts."""
        session = await self.get_session()
        team_id = await self.get_team_id(team_query)
        if not team_id:
            return None

        now = datetime.utcnow() - timedelta(hours=5)
        # Look 6 days back and 3 days ahead for starters
        start_date = (now - timedelta(days=6)).strftime("%Y-%m-%d")
        end_date = (now + timedelta(days=3)).strftime("%Y-%m-%d")

        url = f"{self.BASE_URL}/schedule?sportId=1&teamId={team_id}&startDate={start_date}&endDate={end_date}"
        async with session.get(url) as resp:
            data = await resp.json()

        starter_ids = set()
        for date_obj in data.get('dates', []):
            for game_data in date_obj['games']:
                teams_data = game_data.get('teams', {})
                for side_key in ['away', 'home']:
                    t_data = teams_data.get(side_key, {})
                    # Handle both dict and potential nested structures from API
                    t_id = None
                    if isinstance(t_data.get('team'), dict):
                        t_id = t_data['team'].get('id')
                    elif isinstance(t_data.get('team'), int):
                        t_id = t_data['team']
                    
                    if t_id == team_id:
                        prob = t_data.get('probablePitcher', {})
                        if isinstance(prob, dict) and prob.get('id'):
                            starter_ids.add(prob['id'])


            
        # 1. Fetch hydrated roster for accurate handedness (L/R) and primary position
        roster_url = f"{self.BASE_URL}/teams/{team_id}/roster?hydrate=person"
        hand_map = {}
        sp_ids = set()
        async with session.get(roster_url) as resp:
            roster_data = await resp.json()
            for entry in roster_data.get('roster', []):
                pid = entry['person']['id']
                hand = entry['person'].get('pitchHand', {}).get('code', 'R')
                hand_map[pid] = hand
                if entry['person'].get('primaryPosition', {}).get('code') == 'SP':
                    sp_ids.add(pid)

        if not data.get('dates'):
            return None


        recent_games = []
        for date_obj in data['dates']:
            for game_data in date_obj['games']:
                recent_games.append({
                    'pk': game_data['gamePk'],
                    'date': date_obj['date'],
                })
        
        if not recent_games:
            return None
            
        recent_games.sort(key=lambda x: x['date'], reverse=True)
        
        # Filter out future games when determining 'latest_game' for the table columns
        today_str = now.strftime("%Y-%m-%d")
        past_and_today = [g for g in recent_games if g['date'] <= today_str]
        
        if not past_and_today:
            return None
            
        latest_game_pk = past_and_today[0]['pk']
        latest_date_obj = datetime.strptime(past_and_today[0]['date'], "%Y-%m-%d")


        
        # We look at the 4 days *prior* to the latest game for the TABLE COLUMNS
        past_dates = [(latest_date_obj - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 5)]
        past_dates.reverse() # Oldest first, so 4/7 4/8 4/9 4/10

        # Look 5 days back for starters specifically
        starter_dates = [(latest_date_obj - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 6)]


        box_url = f"{self.BASE_URL}/game/{latest_game_pk}/boxscore"
        async with session.get(box_url) as resp:
            box_data = await resp.json()

        side = 'away'
        if box_data.get('teams', {}).get('home', {}).get('team', {}).get('id') == team_id:
            side = 'home'

        team_info = box_data['teams'][side]
        bullpen_ids = team_info.get('bullpen', [])
        players_db = team_info.get('players', {})

        # Ensure today's starter and opponent's starters are caught if they are in the list
        for side_key in ['away', 'home']:
            t_side = box_data['teams'][side_key]
            if t_side.get('team', {}).get('id') == team_id:
                pitchers = t_side.get('pitchers', [])
                if pitchers:
                    starter_ids.add(pitchers[0])


        if not bullpen_ids:
            return None

        oldboxes = {}
        async def fetch_oldbox(pk, dt):
            b_url = f"{self.BASE_URL}/game/{pk}/boxscore"
            try:
                async with session.get(b_url) as b_resp:
                    b_data = await b_resp.json()
                    bside = 'away'
                    if b_data.get('teams', {}).get('home', {}).get('team', {}).get('id') == team_id:
                        bside = 'home'
                    
                    if dt not in oldboxes:
                        oldboxes[dt] = []
                    oldboxes[dt].append(b_data['teams'][bside])
            except Exception:
                pass

        tasks = []
        for g in recent_games:
            dt = g['date']
            # Fetch boxscores for both display columns AND starter identification range
            if dt in starter_dates or dt in past_dates:
                tasks.append(fetch_oldbox(g['pk'], dt))

        if tasks:
            await asyncio.gather(*tasks)

        bullpen_data = []
        starters = []
        short_dates = [f"{int(pd[5:7])}/{int(pd[8:10])}" for pd in past_dates]

        for pid in bullpen_ids:
            p_key = f"ID{pid}"
            player_info = players_db.get(p_key, {})
            name = player_info.get('person', {}).get('boxscoreName', 'Unknown')
            t_hand = hand_map.get(pid, player_info.get('person', {}).get('pitchHand', {}).get('code', 'R'))
            era = player_info.get('seasonStats', {}).get('pitching', {}).get('era', '-.--')
            
            row = {
                'name': name,
                't': t_hand,
                'era': era,
            }
            
            is_starter = (pid in starter_ids) or (pid in sp_ids)

            # Check past 5 days for starters (current game + 5 prior)
            for pd in starter_dates:
                if pd in oldboxes:
                    for old_team in oldboxes[pd]:
                        if pid in (old_team.get('pitchers', [])[:1]): # Check if they were the actual starter
                            is_starter = True
                            break

            for i, pd in enumerate(past_dates):
                short_pd = short_dates[i]
                total_pitches = 0
                if pd in oldboxes:
                    for old_team in oldboxes[pd]:
                        old_player = old_team.get('players', {}).get(p_key, {})
                        if old_player:
                            p_stats = old_player.get('stats', {}).get('pitching', {})
                            if p_stats and p_stats.get('pitchesThrown', 0) > 0:
                                total_pitches += p_stats['pitchesThrown']
                row[short_pd] = str(total_pitches) if total_pitches > 0 else ""


            if is_starter:
                starters.append(row)
            else:
                bullpen_data.append(row)

        return BullpenData(
            team_name=team_info.get('team', {}).get('name', 'Unknown Team'),
            past_dates=short_dates,
            bullpen=bullpen_data,
            starters=starters
        )

    async def get_savant_game_feed(self, team_query: str = None, player_id: str = None, date: str = None) -> dict:
        """
        Fetches the Baseball Savant game feed (exit velocity data) for a team's game.
        If player_id is given and team_query is None, resolves the player's current team first.
        Returns {'game_pk', 'status', 'away', 'home', 'exit_velocity': [...]}.
        """
        if team_query is None and player_id is not None:
            session = await self.get_session()
            async with session.get(f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam") as resp:
                person = (await resp.json()).get('people', [{}])[0] if resp.status == 200 else {}
            team_id = person.get('currentTeam', {}).get('id')
            if not team_id:
                return {}
            abbrevs = await self.get_team_abbrevs()
            team_query = abbrevs.get(team_id, '')

        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return {}
        game = games[0]

        session = await self.get_session()
        url = f"https://baseballsavant.mlb.com/gf?game_pk={game.game_pk}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        except Exception:
            return {}

        ev = data.get('exit_velocity', [])
        return {
            'game_pk': game.game_pk,
            'status': game.abstract_state,
            'away': game.away.abbreviation,
            'home': game.home.abbreviation,
            'exit_velocity': ev,
            'player_id': player_id,
            'scoreboard': data.get('scoreboard', {}),
        }

    async def get_pitcher_game_feed(self, team_query: str, date: str = None, player_id: int = None) -> dict:
        """
        Returns pitcher pitch data from the Baseball Savant game feed for a team.
        If player_id is given, only that pitcher's data is returned.
        Result: {'away': abbr, 'home': abbr, 'side': 'away'|'home',
                 'pitchers': [{'name', 'total', innings..., 'pitch_data': [...]}]}
        Each pitch_data entry is the raw savant pitch dict.
        """
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return {}
        game = games[0]

        # Determine which side this team is on
        query = team_query.lower()
        aliases = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals",
                   "dbacks": "diamondbacks", "barves": "braves"}
        query = aliases.get(query, query)
        away_name = game.away.name.lower()
        away_abbr = game.away.abbreviation.lower()
        if query == away_abbr or query in away_name:
            side = 'away'
        else:
            side = 'home'

        session = await self.get_session()
        url = f"https://baseballsavant.mlb.com/gf?game_pk={game.game_pk}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        except Exception:
            return {}

        savant_pitchers = data.get(f'{side}_pitchers', {})
        boxscore_pitchers = data.get('boxscore', {}).get('teams', {}).get(side, {}).get('pitchers', [])

        pitchers = []
        columns_seen = set()
        for pid in boxscore_pitchers:
            if player_id is not None and pid != player_id:
                continue
            pitcher_pitches = savant_pitchers.get(str(pid))
            if not pitcher_pitches:
                continue
            p = {'name': pitcher_pitches[0].get('pitcher_name', str(pid)), 'total': len(pitcher_pitches)}
            for pitch in pitcher_pitches:
                inn = str(pitch.get('inning', '?'))
                p[inn] = p.get(inn, 0) + 1
                columns_seen.add(inn)
            p['pitch_data'] = pitcher_pitches
            pitchers.append(p)

        return {
            'away': game.away.abbreviation,
            'home': game.home.abbreviation,
            'game_time': game.game_time_str,
            'status': game.abstract_state,
            'side': side,
            'inning_columns': sorted(columns_seen, key=lambda x: int(x) if x.isdigit() else 99),
            'pitchers': pitchers,
        }

    async def get_todays_games(self, team_query: str = None, date: str = None) -> List[Game]:
        session = await self.get_session()
        # Request all the expanded data your old bot was using
        url = f"{self.BASE_URL}/schedule?sportId=1&hydrate=team,linescore(matchup,runners),previousPlay,person,stats,lineups,probablePitcher,decisions,flags"
        if date:
            url += f"&date={date}"
        print(url)  # Debug: Print the URL being requested
        
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json()
            
        if not data.get('dates'):
            return []
            
        games = []
        for game_data in data['dates'][0]['games']:
            game = Game.from_api_json(game_data)
            
            # Filter by team if a search query was provided
            if team_query:
                query = team_query.lower()
                
                # Common aliases mapping to catch nicknames your old bot used
                aliases = {"nats": "nationals", "yanks": "yankees", "cards": "cardinals", "dbacks": "diamondbacks", "barves": "braves"}
                if query in aliases:
                    query = aliases[query]
                    
                away_name = game.away.name.lower()
                home_name = game.home.name.lower()
                away_abbr = game.away.abbreviation.lower()
                home_abbr = game.home.abbreviation.lower()
                
                if (query != away_abbr and query != home_abbr and 
                    query not in away_name and query not in home_name):
                    continue
            
            games.append(game)
            
        # The schedule endpoint strips hitData from previousPlay. We must fetch the 
        # playByPlay endpoint concurrently for any live games to get the Statcast metrics.
        async def fetch_pbp(g: Game):
            if g.abstract_state == "Live" and g.status != "Delayed":
                pbp_url = f"{self.BASE_URL}/game/{g.game_pk}/playByPlay"
                try:
                    async with session.get(pbp_url) as pbp_resp:
                        if pbp_resp.status == 200:
                            pbp_data = await pbp_resp.json()
                            all_plays = pbp_data.get('allPlays', [])
                            if all_plays:
                                last_play = all_plays[-1]
                                # Fallback to previous play if current play has no description yet
                                if 'result' in last_play and 'description' not in last_play['result'] and len(all_plays) > 1:
                                    last_play = all_plays[-2]
                                
                                desc = last_play.get('result', {}).get('description', g.last_play_desc)
                                g.last_play_desc = _bold_play_description(desc, last_play)
                                g.last_play_pitcher = last_play.get('matchup', {}).get('pitcher', {}).get('fullName', g.last_play_pitcher)
                                
                                for event in last_play.get('playEvents', []):
                                    if 'pitchData' in event:
                                        g.last_pitch_speed = event['pitchData'].get('startSpeed') or 0.0
                                        if 'details' in event and 'type' in event['details']:
                                            g.last_pitch_type = event['details']['type'].get('description', '')
                                    if 'hitData' in event:
                                        hd = event['hitData']
                                        g.statcast_dist = hd.get('totalDistance') or 0.0
                                        g.statcast_speed = hd.get('launchSpeed') or 0.0
                                        g.statcast_angle = hd.get('launchAngle') or 0.0
                except Exception as e:
                    print(f"Error fetching PBP for game {g.game_pk}: {e}")

        # Fetch boxscore for no-hitter/perfect game flagged games to get pitcher details
        async def fetch_nohit_pitchers(g: Game):
            if g.no_hitter or g.perfect_game:
                box_url = f"{self.BASE_URL}/game/{g.game_pk}/boxscore"
                try:
                    async with session.get(box_url) as box_resp:
                        if box_resp.status == 200:
                            box_data = await box_resp.json()
                            side = "home" if g.away.hits == 0 else "away"
                            pitcher_ids = box_data.get('teams', {}).get(side, {}).get('pitchers', [])
                            players = box_data.get('teams', {}).get(side, {}).get('players', {})
                            pitchers = []
                            for pid in pitcher_ids:
                                p_data = players.get(f'ID{pid}', {})
                                p_stats = p_data.get('stats', {}).get('pitching', {})
                                if p_stats:
                                    pitchers.append({
                                        'pitcher': p_data.get('person', {}).get('fullName', 'Unknown'),
                                        'ip': p_stats.get('inningsPitched', '0'),
                                        'bb': str(p_stats.get('baseOnBalls', 0)),
                                        'so': str(p_stats.get('strikeOuts', 0)),
                                        'np': str(p_stats.get('pitchesThrown', 0)),
                                    })
                            g.no_hitter_pitchers = pitchers
                except Exception as e:
                    print(f"Error fetching boxscore for no-hitter game {g.game_pk}: {e}")

        if games:
            await asyncio.gather(*(fetch_pbp(g) for g in games), *(fetch_nohit_pitchers(g) for g in games))
        return games

    async def get_player_pace_stats(self, player_id_or_name: str) -> Optional[PaceData]:

        session = await self.get_session()
        
        # 1. Resolve player ID
        player_id = None
        if player_id_or_name.isdigit():
            player_id = int(player_id_or_name)
        else:
            search_results = await self.search_players(player_id_or_name)
            if search_results:
                player_id = search_results[0].get('id')
        
        if not player_id:
            return None
            
        # 2. Get season stats
        url = f"{self.BASE_URL}/people/{player_id}/stats?stats=season&group=hitting,pitching&hydrate=team"
        async with session.get(url) as resp:
            data = await resp.json()
            
        if not data.get('stats'):
            return None
            
        hitting_split = None
        pitching_split = None
        
        for stat_obj in data['stats']:
            group = stat_obj.get('group', {}).get('displayName')
            if group == 'hitting' and stat_obj.get('splits'):
                hitting_split = stat_obj['splits'][-1]
            if group == 'pitching' and stat_obj.get('splits'):
                pitching_split = stat_obj['splits'][-1]
                
        if not hitting_split and not pitching_split:
            return None
            
        is_pitcher = pitching_split is not None
        split = pitching_split if is_pitcher else hitting_split
        
        roster_url = f"{self.BASE_URL}/people/{player_id}"
        async with session.get(roster_url) as resp:
            p_data = await resp.json()
            person = p_data.get('people', [{}])[0]
            pos_code = person.get('primaryPosition', {}).get('code', '')
            if pos_code != '1' and hitting_split:
                is_pitcher = False
                split = hitting_split
        
        player_name = person.get('fullName', 'Unknown')
        team_id = split.get('team', {}).get('id')
        team_abbrev = split.get('team', {}).get('abbreviation', '??')
        current_stats = split['stat']
        year = split['season']
        
        # 3. Get team games played
        team_gp = 1
        if team_id:
            team_url = f"{self.BASE_URL}/teams/{team_id}?hydrate=standings"
            async with session.get(team_url) as resp:
                t_data = await resp.json()
                team_obj = t_data.get('teams', [{}])[0]
                record = team_obj.get('record', {})
                team_gp = record.get('gamesPlayed', 1)
        
        if team_gp == 0: team_gp = 1
        
        # 4. Calculate projection
        projected = {}
        if is_pitcher:
            keys = ['gamesPitched', 'gamesStarted', 'wins', 'losses', 'saves', 'holds', 'strikeOuts', 'baseOnBalls', 'inningsPitched', 'battersFaced', 'hits', 'runs', 'earnedRuns', 'homeRuns']
        else:
            keys = ['gamesPlayed', 'plateAppearances', 'atBats', 'runs', 'hits', 'doubles', 'triples', 'homeRuns', 'rbi', 'baseOnBalls', 'strikeOuts', 'stolenBases', 'caughtStealing', 'intentionalWalks', 'hitByPitch']
            
        for k in keys:
            val = current_stats.get(k, 0)
            if isinstance(val, str) and k == 'inningsPitched':
                parts = val.split('.')
                pure_innings = float(parts[0])
                if len(parts) > 1:
                    pure_innings += float(parts[1]) / 3.0
                proj_pure = (pure_innings / team_gp) * 162
                whole = int(proj_pure)
                frac = proj_pure - whole
                outs = round(frac * 3)
                if outs == 3:
                    whole += 1
                    outs = 0
                projected[k] = f"{whole}.{outs}"
            else:
                try:
                    num = float(val)
                    proj = (num / team_gp) * 162
                    projected[k] = round(proj)
                except:
                    projected[k] = val
                    
        rate_keys = ['avg', 'obp', 'slg', 'ops', 'era', 'whip']
        for k in rate_keys:
            if k in current_stats:
                projected[k] = current_stats[k]

        return PaceData(
            player_id=player_id,
            player_name=player_name,
            team_abbrev=team_abbrev,
            team_gp=team_gp,
            is_pitcher=is_pitcher,
            current_stats=current_stats,
            projected_stats=projected,
            year=year,
            player_url=f"https://www.mlb.com/player/{player_id}"
        )

    async def get_zone_plot_data(self, player_id_or_name: str, year: str = None, chart_type: str = 'ba') -> Optional[dict]:
        """Fetch batting zone data from Baseball Savant for a given batter."""
        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return None
        player_id = resolved['id']
        player_name = resolved['name']

        from datetime import timezone
        target_year = year or str(datetime.now(timezone.utc).year)
        session = await self.get_session()
        url = (
            f"https://baseballsavant.mlb.com/visuals/sm"
            f"?pitch_type=&batter={player_id}&pitcher=&balls=&strikes="
            f"&year={target_year}&min_strikes=0&bucket_size=0.5&chart_type={chart_type}"
            f"&player_id={player_id}&position=6"
        )
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            cells = await resp.json(content_type=None)

        if not cells:
            return None

        return {
            'player_name': player_name,
            'player_id': player_id,
            'year': target_year,
            'chart_type': chart_type,
            'cells': cells,
        }

    async def get_team_id(self, query: str) -> Optional[int]:
        session = await self.get_session()
        url = f"{self.BASE_URL}/teams?sportId=1"
        try:
            async with session.get(url) as resp:
                data = await resp.json()
                query = query.lower()
                for team in data.get('teams', []):
                    if query in team['name'].lower() or query == team.get('abbreviation', '').lower():
                        return team['id']
        except:
            pass
        return None

    async def get_player_transactions(self, player_id_or_name: str, year: int = None) -> Optional[dict]:
        """Fetch all transactions for a player in a given year."""
        player = await self.resolve_player(player_id_or_name)
        if not player:
            return None

        if year is None:
            year = (datetime.utcnow() - timedelta(hours=5)).year

        session = await self.get_session()
        url = (
            f"{self.BASE_URL}/transactions"
            f"?playerId={player['id']}"
            f"&startDate={year}-01-01"
            f"&endDate={year}-12-31"
        )
        async with session.get(url) as resp:
            data = await resp.json()

        transactions = data.get('transactions', [])
        transactions.sort(key=lambda t: t.get('date', ''))
        return {'player': player, 'year': year, 'transactions': transactions}

