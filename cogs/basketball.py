"""Shared logic for ESPN basketball score commands (NBA / WNBA)."""
from cogs.espn_base import ESPNCog
from discord import app_commands


class BasketballCog(ESPNCog):
    SPORT_PATH = "basketball"

    def _period_label(self, period: int) -> str:
        if period <= 4:
            return f"Q{period}"
        return "OT" if period == 5 else f"OT{period - 4}"

    def _linescore_labels(self, max_periods: int) -> list[str]:
        labels = [f"Q{i}" for i in range(1, min(max_periods, 4) + 1)]
        if max_periods == 5:
            labels.append("OT")
        elif max_periods > 5:
            labels += [f"OT{i}" for i in range(1, max_periods - 3)]
        return labels

    def _score_player(self, stats: list[str], labels: list[str]) -> float:
        def get(key):
            try:
                raw = stats[labels.index(key)]
                return float(raw.split("-")[0]) if raw not in ("-", "") else 0.0
            except (ValueError, IndexError):
                return 0.0
        return get("PTS")*1.0 + get("REB")*1.2 + get("AST")*1.5 + get("STL")*2.0 + get("BLK")*2.0 - get("TO")*1.0

    def _summarize_player(self, stats: list[str], labels: list[str]) -> str:
        def get_int(key):
            try:
                raw = stats[labels.index(key)]
                return int(float(raw.split("-")[0])) if raw not in ("-", "") else 0
            except (ValueError, IndexError):
                return 0
        parts = [f"{get_int('PTS')} PTS"]
        for key in ("REB", "AST", "STL", "BLK", "TO"):
            v = get_int(key)
            if v:
                parts.append(f"{v} {key}")
        return ", ".join(parts)
