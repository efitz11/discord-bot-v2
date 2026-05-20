"""Generic ESPN sport base cog — subclass and set SLUG, SPORT, SPORT_PATH."""
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from core.utils import parse_date

ET_OFFSET = timedelta(hours=4)

ESPN_BASE     = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_WEB_BASE = "https://site.web.api.espn.com/apis/site/v2/sports"

FINAL_STATUSES = {"STATUS_FINAL", "STATUS_FINAL_OT", "STATUS_FINAL_SHOOTOUT"}


def _utc_to_et(iso_str: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(iso_str, fmt) - ET_OFFSET
        except ValueError:
            continue
    return None


def _format_linescore(away_abbr, away_qs, home_abbr, home_qs, away_total, home_total, period_labels: list[str]) -> str:
    max_periods = max(len(away_qs), len(home_qs), len(period_labels))

    def pad_qs(qs, total):
        filled = list(qs) + [None] * (max_periods - len(qs))
        return str(total), [str(s) if s is not None else "-" for s in filled]

    away_tot, away_vals = pad_qs(away_qs, away_total)
    home_tot, home_vals = pad_qs(home_qs, home_total)

    tot_w    = max(len("T"), len(away_tot), len(home_tot))
    q_widths = [max(len(lbl), len(av), len(hv)) for lbl, av, hv in zip(period_labels, away_vals, home_vals)]
    abbr_w   = max(len(away_abbr), len(home_abbr))
    sep      = " "

    def fmt_row(abbr, tot, vals):
        return abbr.ljust(abbr_w) + sep + tot.rjust(tot_w) + " | " + sep.join(v.rjust(w) for v, w in zip(vals, q_widths))

    header = " " * abbr_w + sep + "T".rjust(tot_w) + " | " + sep.join(lbl.rjust(w) for lbl, w in zip(period_labels, q_widths))
    return "\n".join([header, fmt_row(away_abbr, away_tot, away_vals), fmt_row(home_abbr, home_tot, home_vals)])


class ESPNCog(commands.Cog):
    SLUG:        str = ""
    SPORT:       str = ""
    SPORT_PATH:  str = ""   # e.g. "basketball" or "hockey"

    def __init__(self, bot):
        self.bot   = bot
        self._teams: list[dict] = []
        base = f"{ESPN_BASE}/{self.SPORT_PATH}/{self.SLUG}"
        web  = f"{ESPN_WEB_BASE}/{self.SPORT_PATH}/{self.SLUG}"
        self._scoreboard_url = f"{base}/scoreboard"
        self._teams_url      = f"{base}/teams"
        self._summary_url    = f"{web}/summary"

    # ── Override in subclasses ────────────────────────────────────────────────

    def _period_label(self, period: int) -> str:
        """Label for the current period used in the embed title."""
        raise NotImplementedError

    def _linescore_labels(self, max_periods: int) -> list[str]:
        """Column headers for the linescore table."""
        raise NotImplementedError

    def _score_player(self, stats: list[str], labels: list[str]) -> float:
        """Fantasy-style score used to rank top performers."""
        raise NotImplementedError

    def _summarize_player(self, stats: list[str], labels: list[str]) -> str:
        """One-line stat summary string for a player."""
        raise NotImplementedError

    def _is_goalie_group(self, group: dict) -> bool:
        """Return True if this stat group is for goalies (hockey only)."""
        return False

    # ── Shared implementation ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self):
        await self._load_teams()

    async def _load_teams(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(self._teams_url) as resp:
                data = await resp.json()
            league = data.get("sports", [{}])[0].get("leagues", [{}])[0]
            self._teams = [
                {"abbreviation": t["team"]["abbreviation"], "displayName": t["team"]["displayName"]}
                for t in league.get("teams", [])
            ]
            print(f"[{self.SLUG}] loaded {len(self._teams)} teams")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load teams: {e}")

    def _parse_comp(self, comp: dict) -> dict:
        away = next(x for x in comp["competitors"] if x["homeAway"] == "away")
        home = next(x for x in comp["competitors"] if x["homeAway"] == "home")

        away_abbr  = away["team"]["abbreviation"]
        home_abbr  = home["team"]["abbreviation"]
        away_total = away.get("score", "0")
        home_total = home.get("score", "0")
        away_qs    = [int(ls["value"]) for ls in away.get("linescores", [])]
        home_qs    = [int(ls["value"]) for ls in home.get("linescores", [])]

        status      = comp["status"]
        status_name = status["type"]["name"]
        period      = status.get("period", 0)
        clock       = status.get("displayClock", "")

        if status_name == "STATUS_SCHEDULED":
            tipoff_et  = _utc_to_et(comp.get("date", ""))
            status_str = tipoff_et.strftime("%-I:%M %p ET") if tipoff_et else "TBD"
        elif status_name in FINAL_STATUSES:
            suffix = " (SO)" if status_name == "STATUS_FINAL_SHOOTOUT" else (" (OT)" if status_name == "STATUS_FINAL_OT" else "")
            status_str = f"Final{suffix}"
        else:
            status_str = f"{self._period_label(period)} | {clock}"

        return {
            "away_abbr": away_abbr, "home_abbr": home_abbr,
            "away_total": away_total, "home_total": home_total,
            "away_qs": away_qs, "home_qs": home_qs,
            "status_name": status_name, "status_str": status_str,
            "comp": comp,
        }

    async def _fetch_top_performers(self, session, event_id: str) -> dict[str, list[dict]]:
        try:
            async with session.get(self._summary_url, params={"event": event_id}) as resp:
                data = await resp.json()
        except Exception:
            return {}

        result = {}
        for team_block in data.get("boxscore", {}).get("players", []):
            abbr   = team_block.get("team", {}).get("abbreviation", "")
            groups = team_block.get("statistics", [])
            scored = []
            for group in groups:
                if self._is_goalie_group(group):
                    continue
                labels = group.get("labels", [])
                for athlete in group.get("athletes", []):
                    if athlete.get("didNotPlay") is True:
                        continue
                    raw = athlete.get("stats", [])
                    if not raw:
                        continue
                    try:
                        if "MIN" in labels and int(raw[labels.index("MIN")]) == 0:
                            continue
                        if "TOI" in labels and raw[labels.index("TOI")] in ("", "0:00", "-"):
                            continue
                    except (ValueError, IndexError):
                        continue
                    scored.append({
                        "name":    athlete.get("athlete", {}).get("displayName", "Unknown"),
                        "score":   self._score_player(raw, labels),
                        "summary": self._summarize_player(raw, labels),
                    })
            scored.sort(key=lambda x: x["score"], reverse=True)
            result[abbr] = scored[:2]

        return result

    async def _score_impl(self, interaction: discord.Interaction, team: str, date: str = None):
        await interaction.response.defer()

        date_str = parse_date(date)
        params   = {"dates": date_str.replace("-", "")} if date_str else {}

        session = await self.bot.mlb_client.get_session()
        async with session.get(self._scoreboard_url, params=params) as resp:
            data = await resp.json()

        events     = data.get("events", [])
        team_upper = team.upper()
        all_games  = team_upper == "ALL"

        if all_games:
            games = [(event["id"], event["competitions"][0]) for event in events]
        else:
            games = [
                (event["id"], event["competitions"][0])
                for event in events
                if any(x["team"]["abbreviation"].upper() == team_upper for x in event["competitions"][0]["competitors"])
            ]

        if not games:
            when = date_str or "today"
            msg  = f"No games on {when}." if all_games else f"No game on {when} for **{team_upper}**."
            await interaction.followup.send(msg)
            return

        if all_games:
            blocks    = []
            any_live  = False
            for _event_id, comp in games:
                p      = self._parse_comp(comp)
                labels = self._linescore_labels(max(len(p["away_qs"]), len(p["home_qs"]), 1))
                table  = _format_linescore(p["away_abbr"], p["away_qs"], p["home_abbr"], p["home_qs"], p["away_total"], p["home_total"], labels)
                if p["status_name"] not in FINAL_STATUSES and p["status_name"] != "STATUS_SCHEDULED":
                    any_live = True
                blocks.append(f"**{p['away_abbr']} @ {p['home_abbr']} | {p['status_str']}**\n```\n{table}\n```")

            color = discord.Color.orange() if any_live else discord.Color.blue()
            if date_str:
                d = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                d = datetime.utcnow() - ET_OFFSET
            date_label = d.strftime("%A, %B %-d, %Y")
            await interaction.followup.send(embed=discord.Embed(
                title=f"{self.SPORT} Scores — {date_label}",
                description="\n".join(blocks),
                color=color,
            ))
            return

        # Single team
        event_id, comp = games[0]
        p      = self._parse_comp(comp)
        labels = self._linescore_labels(max(len(p["away_qs"]), len(p["home_qs"]), 1))
        table  = _format_linescore(p["away_abbr"], p["away_qs"], p["home_abbr"], p["home_qs"], p["away_total"], p["home_total"], labels)
        is_live = p["status_name"] not in FINAL_STATUSES and p["status_name"] != "STATUS_SCHEDULED"
        color   = discord.Color.orange() if is_live else discord.Color.blue()
        title   = f"{p['away_abbr']} @ {p['home_abbr']} | {p['status_str']}"
        body    = f"```\n{table}\n```"

        if is_live:
            situation = p["comp"].get("situation", {})
            lp        = situation.get("lastPlay", {})
            lp_text   = lp.get("text", "")
            if lp_text:
                lp_team_id = lp.get("team", {}).get("id", "")
                lp_abbr    = next(
                    (x["team"]["abbreviation"] for x in p["comp"]["competitors"] if x["team"].get("id") == lp_team_id),
                    ""
                )
                body += f"\nLast play: {lp_abbr + ' — ' if lp_abbr else ''}{lp_text}"

        if p["status_name"] != "STATUS_SCHEDULED":
            top = await self._fetch_top_performers(session, event_id)
            perf_blocks = []
            for abbr in (p["away_abbr"], p["home_abbr"]):
                players = top.get(abbr, [])
                if players:
                    lines = [f"{abbr}:"] + [f"**{pl['name']}** {pl['summary']}" for pl in players]
                    perf_blocks.append("\n".join(lines))
            if perf_blocks:
                body += "\n\n" + "\n\n".join(perf_blocks)

        await interaction.followup.send(embed=discord.Embed(title=title, description=body, color=color))

    async def _autocomplete_impl(self, current: str) -> list[app_commands.Choice]:
        cur     = current.lower()
        matches = [t for t in self._teams if cur in t["displayName"].lower() or cur in t["abbreviation"].lower()]
        choices = [app_commands.Choice(name=t["displayName"], value=t["abbreviation"]) for t in matches[:9]]
        if not current or "all" in cur:
            choices.insert(0, app_commands.Choice(name="All Teams", value="ALL"))
        return choices[:10]
