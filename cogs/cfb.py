import os
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord import Interaction
from discord.ext import commands, tasks
from cogs.espn_base import _format_linescore, _format_pregame, FINAL_STATUSES
from cogs.football import FootballCog
from cogs.monitor import MonitorCog
from core.utils import et_now

CONFERENCES_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard/conferences"
# The plain /teams endpoint ignores the `groups` filter and returns all divisions in an
# arbitrary order truncated by `limit`, which silently drops FBS teams (e.g. Tennessee).
# The FBS standings tree is grouped by conference and reliably lists every FBS team.
FBS_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/college-football/standings?group=80"
RANKED_VALUE       = "RANKED"
TOP_RANKED_N       = 10

# ── Upset alerts ──────────────────────────────────────────────────────────────
UPSET_CHANNEL_ID       = int(os.getenv("CFB_UPSET_CHANNEL_ID", "0")) or None  # None disables upset alerts
UPSET_STATE_FILE       = os.path.join(os.getenv("STATE_DIR", "."), "cfb_upset_state.json")
UPSET_POLL_SECONDS     = 60
UPSET_IDLE_POLL_MINUTES = 10   # Scoreboard re-check interval while no FBS game is live
UPSET_DAY_ROLLOVER_HOURS = 6   # Scoreboard "day" runs until 6am ET
UPSET_TWO_MIN_SECONDS  = 120
UNRANKED               = 99

# Stages in the order they occur; each is evaluated (and possibly posted) once per game
STAGE_Q4      = "q4"
STAGE_TWO_MIN = "two_min"
STAGE_FINAL   = "final"


def _rank(competitor: dict) -> int:
    rank = competitor.get("curatedRank", {}).get("current") or UNRANKED
    return rank if rank <= 25 else UNRANKED


def _upset_sides(comp: dict):
    """Return (favorite, underdog) competitors if a Top 25 team faces a lower-ranked
    or unranked opponent, else None."""
    a, b = comp["competitors"]
    fav, dog = (a, b) if _rank(a) < _rank(b) else (b, a)
    if _rank(fav) == UNRANKED or _rank(fav) == _rank(dog):
        return None
    return fav, dog


def _underdog_leading(fav: dict, dog: dict) -> bool:
    try:
        return int(dog.get("score", 0)) > int(fav.get("score", 0))
    except ValueError:
        return False


def _upset_stage(comp: dict) -> str | None:
    """The alert stage this scoreboard snapshot falls in, if any."""
    status = comp["status"]
    name   = status["type"]["name"]
    if name in FINAL_STATUSES:
        return STAGE_FINAL
    if status["type"].get("state") != "in":
        return None
    period = status.get("period", 0)
    clock  = status.get("clock", 0.0)
    if period == 4:
        return STAGE_TWO_MIN if clock <= UPSET_TWO_MIN_SECONDS else STAGE_Q4
    if period == 3 and name == "STATUS_END_PERIOD":
        return STAGE_Q4
    return None


class CFBCog(FootballCog):
    SLUG  = "college-football"
    SPORT = "NCAAF"

    def __init__(self, bot):
        super().__init__(bot)
        self._conferences: list[dict] = []
        # {event_id: {"stages": [stages already evaluated], "date": scoreboard YYYYMMDD}} — an entry
        # means the game was seen live, so finals that happened while the bot was down never alert.
        self._upset_state: dict[str, dict] = MonitorCog._load_json(UPSET_STATE_FILE) or {}
        self._upset_next_fetch: datetime | None = None

    async def cog_load(self):
        if UPSET_CHANNEL_ID:
            self.upset_loop.start()

    async def cog_unload(self):
        self.upset_loop.cancel()

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(FBS_STANDINGS_URL) as resp:
                data = await resp.json()
            self._teams = [
                {"abbreviation": e["team"]["abbreviation"], "displayName": e["team"]["displayName"]}
                for conf in data.get("children", [])
                for e in conf.get("standings", {}).get("entries", [])
            ]
            print(f"[{self.SLUG}] loaded {len(self._teams)} teams")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load teams: {e}")

    async def _load_extra(self):
        await super()._load_extra()
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(CONFERENCES_URL) as resp:
                data = await resp.json()
            self._conferences = [c for c in data.get("conferences", []) if c.get("groupId") != "80"]
            print(f"[{self.SLUG}] loaded {len(self._conferences)} conferences")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load conferences: {e}")

    cfb = app_commands.Group(name="cfb", description="College football scores and info")

    @cfb.command(name="score", description="Get the score for a college football team, conference, or the top 25")
    @app_commands.describe(
        team="Team, conference, or 'Top 25 Games'",
        week="Week to show (defaults to the current week)",
    )
    async def score(self, interaction: Interaction, team: str, week: str = None):
        extra_params, week_label = self._resolve_week(week)

        if team == RANKED_VALUE:
            await self._ranked_games_impl(interaction, extra_params, week_label)
            return

        conf = next((c for c in self._conferences if c["groupId"] == team), None)
        if conf:
            await self._score_impl(interaction, groups=team, group_label=f"{conf['shortName']} Games",
                                    extra_params=extra_params, when_label=week_label)
            return

        # Scope to FBS (group 80) — the scoreboard endpoint defaults to a small
        # highlighted subset of games when no group is specified, which can miss teams.
        team_params = {**(extra_params or {}), "groups": "80"}
        await self._score_impl(interaction, team, extra_params=team_params, when_label=week_label)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: Interaction, current: str):
        cur     = current.lower()
        choices = []
        if not current or "top" in cur or "rank" in cur or "25" in cur:
            choices.append(app_commands.Choice(name="Top 25 Games", value=RANKED_VALUE))
        conf_matches = [c for c in self._conferences if cur in c["name"].lower() or cur in c["shortName"].lower()]
        choices += [app_commands.Choice(name=f"{c['shortName']} (Conference)", value=c["groupId"]) for c in conf_matches[:10]]
        remaining = 25 - len(choices)
        team_matches = [t for t in self._teams if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()]
        choices += [app_commands.Choice(name=t["displayName"], value=t["abbreviation"]) for t in team_matches[:remaining]]
        return choices[:25]

    @score.autocomplete("week")
    async def week_autocomplete(self, interaction: Interaction, current: str):
        return await self._week_autocomplete(current)

    async def _ranked_games_impl(self, interaction: Interaction, extra_params: dict = None, week_label: str = None):
        await interaction.response.defer()

        params = {"groups": "80"}
        if extra_params:
            params.update(extra_params)

        session = await self.bot.mlb_client.get_session()
        async with session.get(self._scoreboard_url, params=params) as resp:
            data = await resp.json()

        if not week_label:
            wk = data.get("week", {}).get("number")
            week_label = f"Week {wk}" if wk else "This Week"

        ranked = []
        for event in data.get("events", []):
            comp  = event["competitions"][0]
            ranks = [c.get("curatedRank", {}).get("current") or 99 for c in comp["competitors"]]
            best  = min(r if r <= 25 else 99 for r in ranks)
            if best <= 25:
                ranked.append((best, comp))
        ranked.sort(key=lambda x: x[0])
        top = ranked[:TOP_RANKED_N]

        if not top:
            await interaction.followup.send(f"No ranked games for {week_label}.")
            return

        top.sort(key=lambda x: x[1].get("date", ""))

        blocks   = []
        any_live = False
        for _best, comp in top:
            p = self._parse_comp(comp)
            if p["status_name"] == "STATUS_SCHEDULED":
                table = _format_pregame(p["away_prefix"], p["away_abbr"], p["home_prefix"], p["home_abbr"],
                                         p["odds"], p["broadcast"])
            else:
                labels = self._linescore_labels(max(len(p["away_qs"]), len(p["home_qs"]), 1))
                table  = _format_linescore(p["away_prefix"], p["away_abbr"], p["away_qs"],
                                            p["home_prefix"], p["home_abbr"], p["home_qs"],
                                            p["away_total"], p["home_total"], labels)
            is_live = p["status_name"] not in FINAL_STATUSES and p["status_name"] != "STATUS_SCHEDULED"
            if is_live:
                any_live = True
                extra = self._extra_live_line(p)
                if extra:
                    table += f"\n{extra}"
            blocks.append(f"**{p['away_display']} @ {p['home_display']} | {p['status_str']}**\n```\n{table}\n```")

        color = discord.Color.orange() if any_live else discord.Color.blue()
        await interaction.followup.send(embed=discord.Embed(
            title=f"Top 25 Games — {week_label}",
            description="\n".join(blocks),
            color=color,
        ))

    # ── Upset alerts ──────────────────────────────────────────────────────────

    @tasks.loop(seconds=UPSET_POLL_SECONDS)
    async def upset_loop(self):
        try:
            now = datetime.now()
            if self._upset_next_fetch and now < self._upset_next_fetch:
                return

            # Pin the scoreboard date with a 6am ET rollover so late West Coast / Hawaii
            # games stay on the board until they finish
            board_day = et_now() - timedelta(hours=UPSET_DAY_ROLLOVER_HOURS)
            board_date = board_day.strftime("%Y%m%d")
            session = await self.bot.mlb_client.get_session()
            params  = {"groups": "80", "limit": "300", "dates": board_date}
            async with session.get(self._scoreboard_url, params=params) as resp:
                data = await resp.json()
            comps = {event["id"]: event["competitions"][0] for event in data.get("events", [])}

            any_live = any(c["status"]["type"].get("state") == "in" for c in comps.values())
            self._upset_next_fetch = None if any_live else now + timedelta(minutes=UPSET_IDLE_POLL_MINUTES)

            channel = self.bot.get_channel(UPSET_CHANNEL_ID) or await self.bot.fetch_channel(UPSET_CHANNEL_ID)
            for event_id, comp in comps.items():
                try:
                    await self._check_upset(event_id, comp, channel, board_date)
                except Exception as e:
                    print(f"[{self.SLUG}] upset check failed for event {event_id}: {e}")

            self._prune_upset_state(comps, (board_day - timedelta(days=1)).strftime("%Y%m%d"))
        except Exception as e:
            print(f"[{self.SLUG}] upset loop error: {e}")

    @upset_loop.before_loop
    async def before_upset_loop(self):
        await self.bot.wait_until_ready()

    def _save_upset_state(self) -> None:
        MonitorCog._save_json(UPSET_STATE_FILE, self._upset_state, "CFB upset")

    def _prune_upset_state(self, comps: dict, expire_before: str) -> None:
        """Drop games that are off the scoreboard and either already had their final
        evaluated or are from before `expire_before` (YYYYMMDD — e.g. postponed games).
        A missing or empty scoreboard response therefore never forgets a live game."""
        stale = [
            eid for eid, entry in self._upset_state.items()
            if eid not in comps and (STAGE_FINAL in entry["stages"] or entry["date"] < expire_before)
        ]
        for eid in stale:
            del self._upset_state[eid]
        if stale:
            self._save_upset_state()

    async def _check_upset(self, event_id: str, comp: dict, channel, board_date: str) -> None:
        """Evaluate one game, posting an alert if it just reached a stage with the
        underdog ahead. State is saved immediately so restarts never re-post."""
        sides = _upset_sides(comp)
        if not sides:
            return
        stage = _upset_stage(comp)
        entry = self._upset_state.get(event_id)

        if entry is None:
            if comp["status"]["type"].get("state") != "in":
                return   # Not live yet, or finished before we started watching
            entry = self._upset_state[event_id] = {"stages": [], "date": board_date}
            if stage is None:
                self._save_upset_state()
                return
        if stage is None or stage in entry["stages"]:
            return

        fav, dog = sides
        if _underdog_leading(fav, dog):
            # Post before marking so a failed send is retried on the next tick
            await channel.send(embed=self._build_upset_embed(comp, fav, dog, stage))

        # Mark this stage and any earlier ones we slept through as evaluated
        order = [STAGE_Q4, STAGE_TWO_MIN, STAGE_FINAL]
        entry["stages"] += [s for s in order[:order.index(stage) + 1] if s not in entry["stages"]]
        self._save_upset_state()

    def _build_upset_embed(self, comp: dict, fav: dict, dog: dict, stage: str) -> discord.Embed:
        p = self._parse_comp(comp)

        def name(c):
            return f"{self._rank_prefix(c)}{c['team'].get('location') or c['team']['abbreviation']}"

        score = f"{dog.get('score')}–{fav.get('score')}"
        if stage == STAGE_FINAL:
            title = f"\U0001F4A5 UPSET: {name(dog)} beats {name(fav)} {score}"
            color = discord.Color.red()
        else:
            title = f"\U0001F6A8 Upset Alert: {name(dog)} leads {name(fav)} {score}"
            color = discord.Color.orange()

        status_str = "End of 3rd" if comp["status"].get("period") == 3 else p["status_str"]
        labels = self._linescore_labels(max(len(p["away_qs"]), len(p["home_qs"]), 1))
        table  = _format_linescore(p["away_prefix"], p["away_abbr"], p["away_qs"],
                                    p["home_prefix"], p["home_abbr"], p["home_qs"],
                                    p["away_total"], p["home_total"], labels)
        body = f"**{p['away_display']} @ {p['home_display']} | {status_str}**\n```\n{table}\n```"
        if stage != STAGE_FINAL:
            extra = self._extra_live_line(p)
            if extra:
                body += f"\n{extra}"
            if p["broadcast"]:
                body += f"\nTV: {p['broadcast']}"
        return discord.Embed(title=title, description=body, color=color)

    @commands.command(name="upset_test")
    async def upset_test(self, ctx):
        """Preview the three upset alert stages with mock data. Usage: !upset_test"""
        def competitor(home_away, abbr, location, rank, score, qs):
            return {
                "homeAway": home_away, "score": str(score), "curatedRank": {"current": rank},
                "team": {"id": abbr, "abbreviation": abbr, "location": location},
                "linescores": [{"value": q} for q in qs],
            }

        def mock(period, clock, display, name, state, away_qs, home_qs):
            return {
                "competitors": [
                    competitor("away", "UK", "Kentucky", 99, sum(away_qs), away_qs),
                    competitor("home", "UGA", "Georgia", 2, sum(home_qs), home_qs),
                ],
                "status": {"period": period, "clock": clock, "displayClock": display,
                           "type": {"name": name, "state": state}},
                "broadcasts": [{"names": ["CBS"]}],
                "situation": {"downDistanceText": "3rd & 4 at UGA 38", "possession": "UK"},
            }

        await ctx.message.delete()
        for stage, comp in (
            (STAGE_Q4,      mock(3, 0.0, "0:00", "STATUS_END_PERIOD", "in", [7, 3, 7], [3, 7, 0])),
            (STAGE_TWO_MIN, mock(4, 118.0, "1:58", "STATUS_IN_PROGRESS", "in", [7, 3, 7, 7], [3, 7, 0, 10])),
            (STAGE_FINAL,   mock(4, 0.0, "0:00", "STATUS_FINAL", "post", [7, 3, 7, 7], [3, 7, 0, 10])),
        ):
            fav, dog = _upset_sides(comp)
            await ctx.send(embed=self._build_upset_embed(comp, fav, dog, stage))


async def setup(bot):
    await bot.add_cog(CFBCog(bot))
