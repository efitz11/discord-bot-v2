"""
monitor.py — Live MLB game monitoring cog.

Posts to ALERT_CHANNEL_ID automatically when:
  1. A no-hitter or perfect game is in progress (updates every inning change).
  2. A notable home run is hit (≥420 ft, favorite team HR, ≤5-park HR, xBA < .200, or a
     batter's 2nd+ HR in the same game), once a highlight video is available (or after
     VIDEO_WAIT_MAX_CYCLES minutes with no video, e.g. alternate broadcasts that delay
     uploads). Multi-homer alerts list every HR that batter has hit so far that game.
  3. A walkoff play ends a game (all 30 teams); alert is posted immediately then edited
     with the highlight video once MLB uploads it (up to WALKOFF_VIDEO_WAIT_MAX_CYCLES
     minutes — walkoff clips are bundled with the final recap edit and take longer than
     routine highlights).
  4. FAVORITE_TEAM's starting lineup, as soon as MLB publishes it (within
     LINEUP_CHECK_HOURS of first pitch), in the /mlb box batting format.

Polling strategy:
  - On startup, fetches today's schedule to get all game PKs and start times.
  - The main loop runs every POLL_INTERVAL seconds.  During off-hours (no game
    starting within WAKEUP_WINDOW_MINUTES minutes and no game currently live) the
    loop skips the expensive per-game fetches, keeping API usage very low.
  - At midnight ET the daily schedule is refreshed automatically.
  - Each live game is fetched individually from the live feed endpoint
    (/api/v1.1/game/{pk}/feed/live) so we get complete, real-time play data in
    one call per game.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from core.mlb_client import extract_highlight_videos, format_table, parse_box_score_side, parse_hr_number, LEVEL_ABBREVS

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

ALERT_CHANNEL_ID     = int(os.getenv("ALERT_CHANNEL_ID", "0")) or None  # Set via env; None disables monitor alerts
POLL_INTERVAL        = 60                   # Seconds between monitor ticks
WAKEUP_WINDOW_MINUTES = 30                  # Start polling when a game is this close
HR_DISTANCE_THRESHOLD = 420                 # Feet — minimum projected distance for alert
HR_ALWAYS_ALERT_TEAM  = os.getenv("HR_ALERT_TEAM", "").upper() or None  # Always alert for this team's HRs regardless of distance
HR_PARKS_THRESHOLD    = 5                  # Alert if HR would only be a HR in ≤ this many parks
HR_XBA_THRESHOLD      = 0.200             # Alert if xBA is below this value
MULTI_HR_THRESHOLD    = 2                  # Alert once a batter has hit this many HRs in the same game
_STATE_DIR            = os.getenv("STATE_DIR", ".")
HR_STATE_FILE         = os.path.join(_STATE_DIR, "hr_posted.json")
NH_STATE_FILE         = os.path.join(_STATE_DIR, "nh_state.json")
SUMMARY_STATE_FILE    = os.path.join(_STATE_DIR, "summary_state.json")
WALKOFF_STATE_FILE    = os.path.join(_STATE_DIR, "walkoff_state.json")
CYCLE_STATE_FILE      = os.path.join(_STATE_DIR, "cycle_state.json")
DELAY_STATE_FILE      = os.path.join(_STATE_DIR, "delay_state.json")
LINEUP_STATE_FILE     = os.path.join(_STATE_DIR, "lineup_state.json")
LINEUP_CHECK_HOURS    = 6                   # Start polling for the lineup this many hours before first pitch
VIDEO_WAIT_MAX_CYCLES = 5                   # Poll cycles to wait for highlight video
WALKOFF_VIDEO_WAIT_MAX_CYCLES = 15          # Walkoff clips are bundled with the final recap edit and
                                             # routinely take longer than routine in-game highlights to publish
NH_ALERT_DELAY        = 15                  # Seconds to delay NH alerts (stream spoiler protection)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _et_now() -> datetime:
    """Return the current time in US/Eastern (UTC-4/UTC-5 dynamically)."""
    return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)


def _parse_game_time(game_date_str: str):
    """Parse the gameDate field (ISO 8601 UTC) into a naive ET datetime."""
    if not game_date_str:
        return None
    try:
        dt_utc = datetime.strptime(game_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt_utc.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except ValueError:
        return None


def _last_name(full_name: str) -> str:
    parts = full_name.split(" ", 1)
    return parts[1] if len(parts) == 2 else full_name


def _inning_label(inning: int, is_top: bool) -> str:
    half = "Top" if is_top else "Bot"
    n = inning if inning <= 20 else inning % 10
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
    return f"{half} {inning}{suffix}"


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class MonitorCog(commands.Cog):
    """Background task cog that monitors live MLB games and posts alerts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Today's games: {game_pk: {"start_et": datetime, "away": str, "home": str}}
        self._scheduled_games: dict = {}
        self._schedule_date = None   # YYYY-MM-DD string of the schedule we fetched

        # MiLB affiliate games: {game_pk: {"start_et", "away", "home", "abstract_state", "level"}}
        self._milb_scheduled_games: dict = {}
        self._milb_schedule_date = None

        # No-hitter tracking — loaded from disk so restarts don't lose state
        self._nh_alerted: dict = {}
        self._nh_broken_posted: set = set()

        # HR tracking
        self._hr_pending: dict = {}  # {hr_key: {"cycles_waited": int, "data": dict}}
        self._hr_posted: set = set() # hr_keys already posted
        self._hr_clear_date = None   # date string for which we've done the 6am clear

        # Walkoff tracking
        self._walkoff_pending: dict = {}  # {game_pk: {"cycles_waited": int, "data": dict, "message": Message|None}}
        self._walkoff_posted: set = set() # game_pks already posted
        self._walkoff_clear_date = None

        # Cycle tracking — keyed "{game_pk}_{batter_id}"
        self._cycle_posted: set = set()
        self._cycle_clear_date = None

        # Delay tracking for FAVORITE_TEAM — {game_pk: is_delayed} so we only
        # alert on transitions into/out of a delay
        self._delay_state: dict = {}
        self._delay_clear_date = None
        self._summary_posted_date = None       # date string for which we've posted the morning summary
        self._milb_summary_posted_date = None  # date string for which we've posted the MiLB affiliate summary
        self._milb_ready_since: "datetime | None" = None  # when all MLB+MiLB games first went Final
        self._game_errors_alerted: set = set()            # game_pks that have already raised error alerts today

        # Lineup auto-post tracking
        self._lineup_posted: set = set()  # game_pks whose lineup has been posted
        self._lineup_clear_date = None

        self._load_hr_state()
        self._load_nh_state()
        self._load_summary_state()
        self._load_walkoff_state()
        self._load_cycle_state()
        self._load_delay_state()
        self._load_lineup_state()
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    # ─────────────────────────────────────────────
    # Schedule helpers
    # ─────────────────────────────────────────────

    @staticmethod
    def _load_json(path):
        """Load a JSON state file; returns None if missing or unreadable."""
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_json(path: str, data, label: str) -> None:
        """Atomically write a JSON state file (tmp + rename)."""
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[monitor] failed to save {label} state: {e}")

    def _load_hr_state(self) -> None:
        data = self._load_json(HR_STATE_FILE)
        if data is None:
            self._hr_posted = set()
            return
        if isinstance(data, list):
            self._hr_posted = set(data)
            self._hr_clear_date = None
        else:
            self._hr_posted = set(data.get("posted", []))
            self._hr_clear_date = data.get("clear_date")
        print(f"[monitor] loaded {len(self._hr_posted)} posted HR key(s) from disk (clear_date={self._hr_clear_date})")

    def _save_hr_state(self) -> None:
        self._save_json(HR_STATE_FILE, {"posted": list(self._hr_posted), "clear_date": self._hr_clear_date}, "HR")

    def _load_summary_state(self) -> None:
        state = self._load_json(SUMMARY_STATE_FILE)
        if state is None:
            self._summary_posted_date = None
            self._milb_summary_posted_date = None
            return
        self._summary_posted_date = state.get("date")
        self._milb_summary_posted_date = state.get("milb_date")
        print(f"[monitor] loaded summary state: mlb={self._summary_posted_date} milb={self._milb_summary_posted_date}")

    def _save_summary_state(self) -> None:
        self._save_json(SUMMARY_STATE_FILE, {"date": self._summary_posted_date, "milb_date": self._milb_summary_posted_date}, "summary")

    def _load_nh_state(self) -> None:
        data = self._load_json(NH_STATE_FILE)
        if data is None:
            self._nh_alerted = {}
            self._nh_broken_posted = set()
            return
        # alert_key is stored as a list [inning, half] — restore as tuple (may be null)
        self._nh_alerted = {
            int(pk): {**v, "key": tuple(v["key"]) if v.get("key") is not None else None}
            for pk, v in data.get("nh_alerted", {}).items()
        }
        self._nh_broken_posted = set(int(pk) for pk in data.get("nh_broken_posted", []))
        print(f"[monitor] loaded NH state: {len(self._nh_alerted)} active, {len(self._nh_broken_posted)} broken")

    def _save_nh_state(self) -> None:
        data = {
            "nh_alerted": {
                str(pk): {**v, "key": list(v["key"]) if v.get("key") is not None else None}
                for pk, v in self._nh_alerted.items()
            },
            "nh_broken_posted": list(self._nh_broken_posted),
        }
        self._save_json(NH_STATE_FILE, data, "NH")

    def _load_walkoff_state(self) -> None:
        data = self._load_json(WALKOFF_STATE_FILE)
        if data is None:
            self._walkoff_posted = set()
            return
        self._walkoff_posted = set(int(pk) for pk in data.get("posted", []))
        self._walkoff_clear_date = data.get("clear_date")
        print(f"[monitor] loaded {len(self._walkoff_posted)} posted walkoff(s) from disk")

    def _save_walkoff_state(self) -> None:
        self._save_json(WALKOFF_STATE_FILE, {"posted": list(self._walkoff_posted), "clear_date": self._walkoff_clear_date}, "walkoff")

    def _load_cycle_state(self) -> None:
        data = self._load_json(CYCLE_STATE_FILE)
        if data is None:
            self._cycle_posted = set()
            return
        self._cycle_posted = set(data.get("posted", []))
        self._cycle_clear_date = data.get("clear_date")
        print(f"[monitor] loaded {len(self._cycle_posted)} posted cycle(s) from disk")

    def _save_cycle_state(self) -> None:
        self._save_json(CYCLE_STATE_FILE, {"posted": list(self._cycle_posted), "clear_date": self._cycle_clear_date}, "cycle")

    def _load_delay_state(self) -> None:
        data = self._load_json(DELAY_STATE_FILE)
        if data is None:
            self._delay_state = {}
            return
        self._delay_state = {str(k): bool(v) for k, v in data.get("state", {}).items()}
        self._delay_clear_date = data.get("clear_date")
        print(f"[monitor] loaded delay state for {len(self._delay_state)} game(s) from disk")

    def _save_delay_state(self) -> None:
        self._save_json(DELAY_STATE_FILE, {"state": self._delay_state, "clear_date": self._delay_clear_date}, "delay")

    def _load_lineup_state(self) -> None:
        data = self._load_json(LINEUP_STATE_FILE)
        if data is None:
            self._lineup_posted = set()
            return
        self._lineup_posted = set(int(pk) for pk in data.get("posted", []))
        self._lineup_clear_date = data.get("clear_date")
        print(f"[monitor] loaded {len(self._lineup_posted)} posted lineup(s) from disk")

    def _save_lineup_state(self) -> None:
        self._save_json(LINEUP_STATE_FILE, {"posted": list(self._lineup_posted), "clear_date": self._lineup_clear_date}, "lineup")

    async def _refresh_schedule(self, prune_finished: bool = False) -> None:
        """Fetch today's full MLB schedule and MERGE into the existing game cache.

        We deliberately merge (not replace) so that games which started on the
        prior calendar date but haven't finished yet — i.e. they started at
        11 PM ET and are still going after midnight — are not dropped.

        If prune_finished=True, any previously tracked game that is now
        confirmed Final is removed from the cache and its alert state is pruned.
        This is called on the new-day refresh path.
        """
        now_et = _et_now()
        today_str = now_et.strftime("%Y-%m-%d")

        client = self.bot.mlb_client
        session = await client.get_session()

        url = (
            f"{client.BASE_URL}/schedule?sportId=1"
            f"&date={today_str}"
            f"&hydrate=team"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[monitor] schedule fetch returned {resp.status}")
                    return
                data = await resp.json()
        except Exception as e:
            print(f"[monitor] schedule fetch error: {e}")
            return

        new_games = {}
        for date_obj in data.get("dates", []):
            for g in date_obj.get("games", []):
                pk = g.get("gamePk")
                if not pk:
                    continue
                start_et = _parse_game_time(g.get("gameDate", ""))
                new_games[pk] = {
                    "start_et": start_et,
                    "away": g["teams"]["away"]["team"].get("abbreviation", "???"),
                    "home": g["teams"]["home"]["team"].get("abbreviation", "???"),
                    "abstract_state": g.get("status", {}).get("abstractGameState", "Preview"),
                }

        if prune_finished:
            # Remove games that are now Final from our tracked set.
            # Games NOT in today's schedule (i.e. yesterday's late game still live)
            # are left alone — they'll be processed until they turn Final.
            finished_pks = {
                pk for pk, info in new_games.items()
                if info.get("abstract_state") == "Final"
            }
            for pk in finished_pks:
                self._scheduled_games.pop(pk, None)
                self._nh_alerted.pop(pk, None)
                self._nh_broken_posted.discard(pk)
                # HR state intentionally kept — _hr_posted is a set and harmless;
                # _hr_pending entries expire naturally via VIDEO_WAIT_MAX_CYCLES.
            if finished_pks:
                self._save_nh_state()
                print(f"[monitor] pruned {len(finished_pks)} finished game(s) from tracker")

        # Merge today's games in (add new ones, update metadata for existing ones)
        for pk, info in new_games.items():
            if pk not in self._scheduled_games:
                self._scheduled_games[pk] = info
            else:
                # Update abstract_state so _any_game_active_or_imminent stays accurate
                self._scheduled_games[pk]["abstract_state"] = info["abstract_state"]

        self._schedule_date = today_str
        print(f"[monitor] refreshed schedule for {today_str} — tracking {len(self._scheduled_games)} game(s)")

    async def _refresh_milb_schedule(self) -> None:
        """Fetch today's MiLB affiliate schedule and merge into _milb_scheduled_games."""
        now_et = _et_now()
        today_str = now_et.strftime("%Y-%m-%d")
        fav_team = getattr(self.bot, "favorite_team", None)
        if not fav_team:
            return

        client = self.bot.mlb_client
        session = await client.get_session()
        milb_teams = await client.get_milb_teams()

        affiliate_ids = {t['id']: LEVEL_ABBREVS.get(t.get('level', ''), t.get('level', ''))
                         for t in milb_teams if t.get('parent_abbrev', '').upper() == fav_team.upper()}
        if not affiliate_ids:
            return

        team_id_param = ','.join(str(i) for i in affiliate_ids)
        url = (f"{client.BASE_URL}/schedule?sportId=11,12,13,14,15"
               f"&teamId={team_id_param}&date={today_str}&hydrate=team")
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return
                sched = await resp.json()
        except Exception as e:
            print(f"[monitor] MiLB schedule fetch error: {e}")
            return

        new_games = {}
        for date_obj in sched.get('dates', []):
            for g in date_obj.get('games', []):
                pk = g.get('gamePk')
                if not pk:
                    continue
                away_team = g['teams']['away']['team']
                home_team = g['teams']['home']['team']
                away_id = away_team.get('id')
                home_id = home_team.get('id')
                away_abbr = away_team.get("abbreviation", "???")
                home_abbr = home_team.get("abbreviation", "???")
                level = affiliate_ids.get(away_id) or affiliate_ids.get(home_id, '')
                affiliate_abbr = away_abbr if away_id in affiliate_ids else home_abbr
                new_games[pk] = {
                    "start_et":       _parse_game_time(g.get("gameDate", "")),
                    "away":           away_abbr,
                    "home":           home_abbr,
                    "abstract_state": g.get("status", {}).get("abstractGameState", "Preview"),
                    "level":          level,
                    "affiliate":      affiliate_abbr,
                }

        for pk, info in new_games.items():
            if pk not in self._milb_scheduled_games:
                self._milb_scheduled_games[pk] = info
            else:
                self._milb_scheduled_games[pk]["abstract_state"] = info["abstract_state"]

        # Prune games that are Final
        done = [pk for pk, info in self._milb_scheduled_games.items()
                if info.get("abstract_state") == "Final" and pk not in new_games]
        for pk in done:
            self._milb_scheduled_games.pop(pk, None)

        self._milb_schedule_date = today_str
        print(f"[monitor] refreshed MiLB schedule for {today_str} — tracking {len(self._milb_scheduled_games)} affiliate game(s)")

    def _any_game_active_or_imminent(self) -> bool:
        """Return True if we should be in active-polling mode.

        A game is considered active if:
          - Its abstract_state is Live (covers games running past midnight), OR
          - Its scheduled start is within WAKEUP_WINDOW_MINUTES of now.
        Final games are skipped — they've been pruned or will be on next refresh.

        Also stays active while an HR or walkoff alert is still waiting on a
        highlight video, even if every tracked game has gone Final — otherwise
        the last game of the day finishing would put the loop to sleep before
        the pending alert's wait cycles could run out and it'd never post.
        """
        if self._hr_pending or self._walkoff_pending:
            return True

        now_et = _et_now()
        wakeup = timedelta(minutes=WAKEUP_WINDOW_MINUTES)
        for pk, info in self._scheduled_games.items():
            state = info.get("abstract_state", "")
            if state == "Final":
                continue  # Don't wake up for finished games
            if state == "Live":
                return True  # Always poll live games, regardless of clock
            # Preview / Scheduled — check proximity to first pitch
            start = info.get("start_et")
            if start is None:
                return True  # Unknown start time — keep polling
            if now_et >= start - wakeup:
                return True
        for pk, info in self._milb_scheduled_games.items():
            state = info.get("abstract_state", "")
            if state == "Final":
                continue
            if state == "Live":
                return True
            start = info.get("start_et")
            if start is None:
                return True
            if now_et >= start - wakeup:
                return True
        return False

    # ─────────────────────────────────────────────
    # API helpers
    # ─────────────────────────────────────────────

    async def _fetch_live_feed(self, game_pk: int):
        """Fetch /api/v1.1/game/{pk}/feed/live — full live game state in one call."""
        client = self.bot.mlb_client
        session = await client.get_session()
        url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            print(f"[monitor] live feed error for {game_pk}: {e}")
        return None

    async def _fetch_content(self, game_pk: int) -> dict:
        """Fetch /game/{pk}/content — for highlight video URLs."""
        client = self.bot.mlb_client
        session = await client.get_session()
        url = f"{client.BASE_URL}/game/{game_pk}/content"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            print(f"[monitor] content fetch error for {game_pk}: {e}")
        return {}

    async def _get_alert_channel(self):
        channel_id = getattr(self.bot, 'alert_channel_id', None) or ALERT_CHANNEL_ID
        if not channel_id:
            return None
        ch = self.bot.get_channel(channel_id)
        if ch:
            return ch
        try:
            return await self.bot.fetch_channel(channel_id)
        except Exception:
            return None

    # ─────────────────────────────────────────────
    # Alert builders
    # ─────────────────────────────────────────────

    async def _post_morning_summary(self, channel, date_str: str = None) -> None:
        """Fetch yesterday's top performances and post a morning summary embed."""
        print("[monitor] fetching morning performance summary...")
        try:
            data = await self.bot.mlb_client.get_daily_top_performances(date_str)
        except Exception as e:
            print(f"[monitor] morning summary error: {e}")
            return

        if not data or (not data["hitters"] and not data["pitchers"]):
            print("[monitor] morning summary: no performances to post")
            return

        date_obj = datetime.strptime(data["date"], "%Y-%m-%d")
        date_label = date_obj.strftime("%B ") + str(date_obj.day)

        embed = discord.Embed(
            title=f"⭐ Top Performances — {date_label}",
            color=discord.Color.gold(),
        )

        if data["hitters"]:
            name_w = max(len(_last_name(h["name"])) for h in data["hitters"])
            lines = []
            for h in data["hitters"]:
                name = _last_name(h["name"])
                lines.append(f"{h['team']:<3}  {name:<{name_w}}  {h['summary']}")
            embed.add_field(
                name="🏏 Hitters",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        if data["pitchers"]:
            name_w = max(len(_last_name(p["name"])) for p in data["pitchers"])
            lines = []
            for p in data["pitchers"]:
                name = _last_name(p["name"])
                gs = int(p["score"])
                lines.append(f"{p['team']:<3}  {name:<{name_w}}  {p['summary']}  (GS {gs})")
            embed.add_field(
                name="⚾ Pitchers",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        try:
            await channel.send(embed=embed)
            print(f"[monitor] posted morning summary for {data['date']}")
        except discord.HTTPException as e:
            print(f"[monitor] failed to post morning summary: {e}")

    async def _post_milb_affiliate_summary(self, channel, data: dict) -> None:
        """Post top performances across FAVORITE_TEAM's MiLB affiliates."""
        print("[monitor] posting MiLB affiliate summary...")

        if not data or (not data["hitters"] and not data["pitchers"]):
            print("[monitor] MiLB affiliate summary: no performances to post")
            return

        date_obj = datetime.strptime(data["date"], "%Y-%m-%d")
        date_label = date_obj.strftime("%B ") + str(date_obj.day)
        fav = getattr(self.bot, "favorite_team_full", None) or getattr(self.bot, "favorite_team", "")

        embed = discord.Embed(
            title=f"⭐ {fav} Affiliates — {date_label}",
            color=discord.Color.blue(),
        )

        if data["hitters"]:
            name_w  = max(len(_last_name(h["name"])) for h in data["hitters"])
            level_w = max(len(h.get("level", "")) for h in data["hitters"])
            lines = []
            for h in data["hitters"]:
                name  = _last_name(h["name"])
                level = h.get("level", "")
                lines.append(f"{level:<{level_w}}  {h['team']:<3}  {name:<{name_w}}  {h['summary']}")
            embed.add_field(
                name="🏏 Hitters",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        if data["pitchers"]:
            name_w  = max(len(_last_name(p["name"])) for p in data["pitchers"])
            level_w = max(len(p.get("level", "")) for p in data["pitchers"])
            lines = []
            for p in data["pitchers"]:
                name  = _last_name(p["name"])
                level = p.get("level", "")
                gs    = int(p["score"])
                lines.append(f"{level:<{level_w}}  {p['team']:<3}  {name:<{name_w}}  {p['summary']}  (GS {gs})")
            embed.add_field(
                name="⚾ Pitchers",
                value="```\n" + "\n".join(lines) + "\n```",
                inline=False,
            )

        try:
            await channel.send(embed=embed)
            print(f"[monitor] posted MiLB affiliate summary for {data['date']}")
        except discord.HTTPException as e:
            print(f"[monitor] failed to post MiLB affiliate summary: {e}")

    def _build_nh_pitcher_table(self, pitchers: list) -> str:
        if not pitchers:
            return ""
        labels  = ["pitcher", "ip", "bb", "so", "np"]
        headers = {"pitcher": "PITCHER", "ip": "IP", "bb": "BB", "so": "SO", "np": "NP"}
        return format_table(labels, pitchers, headers, {"pitcher"})

    @staticmethod
    def _nh_remaining_delay(feed: dict) -> float:
        """Return how many seconds to still wait before posting an NH alert.

        Uses the last completed play's endTime so that if the poll cycle already
        ran 12 seconds after the play, we only sleep the remaining 3 seconds
        instead of the full NH_ALERT_DELAY.
        """
        try:
            all_plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
            for play in reversed(all_plays):
                end_time_str = play.get("about", {}).get("endTime", "")
                if end_time_str:
                    end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - end_time).total_seconds()
                    return max(0.0, NH_ALERT_DELAY - elapsed)
        except Exception:
            pass
        return NH_ALERT_DELAY

    async def _check_lineup_post(self, now_et: datetime) -> None:
        """Post FAVORITE_TEAM's starting lineup once MLB publishes it (same format as /mlb box)."""
        fav_team = getattr(self.bot, "favorite_team", None)
        if not fav_team:
            return
        fav_upper = fav_team.upper()

        for pk, info in self._scheduled_games.items():
            if pk in self._lineup_posted or info.get("abstract_state") != "Preview":
                continue
            if info.get("away", "").upper() == fav_upper:
                side = "away"
            elif info.get("home", "").upper() == fav_upper:
                side = "home"
            else:
                continue
            start_et = info.get("start_et")
            if not start_et or now_et < start_et - timedelta(hours=LINEUP_CHECK_HOURS):
                continue

            configured_id = getattr(self.bot, "alert_channel_id", None) or ALERT_CHANNEL_ID
            if not configured_id:
                return  # alerts disabled (no ALERT_CHANNEL_ID)
            channel = await self._get_alert_channel()
            if channel is None:
                print(f"[monitor] lineup check: alert channel not found (ALERT_CHANNEL_ID={configured_id})")
                return

            client = self.bot.mlb_client
            session = await client.get_session()
            try:
                async with session.get(f"{client.BASE_URL}/game/{pk}/boxscore") as resp:
                    if resp.status != 200:
                        continue
                    box_data = await resp.json()
            except Exception as e:
                print(f"[monitor] lineup boxscore fetch error for {pk}: {e}")
                continue

            if len(box_data.get("teams", {}).get(side, {}).get("battingOrder", [])) < 9:
                continue  # lineup not published yet

            try:
                await self._post_lineup(channel, box_data, side, start_et, game_pk=pk)
            except Exception as e:
                print(f"[monitor] failed to post lineup for {pk}: {e}")
                continue  # retry next cycle
            self._lineup_posted.add(pk)
            self._save_lineup_state()
            print(f"[monitor] posted {fav_upper} lineup for game {pk}")

    async def _fetch_probables(self, game_pk: int) -> dict:
        """Return both sides' probable starters from the schedule.

        The schedule's ``probablePitcher`` hydrate is the reliable source for both
        sides (the boxscore only lists a side's starter once its lineup is set).
        Returns ``{'away': {id, name}, 'home': {id, name}}`` (a side may be empty).
        """
        result = {"away": {}, "home": {}}
        client = self.bot.mlb_client
        session = await client.get_session()
        url = f"{client.BASE_URL}/schedule?sportId=1&gamePk={game_pk}&hydrate=probablePitcher"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return result
                data = await resp.json()
        except Exception as e:
            print(f"[monitor] probable-pitcher fetch error for {game_pk}: {e}")
            return result
        for date_obj in data.get("dates", []):
            for g in date_obj.get("games", []):
                if g.get("gamePk") != game_pk:
                    continue
                for side in ("away", "home"):
                    pp = g.get("teams", {}).get(side, {}).get("probablePitcher")
                    if pp:
                        result[side] = {"id": pp.get("id"), "name": pp.get("fullName", "TBD")}

        ids = [str(r["id"]) for r in result.values() if r.get("id")]
        if ids:
            try:
                async with session.get(f"{client.BASE_URL}/people?personIds={','.join(ids)}") as resp:
                    if resp.status == 200:
                        people = (await resp.json()).get("people", [])
                        hands = {p.get("id"): p.get("pitchHand", {}).get("code", "") for p in people}
                        for side in ("away", "home"):
                            if result[side].get("id"):
                                result[side]["hand"] = hands.get(result[side]["id"], "")
            except Exception as e:
                print(f"[monitor] probable-pitcher handedness fetch error for {game_pk}: {e}")
        return result

    @staticmethod
    def _matchup_row(box_data: dict, side: str, probable: dict) -> dict:
        """Build one probable-pitcher row, enriching season stats from the boxscore."""
        if not probable:
            return None
        team = box_data.get("teams", {}).get(side, {})
        player = team.get("players", {}).get(f"ID{probable.get('id')}", {})
        ss = player.get("seasonStats", {}).get("pitching", {})
        return {
            "abbr": team.get("team", {}).get("abbreviation", "???"),
            "name": probable.get("name", "TBD"),
            "hand": probable.get("hand", ""),
            "era": ss.get("era", "-"),
            "whip": ss.get("whip", "-"),
            "wins": ss.get("wins", 0),
            "losses": ss.get("losses", 0),
        }

    @classmethod
    def _format_matchup(cls, box_data: dict, probables: dict) -> str:
        """Build the away-vs-home probable-pitcher block, or '' if neither is set."""
        rows = [r for side in ("away", "home")
                if (r := cls._matchup_row(box_data, side, probables.get(side)))]
        if not rows:
            return ""
        name_w = max(len(r["name"]) for r in rows)
        lines = [
            f"{r['abbr']:<3}  {r['name']:<{name_w}}  {r['hand'] + '  ' if r['hand'] else ''}{r['wins']}-{r['losses']}, {r['era']} ERA, {r['whip']} WHIP"
            for r in rows
        ]
        return "\n".join(lines)

    @staticmethod
    def _format_owns(matchups: list, pitcher_name: str) -> str:
        """Build the 'hitter owns / pitcher owns' block from career matchup stats.

        Mirrors the /mlb matchup buckets: OPS > 1.100 means the hitter owns the
        pitcher, OPS < .500 means the pitcher owns the hitter (min 5 career PA).
        """
        hitter_owns = []
        pitcher_owns = []
        for m in matchups:
            if m.pa < 5:
                continue
            try:
                ops_f = float(m.ops)
            except ValueError:
                ops_f = 0.0

            stat_parts = [f"{m.pa} PA", f"{m.h} H"]
            if m.d > 0: stat_parts.append(f"{m.d} 2B")
            if m.t > 0: stat_parts.append(f"{m.t} 3B")
            if m.hr > 0: stat_parts.append(f"{m.hr} HR")
            if m.bb > 0: stat_parts.append(f"{m.bb} BB")
            if m.so > 0: stat_parts.append(f"{m.so} SO")
            line = f"**{m.batter_name}** {m.avg}/{m.ops} ({', '.join(stat_parts)})"

            if ops_f > 1.100:
                hitter_owns.append(line)
            elif ops_f < .500:
                pitcher_owns.append(line)

        block = ""
        if hitter_owns:
            block += f"**👑 Owns {pitcher_name}**\n" + "\n".join(hitter_owns) + "\n"
        if pitcher_owns:
            block += f"**🔒 Owned by {pitcher_name}**\n" + "\n".join(pitcher_owns) + "\n"
        return block

    async def _post_lineup(self, channel, box_data: dict, side: str, start_et: datetime = None,
                           game_pk: int = None) -> None:
        """Send the starting-lineup embed (batting table in /mlb box format)."""
        box = parse_box_score_side(box_data, side)
        # Pregame the probable pitcher appears as a trailing pseudo-substitute — show starters only
        box.batting_rows = [r for r in box.batting_rows if r.get("is_starter")]
        await self.bot.mlb_client._fill_bench_handedness(box)
        probables = await self._fetch_probables(game_pk) if game_pk else {"away": {}, "home": {}}
        matchup = self._format_matchup(box_data, probables)
        desc = ""
        if matchup:
            desc += f"**Pitching Matchup**\n```\n{matchup}\n```\n"
        desc += f"**{box.team_name} Batting**\n```python\n{box.format_lineup_batting()}\n```"

        # Career matchups: favorite-team hitters who own / are owned by the opposing pitcher
        opp_side = "home" if side == "away" else "away"
        opp_pitcher = probables.get(opp_side) or {}
        opp_pitcher_id = opp_pitcher.get("id")
        fav_abbr = box_data.get("teams", {}).get(side, {}).get("team", {}).get("abbreviation")
        if opp_pitcher_id and fav_abbr:
            try:
                data = await self.bot.mlb_client.get_matchup(fav_abbr, str(opp_pitcher_id))
                if data and data.get("matchups"):
                    owns = self._format_owns(data["matchups"], data["pitcher"])
                    if owns:
                        desc += f"\n{owns}"
            except Exception as e:
                print(f"[monitor] matchup-owns fetch error for game {game_pk}: {e}")

        embed = discord.Embed(
            title=f"Starting Lineup — {box.title}",
            description=desc,
            color=discord.Color.blue(),
        )
        if start_et:
            embed.set_footer(text=f"First pitch {start_et.strftime('%-I:%M %p')} ET")
        await channel.send(embed=embed)

    async def _delayed_nh_alert(self, channel, feed: dict, game_pk: int) -> None:
        await asyncio.sleep(self._nh_remaining_delay(feed))
        await self._post_nh_alert(channel, feed, game_pk)

    async def _delayed_nh_broken_alert(self, channel, feed: dict, was_perfect: bool, pitching_abbr: str = None) -> None:
        await asyncio.sleep(self._nh_remaining_delay(feed))
        await self._post_nh_broken_alert(channel, feed, was_perfect, pitching_abbr)

    async def _delayed_nh_tune_in_alert(self, channel, feed: dict, game_pk: int, pitching_abbr: str, batting_side: str) -> None:
        await asyncio.sleep(self._nh_remaining_delay(feed))
        await self._post_nh_tune_in_alert(channel, feed, game_pk, pitching_abbr, batting_side)

    async def _delayed_pg_broken_alert(self, channel, feed: dict, pitching_abbr: str = None) -> None:
        await asyncio.sleep(self._nh_remaining_delay(feed))
        await self._post_pg_broken_alert(channel, feed, pitching_abbr)

    def _get_next_batters(self, feed: dict, batting_side: str, n: int = 3) -> list:
        live_data     = feed.get("liveData", {})
        boxscore      = live_data.get("boxscore", {})
        batting_order = boxscore.get("teams", {}).get(batting_side, {}).get("battingOrder", [])
        players       = boxscore.get("teams", {}).get(batting_side, {}).get("players", {})
        if not batting_order:
            return []
        half = "top" if batting_side == "away" else "bottom"
        all_plays = live_data.get("plays", {}).get("allPlays", [])
        last_batter_id = None
        for play in reversed(all_plays):
            if play.get("about", {}).get("halfInning") == half and play.get("about", {}).get("isComplete", False):
                last_batter_id = play.get("matchup", {}).get("batter", {}).get("id")
                break
        if last_batter_id is None:
            start_idx = 0
        else:
            try:
                start_idx = (batting_order.index(last_batter_id) + 1) % len(batting_order)
            except ValueError:
                start_idx = 0
        result = []
        for i in range(min(n, len(batting_order))):
            idx  = (start_idx + i) % len(batting_order)
            pid  = batting_order[idx]
            name = players.get(f"ID{pid}", {}).get("person", {}).get("fullName", "Unknown")
            result.append({"name": name, "order": idx + 1})
        return result

    async def _post_nh_tune_in_alert(self, channel, feed: dict, game_pk: int, pitching_abbr: str, batting_side: str) -> None:
        game_data  = feed.get("gameData", {})
        live_data  = feed.get("liveData", {})
        linescore  = live_data.get("linescore", {})
        flags      = game_data.get("flags", {})
        is_perfect = flags.get("perfectGame", False)

        away_abbr  = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
        home_abbr  = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")
        side_key   = "home" if pitching_abbr == home_abbr else "away"

        inning = linescore.get("currentInning", 0)
        is_top = linescore.get("isTopInning", True)
        outs   = linescore.get("outs", 0)

        n      = inning if inning <= 20 else inning % 10
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
        alert_word = "P*RFECT GAME" if is_perfect else "NO-H*TTER"
        title = f"📺 TUNE IN: {pitching_abbr} GOING FOR THE {alert_word} IN THE {inning}{suffix}!"

        boxscore    = live_data.get("boxscore", {})
        pitcher_ids = boxscore.get("teams", {}).get(side_key, {}).get("pitchers", [])
        players     = boxscore.get("teams", {}).get(side_key, {}).get("players", {})
        pitchers    = []
        for pid in pitcher_ids:
            p_data  = players.get(f"ID{pid}", {})
            p_stats = p_data.get("stats", {}).get("pitching", {})
            if p_stats and p_stats.get("pitchesThrown", 0) > 0:
                pitchers.append({
                    "pitcher": p_data.get("person", {}).get("fullName", "Unknown"),
                    "ip": p_stats.get("inningsPitched", "0"),
                    "bb": str(p_stats.get("baseOnBalls", 0)),
                    "so": str(p_stats.get("strikeOuts", 0)),
                    "np": str(p_stats.get("pitchesThrown", 0)),
                })

        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        score_line  = f"{away_abbr} {away_score} — {home_abbr} {home_score}"
        inning_desc = _inning_label(inning, is_top) + f" | {outs} out{'s' if outs != 1 else ''}"

        batting_abbr = away_abbr if batting_side == "away" else home_abbr
        next_batters = self._get_next_batters(feed, batting_side)
        batters_text = "\n".join(f"{b['order']}. {b['name']}" for b in next_batters) if next_batters else "—"

        embed = discord.Embed(
            title=title,
            url=f"https://www.mlb.com/tv/g{game_pk}",
            color=discord.Color.gold() if is_perfect else discord.Color.red(),
        )
        embed.add_field(name="Score",  value=score_line,  inline=True)
        embed.add_field(name="Inning", value=inning_desc, inline=True)
        if pitchers:
            table = self._build_nh_pitcher_table(pitchers)
            embed.add_field(name="Pitchers", value=f"```\n{table}\n```", inline=False)
        embed.add_field(name=f"Next up for {batting_abbr}", value=batters_text, inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post NH tune-in alert: {e}")

    async def _post_nh_broken_alert(self, channel, feed: dict, was_perfect: bool, pitching_abbr: str = None) -> None:
        game_data = feed.get("gameData", {})
        live_data = feed.get("liveData", {})
        linescore = live_data.get("linescore", {})

        away_abbr = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
        home_abbr = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")

        # The team being no-hit bats in "top" if pitching team is home, "bottom" if away.
        # We need this to skip hits by the pitching team (who bats in the other half).
        if pitching_abbr == home_abbr:
            hitting_half = "top"
        elif pitching_abbr == away_abbr:
            hitting_half = "bottom"
        else:
            hitting_half = None  # unknown — fall back to first hit by either team

        # Find the first hit by the team that was being no-hit
        all_plays = live_data.get("plays", {}).get("allPlays", [])
        hit_play = None
        for play in all_plays:
            if play.get("result", {}).get("eventType") in ("single", "double", "triple", "home_run"):
                if hitting_half is None or play.get("about", {}).get("halfInning") == hitting_half:
                    hit_play = play
                    break

        if not hit_play:
            return

        about   = hit_play.get("about", {})
        inning  = about.get("inning", 0)
        is_top  = about.get("halfInning", "top") == "top"
        outs    = about.get("outs", 0)
        desc    = hit_play.get("result", {}).get("description", "")
        pitcher = hit_play.get("matchup", {}).get("pitcher", {}).get("fullName", "")
        batter  = hit_play.get("matchup", {}).get("batter",  {}).get("fullName", "")

        if not pitching_abbr:
            pitching_abbr = home_abbr if is_top else away_abbr

        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        score_line = f"{away_abbr} {away_score} — {home_abbr} {home_score}"

        alert_word = "perfect game" if was_perfect else "no-hitter"
        title = f"💔 {pitching_abbr}'s {alert_word} is over"

        if desc and pitcher:
            desc_fmt  = desc.replace(batter, f"**{batter}**", 1) if batter else desc
            play_text = f"With **{pitcher}** pitching, {desc_fmt}"
        else:
            play_text = desc

        embed = discord.Embed(title=title, color=discord.Color.blue())
        inning_desc = _inning_label(inning, is_top) + f" | {outs} out{'s' if outs != 1 else ''}"
        embed.add_field(name="Score",  value=score_line,  inline=True)
        embed.add_field(name="Inning", value=inning_desc, inline=True)
        if play_text:
            embed.add_field(name="Play", value=play_text, inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post NH broken alert: {e}")

    # Events that put a runner on base without a hit — these end a perfect game
    # while leaving the no-hitter intact.
    PG_BREAK_EVENTS = ("walk", "intent_walk", "hit_by_pitch", "field_error", "catcher_interf")

    async def _post_pg_broken_alert(self, channel, feed: dict, pitching_abbr: str = None) -> None:
        """Perfect game broken (walk/HBP/error) but the no-hitter is still alive."""
        game_data = feed.get("gameData", {})
        live_data = feed.get("liveData", {})
        linescore = live_data.get("linescore", {})

        away_abbr = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
        home_abbr = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")

        # The team being no-hit bats "top" if the pitching team is home, "bottom" if away.
        if pitching_abbr == home_abbr:
            hitting_half = "top"
        elif pitching_abbr == away_abbr:
            hitting_half = "bottom"
        else:
            hitting_half = None  # unknown — fall back to first baserunner by either team

        # Find the first non-hit baserunner allowed by the team throwing the no-hitter
        all_plays = live_data.get("plays", {}).get("allPlays", [])
        break_play = None
        for play in all_plays:
            if play.get("result", {}).get("eventType") in self.PG_BREAK_EVENTS:
                if hitting_half is None or play.get("about", {}).get("halfInning") == hitting_half:
                    break_play = play
                    break

        if not break_play:
            return

        about   = break_play.get("about", {})
        inning  = about.get("inning", 0)
        is_top  = about.get("halfInning", "top") == "top"
        outs    = about.get("outs", 0)
        desc    = break_play.get("result", {}).get("description", "")
        pitcher = break_play.get("matchup", {}).get("pitcher", {}).get("fullName", "")
        batter  = break_play.get("matchup", {}).get("batter",  {}).get("fullName", "")

        if not pitching_abbr:
            pitching_abbr = home_abbr if is_top else away_abbr

        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        score_line = f"{away_abbr} {away_score} — {home_abbr} {home_score}"

        title = f"🧤 {pitching_abbr}'s perfect game is over — no-hitter still alive"

        if desc and pitcher:
            desc_fmt  = desc.replace(batter, f"**{batter}**", 1) if batter else desc
            play_text = f"With **{pitcher}** pitching, {desc_fmt}"
        else:
            play_text = desc

        embed = discord.Embed(title=title, color=discord.Color.blue())
        inning_desc = _inning_label(inning, is_top) + f" | {outs} out{'s' if outs != 1 else ''}"
        embed.add_field(name="Score",  value=score_line,  inline=True)
        embed.add_field(name="Inning", value=inning_desc, inline=True)
        if play_text:
            embed.add_field(name="Play", value=play_text, inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post PG broken alert: {e}")

    async def _post_nh_alert(self, channel, feed: dict, game_pk: int) -> None:
        game_data  = feed.get("gameData", {})
        live_data  = feed.get("liveData", {})
        linescore  = live_data.get("linescore", {})
        flags      = game_data.get("flags", {})
        is_perfect = flags.get("perfectGame", False)
        is_nh      = flags.get("noHitter", False)

        if not is_perfect and not is_nh:
            return

        away_abbr  = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
        home_abbr  = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")
        away_hits  = linescore.get("teams", {}).get("away", {}).get("hits", 0)

        pitching_abbr = home_abbr if away_hits == 0 else away_abbr
        side_key      = "home" if pitching_abbr == home_abbr else "away"

        inning    = linescore.get("currentInning", 0)
        is_top    = linescore.get("isTopInning", True)
        outs      = linescore.get("outs", 0)
        ab_state  = game_data.get("status", {}).get("abstractGameState", "")
        is_final  = ab_state == "Final"

        alert_word = "P*RFECT GAME" if is_perfect else "NO-H*TTER"
        tense      = "THREW A" if is_final else "IS THROWING A"
        title      = f"🚨 {pitching_abbr} {tense} {alert_word}! 🚨"

        # Build pitcher table from boxscore embedded in live feed
        boxscore    = live_data.get("boxscore", {})
        pitcher_ids = boxscore.get("teams", {}).get(side_key, {}).get("pitchers", [])
        players     = boxscore.get("teams", {}).get(side_key, {}).get("players", {})
        pitchers    = []
        for pid in pitcher_ids:
            p_data  = players.get(f"ID{pid}", {})
            p_stats = p_data.get("stats", {}).get("pitching", {})
            if p_stats and p_stats.get("pitchesThrown", 0) > 0:
                pitchers.append({
                    "pitcher": p_data.get("person", {}).get("fullName", "Unknown"),
                    "ip":  p_stats.get("inningsPitched", "0"),
                    "bb":  str(p_stats.get("baseOnBalls", 0)),
                    "so":  str(p_stats.get("strikeOuts", 0)),
                    "np":  str(p_stats.get("pitchesThrown", 0)),
                })

        away_score = linescore.get("teams", {}).get("away", {}).get("runs", 0)
        home_score = linescore.get("teams", {}).get("home", {}).get("runs", 0)
        score_line = f"{away_abbr} {away_score} — {home_abbr} {home_score}"

        if is_final:
            inning_desc = "Final"
        else:
            inning_desc = _inning_label(inning, is_top) + f" | {outs} out{'s' if outs != 1 else ''}"

        embed = discord.Embed(
            title=title,
            color=discord.Color.gold() if is_perfect else discord.Color.red(),
        )
        embed.add_field(name="Score",  value=score_line, inline=True)
        embed.add_field(name="Inning", value=inning_desc, inline=True)
        if pitchers:
            table = self._build_nh_pitcher_table(pitchers)
            embed.add_field(name="Pitchers", value=f"```\n{table}\n```", inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post NH alert: {e}")

    @staticmethod
    def _extract_hr_summary(play: dict) -> dict:
        """Pull the fields needed for one line of a multi-homer alert out of a home_run play."""
        dist = ev = la = 0
        pitch_type = pitch_spd = ""
        play_id = None
        for event in play.get("playEvents", []):
            if event.get("details", {}).get("isInPlay") and "hitData" in event:
                hd         = event["hitData"]
                dist       = int(hd.get("totalDistance") or 0)
                ev         = float(hd.get("launchSpeed") or 0)
                la         = int(hd.get("launchAngle") or 0)
                pitch_type = event.get("details", {}).get("type", {}).get("description", "")
                pitch_spd  = float(event.get("pitchData", {}).get("startSpeed") or 0)
                play_id    = event.get("playId")
                break

        about = play.get("about", {})
        half  = about.get("halfInning", "top")
        return {
            "pitcher":     play.get("matchup", {}).get("pitcher", {}).get("fullName", "Unknown"),
            "desc":        play.get("result", {}).get("description", ""),
            "inning":      f"{'bot' if half == 'bottom' else 'top'} {about.get('inning', 0)}",
            "dist":        dist,
            "ev":          ev,
            "la":          la,
            "pitch_type":  pitch_type,
            "pitch_speed": pitch_spd,
            "play_id":     play_id,
            "video_url":   "",
            "video_blurb": "",
            "xba":         None,
            "parks":       None,
        }

    def _should_post_hr(self, hr: dict) -> bool:
        if HR_ALWAYS_ALERT_TEAM and hr["batter_team"] == HR_ALWAYS_ALERT_TEAM:
            return True
        if hr.get("game_hr_num", 1) >= MULTI_HR_THRESHOLD:
            return True
        if hr["dist"] >= HR_DISTANCE_THRESHOLD:
            return True
        parks = hr.get("parks")
        if parks is not None and 0 < parks <= HR_PARKS_THRESHOLD:
            return True
        xba = hr.get("xba")
        if xba is not None and xba < HR_XBA_THRESHOLD:
            return True
        return False

    async def _fetch_savant_hr_data(self, game_pk: int) -> dict:
        """Returns {play_id: {'xba': float|None, 'parks': int|None}} from Savant game feed."""
        session = await self.bot.mlb_client.get_session()
        url = f"https://baseballsavant.mlb.com/gf?game_pk={game_pk}"
        try:
            async with session.get(url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json(content_type=None)
        except Exception:
            return {}
        result = {}
        for entry in data.get("exit_velocity", []):
            play_id = entry.get("play_id")
            if play_id:
                xba_str = entry.get("xba", "")
                parks   = entry.get("contextMetrics", {}).get("homeRunBallparks")
                result[play_id] = {
                    "xba":   float(xba_str) if xba_str else None,
                    "parks": int(parks) if parks is not None else None,
                }
        return result

    def _build_walkoff_embed(self, wo: dict) -> discord.Embed:
        away        = wo["away"]
        home        = wo["home"]
        away_score  = wo["away_score"]
        home_score  = wo["home_score"]
        batter      = wo["batter"]
        pitcher     = wo["pitcher"]
        inning      = wo["inning"].title()
        desc        = wo["desc"]
        video_url   = wo.get("video_url", "")
        video_blurb = wo.get("video_blurb", "Watch")

        title    = f"🚶 {home} walks off {away}: {home_score}-{away_score}"
        desc_fmt = desc.replace(batter, f"**{batter}**", 1)
        body     = f"**{inning}:** With **{pitcher}** pitching, {desc_fmt}"
        if video_url:
            body += f"\n> [🎥 **{video_blurb or 'Watch'}**]({video_url})"

        return discord.Embed(title=title, description=body, color=discord.Color.green())

    async def _post_walkoff_alert(self, channel, wo: dict) -> "discord.Message | None":
        embed = self._build_walkoff_embed(wo)
        try:
            return await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post walkoff alert: {e}")
            return None

    async def _edit_walkoff_alert(self, msg: discord.Message, wo: dict) -> None:
        embed = self._build_walkoff_embed(wo)
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to edit walkoff alert: {e}")

    async def _post_hr_alert(self, channel, hr: dict) -> None:
        batter     = hr["batter"]
        team       = hr["batter_team"]
        pitcher    = hr["pitcher"]
        dist       = hr["dist"]
        ev         = hr["ev"]
        la         = hr["la"]
        pitch_type = hr.get("pitch_type", "")
        pitch_spd  = hr.get("pitch_speed", 0.0)
        inning     = hr.get("inning", "").title()
        hr_num     = hr.get("num", 0)
        desc       = hr.get("desc", "")
        video_url  = hr.get("video_url", "")
        video_blurb = hr.get("video_blurb", "Watch")
        xba        = hr.get("xba")
        parks      = hr.get("parks")

        away    = hr.get("away", "")
        home    = hr.get("home", "")
        if away and home:
            away_score = hr.get("away_score", 0)
            home_score = hr.get("home_score", 0)
            matchup = f"{away} {away_score} @ {home} {home_score}"
        else:
            matchup = team

        num_str = f" (#{hr_num})" if hr_num else ""

        # Most notable qualifier goes in the title; the others fall to the stats line
        if parks is not None and 0 < parks <= HR_PARKS_THRESHOLD:
            title_key = "parks"
            title_stat = f"{parks}/30 parks"
        elif xba is not None and xba < HR_XBA_THRESHOLD:
            title_key = "xba"
            title_stat = f"xBA {xba:.3f}"
        else:
            title_key = "dist"
            title_stat = f"{dist} ft"

        title = f"💣 {matchup} — ({team}) {batter}{num_str} | {title_stat}"

        pitch_parts = []
        if pitch_type and pitch_spd:
            pitch_parts.append(f"{pitch_spd:.1f} mph {pitch_type}")

        hit_parts = []
        if ev:
            hit_parts.append(f"{ev:.1f} mph EV")
        if la:
            hit_parts.append(f"{la}° LA")
        if title_key != "dist" and dist:
            hit_parts.append(f"{dist} ft")
        if title_key != "xba" and xba is not None:
            hit_parts.append(f"xBA {xba:.3f}")
        if title_key != "parks" and parks is not None:
            hit_parts.append(f"{parks}/30 parks")

        desc_fmt = desc.replace(batter, f"**{batter}**", 1)
        body = f"**{inning}:** With **{pitcher}** pitching, {desc_fmt}"
        if pitch_parts:
            body += f"\n> *{' | '.join(pitch_parts)}*"
        if hit_parts:
            body += f"\n> *{' | '.join(hit_parts)}*"
        if video_url:
            body += f"\n> [🎥 **{video_blurb or 'Watch'}**]({video_url})"

        embed = discord.Embed(
            title=title,
            description=body,
            color=discord.Color.orange(),
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post HR alert: {e}")

    async def _post_multi_hr_alert(self, channel, hr: dict, homers: list) -> None:
        """Alert for a batter's 2nd+ HR in a game — lists every HR they've hit so far."""
        batter = hr["batter"]
        team   = hr["batter_team"]
        away   = hr.get("away", "")
        home   = hr.get("home", "")
        if away and home:
            matchup = f"{away} {hr.get('away_score', 0)} @ {home} {hr.get('home_score', 0)}"
        else:
            matchup = team

        n = len(homers)
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        title = f"{'💣' * n} {matchup} — ({team}) {batter}'s {n}{suffix} home run"

        lines = []
        for i, homer in enumerate(homers, start=1):
            desc_fmt = homer["desc"].replace(batter, f"**{batter}**", 1)
            line = f"**#{i} — {homer['inning'].title()}:** With **{homer['pitcher']}** pitching, {desc_fmt}"

            stat_parts = []
            if homer.get("pitch_type") and homer.get("pitch_speed"):
                stat_parts.append(f"{homer['pitch_speed']:.1f} mph {homer['pitch_type']}")
            if homer.get("ev"):
                stat_parts.append(f"{homer['ev']:.1f} mph EV")
            if homer.get("la"):
                stat_parts.append(f"{homer['la']}° LA")
            if homer.get("dist"):
                stat_parts.append(f"{homer['dist']} ft")
            if homer.get("xba") is not None:
                stat_parts.append(f"xBA {homer['xba']:.3f}")
            if homer.get("parks") is not None:
                stat_parts.append(f"{homer['parks']}/30 parks")
            if stat_parts:
                line += f"\n> *{' | '.join(stat_parts)}*"
            if homer.get("video_url"):
                line += f"\n> [🎥 **{homer.get('video_blurb') or 'Watch'}**]({homer['video_url']})"

            lines.append(line)

        body = "\n\n".join(lines)
        if len(body) > 4096:
            body = body[:4093] + "..."

        embed = discord.Embed(title=title, description=body, color=discord.Color.orange())
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post multi-HR alert: {e}")

    @staticmethod
    def _format_abs_pbp(at_bats: list) -> str:
        """Format a player's at-bats as a Play-by-Play block, matching /abs output."""
        out = "### Play-by-Play\n"
        for ab in at_bats:
            if not ab.is_complete:
                out += f"**{ab.inning.title()}:** Currently at bat.\n\n"
                continue
            scoring = "__" if ab.is_scoring else ""
            line = f"**{ab.inning.title()}:** {scoring}With **{ab.pitcher_name}** pitching, {ab.description}{scoring}"
            if ab.pitch_data or ab.statcast_data:
                extras = " | ".join(filter(None, [ab.pitch_data, ab.statcast_data]))
                line += f" *({extras})*"
            out += line + "\n"
            if ab.video_url:
                out += f"> [🎥 **{ab.video_blurb}**]({ab.video_url})\n"
            out += "\n"
        return out

    async def _post_cycle_alert(self, channel, cyc: dict) -> None:
        batter = cyc["batter"]
        team   = cyc["batter_team"]
        away, home = cyc.get("away", ""), cyc.get("home", "")

        # Pull the player's full game line + at-bats the same way /abs does, so the
        # alert shows every plate appearance with pitch/Statcast/video detail.
        stats = None
        try:
            stats_list = await self.bot.mlb_client.get_player_game_stats(
                str(cyc["player_id"]), date=cyc.get("date"), include_abs=True
            )
            # In a doubleheader, pick the game that actually contains the cycle
            for s in stats_list:
                bs = s.batting_stats or {}
                if bs.get("doubles", 0) >= 1 and bs.get("triples", 0) >= 1 and bs.get("homeRuns", 0) >= 1 and bs.get("hits", 0) >= 4:
                    stats = s
                    break
            if stats is None:
                stats = next((s for s in stats_list if s.at_bats), stats_list[0] if stats_list else None)
        except Exception as e:
            print(f"[monitor] cycle alert at-bat fetch failed: {e}")

        # Title — prefer the matchup from cycle data; fall back to the fetched
        # stats (used by the test command, which doesn't supply team/score).
        if not team and stats:
            team = stats.team_abbrev
        if away and home:
            matchup = f"{away} {cyc.get('away_score', 0)} @ {home} {cyc.get('home_score', 0)}"
        elif stats:
            matchup = f"{stats.team_abbrev} {'vs' if stats.is_home else '@'} {stats.opp_abbrev}"
        else:
            matchup = team
        kind = "the natural cycle" if cyc.get("natural") else "the cycle"
        title = f"🔄 {matchup} — ({team}) {batter} hit for {kind}!"

        desc = ""
        if cyc.get("natural"):
            desc += "*single → double → triple → HR, in order — a natural cycle!*\n"
        if stats and (stats.batting_stats or stats.pitching_stats):
            desc += f"```python\n{stats.format_discord_code_block()}\n```\n"
        if stats and stats.at_bats:
            desc += self._format_abs_pbp(stats.at_bats)
        else:
            # Fallback: just the four cycle hits if at-bat detail is unavailable
            type_labels = {"single": "Single", "double": "Double", "triple": "Triple", "home_run": "Home Run"}
            types = cyc.get("types", {})
            desc += "\n".join(
                f"> **{type_labels[t]}** — inning {types[t]}"
                for t in ("single", "double", "triple", "home_run") if t in types
            )

        if len(desc) > 4096:
            desc = desc[:4093] + "..."

        embed = discord.Embed(title=title, description=desc.strip(), color=discord.Color.gold())
        if stats and stats.headshot_url:
            embed.set_thumbnail(url=stats.headshot_url)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post cycle alert: {e}")

    async def _post_milb_hr_alert(self, channel, hr: dict) -> None:
        batter     = hr["batter"]
        team       = hr["batter_team"]
        pitcher    = hr["pitcher"]
        level      = hr.get("level", "MiLB")
        dist       = hr.get("dist", 0)
        ev         = hr.get("ev", 0)
        la         = hr.get("la", 0)
        pitch_type = hr.get("pitch_type", "")
        pitch_spd  = hr.get("pitch_speed", 0.0)
        inning     = hr.get("inning", "").title()
        hr_num     = hr.get("num", 0)
        desc       = hr.get("desc", "")

        away    = hr.get("away", "")
        home    = hr.get("home", "")
        if away and home:
            away_score = hr.get("away_score", 0)
            home_score = hr.get("home_score", 0)
            matchup = f"{away} {away_score} @ {home} {home_score}"
        else:
            matchup = team
        num_str = f" (#{hr_num})" if hr_num else ""

        title = f"💣 ({level}) {matchup} — ({team}) {batter}{num_str}"

        pitch_parts = []
        if pitch_type and pitch_spd:
            pitch_parts.append(f"{pitch_spd:.1f} mph {pitch_type}")

        hit_parts = []
        if ev:
            hit_parts.append(f"{ev:.1f} mph EV")
        if la:
            hit_parts.append(f"{la}° LA")
        if dist:
            hit_parts.append(f"{dist} ft")

        desc_fmt = desc.replace(batter, f"**{batter}**", 1)
        body = f"**{inning}:** With **{pitcher}** pitching, {desc_fmt}"
        if pitch_parts:
            body += f"\n> *{' | '.join(pitch_parts)}*"
        if hit_parts:
            body += f"\n> *{' | '.join(hit_parts)}*"

        embed = discord.Embed(title=title, description=body, color=discord.Color.orange())
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post MiLB HR alert: {e}")

    async def _process_milb_game(self, game_pk: int, channel) -> None:
        """Check a MiLB affiliate game for new HRs and post alerts."""
        feed = await self._fetch_live_feed(game_pk)
        if not feed:
            return

        ab_state = feed.get("gameData", {}).get("status", {}).get("abstractGameState", "Preview")
        if ab_state == "Preview":
            return

        live_data      = feed.get("liveData", {})
        all_plays      = live_data.get("plays", {}).get("allPlays", [])
        sched_info     = self._milb_scheduled_games.get(game_pk, {})
        away_abbr      = sched_info.get("away", "???")
        home_abbr      = sched_info.get("home", "???")
        level          = sched_info.get("level", "MiLB")
        affiliate_abbr = sched_info.get("affiliate", "")
        milb_linescore = live_data.get("linescore", {}).get("teams", {})
        milb_away_runs = milb_linescore.get("away", {}).get("runs", 0)
        milb_home_runs = milb_linescore.get("home", {}).get("runs", 0)

        for play in all_plays:
            if play.get("result", {}).get("eventType") != "home_run":
                continue

            about      = play.get("about", {})
            at_bat_idx = about.get("atBatIndex", 0)
            hr_key     = f"{game_pk}_{at_bat_idx}"

            if hr_key in self._hr_posted:
                continue

            end_time_str = about.get("endTime", "")
            if end_time_str:
                try:
                    end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - end_time).total_seconds() > 600:
                        self._hr_posted.add(hr_key)
                        continue
                except Exception:
                    pass

            dist = ev = la = 0
            pitch_type = pitch_spd = ""
            for event in play.get("playEvents", []):
                if event.get("details", {}).get("isInPlay") and "hitData" in event:
                    hd         = event["hitData"]
                    dist       = int(hd.get("totalDistance") or 0)
                    ev         = float(hd.get("launchSpeed") or 0)
                    la         = int(hd.get("launchAngle") or 0)
                    pitch_type = event.get("details", {}).get("type", {}).get("description", "")
                    pitch_spd  = float(event.get("pitchData", {}).get("startSpeed") or 0)
                    break

            batter  = play.get("matchup", {}).get("batter", {}).get("fullName", "Unknown")
            pitcher = play.get("matchup", {}).get("pitcher", {}).get("fullName", "Unknown")
            desc    = play.get("result", {}).get("description", "")
            half    = about.get("halfInning", "top")
            inn_num = about.get("inning", 0)
            batter_team = home_abbr if half == "bottom" else away_abbr
            if affiliate_abbr and batter_team != affiliate_abbr:
                self._hr_posted.add(hr_key)  # mark so we don't recheck each poll
                continue

            hr_num = parse_hr_number(desc)

            # Score *after this play* (see MLB HR path) — avoids stamping a later
            # score onto an earlier HR when two are detected in the same poll.
            result    = play.get("result", {})
            play_away = result.get("awayScore", milb_away_runs)
            play_home = result.get("homeScore", milb_home_runs)

            hr_data = {
                "batter":       batter,
                "batter_team":  batter_team,
                "pitcher":      pitcher,
                "away":         away_abbr,
                "home":         home_abbr,
                "away_score":   play_away,
                "home_score":   play_home,
                "level":        level,
                "dist":         dist,
                "ev":           ev,
                "la":           la,
                "pitch_type":   pitch_type,
                "pitch_speed":  pitch_spd,
                "num":          hr_num,
                "inning":       f"{'bot' if half == 'bottom' else 'top'} {inn_num}",
                "desc":         desc,
                "game_pk":      game_pk,
            }

            if hr_key not in self._hr_posted:
                await self._post_milb_hr_alert(channel, hr_data)
                self._hr_posted.add(hr_key)
                self._save_hr_state()

    # ─────────────────────────────────────────────
    # Per-game processing
    # ─────────────────────────────────────────────

    async def _check_game_delay(self, game_pk: int, game_data: dict, channel) -> None:
        """Post a delay / resume alert when FAVORITE_TEAM's game enters or leaves a
        delay. Tracks per-game delay state so we only alert on transitions."""
        fav = getattr(self.bot, "favorite_team", None)
        if not fav:
            return
        sched_info = self._scheduled_games.get(game_pk, {})
        fav_upper = fav.upper()
        if sched_info.get("away", "").upper() != fav_upper and sched_info.get("home", "").upper() != fav_upper:
            return

        status     = game_data.get("status", {})
        detailed   = status.get("detailedState", "")
        ab_state   = status.get("abstractGameState", "Preview")
        is_delayed = "delayed" in detailed.lower()

        key  = str(game_pk)
        prev = self._delay_state.get(key)

        # First time we see this game: record the baseline silently so a restart
        # mid-delay doesn't fire a spurious alert.
        if prev is None:
            self._delay_state[key] = is_delayed
            self._save_delay_state()
            return
        if prev == is_delayed:
            return

        # Don't treat postponed/cancelled/suspended as a resume
        if not is_delayed and any(w in detailed.lower() for w in ("postponed", "cancel", "suspended")):
            self._delay_state[key] = is_delayed
            self._save_delay_state()
            return

        away = sched_info.get("away", "???")
        home = sched_info.get("home", "???")
        self._delay_state[key] = is_delayed
        self._save_delay_state()
        print(f"[monitor] {'delay' if is_delayed else 'resume'} alert: {away}@{home} '{detailed}' game={game_pk}")
        await self._post_delay_alert(channel, away, home, is_delayed, detailed, ab_state, status.get("reason", ""))

    async def _post_delay_alert(self, channel, away: str, home: str, is_delayed: bool,
                                detailed: str, ab_state: str, reason: str = "") -> None:
        if is_delayed:
            why = f" — {reason}" if reason and reason.lower() not in detailed.lower() else ""
            title = f"⏸️ {away} @ {home} — {detailed}{why}"
            body  = "Play has been delayed." if ab_state == "Live" else "The game has been delayed."
            color = discord.Color.dark_gray()
        else:
            # Decide by detailedState, not abstractGameState: "Warmup" reports
            # abstractGameState=Live even though play hasn't started.
            color = discord.Color.green()
            d = detailed.lower()
            if "in progress" in d:
                title = f"▶️ {away} @ {home} — Play has resumed"
                body  = "The delay is over and play has resumed."
            elif "warmup" in d:
                title = f"▶️ {away} @ {home} — Warmup"
                body  = "The delay is over — teams are warming up."
            else:
                title = f"▶️ {away} @ {home} — Delay lifted"
                body  = "The delay is over — the game is about to start."

        embed = discord.Embed(title=title, description=body, color=color)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post delay/resume alert: {e}")

    async def _process_game(self, game_pk: int, channel) -> None:
        feed = await self._fetch_live_feed(game_pk)
        if not feed:
            return

        game_data = feed.get("gameData", {})
        live_data = feed.get("liveData", {})
        ab_state  = game_data.get("status", {}).get("abstractGameState", "Preview")

        if game_pk in self._scheduled_games:
            self._scheduled_games[game_pk]["abstract_state"] = ab_state

        # Delay / resume alerts for FAVORITE_TEAM — checked before the Preview
        # early-return since a delayed start is still abstractGameState=Preview.
        await self._check_game_delay(game_pk, game_data, channel)

        if ab_state == "Preview":
            return  # Game hasn't started

        flags     = game_data.get("flags", {})
        linescore = live_data.get("linescore", {})
        inning    = linescore.get("currentInning", 0)
        is_top    = linescore.get("isTopInning", True)
        is_final  = ab_state == "Final"

        # ── No-hitter / perfect game ─────────────────────────────────────────
        is_pg = flags.get("perfectGame", False)
        is_nh = flags.get("noHitter", False)

        # After a break-up the Stats API's noHitter/perfectGame flag can flicker back
        # to True even though the hit is still on the board (a transient glitch). With
        # the stored state already popped, that re-raised flag would look like a fresh
        # no-hitter and post a duplicate alert. Distinguish a glitch from a genuine
        # official-scoring reversal by the actual hit count: a real no-hitter has a
        # team at 0 hits; a glitch leaves the hit recorded. Only a true 0-hit reversal
        # clears the break-up flag and lets the no-hitter resume.
        if (is_pg or is_nh) and game_pk in self._nh_broken_posted:
            ls_teams = linescore.get("teams", {})
            no_hit_live = (ls_teams.get("away", {}).get("hits", 0) == 0
                           or ls_teams.get("home", {}).get("hits", 0) == 0)
            if no_hit_live:
                print(f"[monitor] no-hitter restored by scoring reversal, resuming: game={game_pk}")
                self._nh_broken_posted.discard(game_pk)
                self._save_nh_state()

        if (is_pg or is_nh) and game_pk in self._nh_broken_posted:
            pass  # flag glitch after break-up — hit still on the board, ignore
        elif is_pg or is_nh:
            stored = self._nh_alerted.get(game_pk)

            # Determine which team is throwing the NH so break-up alerts find the right hit
            nh_away_abbr = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
            nh_home_abbr = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")
            away_hits    = linescore.get("teams", {}).get("away", {}).get("hits", 0)
            nh_pitching  = nh_home_abbr if away_hits == 0 else nh_away_abbr
            home_pitching = (nh_pitching == nh_home_abbr)

            # Perfect game broken (walk/HBP/error) but the no-hitter is still alive: post a
            # follow-up with the same break-play info. Only once, and only if we'd already
            # announced the perfect game.
            pg_broken_posted = (stored or {}).get("pg_broken_posted", False)
            fire_pg_broken = (
                stored is not None
                and stored.get("perfect", False)
                and not is_pg and is_nh
                and stored.get("alert_posted", False)
                and not pg_broken_posted
            )
            if fire_pg_broken:
                pg_pitching = stored.get("pitching_abbr", nh_pitching)
                print(f"[monitor] perfect game broken, no-hitter continues: {pg_pitching} game={game_pk}")
                asyncio.create_task(self._delayed_pg_broken_alert(channel, feed, pg_pitching))
                pg_broken_posted = True

            # Alert the moment the pitching team records the 3rd out of their half, rather
            # than waiting for the next half to begin (when isTopInning flips). inningState
            # cycles Top → Middle → Bottom → End: "Middle" marks the top half done, "End"
            # the bottom half done, and both appear before isTopInning flips. The
            # isTopInning-based fallbacks (`not is_top` / `is_top`) still catch the
            # completed half if a 60s poll misses the brief Middle/End window.
            inning_state = linescore.get("inningState", "")
            completed_half = None
            if home_pitching:
                # Home throws the top; complete once we leave the Top state (Middle onward).
                if inning_state != "Top":
                    completed_half = (inning, "top")
            else:
                # Away throws the bottom; complete at End, or once the next top has begun.
                if inning_state == "End":
                    completed_half = (inning, "bottom")
                elif is_top and inning > 1:
                    completed_half = (inning - 1, "bottom")

            alert_key = ("final", inning) if is_final else completed_half
            should_alert = is_final or completed_half is not None

            # Tune-in alert: fires when entering the pitching team's half inning at inning 9+
            entering_pitching_half = (home_pitching and is_top) or (not home_pitching and not is_top)
            stored_tune_in         = (stored or {}).get("tune_in_inning", 0)
            should_tune_in         = inning >= 9 and entering_pitching_half and inning > stored_tune_in

            key_changed       = stored is None or stored["key"] != alert_key
            prev_alert_posted = (stored or {}).get("alert_posted", False)
            # Fire alert if: (a) key changed and should_alert, OR (b) should_alert but alert
            # was never posted yet — catches cases where the bot saw the NH mid-inning first
            # (should_alert=False) or crashed during the 15-second delay before the alert sent.
            firing_alert = should_alert and (key_changed or not prev_alert_posted)

            if key_changed or should_tune_in or firing_alert or fire_pg_broken:
                self._nh_alerted[game_pk] = {
                    "key":           alert_key,
                    "perfect":       is_pg,
                    "pitching_abbr": nh_pitching,
                    "tune_in_inning": inning if should_tune_in else stored_tune_in,
                    "alert_posted":  prev_alert_posted or firing_alert,
                    "pg_broken_posted": pg_broken_posted,
                }
                self._save_nh_state()
                if firing_alert:
                    half_str = "bottom" if not is_top else "top"
                    print(f"[monitor] NH alert: {nh_pitching} inn={inning} {half_str} game={game_pk}")
                    asyncio.create_task(self._delayed_nh_alert(channel, feed, game_pk))
                elif key_changed:
                    half_str = "bottom" if not is_top else "top"
                    print(f"[monitor] NH detected, holding alert: {nh_pitching} inn={inning} {half_str} should_alert={should_alert} game={game_pk}")
                if should_tune_in:
                    batting_side = "away" if home_pitching else "home"
                    asyncio.create_task(self._delayed_nh_tune_in_alert(channel, feed, game_pk, nh_pitching, batting_side))
        else:
            # Flag was cleared — post break-up alert only if we actually posted an NH alert
            nh_changed = False
            if (game_pk in self._nh_alerted
                    and game_pk not in self._nh_broken_posted
                    and self._nh_alerted[game_pk].get("alert_posted", False)):
                was_perfect   = self._nh_alerted[game_pk].get("perfect", False)
                pitching_abbr = self._nh_alerted[game_pk].get("pitching_abbr")
                self._nh_broken_posted.add(game_pk)
                asyncio.create_task(self._delayed_nh_broken_alert(channel, feed, was_perfect, pitching_abbr))
                nh_changed = True
            if game_pk in self._nh_alerted:
                self._nh_alerted.pop(game_pk)
                nh_changed = True
            if nh_changed:
                self._save_nh_state()

        # ── Home runs ≥ threshold ────────────────────────────────────────────
        all_plays  = live_data.get("plays", {}).get("allPlays", [])
        sched_info = self._scheduled_games.get(game_pk, {})
        away_abbr  = sched_info.get("away", "???")
        home_abbr  = sched_info.get("home", "???")
        hr_linescore  = live_data.get("linescore", {}).get("teams", {})
        hr_away_runs  = hr_linescore.get("away", {}).get("runs", 0)
        hr_home_runs  = hr_linescore.get("home", {}).get("runs", 0)

        # Group this game's HR plays by batter (in at-bat order) so each HR can be
        # tagged with its ordinal for that batter (2nd, 3rd, ...) — this is what
        # drives the multi-homer-game alert and lets it list every prior shot.
        hr_plays_by_batter: dict = {}
        for p in all_plays:
            if p.get("result", {}).get("eventType") != "home_run":
                continue
            b   = p.get("matchup", {}).get("batter", {}).get("id")
            idx = p.get("about", {}).get("atBatIndex", 0)
            hr_plays_by_batter.setdefault(b, []).append((idx, p))
        for lst in hr_plays_by_batter.values():
            lst.sort(key=lambda t: t[0])

        for play in all_plays:
            if play.get("result", {}).get("eventType") != "home_run":
                continue

            about      = play.get("about", {})
            at_bat_idx = about.get("atBatIndex", 0)
            hr_key     = f"{game_pk}_{at_bat_idx}"

            if hr_key in self._hr_posted:
                continue

            # Skip plays older than 10 minutes — catches stale HRs on restart.
            # Only applies to plays we've never tracked before: once a HR is in
            # _hr_pending it must keep going through its own cycle-based video
            # wait/fallback, or this wall-clock check would race that fallback
            # and silently swallow the alert before it ever posts.
            if hr_key not in self._hr_pending:
                end_time_str = about.get("endTime", "")
                if end_time_str:
                    try:
                        end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - end_time).total_seconds() > 600:
                            self._hr_posted.add(hr_key)
                            continue
                    except Exception:
                        pass

            # Extract Statcast metrics
            dist = ev = la = 0
            pitch_type = pitch_spd = ""
            play_id = None
            for event in play.get("playEvents", []):
                if event.get("details", {}).get("isInPlay") and "hitData" in event:
                    hd         = event["hitData"]
                    dist       = int(hd.get("totalDistance") or 0)
                    ev         = float(hd.get("launchSpeed") or 0)
                    la         = int(hd.get("launchAngle") or 0)
                    pitch_type = event.get("details", {}).get("type", {}).get("description", "")
                    pitch_spd  = float(event.get("pitchData", {}).get("startSpeed") or 0)
                    play_id    = event.get("playId")
                    break

            batter  = play.get("matchup", {}).get("batter", {}).get("fullName", "Unknown")
            pitcher = play.get("matchup", {}).get("pitcher", {}).get("fullName", "Unknown")
            rbi     = play.get("result", {}).get("rbi", 0)
            desc    = play.get("result", {}).get("description", "")
            half    = about.get("halfInning", "top")
            inn_num = about.get("inning", 0)
            batter_team  = home_abbr if half == "bottom" else away_abbr

            hr_num = parse_hr_number(desc)

            pitcher_team = away_abbr if half == "bottom" else home_abbr

            # Score *after this play* — falls back to the live linescore for the
            # rare play that lacks per-play scores. Using the play's own score
            # avoids stamping a later score onto an earlier HR when two HRs are
            # detected in the same poll (e.g. back-to-back shots).
            result      = play.get("result", {})
            play_away   = result.get("awayScore", hr_away_runs)
            play_home   = result.get("homeScore", hr_home_runs)

            batter_id    = play.get("matchup", {}).get("batter", {}).get("id")
            batter_hrs   = hr_plays_by_batter.get(batter_id, [])
            game_hr_num  = next((i + 1 for i, (idx, _) in enumerate(batter_hrs) if idx == at_bat_idx), 1)

            hr_data = {
                "batter":       batter,
                "batter_id":    batter_id,
                "batter_team":  batter_team,
                "pitcher":      pitcher,
                "pitcher_team": pitcher_team,
                "away":         away_abbr,
                "home":         home_abbr,
                "away_score":   play_away,
                "home_score":   play_home,
                "dist":         dist,
                "ev":           ev,
                "la":           la,
                "pitch_type":   pitch_type,
                "pitch_speed":  pitch_spd,
                "rbi":          rbi,
                "num":          hr_num,
                "game_hr_num":  game_hr_num,
                "at_bat_idx":   at_bat_idx,
                "inning":       f"{'bot' if half == 'bottom' else 'top'} {inn_num}",
                "desc":         desc,
                "play_id":      play_id,
                "game_pk":      game_pk,
                "video_url":    "",
                "video_blurb":  "",
                "xba":          None,
                "parks":        None,
            }

            if hr_key not in self._hr_pending:
                self._hr_pending[hr_key] = {"cycles_waited": 0, "data": hr_data}

        # ── Resolve videos for this game's pending HRs ───────────────────────
        pending_here = {
            k: v for k, v in self._hr_pending.items()
            if v["data"]["game_pk"] == game_pk and not v.get("milb")
        }

        if pending_here:
            content_data = await self._fetch_content(game_pk)
            content_dict = extract_highlight_videos(content_data)

            savant_data = await self._fetch_savant_hr_data(game_pk)

            for hr_key, pending in list(pending_here.items()):
                if hr_key in self._hr_posted:
                    continue
                hr      = pending["data"]
                play_id = hr.get("play_id")
                cycles  = pending["cycles_waited"]

                if play_id and play_id in content_dict:
                    hr["video_url"]  = content_dict[play_id]["url"]
                    hr["video_blurb"] = content_dict[play_id]["blurb"]
                    video_found = True
                else:
                    video_found = False

                if play_id and play_id in savant_data:
                    hr["xba"]   = savant_data[play_id]["xba"]
                    hr["parks"] = savant_data[play_id]["parks"]

                if video_found or cycles >= VIDEO_WAIT_MAX_CYCLES:
                    if self._should_post_hr(hr):
                        if hr.get("game_hr_num", 1) >= MULTI_HR_THRESHOLD:
                            homers = []
                            for idx, prior_play in hr_plays_by_batter.get(hr.get("batter_id"), []):
                                if idx > hr.get("at_bat_idx", -1):
                                    break
                                summary = self._extract_hr_summary(prior_play)
                                if summary["play_id"] and summary["play_id"] in content_dict:
                                    summary["video_url"]   = content_dict[summary["play_id"]]["url"]
                                    summary["video_blurb"] = content_dict[summary["play_id"]]["blurb"]
                                if summary["play_id"] and summary["play_id"] in savant_data:
                                    summary["xba"]   = savant_data[summary["play_id"]]["xba"]
                                    summary["parks"] = savant_data[summary["play_id"]]["parks"]
                                homers.append(summary)
                            await self._post_multi_hr_alert(channel, hr, homers)
                        else:
                            await self._post_hr_alert(channel, hr)
                    self._hr_posted.add(hr_key)
                    self._save_hr_state()
                    del self._hr_pending[hr_key]
                else:
                    self._hr_pending[hr_key]["cycles_waited"] += 1

        # ── Hitting for the cycle ────────────────────────────────────────────
        # A cycle = single + double + triple + home run by one batter in a game.
        # Build each batter's distinct hit types from the play-by-play; the play
        # that adds the 4th type is the completing hit.
        CYCLE_TYPES = ("single", "double", "triple", "home_run")
        cycle_progress: dict = {}
        for play in all_plays:
            ev = play.get("result", {}).get("eventType")
            if ev not in CYCLE_TYPES:
                continue
            matchup = play.get("matchup", {})
            bid = matchup.get("batter", {}).get("id")
            if bid is None:
                continue
            prog = cycle_progress.setdefault(bid, {
                "name":  matchup.get("batter", {}).get("fullName", "Unknown"),
                "half":  play.get("about", {}).get("halfInning", "top"),
                "types": {},      # hit type -> inning of first occurrence
                "order": [],      # hit types in the order achieved
                "complete_play": None,
            })
            if ev not in prog["types"]:
                prog["types"][ev] = play.get("about", {}).get("inning", 0)
                prog["order"].append(ev)
                if len(prog["types"]) == 4:
                    prog["complete_play"] = play

        for bid, prog in cycle_progress.items():
            if len(prog["types"]) < 4:
                continue
            cyc_key = f"{game_pk}_{bid}"
            if cyc_key in self._cycle_posted:
                continue

            cp_about = prog["complete_play"].get("about", {})
            # Skip stale cycles (bot restart > 10 min after the completing hit)
            end_time_str = cp_about.get("endTime", "")
            if end_time_str:
                try:
                    end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - end_time).total_seconds() > 600:
                        self._cycle_posted.add(cyc_key)
                        self._save_cycle_state()
                        continue
                except Exception:
                    pass

            half = prog["half"]
            cp_result = prog["complete_play"].get("result", {})
            cycle_data = {
                "player_id":   bid,
                "batter":      prog["name"],
                "batter_team": home_abbr if half == "bottom" else away_abbr,
                "away":        away_abbr,
                "home":        home_abbr,
                "away_score":  cp_result.get("awayScore", hr_away_runs),
                "home_score":  cp_result.get("homeScore", hr_home_runs),
                "types":       prog["types"],
                "natural":     prog["order"] == list(CYCLE_TYPES),
                "date":        None,  # live game — use the team's current schedule
            }
            await self._post_cycle_alert(channel, cycle_data)
            self._cycle_posted.add(cyc_key)
            self._save_cycle_state()

        # ── Walkoff detection ────────────────────────────────────────────────
        if (
            is_final
            and all_plays
            and game_pk not in self._walkoff_posted
            and game_pk not in self._walkoff_pending
        ):
            last_play   = all_plays[-1]
            last_about  = last_play.get("about", {})
            last_result = last_play.get("result", {})
            if (
                last_about.get("halfInning") == "bottom"
                and last_about.get("isScoringPlay")
                and last_result.get("homeScore", 0) > last_result.get("awayScore", 0)
            ):
                # Skip stale walkoffs (bot restart after > 20 min)
                end_time_str = last_about.get("endTime", "")
                stale = False
                if end_time_str:
                    try:
                        end_time = datetime.strptime(end_time_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                        if (datetime.now(timezone.utc) - end_time).total_seconds() > 1200:
                            stale = True
                    except Exception:
                        pass
                if stale:
                    self._walkoff_posted.add(game_pk)
                    self._save_walkoff_state()
                else:
                    batter  = last_play.get("matchup", {}).get("batter", {}).get("fullName", "Unknown")
                    pitcher = last_play.get("matchup", {}).get("pitcher", {}).get("fullName", "Unknown")
                    desc       = last_result.get("description", "")
                    inn_num    = last_about.get("inning", 9)
                    away_score = last_result.get("awayScore", 0)
                    home_score = last_result.get("homeScore", 0)

                    play_id = None
                    for evt in last_play.get("playEvents", []):
                        if evt.get("isPitch") and evt.get("details", {}).get("isInPlay"):
                            play_id = evt.get("playId")
                            break
                    if not play_id:
                        for evt in last_play.get("playEvents", []):
                            play_id = evt.get("playId")
                            if play_id:
                                break

                    wo_data = {
                        "game_pk":    game_pk,
                        "away":       away_abbr,
                        "home":       home_abbr,
                        "away_score": away_score,
                        "home_score": home_score,
                        "batter":     batter,
                        "pitcher":    pitcher,
                        "inning":     f"bot {inn_num}",
                        "desc":       desc,
                        "play_id":    play_id,
                    }
                    self._walkoff_pending[game_pk] = {
                        "cycles_waited": 0,
                        "data":          wo_data,
                        "message":       None,
                    }

        # ── Resolve walkoff video / post pending walkoff alert ───────────────
        if game_pk in self._walkoff_pending and game_pk not in self._walkoff_posted:
            pending_wo = self._walkoff_pending[game_pk]
            wo         = pending_wo["data"]
            cycles     = pending_wo["cycles_waited"]
            msg        = pending_wo["message"]

            if msg is None:
                msg = await self._post_walkoff_alert(channel, wo)
                pending_wo["message"] = msg

            video_url = video_blurb = ""
            play_id = wo.get("play_id")
            if play_id:
                wo_content = await self._fetch_content(game_pk)
                video = extract_highlight_videos(wo_content).get(play_id)
                if video:
                    video_url   = video["url"]
                    video_blurb = video["blurb"] or "Watch"

            if video_url:
                wo["video_url"]   = video_url
                wo["video_blurb"] = video_blurb
                if msg:
                    await self._edit_walkoff_alert(msg, wo)
                self._walkoff_posted.add(game_pk)
                self._save_walkoff_state()
                del self._walkoff_pending[game_pk]
            elif cycles >= WALKOFF_VIDEO_WAIT_MAX_CYCLES:
                self._walkoff_posted.add(game_pk)
                self._save_walkoff_state()
                del self._walkoff_pending[game_pk]
            else:
                pending_wo["cycles_waited"] += 1

    # ─────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────

    @tasks.loop(seconds=POLL_INTERVAL)
    async def monitor_loop(self) -> None:
        try:
            now_et    = _et_now()
            today_str = now_et.strftime("%Y-%m-%d")

            # Refresh schedule at bot startup or when the date rolls over.
            # On rollover we pass prune_finished=True so completed games are
            # evicted, but any game still Live after midnight keeps running.
            if self._schedule_date != today_str or not self._scheduled_games:
                is_new_day = self._schedule_date is not None and self._schedule_date != today_str
                await self._refresh_schedule(prune_finished=is_new_day)
                if is_new_day:
                    self._milb_ready_since = None
                    self._milb_scheduled_games.clear()
                    self._game_errors_alerted.clear()
                    print("[monitor] new calendar day — schedule merged, finished games pruned")

            if self._milb_schedule_date != today_str:
                await self._refresh_milb_schedule()

            # No alert channel configured → alerts disabled. Keep the schedule
            # fresh above (test commands use it) but skip all alert work.
            if not (getattr(self.bot, "alert_channel_id", None) or ALERT_CHANNEL_ID):
                return

            # Clear HR and walkoff state at 6am ET each day
            if now_et.hour >= 6 and self._hr_clear_date != today_str:
                self._hr_posted.clear()
                self._hr_clear_date = today_str
                self._save_hr_state()
                self._milb_ready_since = None
                print("[monitor] 6am ET — HR posted state cleared")
            if now_et.hour >= 6 and self._walkoff_clear_date != today_str:
                self._walkoff_posted.clear()
                self._walkoff_pending.clear()
                self._walkoff_clear_date = today_str
                self._save_walkoff_state()
                print("[monitor] 6am ET — walkoff posted state cleared")
            if now_et.hour >= 6 and self._cycle_clear_date != today_str:
                self._cycle_posted.clear()
                self._cycle_clear_date = today_str
                self._save_cycle_state()
                print("[monitor] 6am ET — cycle posted state cleared")
            if now_et.hour >= 6 and self._delay_clear_date != today_str:
                self._delay_state.clear()
                self._delay_clear_date = today_str
                self._save_delay_state()
                print("[monitor] 6am ET — delay state cleared")
            if now_et.hour >= 6 and self._lineup_clear_date != today_str:
                self._lineup_posted.clear()
                self._lineup_clear_date = today_str
                self._save_lineup_state()
                print("[monitor] 6am ET — lineup posted state cleared")

            # Morning performance summary at 8am ET
            if now_et.hour >= 8 and self._summary_posted_date != today_str:
                self._summary_posted_date = today_str
                self._save_summary_state()
                ch = await self._get_alert_channel()
                if ch:
                    asyncio.create_task(self._post_morning_summary(ch))

            # MiLB affiliate summary — post 5 min after all affiliate games AND the
            # MLB club's game are Final.
            fav_team = getattr(self.bot, "favorite_team", None)
            if fav_team and now_et.hour >= 12 and self._milb_summary_posted_date != today_str:
                try:
                    milb_data = await self.bot.mlb_client.get_milb_affiliate_top_performances(today_str, fav_team)
                    if milb_data is not None:
                        # Also require the MLB club's game (if any) to be Final
                        fav_upper = fav_team.upper()
                        mlb_game_done = all(
                            info.get("abstract_state") == "Final"
                            for info in self._scheduled_games.values()
                            if info.get("away", "").upper() == fav_upper
                            or info.get("home", "").upper() == fav_upper
                        )
                        if mlb_game_done:
                            if self._milb_ready_since is None:
                                self._milb_ready_since = now_et
                                print(f"[monitor] all affiliate+MLB games Final — will post MiLB summary in 5 min")
                            elif (now_et - self._milb_ready_since) >= timedelta(minutes=5):
                                self._milb_summary_posted_date = today_str
                                self._save_summary_state()
                                ch = await self._get_alert_channel()
                                if ch:
                                    asyncio.create_task(self._post_milb_affiliate_summary(ch, milb_data))
                except Exception as e:
                    print(f"[monitor] MiLB affiliate summary check error: {e}")

            # Favorite-team lineup post — runs before the cheap-sleep check because
            # lineups are published hours before games go live
            try:
                await self._check_lineup_post(now_et)
            except Exception as e:
                print(f"[monitor] lineup check error: {e}")

            # Sleep cheaply when no games are live or imminent
            if not self._any_game_active_or_imminent():
                return

            configured_id = getattr(self.bot, "alert_channel_id", None) or ALERT_CHANNEL_ID
            channel = await self._get_alert_channel()
            if channel is None:
                if configured_id:
                    print(f"[monitor] alert channel not found (ALERT_CHANNEL_ID={configured_id})")
                return

            # Process all games concurrently (MLB + MiLB affiliates)
            mlb_pks = list(self._scheduled_games.keys())
            milb_pks = list(self._milb_scheduled_games.keys())
            
            tasks = [self._process_game(pk, channel) for pk in mlb_pks] + \
                    [self._process_milb_game(pk, channel) for pk in milb_pks]
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Pair tasks with labels to identify failures
                labels = []
                pks = []
                for pk in mlb_pks:
                    info = self._scheduled_games[pk]
                    labels.append(f"MLB game {info.get('away', '???')} @ {info.get('home', '???')} (PK: {pk})")
                    pks.append(pk)
                for pk in milb_pks:
                    info = self._milb_scheduled_games[pk]
                    labels.append(f"MiLB {info.get('level', 'AAA')} game {info.get('away', '???')} @ {info.get('home', '???')} (PK: {pk})")
                    pks.append(pk)
                
                for pk, label, res in zip(pks, labels, results):
                    if isinstance(res, Exception):
                        import traceback
                        # Log to systemd / console
                        print(f"[monitor] Error processing {label}: {res}")
                        traceback.print_exception(type(res), res, res.__traceback__)
                        
                        # Warn in Discord channel once per game per session
                        if pk not in self._game_errors_alerted:
                            self._game_errors_alerted.add(pk)
                            try:
                                await channel.send(
                                    f"⚠️ **[monitor] Error processing {label}:** `{res.__class__.__name__}: {res}`\n"
                                    f"Check bot logs for the full traceback."
                                )
                            except Exception as discord_err:
                                print(f"[monitor] Failed to send error alert to Discord: {discord_err}")

        except Exception as e:
            print(f"[monitor] unhandled error: {e}")

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self.bot.wait_until_ready()
        print("[monitor] bot ready — monitor loop started")

    @commands.command(name="nh_test")
    async def nh_test(self, ctx, *args):
        """Test NH alerts with mock data. Usage: !nh_test [away] [perfect]"""
        args_lower  = [a.lower() for a in args]
        is_perfect  = any(a in ("perfect", "pg", "yes", "true") for a in args_lower)
        away_pitching = "away" in args_lower

        if away_pitching:
            # NYY (away) pitching NH vs WSH (home, 0 hits)
            # NYY has hits (top half) — those should be skipped
            # WSH breaks it with a bottom-half hit in the 8th
            mock_feed = {
                "gameData": {
                    "flags": {"noHitter": True, "perfectGame": is_perfect},
                    "status": {"abstractGameState": "Live"},
                    "teams": {
                        "away": {"abbreviation": "NYY"},
                        "home": {"abbreviation": "WSH"},
                    },
                },
                "liveData": {
                    "linescore": {
                        "currentInning": 7,
                        "isTopInning": True,
                        "outs": 2,
                        "teams": {
                            "away": {"runs": 3, "hits": 7},
                            "home": {"runs": 0, "hits": 0},
                        },
                    },
                    "boxscore": {
                        "teams": {
                            "away": {
                                "pitchers": [700002],
                                "players": {
                                    "ID700002": {
                                        "person": {"fullName": "Gerrit Cole"},
                                        "stats": {"pitching": {"inningsPitched": "6.2", "baseOnBalls": 0, "strikeOuts": 10, "pitchesThrown": 92}},
                                    }
                                },
                            }
                        }
                    },
                    "plays": {
                        "allPlays": [
                            # Away team (NYY) hits earlier — these should NOT be reported as the breaking play
                            {
                                "result": {"eventType": "home_run", "event": "Home Run", "description": "Aaron Judge homers (12) on a fly ball to left field."},
                                "matchup": {"batter": {"fullName": "Aaron Judge"}, "pitcher": {"fullName": "MacKenzie Gore"}},
                                "about": {"inning": 3, "halfInning": "top", "atBatIndex": 8},
                            },
                            {
                                "result": {"eventType": "single", "event": "Single", "description": "Juan Soto singles on a line drive to right field."},
                                "matchup": {"batter": {"fullName": "Juan Soto"}, "pitcher": {"fullName": "MacKenzie Gore"}},
                                "about": {"inning": 5, "halfInning": "top", "atBatIndex": 14},
                            },
                            # Home team (WSH) breaks the NH in the 8th — this is the correct play
                            {
                                "result": {"eventType": "single", "event": "Single", "description": "CJ Abrams singles on a ground ball up the middle."},
                                "matchup": {"batter": {"fullName": "CJ Abrams"}, "pitcher": {"fullName": "Gerrit Cole"}},
                                "about": {"inning": 8, "halfInning": "bottom", "atBatIndex": 24},
                            },
                        ]
                    },
                },
            }
            pitching_abbr = "NYY"
        else:
            # WSH (home) pitching NH vs NYY (away, 0 hits)
            # WSH has hits (bottom half) — those should be skipped
            # NYY breaks it with a top-half hit in the 8th
            mock_feed = {
                "gameData": {
                    "flags": {"noHitter": True, "perfectGame": is_perfect},
                    "status": {"abstractGameState": "Live"},
                    "teams": {
                        "away": {"abbreviation": "NYY"},
                        "home": {"abbreviation": "WSH"},
                    },
                },
                "liveData": {
                    "linescore": {
                        "currentInning": 7,
                        "isTopInning": False,
                        "outs": 2,
                        "teams": {
                            "away": {"runs": 0, "hits": 0},
                            "home": {"runs": 3, "hits": 7},
                        },
                    },
                    "boxscore": {
                        "teams": {
                            "home": {
                                "pitchers": [700001],
                                "players": {
                                    "ID700001": {
                                        "person": {"fullName": "MacKenzie Gore"},
                                        "stats": {"pitching": {"inningsPitched": "6.2", "baseOnBalls": 1, "strikeOuts": 8, "pitchesThrown": 98}},
                                    }
                                },
                            }
                        }
                    },
                    "plays": {
                        "allPlays": [
                            # Home team (WSH) hits earlier — these should NOT be reported as the breaking play
                            {
                                "result": {"eventType": "single", "event": "Single", "description": "CJ Abrams singles on a ground ball to second base."},
                                "matchup": {"batter": {"fullName": "CJ Abrams"}, "pitcher": {"fullName": "Gerrit Cole"}},
                                "about": {"inning": 2, "halfInning": "bottom", "atBatIndex": 5},
                            },
                            {
                                "result": {"eventType": "home_run", "event": "Home Run", "description": "Jesse Winker homers (3) on a fly ball to left field."},
                                "matchup": {"batter": {"fullName": "Jesse Winker"}, "pitcher": {"fullName": "Gerrit Cole"}},
                                "about": {"inning": 4, "halfInning": "bottom", "atBatIndex": 13},
                            },
                            # Away team (NYY) breaks the NH in the 8th — this is the correct play
                            {
                                "result": {"eventType": "single", "event": "Single", "description": "Gleyber Torres singles on a line drive to left field."},
                                "matchup": {"batter": {"fullName": "Gleyber Torres"}, "pitcher": {"fullName": "MacKenzie Gore"}},
                                "about": {"inning": 8, "halfInning": "top", "atBatIndex": 24},
                            },
                        ]
                    },
                },
            }
            pitching_abbr = "WSH"

        await ctx.message.delete()
        # In-progress alert (no delay)
        await self._post_nh_alert(ctx.channel, mock_feed, 0)
        # Broken-up alert (no delay)
        mock_feed["gameData"]["flags"]["noHitter"] = False
        mock_feed["gameData"]["flags"]["perfectGame"] = False
        await self._post_nh_broken_alert(ctx.channel, mock_feed, is_perfect, pitching_abbr=pitching_abbr)


    @commands.command(name="lineup_test")
    async def lineup_test(self, ctx):
        """Test the favorite-team lineup post for today's game. Usage: !lineup_test"""
        await ctx.message.delete()
        fav_team = getattr(self.bot, "favorite_team", None)
        if not fav_team:
            await ctx.channel.send("No FAVORITE_TEAM configured.")
            return
        fav_upper = fav_team.upper()
        for pk, info in self._scheduled_games.items():
            if info.get("away", "").upper() == fav_upper:
                side = "away"
            elif info.get("home", "").upper() == fav_upper:
                side = "home"
            else:
                continue
            client = self.bot.mlb_client
            session = await client.get_session()
            async with session.get(f"{client.BASE_URL}/game/{pk}/boxscore") as resp:
                if resp.status != 200:
                    await ctx.channel.send(f"Boxscore fetch for game {pk} returned {resp.status}.")
                    return
                box_data = await resp.json()
            if len(box_data.get("teams", {}).get(side, {}).get("battingOrder", [])) < 9:
                await ctx.channel.send(f"Lineup for game {pk} not published yet.")
                return
            await self._post_lineup(ctx.channel, box_data, side, info.get("start_et"), game_pk=pk)
            return
        await ctx.channel.send(f"No {fav_upper} game scheduled today.")

    @commands.command(name="summary_test")
    async def summary_test(self, ctx, date: str = None):
        """Test the morning performance summary. Usage: !summary_test [YYYY-MM-DD]"""
        await ctx.message.delete()
        await self._post_morning_summary(ctx.channel, date)

    @commands.command(name="milb_summary_test")
    async def milb_summary_test(self, ctx, date: str = None):
        """Test the MiLB affiliate summary. Usage: !milb_summary_test [YYYY-MM-DD]"""
        await ctx.message.delete()
        fav_team = getattr(self.bot, "favorite_team", None)
        if not fav_team:
            await ctx.channel.send("No FAVORITE_TEAM configured.")
            return
        if date is None:
            et_now = _et_now()
            date = (et_now - timedelta(days=1)).strftime("%Y-%m-%d")
        data = await self.bot.mlb_client.get_milb_affiliate_top_performances(date, fav_team)
        if data is None:
            await ctx.channel.send(f"No completed affiliate games found for {date}.")
            return
        await self._post_milb_affiliate_summary(ctx.channel, data)

    @commands.command(name="milb_hr_test")
    async def milb_hr_test(self, ctx):
        """Test MiLB HR alert with mock data. Usage: !milb_hr_test"""
        mock_hr = {
            "batter":      "James Wood",
            "batter_team": "WIL",
            "pitcher":     "Jake Cousins",
            "pitcher_team": "BOW",
            "away":        "WIL",
            "home":        "BOW",
            "away_score":  3,
            "home_score":  2,
            "level":       "A+",
            "dist":        412,
            "ev":          108.2,
            "la":          32,
            "pitch_type":  "Curveball",
            "pitch_speed": 78.4,
            "num":         5,
            "inning":      "top 4",
            "desc":        "James Wood homers (5) on a fly ball to right center field.",
        }
        await ctx.message.delete()
        await self._post_milb_hr_alert(ctx.channel, mock_hr)

    @commands.command(name="hr_test")
    async def hr_test(self, ctx):
        """Test HR alert with mock data. Usage: !hr_test"""
        mock_hr = {
            "batter":      "Mickey Moniak",
            "batter_team": "COL",
            "pitcher":     "Corbin Burnes",
            "pitcher_team": "ATH",
            "away":        "ATH",
            "home":        "COL",
            "dist":        438,
            "ev":          112.4,
            "la":          28,
            "pitch_type":  "Four-Seam Fastball",
            "pitch_speed": 95.2,
            "rbi":         2,
            "num":         11,
            "inning":      "bot 5",
            "desc":        "Mickey Moniak homers (11) on a fly ball to left center field. Charlie Blackmon scores.",
            "play_id":     None,
            "game_pk":     0,
            "video_url":   "",
            "video_blurb": "",
            "xba":         0.891,
            "parks":       29,
        }
        await ctx.message.delete()
        await self._post_hr_alert(ctx.channel, mock_hr)

    @commands.command(name="multi_hr_test")
    async def multi_hr_test(self, ctx):
        """Test the multi-homer-game alert with mock data. Usage: !multi_hr_test"""
        base_hr = {
            "batter":      "Mickey Moniak",
            "batter_team": "COL",
            "away":        "ATH",
            "home":        "COL",
            "away_score":  3,
            "home_score":  5,
        }
        homers = [
            {
                "pitcher": "Corbin Burnes", "desc": "Mickey Moniak homers (11) on a fly ball to left field.",
                "inning": "bot 2", "dist": 402, "ev": 104.1, "la": 26,
                "pitch_type": "Sinker", "pitch_speed": 94.8, "play_id": None,
                "video_url": "https://www.mlb.com/video/moniak-homers-11", "video_blurb": "Moniak's 11th home run of the season",
                "xba": 0.720, "parks": 27,
            },
            {
                "pitcher": "Corbin Burnes", "desc": "Mickey Moniak homers (12) on a fly ball to left center field. Charlie Blackmon scores.",
                "inning": "bot 5", "dist": 438, "ev": 112.4, "la": 28,
                "pitch_type": "Four-Seam Fastball", "pitch_speed": 95.2, "play_id": None,
                "video_url": "https://www.mlb.com/video/moniak-homers-12", "video_blurb": "Moniak's 12th home run of the season",
                "xba": 0.891, "parks": 29,
            },
        ]
        mock_hr = {**base_hr, "game_hr_num": len(homers)}
        await ctx.message.delete()
        await self._post_multi_hr_alert(ctx.channel, mock_hr, homers)

    @commands.command(name="cycle_test")
    async def cycle_test(self, ctx, player: str = "CJ Abrams", *, date: str = None):
        """Render a cycle alert using a real player's at-bats. Usage: !cycle_test <player> [date]

        Pulls the player's real game line + play-by-play (regardless of whether
        they actually cycled) so the alert formatting can be previewed.
        """
        resolved = await self.bot.mlb_client.resolve_player(player)
        if not resolved:
            await ctx.send(f"Could not find player '{player}'.")
            return
        mock_cycle = {
            "player_id":   resolved["id"],
            "batter":      resolved["name"],
            "batter_team": "",
            "away":        "",
            "home":        "",
            "natural":     False,
            "date":        date,
        }
        await ctx.message.delete()
        await self._post_cycle_alert(ctx.channel, mock_cycle)

    @commands.command(name="delay_test")
    async def delay_test(self, ctx):
        """Preview the delay + resume alerts. Usage: !delay_test"""
        await ctx.message.delete()
        await self._post_delay_alert(ctx.channel, "PHI", "WSH", True, "Delayed Start", "Preview", "Rain")
        await self._post_delay_alert(ctx.channel, "PHI", "WSH", False, "Warmup", "Live", "")
        await self._post_delay_alert(ctx.channel, "PHI", "WSH", True, "Delayed", "Live", "Rain")
        await self._post_delay_alert(ctx.channel, "PHI", "WSH", False, "In Progress", "Live", "")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MonitorCog(bot))
