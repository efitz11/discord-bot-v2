"""
monitor.py — Live MLB game monitoring cog.

Posts to ALERT_CHANNEL_ID automatically when:
  1. A no-hitter or perfect game is in progress (updates every inning change).
  2. A home run with projected distance >= HR_DISTANCE_THRESHOLD feet is hit,
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

ALERT_CHANNEL_ID     = 733459392263094335   # Channel to post alerts into
POLL_INTERVAL        = 60                   # Seconds between monitor ticks
WAKEUP_WINDOW_MINUTES = 30                  # Start polling when a game is this close
HR_DISTANCE_THRESHOLD = 420                 # Feet — minimum projected distance for alert
HR_ALWAYS_ALERT_TEAM  = "WSH"              # Always alert for this team's HRs regardless of distance
HR_STATE_FILE         = "hr_posted.json"   # Persists posted HR keys across restarts
VIDEO_WAIT_MAX_CYCLES = 10                  # Poll cycles to wait for highlight video

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

        # No-hitter tracking: {game_pk: (inning, inning_key)}
        # We post once per half-inning transition.
        self._nh_alerted: dict = {}

        # HR tracking
        self._hr_pending: dict = {}  # {hr_key: {"cycles_waited": int, "data": dict}}
        self._hr_posted: set = set() # hr_keys already posted

        self._load_hr_state()
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
        try:
            with open(HR_STATE_FILE, "w") as f:
                json.dump(list(self._hr_posted), f)
        except Exception as e:
            print(f"[monitor] failed to save HR state: {e}")

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
                # HR state intentionally kept — _hr_posted is a set and harmless;
                # _hr_pending entries expire naturally via VIDEO_WAIT_MAX_CYCLES.
            if finished_pks:
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
        ch = self.bot.get_channel(ALERT_CHANNEL_ID)
        if ch:
            return ch
        try:
            return await self.bot.fetch_channel(ALERT_CHANNEL_ID)
        except Exception:
            return None

    # ─────────────────────────────────────────────
    # Alert builders
    # ─────────────────────────────────────────────

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

        alert_word = "PERFECT GAME" if is_perfect else "NO-HITTER"
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
        embed.add_field(name="Matchup", value=f"**{away_abbr} @ {home_abbr}**", inline=True)
        embed.add_field(name="Score",   value=score_line,                        inline=True)
        embed.add_field(name="Inning",  value=inning_desc,                       inline=True)
        if pitchers:
            table = self._build_nh_pitcher_table(pitchers)
            embed.add_field(name="Pitchers", value=f"```\n{table}\n```", inline=False)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            print(f"[monitor] failed to post NH alert: {e}")

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

        num_str = f" (#{hr_num})" if hr_num else ""
        title   = f"💣 {team} — {batter}{num_str} | {dist} ft"

        parts = []
        if pitch_type and pitch_spd:
            parts.append(f"{pitch_spd:.1f} mph {pitch_type}")
        if ev:
            parts.append(f"{ev:.1f} mph EV")
        if la:
            parts.append(f"{la}° LA")

        desc_fmt = desc.replace(batter, f"**{batter}**", 1)
        body = f"**{inning}:** With **{pitcher}** pitching, {desc_fmt}"
        if parts:
            body += f"\n> *{' | '.join(parts)}*"
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
            # Post once per inning transition
            alert_key = (inning, "final" if is_final else is_top)
            if self._nh_alerted.get(game_pk) != alert_key:
                await self._post_nh_alert(channel, feed, game_pk)
                self._nh_alerted[game_pk] = alert_key
        else:
            # Flag was cleared (hit allowed) — clean up tracker
            self._nh_alerted.pop(game_pk, None)

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

            if dist < HR_DISTANCE_THRESHOLD and batter_team != HR_ALWAYS_ALERT_TEAM:
                continue

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
            }

            if hr_key not in self._hr_pending:
                self._hr_pending[hr_key] = {"cycles_waited": 0, "data": hr_data}

        # ── Resolve videos for this game's pending HRs ───────────────────────
        pending_here = {
            k: v for k, v in self._hr_pending.items()
            if v["data"]["game_pk"] == game_pk
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
                hr["video_url"]  = content_dict[play_id]["url"]
                hr["video_blurb"] = content_dict[play_id]["blurb"]
                video_found = True
            else:
                video_found = False

            if video_found or cycles >= VIDEO_WAIT_MAX_CYCLES:
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
                    self._hr_posted.clear()
                    self._save_hr_state()
                    print("[monitor] new calendar day — schedule merged, finished games pruned, HR state cleared")

            # Sleep cheaply when no games are live or imminent
            if not self._any_game_active_or_imminent():
                return

            channel = await self._get_alert_channel()
            if channel is None:
                print(f"[monitor] alert channel {ALERT_CHANNEL_ID} not found")
                return

            # Process all games concurrently
            await asyncio.gather(
                *(self._process_game(pk, channel) for pk in self._scheduled_games),
                return_exceptions=True,
            )

        except Exception as e:
            print(f"[monitor] unhandled error: {e}")

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self.bot.wait_until_ready()
        print("[monitor] bot ready — monitor loop started")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MonitorCog(bot))
