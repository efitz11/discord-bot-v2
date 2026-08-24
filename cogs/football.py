"""Shared logic for ESPN football score commands (NFL / college football)."""
from discord import app_commands
from cogs.espn_base import ESPNCog

_PERIOD_NAMES = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}

# Stat groups worth surfacing as "top performers" (skips defense/kicking/special teams)
_SKILL_GROUPS = {"passing", "rushing", "receiving"}


class FootballCog(ESPNCog):
    SPORT_PATH = "football"

    def __init__(self, bot):
        super().__init__(bot)
        self._weeks: list[dict] = []
        self._season_year = None

    async def _load_extra(self):
        await self._load_calendar()

    async def _load_calendar(self):
        try:
            session = await self.bot.mlb_client.get_session()
            async with session.get(self._scoreboard_url) as resp:
                data = await resp.json()
            self._season_year = data.get("season", {}).get("year")
            cal = data.get("leagues", [{}])[0].get("calendar", [])
            weeks = []
            for block in cal:
                seasontype = block.get("value")
                for entry in block.get("entries", []):
                    weeks.append({
                        "label":      entry.get("label"),
                        "week":       entry.get("value"),
                        "seasontype": seasontype,
                    })
            self._weeks = weeks
            print(f"[{self.SLUG}] loaded {len(weeks)} weeks")
        except Exception as e:
            print(f"[{self.SLUG}] failed to load calendar: {e}")

    def _resolve_week(self, week: str):
        """Turn a 'seasontype|weeknum' autocomplete value into (extra_params, label)."""
        if not week:
            return None, None
        try:
            seasontype, weeknum = week.split("|", 1)
        except ValueError:
            return None, None
        entry  = next((w for w in self._weeks if w["seasontype"] == seasontype and w["week"] == weeknum), None)
        label  = entry["label"] if entry else f"Week {weeknum}"
        params = {"seasontype": seasontype, "week": weeknum}
        if self._season_year:
            params["year"] = self._season_year
        return params, label

    async def _week_autocomplete(self, current: str) -> list[app_commands.Choice]:
        cur     = current.lower()
        matches = [w for w in self._weeks if cur in w["label"].lower()]
        return [app_commands.Choice(name=w["label"], value=f"{w['seasontype']}|{w['week']}") for w in matches[:25]]

    def _period_label(self, period: int) -> str:
        if period <= 4:
            return _PERIOD_NAMES.get(period, f"Q{period}")
        return "OT" if period == 5 else f"OT{period - 4}"

    def _linescore_labels(self, max_periods: int) -> list[str]:
        labels = [f"Q{i}" for i in range(1, min(max_periods, 4) + 1)]
        if max_periods == 5:
            labels.append("OT")
        elif max_periods > 5:
            labels += [f"OT{i}" for i in range(1, max_periods - 3)]
        return labels

    def _rank_prefix(self, competitor: dict) -> str:
        rank = competitor.get("curatedRank", {}).get("current")
        if rank and rank <= 25:
            return f"({rank}) "
        return ""

    def _extra_live_line(self, p: dict) -> str:
        situation = p["comp"].get("situation", {})
        dd = situation.get("shortDownDistanceText") or situation.get("downDistanceText")
        if not dd:
            return ""
        poss_id   = situation.get("possession")
        poss_abbr = next(
            (c["team"]["abbreviation"] for c in p["comp"]["competitors"] if c["team"].get("id") == poss_id),
            ""
        )
        prefix = f"{poss_abbr} \U0001F3C8 " if poss_abbr else ""
        return f"{prefix}{dd}"

    def _score_group_stat(self, group_name: str, stats: list[str], labels: list[str]) -> float:
        def get(key):
            try:
                raw = stats[labels.index(key)]
                return float(raw) if raw not in ("-", "", "--") else 0.0
            except (ValueError, IndexError):
                return 0.0

        if group_name == "passing":
            return get("YDS") * 0.04 + get("TD") * 4.0 - get("INT") * 2.0
        if group_name == "rushing":
            return get("YDS") * 0.1 + get("TD") * 6.0
        if group_name == "receiving":
            return get("YDS") * 0.1 + get("TD") * 6.0 + get("REC") * 0.5
        return 0.0

    def _summarize_group_stat(self, group_name: str, stats: list[str], labels: list[str]) -> str:
        def get(key, default="0"):
            try:
                return stats[labels.index(key)]
            except (ValueError, IndexError):
                return default

        if group_name == "passing":
            parts = [f"{get('C/ATT')}, {get('YDS')} YDS"]
            td, it = get("TD"), get("INT")
            if td != "0": parts.append(f"{td} TD")
            if it != "0": parts.append(f"{it} INT")
            return ", ".join(parts)
        if group_name == "rushing":
            parts = [f"{get('CAR')} CAR, {get('YDS')} YDS"]
            td = get("TD")
            if td != "0": parts.append(f"{td} TD")
            return ", ".join(parts)
        if group_name == "receiving":
            parts = [f"{get('REC')} REC, {get('YDS')} YDS"]
            td = get("TD")
            if td != "0": parts.append(f"{td} TD")
            return ", ".join(parts)
        return ""

    async def _fetch_top_performers(self, session, event_id: str) -> dict[str, list[dict]]:
        try:
            async with session.get(self._summary_url, params={"event": event_id}) as resp:
                data = await resp.json()
        except Exception:
            return {}

        result = {}
        for team_block in data.get("boxscore", {}).get("players", []):
            abbr   = team_block.get("team", {}).get("abbreviation", "")
            scored = []
            for group in team_block.get("statistics", []):
                gname = group.get("name", "")
                if gname not in _SKILL_GROUPS:
                    continue
                labels = group.get("labels", [])
                for athlete in group.get("athletes", []):
                    if athlete.get("didNotPlay") is True:
                        continue
                    raw = athlete.get("stats", [])
                    if not raw:
                        continue
                    scored.append({
                        "name":    athlete.get("athlete", {}).get("displayName", "Unknown"),
                        "score":   self._score_group_stat(gname, raw, labels),
                        "summary": self._summarize_group_stat(gname, raw, labels),
                    })
            scored.sort(key=lambda x: x["score"], reverse=True)
            result[abbr] = scored[:2]

        return result
