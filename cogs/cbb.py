from datetime import datetime

import discord
from discord import app_commands
from discord import Interaction
from cogs.basketball import BasketballCog
from cogs.espn_base import _format_linescore, _format_pregame, FINAL_STATUSES
from core.utils import parse_date, et_now

CONFERENCES_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard/conferences"
# The plain /teams endpoint is truncated and mixes divisions; the D-I standings tree
# is grouped by conference and lists every D-I team.
D1_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/basketball/mens-college-basketball/standings?group=50"
D1_GROUP         = "50"
RANKED_VALUE     = "RANKED"
TOP_RANKED_N     = 15
SCOREBOARD_LIMIT = "300"


class CBBCog(BasketballCog):
    SLUG  = "mens-college-basketball"
    SPORT = "NCAAM"

    def __init__(self, bot):
        super().__init__(bot)
        self._conferences: list[dict] = []

    # ── College hoops plays two halves ────────────────────────────────────────

    def _period_label(self, period: int) -> str:
        if period <= 2:
            return "1st" if period == 1 else "2nd"
        return "OT" if period == 3 else f"{period - 2}OT"

    def _linescore_labels(self, max_periods: int) -> list[str]:
        labels = [f"H{i}" for i in range(1, min(max_periods, 2) + 1)]
        if max_periods == 3:
            labels.append("OT")
        elif max_periods > 3:
            labels += [f"OT{i}" for i in range(1, max_periods - 1)]
        return labels

    def _rank_prefix(self, competitor: dict) -> str:
        rank = competitor.get("curatedRank", {}).get("current")
        if rank and rank <= 25:
            return f"({rank}) "
        return ""

    def _parse_comp(self, comp: dict) -> dict:
        p = super()._parse_comp(comp)
        if p["status_name"] == "STATUS_HALFTIME":
            p["status_str"] = "Halftime"
        elif p["status_name"] == "STATUS_END_PERIOD":
            p["status_str"] = f"End of {self._period_label(comp['status'].get('period', 0))}"
        return p

    # ── Data loading ──────────────────────────────────────────────────────────

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(D1_STANDINGS_URL) as resp:
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
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(CONFERENCES_URL) as resp:
                data = await resp.json()
            self._conferences = [c for c in data.get("conferences", []) if c.get("groupId") != D1_GROUP]
            print(f"[{self.SLUG}] loaded {len(self._conferences)} conferences")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load conferences: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────

    cbb = app_commands.Group(name="cbb", description="College basketball scores and info")

    @cbb.command(name="score", description="Get the score for a college basketball team, conference, or the top 25")
    @app_commands.describe(
        team="Team, conference, or 'Top 25 Games'",
        date="Date (e.g. yesterday, -1, 11/19, 11/19/26)",
    )
    async def score(self, interaction: Interaction, team: str, date: str = None):
        base_params = {"limit": SCOREBOARD_LIMIT}

        if team == RANKED_VALUE:
            await self._ranked_games_impl(interaction, date)
            return

        conf = next((c for c in self._conferences if c["groupId"] == team), None)
        if conf:
            await self._score_impl(interaction, date=date, groups=team,
                                    group_label=f"{conf['shortName']} Games", extra_params=base_params)
            return

        # Scope to D-I (group 50) — without a group the scoreboard only returns a
        # highlighted subset of games, which can miss teams.
        await self._score_impl(interaction, team, date, extra_params={**base_params, "groups": D1_GROUP})

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

    async def _ranked_games_impl(self, interaction: Interaction, date: str = None):
        await interaction.response.defer()

        date_str = parse_date(date)
        params   = {"groups": D1_GROUP, "limit": SCOREBOARD_LIMIT}
        if date_str:
            params["dates"] = date_str.replace("-", "")

        session = await self.bot.mlb_client.get_session()
        async with session.get(self._scoreboard_url, params=params) as resp:
            data = await resp.json()

        day       = datetime.strptime(date_str, "%Y-%m-%d") if date_str else et_now()
        day_label = day.strftime("%A, %B %-d, %Y")

        ranked = []
        for event in data.get("events", []):
            comp = event["competitions"][0]
            best = min((c.get("curatedRank", {}).get("current") or 99) for c in comp["competitors"])
            if best <= 25:
                ranked.append((best, comp))
        ranked.sort(key=lambda x: x[0])
        top = ranked[:TOP_RANKED_N]

        if not top:
            await interaction.followup.send(f"No ranked games on {day_label}.")
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
            if p["status_name"] not in FINAL_STATUSES and p["status_name"] != "STATUS_SCHEDULED":
                any_live = True
            blocks.append(f"**{p['away_display']} @ {p['home_display']} | {p['status_str']}**\n```\n{table}\n```")

        title = f"Top 25 Games — {day_label}"
        if len(ranked) > TOP_RANKED_N:
            title += f" (top {TOP_RANKED_N} of {len(ranked)})"
        await interaction.followup.send(embed=discord.Embed(
            title=title,
            description="\n".join(blocks),
            color=discord.Color.orange() if any_live else discord.Color.blue(),
        ))


async def setup(bot):
    await bot.add_cog(CBBCog(bot))
