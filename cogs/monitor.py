"""
monitor.py — Live MLB game monitoring cog.

Posts to ALERT_CHANNEL_ID automatically when:
  1. A no-hitter or perfect game is in progress (updates every inning change).
  2. A notable home run is hit (≥420 ft, favorite team HR, ≤5-park HR, or xBA < .200),
     once a highlight video is available.

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

import discord
from discord.ext import commands, tasks

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
_STATE_DIR            = os.getenv("STATE_DIR", ".")
HR_STATE_FILE         = os.path.join(_STATE_DIR, "hr_posted.json")
NH_STATE_FILE         = os.path.join(_STATE_DIR, "nh_state.json")
SUMMARY_STATE_FILE    = os.path.join(_STATE_DIR, "summary_state.json")
VIDEO_WAIT_MAX_CYCLES = 10                  # Poll cycles to wait for highlight video
NH_ALERT_DELAY        = 15                  # Seconds to delay NH alerts (stream spoiler protection)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _et_now() -> datetime:
    """Return the current time in US/Eastern (UTC-4 during baseball season)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=4)


def _parse_game_time(game_date_str: str):
    """Parse the gameDate field (ISO 8601 UTC) into a naive ET datetime."""
    if not game_date_str:
        return None
    try:
        dt_utc = datetime.strptime(game_date_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt_utc - timedelta(hours=4)
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
        self._summary_posted_date = None       # date string for which we've posted the morning summary
        self._milb_summary_posted_date = None  # date string for which we've posted the MiLB affiliate summary
        self._milb_ready_since: "datetime | None" = None  # when all MLB+MiLB games first went Final

        self._load_hr_state()
        self._load_nh_state()
        self._load_summary_state()
        self.monitor_loop.start()

    def cog_unload(self):
        self.monitor_loop.cancel()

    # ─────────────────────────────────────────────
    # Schedule helpers
    # ─────────────────────────────────────────────

    def _load_hr_state(self) -> None:
        try:
            with open(HR_STATE_FILE) as f:
                self._hr_posted = set(json.load(f))
            print(f"[monitor] loaded {len(self._hr_posted)} posted HR key(s) from disk")
        except (FileNotFoundError, json.JSONDecodeError):
            self._hr_posted = set()

    def _save_hr_state(self) -> None:
        tmp = HR_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(list(self._hr_posted), f)
            os.replace(tmp, HR_STATE_FILE)
        except Exception as e:
            print(f"[monitor] failed to save HR state: {e}")

    def _load_summary_state(self) -> None:
        try:
            with open(SUMMARY_STATE_FILE) as f:
                state = json.load(f)
            self._summary_posted_date = state.get("date")
            self._milb_summary_posted_date = state.get("milb_date")
            print(f"[monitor] loaded summary state: mlb={self._summary_posted_date} milb={self._milb_summary_posted_date}")
        except (FileNotFoundError, json.JSONDecodeError):
            self._summary_posted_date = None
            self._milb_summary_posted_date = None

    def _save_summary_state(self) -> None:
        tmp = SUMMARY_STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"date": self._summary_posted_date, "milb_date": self._milb_summary_posted_date}, f)
            os.replace(tmp, SUMMARY_STATE_FILE)
        except Exception as e:
            print(f"[monitor] failed to save summary state: {e}")

    def _load_nh_state(self) -> None:
        try:
            with open(NH_STATE_FILE) as f:
                data = json.load(f)
            # alert_key is stored as a list [inning, half] — restore as tuple
            self._nh_alerted = {
                int(pk): {**v, "key": tuple(v["key"])}
                for pk, v in data.get("nh_alerted", {}).items()
            }
            self._nh_broken_posted = set(int(pk) for pk in data.get("nh_broken_posted", []))
            print(f"[monitor] loaded NH state: {len(self._nh_alerted)} active, {len(self._nh_broken_posted)} broken")
        except (FileNotFoundError, json.JSONDecodeError):
            self._nh_alerted = {}
            self._nh_broken_posted = set()

    def _save_nh_state(self) -> None:
        try:
            data = {
                "nh_alerted": {
                    str(pk): {**v, "key": list(v["key"])}
                    for pk, v in self._nh_alerted.items()
                },
                "nh_broken_posted": list(self._nh_broken_posted),
            }
            tmp = NH_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, NH_STATE_FILE)
        except Exception as e:
            print(f"[monitor] failed to save NH state: {e}")

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

        _level_abbrev = {"Triple-A": "AAA", "Double-A": "AA", "High-A": "A+", "Single-A": "A", "Rookie": "Rk"}
        affiliate_ids = {t['id']: _level_abbrev.get(t.get('level', ''), t.get('level', ''))
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
                level = affiliate_ids.get(away_id) or affiliate_ids.get(home_id, '')
                new_games[pk] = {
                    "start_et":      _parse_game_time(g.get("gameDate", "")),
                    "away":          away_team.get("abbreviation", "???"),
                    "home":          home_team.get("abbreviation", "???"),
                    "abstract_state": g.get("status", {}).get("abstractGameState", "Preview"),
                    "level":         level,
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
        """
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
        widths  = {k: len(v) for k, v in headers.items()}
        for row in pitchers:
            for k in labels:
                widths[k] = max(widths[k], len(str(row.get(k, ""))))
        def fmt_row(row):
            return " ".join(
                str(row.get(k, "")).ljust(widths[k]) if k == "pitcher"
                else str(row.get(k, "")).rjust(widths[k])
                for k in labels
            )
        header = " ".join(
            headers[k].ljust(widths[k]) if k == "pitcher"
            else headers[k].rjust(widths[k])
            for k in labels
        )
        return header + "\n" + "\n".join(fmt_row(r) for r in pitchers)

    async def _delayed_nh_alert(self, channel, feed: dict, game_pk: int) -> None:
        await asyncio.sleep(NH_ALERT_DELAY)
        await self._post_nh_alert(channel, feed, game_pk)

    async def _delayed_nh_broken_alert(self, channel, feed: dict, was_perfect: bool, pitching_abbr: str = None) -> None:
        await asyncio.sleep(NH_ALERT_DELAY)
        await self._post_nh_broken_alert(channel, feed, was_perfect, pitching_abbr)

    async def _delayed_nh_tune_in_alert(self, channel, feed: dict, game_pk: int, pitching_abbr: str, batting_side: str) -> None:
        await asyncio.sleep(NH_ALERT_DELAY)
        await self._post_nh_tune_in_alert(channel, feed, game_pk, pitching_abbr, batting_side)

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

    def _should_post_hr(self, hr: dict) -> bool:
        if HR_ALWAYS_ALERT_TEAM and hr["batter_team"] == HR_ALWAYS_ALERT_TEAM:
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
        matchup = f"{away}@{home}" if away and home else team

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
        video_url  = hr.get("video_url", "")
        video_blurb = hr.get("video_blurb", "Watch")

        away    = hr.get("away", "")
        home    = hr.get("home", "")
        matchup = f"{away}@{home}" if away and home else team
        num_str = f" (#{hr_num})" if hr_num else ""

        title = f"💣 {level} {matchup} — ({team}) {batter}{num_str}"

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
        if video_url:
            body += f"\n> [🎥 **{video_blurb or 'Watch'}**]({video_url})"

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

        live_data  = feed.get("liveData", {})
        all_plays  = live_data.get("plays", {}).get("allPlays", [])
        sched_info = self._milb_scheduled_games.get(game_pk, {})
        away_abbr  = sched_info.get("away", "???")
        home_abbr  = sched_info.get("home", "???")
        level      = sched_info.get("level", "MiLB")

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
            desc    = play.get("result", {}).get("description", "")
            half    = about.get("halfInning", "top")
            inn_num = about.get("inning", 0)
            batter_team = home_abbr if half == "bottom" else away_abbr

            hr_num = 0
            for keyword in ("grand slam", "home run", "homers"):
                if keyword in desc:
                    m = re.search(r'\((\d+)\)', desc[desc.index(keyword):])
                    if m:
                        hr_num = int(m.group(1))
                    break

            hr_data = {
                "batter":       batter,
                "batter_team":  batter_team,
                "pitcher":      pitcher,
                "away":         away_abbr,
                "home":         home_abbr,
                "level":        level,
                "dist":         dist,
                "ev":           ev,
                "la":           la,
                "pitch_type":   pitch_type,
                "pitch_speed":  pitch_spd,
                "num":          hr_num,
                "inning":       f"{'bot' if half == 'bottom' else 'top'} {inn_num}",
                "desc":         desc,
                "play_id":      play_id,
                "game_pk":      game_pk,
                "video_url":    "",
                "video_blurb":  "",
            }

            if hr_key not in self._hr_pending:
                self._hr_pending[hr_key] = {"cycles_waited": 0, "data": hr_data, "milb": True}

        pending_here = {
            k: v for k, v in self._hr_pending.items()
            if v["data"]["game_pk"] == game_pk and v.get("milb")
        }
        if not pending_here:
            return

        content_data = await self._fetch_content(game_pk)
        content_dict = {}
        for item in content_data.get("highlights", {}).get("highlights", {}).get("items", []):
            if "guid" in item:
                for pb in item.get("playbacks", []):
                    if pb.get("name") == "mp4Avc":
                        content_dict[item["guid"]] = {
                            "url":   pb["url"],
                            "blurb": item.get("headline", item.get("blurb", "")),
                        }
                        break

        for hr_key, pending in list(pending_here.items()):
            if hr_key in self._hr_posted:
                continue
            hr      = pending["data"]
            play_id = hr.get("play_id")
            cycles  = pending["cycles_waited"]

            if play_id and play_id in content_dict:
                hr["video_url"]   = content_dict[play_id]["url"]
                hr["video_blurb"] = content_dict[play_id]["blurb"]
                video_found = True
            else:
                video_found = False

            if video_found or cycles >= VIDEO_WAIT_MAX_CYCLES:
                await self._post_milb_hr_alert(channel, hr)
                self._hr_posted.add(hr_key)
                self._save_hr_state()
                del self._hr_pending[hr_key]
            else:
                self._hr_pending[hr_key]["cycles_waited"] += 1

    # ─────────────────────────────────────────────
    # Per-game processing
    # ─────────────────────────────────────────────

    async def _process_game(self, game_pk: int, channel) -> None:
        feed = await self._fetch_live_feed(game_pk)
        if not feed:
            return

        game_data = feed.get("gameData", {})
        live_data = feed.get("liveData", {})
        ab_state  = game_data.get("status", {}).get("abstractGameState", "Preview")

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

        if is_pg or is_nh:
            # Post once per inning transition, but only after the pitching team completes their half inning:
            # - Home pitching (top half): alert when is_top=False (top just ended)
            # - Away pitching (bottom half): alert when is_top=True (bottom just ended)
            alert_key = (inning, "final" if is_final else is_top)
            stored = self._nh_alerted.get(game_pk)

            # Determine which team is throwing the NH so break-up alerts find the right hit
            nh_away_abbr = game_data.get("teams", {}).get("away", {}).get("abbreviation", "???")
            nh_home_abbr = game_data.get("teams", {}).get("home", {}).get("abbreviation", "???")
            away_hits    = linescore.get("teams", {}).get("away", {}).get("hits", 0)
            nh_pitching  = nh_home_abbr if away_hits == 0 else nh_away_abbr
            home_pitching = (nh_pitching == nh_home_abbr)

            # Only alert after the pitching team finishes their half inning
            should_alert = is_final or (home_pitching and not is_top) or (not home_pitching and is_top)

            # Tune-in alert: fires when entering the pitching team's half inning at inning 9+
            entering_pitching_half = (home_pitching and is_top) or (not home_pitching and not is_top)
            stored_tune_in         = (stored or {}).get("tune_in_inning", 0)
            should_tune_in         = inning >= 9 and entering_pitching_half and inning > stored_tune_in

            key_changed = stored is None or stored["key"] != alert_key
            if key_changed or should_tune_in:
                self._nh_alerted[game_pk] = {
                    "key":           alert_key,
                    "perfect":       is_pg,
                    "pitching_abbr": nh_pitching,
                    "tune_in_inning": inning if should_tune_in else stored_tune_in,
                }
                self._save_nh_state()
                if key_changed and should_alert:
                    asyncio.create_task(self._delayed_nh_alert(channel, feed, game_pk))
                if should_tune_in:
                    batting_side = "away" if home_pitching else "home"
                    asyncio.create_task(self._delayed_nh_tune_in_alert(channel, feed, game_pk, nh_pitching, batting_side))
        else:
            # Flag was cleared — post break-up alert if we were tracking this game
            nh_changed = False
            if game_pk in self._nh_alerted and game_pk not in self._nh_broken_posted:
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
        all_plays = live_data.get("plays", {}).get("allPlays", [])
        sched_info = self._scheduled_games.get(game_pk, {})
        away_abbr  = sched_info.get("away", "???")
        home_abbr  = sched_info.get("home", "???")

        for play in all_plays:
            if play.get("result", {}).get("eventType") != "home_run":
                continue

            about      = play.get("about", {})
            at_bat_idx = about.get("atBatIndex", 0)
            hr_key     = f"{game_pk}_{at_bat_idx}"

            if hr_key in self._hr_posted:
                continue

            # Skip plays older than 10 minutes — catches stale HRs on restart
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

            hr_num = 0
            for keyword in ("grand slam", "home run", "homers"):
                if keyword in desc:
                    m = re.search(r'\((\d+)\)', desc[desc.index(keyword):])
                    if m:
                        hr_num = int(m.group(1))
                    break

            pitcher_team = away_abbr if half == "bottom" else home_abbr

            hr_data = {
                "batter":       batter,
                "batter_team":  batter_team,
                "pitcher":      pitcher,
                "pitcher_team": pitcher_team,
                "away":         away_abbr,
                "home":         home_abbr,
                "dist":         dist,
                "ev":           ev,
                "la":           la,
                "pitch_type":   pitch_type,
                "pitch_speed":  pitch_spd,
                "rbi":          rbi,
                "num":          hr_num,
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
        if not pending_here:
            return

        content_data = await self._fetch_content(game_pk)
        content_dict = {}
        for item in content_data.get("highlights", {}).get("highlights", {}).get("items", []):
            if "guid" in item:
                for pb in item.get("playbacks", []):
                    if pb.get("name") == "mp4Avc":
                        content_dict[item["guid"]] = {
                            "url":   pb["url"],
                            "blurb": item.get("headline", item.get("blurb", "")),
                        }
                        break

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
                    await self._post_hr_alert(channel, hr)
                self._hr_posted.add(hr_key)
                self._save_hr_state()
                del self._hr_pending[hr_key]
            else:
                self._hr_pending[hr_key]["cycles_waited"] += 1

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
                    print("[monitor] new calendar day — schedule merged, finished games pruned")

            if self._milb_schedule_date != today_str:
                await self._refresh_milb_schedule()

            # Clear HR state at 6am ET each day
            if now_et.hour >= 6 and self._hr_clear_date != today_str:
                self._hr_posted.clear()
                self._save_hr_state()
                self._hr_clear_date = today_str
                self._milb_ready_since = None
                print("[monitor] 6am ET — HR posted state cleared")

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

            # Sleep cheaply when no games are live or imminent
            if not self._any_game_active_or_imminent():
                return

            channel = await self._get_alert_channel()
            if channel is None:
                print(f"[monitor] alert channel not found (ALERT_CHANNEL_ID={getattr(self.bot, 'alert_channel_id', None) or ALERT_CHANNEL_ID})")
                return

            # Process all games concurrently (MLB + MiLB affiliates)
            await asyncio.gather(
                *(self._process_game(pk, channel) for pk in self._scheduled_games),
                *(self._process_milb_game(pk, channel) for pk in self._milb_scheduled_games),
                return_exceptions=True,
            )

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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MonitorCog(bot))
