import aiohttp
import asyncio
import re
import urllib.parse
from typing import List, Optional
from datetime import datetime, timedelta

from core.utils import et_now, utc_to_et
# Data models and shared formatting helpers live in core.models; star-import +
# re-export so existing `from core.mlb_client import X` imports keep working.
from core.models import *  # noqa: F401,F403
from core.models import _bold_play_description  # noqa: F401


class MLBClient:
    BASE_URL = "https://statsapi.mlb.com/api/v1"

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._team_abbrevs: Optional[dict] = None
        self._milb_teams_cache: Optional[list] = None
        self.favorite_team_name: Optional[str] = None
        self.favorite_team_affiliates: list = []


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


    async def _team_abbrev(self, person: dict, fallback: str = 'FA') -> str:
        """Get team abbreviation from a person dict, falling back to ID lookup when missing."""
        abbrev = person.get('currentTeam', {}).get('abbreviation', '')
        if abbrev:
            return abbrev
        team_id = person.get('currentTeam', {}).get('id')
        if team_id:
            abbrevs = await self.get_team_abbrevs()
            return abbrevs.get(team_id, fallback)
        return fallback

    async def get_milb_teams(self) -> list:
        if self._milb_teams_cache:
            return self._milb_teams_cache
        session = await self.get_session()
        season = str(datetime.now().year)
        async with session.get(f"{self.BASE_URL}/teams?sportIds=11,12,13,14,15&season={season}") as resp:
            milb_data = await resp.json()
        async with session.get(f"{self.BASE_URL}/teams?sportId=1") as resp:
            mlb_data = await resp.json()
        mlb_abbrevs = {t['id']: t.get('abbreviation', '') for t in mlb_data.get('teams', [])}
        result = []
        for t in milb_data.get('teams', []):
            parent_id = t.get('parentOrgId')
            result.append({
                'id': t['id'],
                'name': t.get('name', ''),
                'abbreviation': t.get('abbreviation', ''),
                'level': t.get('sport', {}).get('name', ''),
                'parent_id': parent_id,
                'parent_name': t.get('parentOrgName', ''),
                'parent_abbrev': mlb_abbrevs.get(parent_id, ''),
            })
        self._milb_teams_cache = result
        return result

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
            affiliates = getattr(self, 'favorite_team_affiliates', [])
            fav_name = getattr(self, 'favorite_team_name', None)
            for p in players:
                team = p.get('name_display_club', '').lower()
                if (fav_name and fav_name in team) or any(aff in team for aff in affiliates):
                    return {'id': str(p['id']), 'name': p['name']}
            return {'id': str(players[0]['id']), 'name': players[0]['name']}
        else:
            # For MLB: favorite team first, then active MLB, then anyone
            fav_name = getattr(self, 'favorite_team_name', None)
            fav_match = None
            mlb_match = None
            for p in players:
                team = p.get('name_display_club', '')
                if not team:
                    continue
                if fav_name and fav_name in team.lower() and fav_match is None:
                    fav_match = p
                elif p.get('mlb') == 1 and mlb_match is None:
                    mlb_match = p

            best = fav_match or mlb_match or players[0]
            return {'id': str(best['id']), 'name': best['name']}

    async def get_team_id(self, team_query: str) -> Optional[int]:
        if not team_query: return None
        query = resolve_team_alias(team_query)
        
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

        now = et_now()
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

            content_dict = extract_highlight_videos(content_data)

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

    async def get_game_plays_for_inning(self, team_query: str, inning: int, date: str = None):
        """Return (game, plays, team_abbrev, team_half) for a team's batting side of a given inning."""
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return None, None, None, None

        game = games[0]
        team_id = await self.get_team_id(team_query)
        if not team_id:
            return game, [], game.away.abbreviation, 'top'

        team_side = 'away' if game.away.id == team_id else 'home'
        team_half = 'top' if team_side == 'away' else 'bottom'
        team_abbrev = game.away.abbreviation if team_side == 'away' else game.home.abbreviation

        session = await self.get_session()
        pbp_url = f"{self.BASE_URL}/game/{game.game_pk}/playByPlay"
        content_url = f"{self.BASE_URL}/game/{game.game_pk}/content"

        try:
            async with session.get(pbp_url) as resp:
                pbp_data = await resp.json() if resp.status == 200 else {}
            async with session.get(content_url) as resp:
                content_data = await resp.json() if resp.status == 200 else {}
        except Exception as e:
            print(f"Error fetching plays for inning: {e}")
            return game, [], team_abbrev, team_half

        content_dict = extract_highlight_videos(content_data)

        plays = []
        for play in pbp_data.get('allPlays', []):
            about = play.get('about', {})
            if about.get('halfInning') != team_half:
                continue
            if about.get('inning') != inning:
                continue

            result = play.get('result', {})
            desc = result.get('description', '')
            desc = _bold_play_description(desc, play)

            score_str = ''
            if 'awayScore' in result:
                score_str = f"({result['awayScore']}-{result['homeScore']})"

            count = play.get('count', {})
            balls = count.get('balls', 0)
            strikes = count.get('strikes', 0)
            outs_before = about.get('startOuts', 0)

            vid_url, vid_blurb = '', ''
            events = play.get('playEvents', [])
            if events:
                play_id = events[-1].get('playId', '')
                if play_id and play_id in content_dict:
                    vid_url = content_dict[play_id]['url']
                    vid_blurb = content_dict[play_id]['blurb']

            plays.append({
                'event': result.get('event', ''),
                'desc': desc,
                'score': score_str,
                'count': f"{balls}-{strikes}",
                'outs_before': outs_before,
                'is_scoring': about.get('isScoringPlay', False),
                'video_url': vid_url,
                'video_blurb': vid_blurb,
                'rbi': result.get('rbi', 0),
            })

        return game, plays, team_abbrev, team_half

    async def get_game_linescore_data(self, team_query: str, date: str = None):
        """Return (game, linescore_json) for a team's game."""
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return None, None

        game = games[0]
        session = await self.get_session()

        url = f"{self.BASE_URL}/game/{game.game_pk}/linescore"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return game, None
                return game, await resp.json()
        except Exception as e:
            print(f"Error fetching linescore: {e}")
            return game, None

    async def get_game_win_probability(self, team_query: str, date: str = None):
        """Return (game, wp_plays) for a team's game.

        wp_plays is the raw /winProbability play list, each entry carrying
        homeTeamWinProbability and homeTeamWinProbabilityAdded (WPA).
        """
        games = await self.get_todays_games(team_query=team_query, date=date)
        if not games:
            return None, None

        game = games[0]
        session = await self.get_session()

        url = f"{self.BASE_URL}/game/{game.game_pk}/winProbability"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return game, None
                return game, await resp.json()
        except Exception as e:
            print(f"Error fetching win probability: {e}")
            return game, None

    async def get_recent_home_runs(self, date: str = None) -> List[dict]:
        session = await self.get_session()
        date_str = date or et_now().strftime("%Y-%m-%d")

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

            content_dict = extract_highlight_videos(content_data)

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
                hr_num = parse_hr_number(desc)

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
        
        headshot_url = player_headshot_url(player_id)

        # Fetch player info to find out what team they are currently on
        person_url = f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam,team"
        async with session.get(person_url) as resp:
            person_data = await resp.json()

        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        team_id = person.get('currentTeam', {}).get('id')
        team_abbrev = await self._team_abbrev(person, 'TEAM')

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
        game_year = int(game_date[:4])
        game_date_formatted = f"{int(game_date[5:7])}/{int(game_date[8:10])}" + (f"/{game_year}" if game_year != datetime.now().year else "")

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
            if pitching and pitching.get('inningsPitched', '0.0') == '0.0' and pitching.get('battersFaced', 0) == 0:
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
                        pbp_data = (await resp.json() if resp.status == 200 else {}) or {}
                    async with session.get(content_url) as resp:
                        content_data = (await resp.json() if resp.status == 200 else {}) or {}
                except Exception as e:
                    print(f"Error fetching AB data: {e}")
                    pbp_data, content_data = {}, {}
                    
                content_dict = extract_highlight_videos(content_data)

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

        headshot_url = player_headshot_url(player_id)

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
        info_line = build_player_info_line(person)

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

        stat_types_to_fetch = stat_groups_for(pos, stat_type)

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
        team_abbrev = await self._team_abbrev(person)

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

    async def get_player_last_games(self, player_id_or_name: str, num_games: int = 10, stat_type: str = None, milb: bool = False, days: int = None) -> List[PlayerSeasonStats]:
        """Fetch a player's aggregated stats over their last N games (or last N days) using lastXGames or byDateRange."""
        session = await self.get_session()

        resolved = await self.resolve_player(player_id_or_name, milb=milb)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']

        headshot_url = player_headshot_url(player_id)

        if days is not None:
            # byDateRange: fetch person info and stats separately
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=days)
            end_date = end_dt.strftime('%Y-%m-%d')
            start_date = start_dt.strftime('%Y-%m-%d')
            label = f"Last {days} Days"

            person_url = f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam"
            async with session.get(person_url) as resp:
                person_data = await resp.json()
            if not person_data.get('people'):
                return []
            person = person_data['people'][0]
            player_name = person.get('fullName', player_name)
            pos = person.get('primaryPosition', {}).get('abbreviation', '')
            team_abbrev = await self._team_abbrev(person)

            info_line = build_player_info_line(person, bt_label=True)
            stat_types_to_fetch = stat_groups_for(pos, stat_type)

            milb_suffix = "&leagueListId=milb_all" if milb else ""
            results = []
            for st in stat_types_to_fetch:
                url = (f"{self.BASE_URL}/people/{player_id}/stats"
                       f"?stats=byDateRange&startDate={start_date}&endDate={end_date}&group={st}{milb_suffix}")
                async with session.get(url) as resp:
                    data = await resp.json()
                found_stats = []
                effective_team = team_abbrev
                for stat_block in data.get('stats', []):
                    splits = stat_block.get('splits', [])
                    if splits:
                        s = splits[-1].get('stat', {})
                        split_abbrev = splits[-1].get('team', {}).get('abbreviation', '')
                        if split_abbrev:
                            effective_team = split_abbrev
                        s['team'] = split_abbrev or team_abbrev
                        found_stats.append(s)
                if found_stats:
                    results.append(PlayerSeasonStats(
                        player_name=player_name,
                        team_abbrev=effective_team,
                        stat_type=st,
                        years=label,
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
                        years=label,
                        is_career=False,
                        info_line=info_line,
                        stats=[],
                        info_message=f"No {st} stats found for this player in the last {days} days.",
                        headshot_url=headshot_url,
                    ))
            return results

        # lastXGames via person hydrate
        league_list_id = "milb_all" if milb else "mlb_hist"
        person_url = (
            f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam,team,"
            f"stats(type=[lastXGames](team(league)),leagueListId={league_list_id},limit={num_games},group=[hitting,pitching])"
        )
        async with session.get(person_url) as resp:
            person_data = await resp.json()

        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        pos = person.get('primaryPosition', {}).get('abbreviation', '')
        team_abbrev = await self._team_abbrev(person)

        info_line = build_player_info_line(person, bt_label=True)
        stat_types_to_fetch = stat_groups_for(pos, stat_type)

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

    async def get_player_game_log(self, player: str, n: int = 5, before_date: str = None, milb: bool = False) -> Optional[PlayerGameLogData]:
        session = await self.get_session()
        resolved = await self.resolve_player(player, milb=milb)
        if not resolved:
            return None
        player_id = resolved['id']
        player_name = resolved['name']
        headshot_url = player_headshot_url(player_id)

        # Fetch person info and teams list concurrently
        person_url = f"{self.BASE_URL}/people/{player_id}?hydrate=currentTeam"
        teams_sport_ids = "11,12,13,14,15" if milb else "1"
        teams_url = f"{self.BASE_URL}/teams?sportIds={teams_sport_ids}" if milb else f"{self.BASE_URL}/teams?sportId=1"
        async with session.get(person_url) as resp:
            person_data = await resp.json()
        async with session.get(teams_url) as resp:
            teams_data = await resp.json()

        person = person_data.get('people', [{}])[0]
        pos_code = person.get('primaryPosition', {}).get('code', '')
        player_name = person.get('fullName', player_name)

        # Build team ID → abbreviation lookup
        team_abbrev_map = {t['id']: t['abbreviation'] for t in teams_data.get('teams', []) if 'id' in t and 'abbreviation' in t}

        current_team_id = person.get('currentTeam', {}).get('id')
        team_abbrev = team_abbrev_map.get(current_team_id, '??') if current_team_id else '??'

        season = str(datetime.now().year)
        hitting_splits = []
        pitching_splits = []
        milb_suffix = "&leagueListId=milb_all" if milb else ""
        for grp, target in [('hitting', hitting_splits), ('pitching', pitching_splits)]:
            url = f"{self.BASE_URL}/people/{player_id}/stats?stats=gameLog&season={season}&group={grp}{milb_suffix}"
            async with session.get(url) as resp:
                data = await resp.json()
            for stat_block in data.get('stats', []):
                target.extend(stat_block.get('splits', []))

        # pos_code '1' = pitcher; 'TWP' = two-way
        if pos_code == '1':
            position_type = 'pitching'
            splits = pitching_splits if pitching_splits else hitting_splits
        else:
            position_type = 'hitting'
            splits = hitting_splits if hitting_splits else pitching_splits

        if not splits:
            return None

        # Sort by date ascending (API usually is, but ensure it)
        splits.sort(key=lambda s: s.get('date', ''))

        if before_date:
            splits = [s for s in splits if s.get('date', '') <= before_date]

        recent = splits[-n:][::-1]  # newest first

        rows = []
        for split in recent:
            date_str = split.get('date', '')
            try:
                dt = datetime.strptime(date_str, '%Y-%m-%d')
                date_fmt = f"{dt.month}-{dt.day}"
            except Exception:
                date_fmt = date_str
            opp_id = split.get('opponent', {}).get('id')
            opp = team_abbrev_map.get(opp_id, '?') if opp_id else '?'
            stat = split.get('stat', {})
            if position_type == 'hitting':
                rows.append({
                    'date': date_fmt,
                    'opp': opp,
                    'ab': str(stat.get('atBats', 0)),
                    'r': str(stat.get('runs', 0)),
                    'h': str(stat.get('hits', 0)),
                    '2b': str(stat.get('doubles', 0)),
                    '3b': str(stat.get('triples', 0)),
                    'hr': str(stat.get('homeRuns', 0)),
                    'rbi': str(stat.get('rbi', 0)),
                    'bb': str(stat.get('baseOnBalls', 0)),
                    'so': str(stat.get('strikeOuts', 0)),
                    'lob': str(stat.get('leftOnBase', 0)),
                    'avg': stat.get('avg', '.000'),
                    'obp': stat.get('obp', '.000'),
                    'slg': stat.get('slg', '.000'),
                    'ops': stat.get('ops', '.000'),
                })
            else:
                rows.append({
                    'date': date_fmt,
                    'opp': opp,
                    'ip': str(stat.get('inningsPitched', '0')),
                    'h': str(stat.get('hits', 0)),
                    'r': str(stat.get('runs', 0)),
                    'er': str(stat.get('earnedRuns', 0)),
                    'bb': str(stat.get('baseOnBalls', 0)),
                    'so': str(stat.get('strikeOuts', 0)),
                    'hr': str(stat.get('homeRuns', 0)),
                    'p': str(stat.get('numberOfPitches', 0)),
                    's': str(stat.get('strikes', 0)),
                    'dec': 'W' if stat.get('wins') else 'L' if stat.get('losses') else 'S' if stat.get('saves') else '',
                })

        return PlayerGameLogData(
            player_id=player_id,
            player_name=player_name,
            team_abbrev=team_abbrev,
            headshot_url=headshot_url,
            position_type=position_type,
            rows=rows,
        )

    async def get_player_splits(self, player_id_or_name: str, sit_code: str, year: str = None, stat_type: str = None) -> List[PlayerSeasonStats]:
        session = await self.get_session()

        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return []
        player_id = resolved['id']
        player_name = resolved['name']

        headshot_url = player_headshot_url(player_id)

        async with session.get(f"{self.BASE_URL}/people/{player_id}") as resp:
            person_data = await resp.json()
        if not person_data.get('people'):
            return []

        person = person_data['people'][0]
        player_name = person.get('fullName', player_name)
        pos = person.get('primaryPosition', {}).get('abbreviation', '')
        team_abbrev = await self._team_abbrev(person)

        info_line = f"{pos}  |  {person.get('batSide', {}).get('code', '')}/{person.get('pitchHand', {}).get('code', '')}  |  {person.get('height', '')}  |  {person.get('weight', '')} lbs"

        if not stat_type:
            stat_type = "pitching" if pos == "P" else "hitting"

        season = year or str(datetime.now().year)

        _month_abbrevs = {'3':'Mar','4':'Apr','5':'May','6':'Jun','7':'Jul','8':'Aug','9':'Sep','10':'Oct'}
        _pos_abbrevs   = {'p1':'P','p2':'C','p3':'1B','p4':'2B','p5':'3B','p6':'SS','p7':'LF','p8':'CF','p9':'RF','pD':'DH','pH':'PH'}
        all_months     = sit_code == "all_months"
        all_positions  = sit_code == "all_positions"
        if all_months:
            api_sit_code = "3,4,5,6,7,8,9,10"
        elif all_positions:
            api_sit_code = "p1,p2,p3,p4,p5,p6,p7,p8,p9,pD,pH"
        else:
            api_sit_code = sit_code

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
        elif all_positions:
            stat_rows = []
            for sp in splits:
                code = sp.get('split', {}).get('code', '')
                s = sp.get('stat', {})
                s['season'] = _pos_abbrevs.get(code, code)
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
            team_abbrev = await self._team_abbrev(person)
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
                team_id = person.get('currentTeam', {}).get('id')
                if team_id:
                    abbrevs = await self.get_team_abbrevs()
                    team_abbrev = abbrevs.get(team_id, '')
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
        
        target_year = str(year) if year else str(et_now().year)
        
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
        return PlayerPercentiles(resolved['name'], team_abbrev, str(year_stats.get('year')), stat_type, table_rows, player_id=pid)

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
        target_year = year or str(et_now().year)

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
        target_year = year or str(et_now().year)
        
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
        query = resolve_team_alias(team_query)
        if query in game.home.abbreviation.lower() or query in game.home.name.lower():
            side = "home"
        else:
            side = "away"

        # Fetch boxscore
        box_url = f"{self.BASE_URL}/game/{game.game_pk}/boxscore"
        async with session.get(box_url) as resp:
            box_data = await resp.json()

        box = parse_box_score_side(box_data, side)
        box.game_status = game.status
        box.game_abstract_state = game.abstract_state
        box.game_date = game.game_date_str
        return box

    async def get_milb_box_score(self, team_id: int, date: str = None) -> Optional["BoxScoreData"]:
        session = await self.get_session()
        sched_url = f"{self.BASE_URL}/schedule?sportIds=11,12,13,14,15&teamId={team_id}"
        if date:
            sched_url += f"&date={date}"
        async with session.get(sched_url) as resp:
            sched_data = await resp.json()
        dates = sched_data.get('dates', [])
        if not dates or not dates[0].get('games'):
            return None
        game_data = dates[0]['games'][0]
        game_pk = game_data['gamePk']
        game_status = game_data.get('status', {}).get('detailedState', '')
        game_abstract = game_data.get('status', {}).get('abstractGameState', '')
        raw_date = dates[0].get('date', '')
        try:
            parsed = datetime.strptime(raw_date, '%Y-%m-%d')
            fmt = '%A, %b %d, %Y' if parsed.year != datetime.now().year else '%A, %b %d'
            game_date = parsed.strftime(fmt).replace(' 0', ' ')
        except Exception:
            game_date = raw_date

        home_team_id = game_data.get('teams', {}).get('home', {}).get('team', {}).get('id')
        side = 'home' if home_team_id == team_id else 'away'

        box_url = f"{self.BASE_URL}/game/{game_pk}/boxscore"
        async with session.get(box_url) as resp:
            box_data = await resp.json()

        box = parse_box_score_side(box_data, side)
        box.game_status = game_status
        box.game_abstract_state = game_abstract
        box.game_date = game_date
        return box

    async def get_leaders(self, stat: str, stat_group: str = None, team_id: str = None, year: str = None, league: str = None, player_pool: str = None, position: str = None, reverse: bool = False) -> List["Leader"]:
        session = await self.get_session()
        
        stat_group = stat_group or "hitting"
        season = year or et_now().year
        if not year and et_now().month < 3:
            season -= 1

        # Determine sort direction up front so the API returns the correct end of
        # the FULL pool. Sorting only locally works when every player is in the
        # response, but large pools (ALL/ROOKIES) exceed the limit and come back
        # as an arbitrary truncated slice — reversing that drops players.
        lower_is_better = {
            "earnedRunAverage", "era", "walksAndHitsPerInningPitched", "whip",
            "hitsPer9Inn", "walksPer9Inn", "homeRunsPer9", "homeRunsPer9Inn",
            "runsScoredPer9", "runsAllowed", "hitsAllowed", "walksAllowed",
        }
        hi_to_lo = stat not in lower_is_better
        if reverse:
            hi_to_lo = not hi_to_lo

        # Use the stats/season endpoint, sorted server-side (sortStat/order) so the
        # top — or bottom, when reversed — of the full pool comes back first
        # regardless of how many players the pool contains.
        params = {
            "stats": "season",
            "group": stat_group,
            "sportId": "1",
            "season": season,
            "limit": 200,
            "playerPool": player_pool.upper() if player_pool else "QUALIFIED",
            "sortStat": stat,
            "order": "desc" if hi_to_lo else "asc",
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

        # Re-sort locally as a safety net for ties and any stat the API doesn't
        # sort server-side; hi_to_lo was already computed above for the request.
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
            season = et_now().year
            if et_now().month < 3:
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

        now = et_now()
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
            team_abbrev=team_info.get('team', {}).get('abbreviation', ''),
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
        query = resolve_team_alias(team_query)
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

    async def get_milb_games(self, team_query: str, date: str = None, level_filter: str = None):
        """Fetch MiLB games for a team_query (org:{id}, team:{id}, or name/abbrev fallback).
        Returns (List[Game], label_str)."""
        session = await self.get_session()
        season = str(datetime.now().year)

        async with session.get(f"{self.BASE_URL}/teams?sportId=1") as resp:
            mlb_data = await resp.json()
        async with session.get(f"{self.BASE_URL}/teams?sportIds=11,12,13,14,15&season={season}") as resp:
            milb_data = await resp.json()

        mlb_teams = mlb_data.get('teams', [])
        milb_teams = milb_data.get('teams', [])

        if team_query.isdigit():
            team_id = int(team_query)
            mlb_match = next((t for t in mlb_teams if t['id'] == team_id), None)
            if mlb_match:
                label = f"{mlb_match.get('teamName', mlb_match['name'])} Affiliates"
                affiliate_ids = [t['id'] for t in milb_teams if t.get('parentOrgId') == team_id]
                if not affiliate_ids:
                    return [], label
                team_id_param = ','.join(str(i) for i in affiliate_ids)
            else:
                milb_match = next((t for t in milb_teams if t['id'] == team_id), None)
                if not milb_match:
                    return [], ""
                label = milb_match['name']
                team_id_param = str(team_id)
        else:
            query = team_query.strip().lower()
            mlb_match = next(
                (t for t in mlb_teams if query == t.get('abbreviation', '').lower()
                 or query in t.get('name', '').lower()
                 or query in t.get('teamName', '').lower()),
                None
            )

            if mlb_match:
                org_id = mlb_match['id']
                label = f"{mlb_match.get('teamName', mlb_match['name'])} Affiliates"
                affiliate_ids = [t['id'] for t in milb_teams if t.get('parentOrgId') == org_id]
                if not affiliate_ids:
                    return [], label
                team_id_param = ','.join(str(i) for i in affiliate_ids)
            else:
                milb_match = next(
                    (t for t in milb_teams if query == t.get('abbreviation', '').lower()
                     or query in t.get('name', '').lower()
                     or query in t.get('teamName', '').lower()),
                    None
                )
                if not milb_match:
                    return [], ""
                label = milb_match['name']
                team_id_param = str(milb_match['id'])

        url = (f"{self.BASE_URL}/schedule?sportId=11,12,13,14,15"
               f"&teamId={team_id_param}"
               f"&hydrate=team,linescore(matchup,runners),probablePitcher,decisions")
        if date:
            url += f"&date={date}"

        async with session.get(url) as resp:
            data = await resp.json()

        if not data.get('dates'):
            return [], label

        level_abbrevs = {"aaa": "AAA", "aa": "AA", "a+": "A+", "high-a": "A+", "a": "A", "single-a": "A"}
        want_level = level_abbrevs.get(level_filter.lower(), level_filter.upper()) if level_filter else None

        games = []
        for game_data in data['dates'][0]['games']:
            game = Game.from_api_json(game_data)
            if want_level and game.level != want_level:
                continue
            games.append(game)

        return games, label

    async def get_todays_games(self, team_query: str = None, date: str = None) -> List[Game]:
        session = await self.get_session()
        # Request all the expanded data your old bot was using
        url = f"{self.BASE_URL}/schedule?sportId=1&hydrate=team,venue(location),linescore(matchup,runners),previousPlay,person,stats,lineups,probablePitcher,decisions,flags"
        if date:
            url += f"&date={date}"

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
                query = resolve_team_alias(team_query)

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

    async def get_player_transactions(self, player_id_or_name: str, year: int = None) -> Optional[dict]:
        """Fetch all transactions for a player in a given year."""
        player = await self.resolve_player(player_id_or_name)
        if not player:
            return None

        if year is None:
            year = et_now().year

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

    async def get_rolling_xwoba(self, player_id_or_name: str) -> Optional[dict]:
        """Fetch rolling xwOBA data from Savant for a batter.

        Returns {'player_name': str, 'team_abbrev': str, 'windows': {50: [...], 100: [...], 250: [...]}}
        where each list is [{'date': 'YYYY-MM-DD', 'xwoba': float}, ...] in chronological order.
        Returns None if the player is not found or has no data.
        """
        session = await self.get_session()
        resolved = await self.resolve_player(player_id_or_name)
        if not resolved:
            return None
        pid = resolved['id']

        team_abbrev = ""
        try:
            async with session.get(f"{self.BASE_URL}/people/{pid}?hydrate=currentTeam") as resp:
                pdata = (await resp.json()).get('people', [{}])[0]
                team_id = pdata.get('currentTeam', {}).get('id')
                if team_id:
                    abbrevs = await self.get_team_abbrevs()
                    team_abbrev = abbrevs.get(team_id, '')
        except Exception:
            pass

        url = f"https://baseballsavant.mlb.com/player-services/rolling-thumb?playerId={pid}&playerType=Y"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        def parse_window(entries):
            pts = []
            for e in entries:
                try:
                    pts.append({'date': e['max_game_date'][:10], 'xwoba': float(e['xwoba'])})
                except (KeyError, ValueError):
                    continue
            pts.reverse()  # API returns newest-first; flip to chronological
            return pts

        windows = {
            50:  parse_window(data.get('plate50',  [])),
            100: parse_window(data.get('plate100', [])),
            250: parse_window(data.get('plate250', [])),
        }
        if not any(windows.values()):
            return None

        return {'player_name': resolved['name'], 'team_abbrev': team_abbrev, 'windows': windows}


    async def get_daily_top_performances(self, date_str: str = None) -> Optional[dict]:
        """Return top hitter and pitcher performances for a given date (default: yesterday ET).

        Scores hitters with a weighted point system and pitchers using game score.
        Returns {'date': str, 'hitters': [...], 'pitchers': [...]} or None if no games found.
        Each entry has: name, team, opponent, score, summary.
        """
        session = await self.get_session()

        if date_str is None:
            date_str = (et_now() - timedelta(days=1)).strftime("%Y-%m-%d")

        url = f"{self.BASE_URL}/schedule?sportId=1&date={date_str}&hydrate=team"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            sched = await resp.json()

        game_infos = []
        for date_obj in sched.get("dates", []):
            for g in date_obj.get("games", []):
                if g.get("status", {}).get("abstractGameState") != "Final":
                    continue
                pk = g.get("gamePk")
                away = g["teams"]["away"]["team"].get("abbreviation", "???")
                home = g["teams"]["home"]["team"].get("abbreviation", "???")
                game_infos.append((pk, away, home))

        if not game_infos:
            return None

        async def _fetch_box(pk):
            try:
                async with session.get(f"{self.BASE_URL}/game/{pk}/boxscore") as r:
                    return await r.json() if r.status == 200 else None
            except Exception:
                return None

        boxes = await asyncio.gather(*(_fetch_box(pk) for pk, _, _ in game_infos))

        hitters = []
        pitchers = []

        for (pk, away_abbr, home_abbr), box in zip(game_infos, boxes):
            if box is None:
                continue
            for side, team_abbr, opp_abbr in [("away", away_abbr, home_abbr), ("home", home_abbr, away_abbr)]:
                side_hitters, side_pitchers = collect_team_performances(box.get("teams", {}).get(side, {}))
                for h in side_hitters:
                    hitters.append({"name": h["name"], "team": team_abbr, "opponent": opp_abbr,
                                    "score": h["score"], "summary": h["summary"]})
                for p in side_pitchers:
                    if p["outs"] < 15:  # require 5 IP minimum
                        continue
                    pitchers.append({"name": p["name"], "team": team_abbr, "opponent": opp_abbr,
                                     "score": p["score"], "summary": p["summary"]})

        hitters.sort(key=lambda x: x["score"], reverse=True)
        pitchers.sort(key=lambda x: x["score"], reverse=True)

        return {
            "date":     date_str,
            "hitters":  hitters[:7],
            "pitchers": pitchers[:3],
        }

    async def get_game_top_performers(self, game_pk: int, away_abbr: str, home_abbr: str) -> Optional[dict]:
        """Return top performers for a single game.

        Uses the same scoring as get_daily_top_performances.
        Returns {'hitters': [...], 'pitchers': [...]} or None on failure.
        Hitters: top 3 across both teams. Pitchers: game_score >= 64 (no IP floor).
        Each entry has: name, team, score, summary.
        """
        session = await self.get_session()
        try:
            async with session.get(f"{self.BASE_URL}/game/{game_pk}/boxscore") as r:
                if r.status != 200:
                    return None
                box = await r.json()
        except Exception:
            return None

        hitters = []
        pitchers = []

        for side, team_abbr, opp_abbr in [("away", away_abbr, home_abbr), ("home", home_abbr, away_abbr)]:
            side_hitters, side_pitchers = collect_team_performances(box.get("teams", {}).get(side, {}))
            for h in side_hitters:
                hitters.append({"name": h["name"], "team": team_abbr,
                                "score": h["score"], "summary": h["summary"]})
            for p in side_pitchers:
                if p["score"] < 64:
                    continue
                pitchers.append({"name": p["name"], "team": team_abbr,
                                 "score": p["score"], "summary": p["summary"]})

        hitters.sort(key=lambda x: x["score"], reverse=True)
        pitchers.sort(key=lambda x: x["score"], reverse=True)

        if not hitters and not pitchers:
            return None

        return {
            "hitters":  hitters[:3],
            "pitchers": pitchers,
        }

    async def get_milb_affiliate_top_performances(self, date_str: str, fav_team_abbrev: str) -> Optional[dict]:
        """Return top hitter/pitcher performances for a team's MiLB affiliates on date_str.

        Returns None if any affiliate game is still in progress or no games are scheduled.
        Returns {'date': str, 'hitters': [...], 'pitchers': [...]} when all games are Final.
        Each entry has: name, team, level, score, summary.
        """
        session = await self.get_session()
        milb_teams = await self.get_milb_teams()

        affiliate_ids = [t['id'] for t in milb_teams if t.get('parent_abbrev', '').upper() == fav_team_abbrev.upper()]
        if not affiliate_ids:
            return None

        level_map = {t['id']: LEVEL_ABBREVS.get(t.get('level', ''), t.get('level', '')) for t in milb_teams}
        team_level_map: dict = {}

        team_id_param = ','.join(str(i) for i in affiliate_ids)
        url = (f"{self.BASE_URL}/schedule?sportId=11,12,13,14,15"
               f"&teamId={team_id_param}&date={date_str}&hydrate=team")

        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            sched = await resp.json()

        game_infos = []
        for date_obj in sched.get('dates', []):
            for g in date_obj.get('games', []):
                if g.get('status', {}).get('abstractGameState') != 'Final':
                    return None  # A game is still in progress
                pk = g.get('gamePk')
                away_team = g['teams']['away']['team']
                home_team = g['teams']['home']['team']
                away_abbr = away_team.get('abbreviation', '???')
                home_abbr = home_team.get('abbreviation', '???')
                away_id = away_team.get('id')
                home_id = home_team.get('id')
                if away_id in level_map:
                    team_level_map[away_abbr] = level_map[away_id]
                if home_id in level_map:
                    team_level_map[home_abbr] = level_map[home_id]
                affiliate_side = "away" if away_id in affiliate_ids else "home"
                game_infos.append((pk, away_abbr, home_abbr, affiliate_side))

        if not game_infos:
            return None

        async def _fetch_box(pk):
            try:
                async with session.get(f"{self.BASE_URL}/game/{pk}/boxscore") as r:
                    return await r.json() if r.status == 200 else None
            except Exception:
                return None

        boxes = await asyncio.gather(*(_fetch_box(pk) for pk, _, _, _ in game_infos))

        hitters = []
        pitchers = []

        for (pk, away_abbr, home_abbr, affiliate_side), box in zip(game_infos, boxes):
            if box is None:
                continue
            team_abbr = away_abbr if affiliate_side == "away" else home_abbr
            level = team_level_map.get(team_abbr, "")

            side_hitters, side_pitchers = collect_team_performances(box.get("teams", {}).get(affiliate_side, {}))
            for h in side_hitters:
                hitters.append({"name": h["name"], "team": team_abbr, "level": level,
                                "score": h["score"], "summary": h["summary"]})
            for p in side_pitchers:
                if p["outs"] < 15:  # 5 IP minimum
                    continue
                pitchers.append({"name": p["name"], "team": team_abbr, "level": level,
                                 "score": p["score"], "summary": p["summary"]})

        hitters.sort(key=lambda x: x["score"], reverse=True)
        pitchers.sort(key=lambda x: x["score"], reverse=True)

        return {
            "date":     date_str,
            "hitters":  hitters[:7],
            "pitchers": pitchers[:3],
        }
