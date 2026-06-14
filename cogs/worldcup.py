"""FIFA World Cup cog — scores and group standings from ESPN's public soccer API."""
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

from core.utils import parse_date, et_now
from core.models import format_table
from cogs.espn_base import _utc_to_et

SLUG           = "fifa.world"
SCOREBOARD_URL = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{SLUG}/scoreboard"
STANDINGS_URL  = f"https://site.api.espn.com/apis/v2/sports/soccer/{SLUG}/standings"
TEAMS_URL      = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{SLUG}/teams"


class WorldCupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._teams: list[dict] = []

    worldcup = app_commands.Group(name="worldcup", description="FIFA World Cup scores and standings")

    @commands.Cog.listener()
    async def on_ready(self):
        await self._load_teams()

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(TEAMS_URL) as resp:
                data = await resp.json()
            league = data.get("sports", [{}])[0].get("leagues", [{}])[0]
            self._teams = [
                {"abbreviation": t["team"]["abbreviation"], "displayName": t["team"]["displayName"]}
                for t in league.get("teams", [])
            ]
            print(f"[worldcup] loaded {len(self._teams)} teams")
        except Exception as e:
            print(f"[worldcup] failed to load teams: {e}")

    # ── Match parsing ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_match(event: dict) -> dict:
        comp   = event["competitions"][0]
        home   = next(x for x in comp["competitors"] if x["homeAway"] == "home")
        away   = next(x for x in comp["competitors"] if x["homeAway"] == "away")
        status = comp["status"]
        state  = status["type"].get("state", "pre")  # pre | in | post

        if state == "pre":
            kickoff = _utc_to_et(event.get("date", ""))
            status_str = kickoff.strftime("%-I:%M %p ET") if kickoff else "TBD"
        elif state == "post":
            status_str = status["type"].get("shortDetail", "FT")
        elif status["type"].get("name") == "STATUS_HALFTIME":
            status_str = "HT"
        else:
            status_str = status.get("displayClock") or status["type"].get("shortDetail", "Live")

        id_to_abbr = {
            home["team"]["id"]: home["team"].get("abbreviation", "???"),
            away["team"]["id"]: away["team"].get("abbreviation", "???"),
        }
        goals, red_cards = [], []
        for det in comp.get("details", []):
            if det.get("shootout"):
                continue  # shootout result is already reflected in the status line
            minute = det.get("clock", {}).get("displayValue", "")
            abbr   = id_to_abbr.get(det.get("team", {}).get("id"), "")
            ath    = (det.get("athletesInvolved") or [{}])[0]
            who    = ath.get("shortName") or ath.get("displayName", "Unknown")
            if det.get("scoringPlay"):
                tag = " (pen)" if det.get("penaltyKick") else (" (OG)" if det.get("ownGoal") else "")
                goals.append(f"⚽ {minute} {who}{tag} ({abbr})")
            elif det.get("redCard"):
                red_cards.append(f"🟥 {minute} {who} ({abbr})")

        return {
            "home_abbr": home["team"].get("abbreviation", "???"),
            "away_abbr": away["team"].get("abbreviation", "???"),
            "home_name": home["team"].get("displayName", "?"),
            "away_name": away["team"].get("displayName", "?"),
            "home_score": home.get("score", "0"),
            "away_score": away.get("score", "0"),
            "state": state,
            "status_str": status_str,
            "goals": goals,
            "red_cards": red_cards,
            "venue": comp.get("venue", {}).get("fullName", ""),
        }

    @staticmethod
    def _match_header(m: dict, full_names: bool = False) -> str:
        emoji = {"pre": "🗓️", "in": "🔴", "post": "🏁"}[m["state"]]
        h = m["home_name"] if full_names else m["home_abbr"]
        a = m["away_name"] if full_names else m["away_abbr"]
        if m["state"] == "pre":
            return f"{emoji} {h} vs {a} — {m['status_str']}"
        return f"{emoji} {h} {m['home_score']} - {m['away_score']} {a} — {m['status_str']}"

    # ── /worldcup score ───────────────────────────────────────────────────────

    @worldcup.command(name="score", description="World Cup scores for today or a specific date")
    @app_commands.describe(team="Country (leave blank for all matches)", date="Date (e.g. yesterday, 6/15, +1)")
    async def score(self, interaction: discord.Interaction, team: str = None, date: str = None):
        await interaction.response.defer()

        date_str = parse_date(date)
        target   = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else et_now().date()

        # ESPN groups matches by ET calendar date, so a 12:00 AM ET kickoff lands on the
        # *next* date. The ESPN app instead shows those late-night games as part of the
        # prior night's slate, so we pull the target date plus the early-morning games of
        # the following date (before LATE_NIGHT_CUTOFF) and present them together.
        LATE_NIGHT_CUTOFF = 6  # hour (ET); kickoffs before this belong to the previous night

        session = await self.bot.mlb_client.get_session()
        events = []
        for d, in_window in (
            (target, lambda h: h >= LATE_NIGHT_CUTOFF),
            (target + timedelta(days=1), lambda h: h < LATE_NIGHT_CUTOFF),
        ):
            async with session.get(SCOREBOARD_URL, params={"dates": d.strftime("%Y%m%d")}) as resp:
                data = await resp.json()
            for e in data.get("events", []):
                et = _utc_to_et(e.get("date", ""))
                if et and in_window(et.hour):
                    events.append(e)
        events.sort(key=lambda e: e.get("date", ""))

        if team:
            q = team.lower()
            events = [
                e for e in events
                if any(q == x["team"].get("abbreviation", "").lower()
                       or q in x["team"].get("displayName", "").lower()
                       for x in e["competitions"][0]["competitors"])
            ]

        when = date_str or "today"
        if not events:
            msg = f"No World Cup matches on {when} for **{team}**." if team else f"No World Cup matches on {when}."
            await interaction.followup.send(msg)
            return

        matches  = [self._parse_match(e) for e in events]
        any_live = any(m["state"] == "in" for m in matches)
        color    = discord.Color.orange() if any_live else discord.Color.blue()

        if team and len(matches) == 1:
            # Single-match detail view
            m = matches[0]
            body = ""
            if m["goals"]:
                body += "\n".join(m["goals"]) + "\n"
            elif m["state"] != "pre":
                body += "*No goals yet.*\n" if m["state"] == "in" else "*No goals.*\n"
            if m["red_cards"]:
                body += "\n".join(m["red_cards"]) + "\n"
            embed = discord.Embed(
                title=self._match_header(m, full_names=True),
                description=body.strip() or None,
                color=color,
            )
            if m["venue"]:
                embed.set_footer(text=m["venue"])
            await interaction.followup.send(embed=embed)
            return

        # All matches for the day
        blocks = []
        for m in matches:
            block = f"**{self._match_header(m, full_names=True)}**"
            details = m["goals"] + m["red_cards"]
            if details:
                block += "\n" + "\n".join(details)
            blocks.append(block)

        date_label = target.strftime("%A, %B %-d, %Y")

        embed = discord.Embed(
            title=f"🏆 World Cup — {date_label}",
            description="\n\n".join(blocks)[:4096],
            color=color,
        )
        await interaction.followup.send(embed=embed)

    @score.autocomplete("team")
    async def team_autocomplete(self, interaction: discord.Interaction, current: str):
        cur = current.lower()
        matches = [t for t in self._teams
                   if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()]
        return [app_commands.Choice(name=t["displayName"], value=t["abbreviation"])
                for t in matches[:25]]

    # ── /worldcup standings ───────────────────────────────────────────────────

    @worldcup.command(name="standings", description="World Cup group standings")
    @app_commands.describe(group="Group to show (leave blank for all groups)")
    @app_commands.choices(group=[app_commands.Choice(name=f"Group {g}", value=g) for g in "ABCDEFGHIJKL"])
    async def standings(self, interaction: discord.Interaction, group: app_commands.Choice[str] = None):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()
        async with session.get(STANDINGS_URL) as resp:
            data = await resp.json()

        children = data.get("children", [])
        if group:
            children = [c for c in children if c.get("name", "") == f"Group {group.value}"]

        if not children:
            await interaction.followup.send("No standings available.")
            return

        labels  = ["team", "gp", "w", "d", "l", "gf", "ga", "gd", "pts"]
        headers = {"team": "TEAM", "gp": "GP", "w": "W", "d": "D", "l": "L",
                   "gf": "GF", "ga": "GA", "gd": "GD", "pts": "PTS"}

        embed = discord.Embed(title="🏆 World Cup Standings", color=discord.Color.gold())
        for grp in children:
            rows = []
            for entry in grp.get("standings", {}).get("entries", []):
                stats = {s.get("name"): s.get("displayValue", "") for s in entry.get("stats", [])}
                rows.append({
                    "team": entry.get("team", {}).get("displayName", "?")[:14],
                    "gp":   stats.get("gamesPlayed", "0"),
                    "w":    stats.get("wins", "0"),
                    "d":    stats.get("ties", "0"),
                    "l":    stats.get("losses", "0"),
                    "gf":   stats.get("pointsFor", "0"),
                    "ga":   stats.get("pointsAgainst", "0"),
                    "gd":   stats.get("pointDifferential", "0"),
                    "pts":  stats.get("points", "0"),
                    "_rank": int(stats.get("rank") or 99),
                })
            rows.sort(key=lambda r: r["_rank"])
            table = format_table(labels, rows, headers, {"team"})
            embed.add_field(name=grp.get("name", "Group"), value=f"```\n{table}\n```", inline=False)

        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(WorldCupCog(bot))
