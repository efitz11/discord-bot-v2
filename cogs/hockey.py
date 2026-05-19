"""Shared logic for ESPN hockey score commands (NHL)."""
from cogs.espn_base import ESPNCog


_PERIOD_NAMES = {1: "1st", 2: "2nd", 3: "3rd", 4: "OT", 5: "SO"}


class HockeyCog(ESPNCog):
    SPORT_PATH = "hockey"

    def _period_label(self, period: int) -> str:
        return _PERIOD_NAMES.get(period, f"OT{period - 3}")

    def _linescore_labels(self, max_periods: int) -> list[str]:
        base = ["1st", "2nd", "3rd"]
        if max_periods > 3:
            base.append("OT")
        if max_periods > 4:
            base.append("SO")
        return base[:max_periods]

    def _is_goalie_group(self, group: dict) -> bool:
        return "GA" in group.get("labels", [])

    def _score_player(self, stats: list[str], labels: list[str]) -> float:
        def get(key):
            try:
                raw = stats[labels.index(key)]
                return float(raw.split("-")[0]) if raw not in ("-", "", "--") else 0.0
            except (ValueError, IndexError):
                return 0.0
        return get("G")*5.0 + get("A")*3.0 + get("SOG")*0.5 + get("BS")*0.5 - get("PIM")*0.25

    def _summarize_player(self, stats: list[str], labels: list[str]) -> str:
        def get_int(key):
            try:
                raw = stats[labels.index(key)]
                return int(float(raw.split("-")[0])) if raw not in ("-", "", "--") else 0
            except (ValueError, IndexError):
                return 0

        parts = []
        g = get_int("G")
        if g: parts.append(f"{g}G")
        a = get_int("A")
        if a: parts.append(f"{a}A")
        if not parts:
            parts.append("0G")
        sog = get_int("SOG")
        if sog: parts.append(f"{sog} SOG")
        pm = get_int("+/-")
        if pm != 0: parts.append(f"{'+' if pm > 0 else ''}{pm}")
        pim = get_int("PIM")
        if pim: parts.append(f"{pim} PIM")
        return ", ".join(parts)
