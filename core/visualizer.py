import io
import colorsys
from PIL import Image, ImageDraw, ImageFont
from typing import List, Optional, Tuple
import os
import math

_FONT_DIR = "/usr/share/fonts/truetype/dejavu/"


def _dv(name: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(_FONT_DIR + name, size)
    except Exception:
        return ImageFont.load_default()


def _pct_color(pct: int) -> Tuple[int, int, int]:
    """Map a 0-100 percentile to a red→gray→blue color matching Savant's palette."""
    if pct >= 70: return (29, 125, 212)
    if pct >= 55: return (100, 175, 230)
    if pct >= 45: return (150, 150, 165)
    if pct >= 30: return (225, 130, 60)
    return (200, 48, 48)


# (primary, secondary) RGB colors keyed by MLB Stats API team abbreviation.
TEAM_COLORS = {
    "ARI": ((167, 25, 48),  (227, 212, 173)),
    "ATL": ((19, 39, 79),   (206, 17, 65)),
    "BAL": ((223, 70, 1),   (0, 0, 0)),
    "BOS": ((189, 48, 57),  (12, 35, 64)),
    "CHC": ((14, 51, 134),  (204, 52, 51)),
    "CWS": ((39, 37, 31),   (196, 206, 212)),
    "CHW": ((39, 37, 31),   (196, 206, 212)),
    "CIN": ((198, 1, 31),   (0, 0, 0)),
    "CLE": ((0, 56, 93),    (227, 25, 55)),
    "COL": ((51, 0, 111),   (196, 206, 212)),
    "DET": ((12, 35, 64),   (250, 70, 22)),
    "HOU": ((0, 45, 98),    (235, 110, 31)),
    "KC":  ((0, 70, 135),   (189, 155, 89)),
    "LAA": ((186, 0, 33),   (0, 50, 99)),
    "LAD": ((0, 90, 156),   (239, 62, 66)),
    "MIA": ((0, 163, 224),  (239, 91, 46)),
    "MIL": ((18, 40, 75),   (255, 197, 47)),
    "MIN": ((0, 43, 92),    (211, 17, 69)),
    "NYM": ((0, 45, 114),   (252, 89, 16)),
    "NYY": ((12, 35, 64),   (196, 206, 212)),
    "OAK": ((0, 56, 49),    (239, 178, 30)),
    "ATH": ((0, 56, 49),    (239, 178, 30)),
    "PHI": ((232, 24, 40),  (0, 45, 114)),
    "PIT": ((253, 184, 39), (0, 0, 0)),
    "SD":  ((47, 36, 29),   (255, 196, 37)),
    "SEA": ((0, 92, 92),    (12, 44, 86)),
    "SF":  ((253, 90, 30),  (0, 0, 0)),
    "STL": ((196, 30, 58),  (12, 35, 64)),
    "TB":  ((9, 44, 92),    (143, 188, 230)),
    "TEX": ((0, 50, 120),   (192, 17, 31)),
    "TOR": ((19, 74, 142),  (29, 45, 68)),
    "WSH": ((171, 0, 3),    (20, 34, 90)),
}
_DEFAULT_COLORS = ((100, 180, 255), (255, 145, 85))  # fallback blue / orange


def _team_colors(abbrev: Optional[str]) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    return TEAM_COLORS.get((abbrev or "").upper(), _DEFAULT_COLORS)


def _luminance(c: Tuple[int, int, int]) -> float:
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _readable(color: Tuple[int, int, int], floor: int = 110) -> Tuple[int, int, int]:
    """Lighten a color toward white if it's too dark to read on the dark background."""
    lum = _luminance(color)
    if lum >= floor:
        return color
    t = (floor - lum) / floor
    return tuple(int(round(ch + (255 - ch) * t)) for ch in color)


def _circle_headshot(img_bytes: bytes, size: int, ring: Tuple[int, int, int], gap: int = 6, ring_width: int = 3) -> Optional[Image.Image]:
    """Crop a headshot to a circle with a team-colored ring sitting just outside it."""
    try:
        base = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception:
        return None
    inner = size - 2 * gap
    w, h = base.size
    s = min(w, h)
    base = base.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((inner, inner), Image.LANCZOS)
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, inner - 1, inner - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (gap, gap), mask)
    ImageDraw.Draw(out).ellipse([1, 1, size - 2, size - 2], outline=ring, width=ring_width)
    return out


def generate_compare_percentiles_image(
    p1_label: str, p2_label: str,
    year_str: str, stat_type: str,
    sections: list,
    p1_team: Optional[str] = None, p2_team: Optional[str] = None,
    p1_headshot: Optional[bytes] = None, p2_headshot: Optional[bytes] = None,
    mode: str = "relative",
) -> io.BytesIO:
    """Render a side-by-side percentile comparison chart styled after Baseball Savant."""

    # ── Layout ──────────────────────────────────────────────────
    W        = 760
    PAD      = 18
    VAL_W    = 34    # fixed-width column for the percentile number
    CTR_W    = 164   # center column for the stat label
    BAR_W    = (W - 2 * PAD - 2 * VAL_W - CTR_W) // 2   # ≈ 245 px per bar

    TITLE_H  = 52
    HEAD     = 72    # headshot diameter
    HEAD_H   = HEAD + 6
    NAMES_H  = 38
    CAT_H    = 34
    ROW_H    = 31

    n_rows = sum(len(rows) for _, rows in sections)
    n_cats = len(sections)
    total_h = TITLE_H + HEAD_H + NAMES_H + n_cats * CAT_H + n_rows * ROW_H + PAD

    # ── Colors ──────────────────────────────────────────────────
    BG       = (16, 18, 27)
    CAT_BG   = (26, 30, 48)
    ROW_ALT  = (20, 23, 35)
    TRACK    = (34, 38, 56)
    TEXT     = (224, 224, 235)
    DIM      = (110, 115, 140)
    WIN_COL  = (29, 125, 212)   # better percentile — matches Savant's high-percentile blue
    LOSE_COL = (200, 48, 48)    # worse percentile — matches Savant's low-percentile red
    TIE_COL  = (150, 150, 165)  # tied — matches Savant's mid-percentile gray
    # Team-derived colors: primary fills the bar, secondary outlines it so two
    # teams sharing a primary color stay distinguishable.
    p1_primary, p1_secondary = _team_colors(p1_team)
    p2_primary, p2_secondary = _team_colors(p2_team)
    P1_COL   = p1_primary        # left player bar fill
    P2_COL   = p2_primary        # right player bar fill
    P1_NAME  = _readable(p1_primary)   # legible-on-dark name color
    P2_NAME  = _readable(p2_primary)

    # ── Fonts ───────────────────────────────────────────────────
    f_title = _dv("DejaVuSans-Bold.ttf", 17)
    f_bold  = _dv("DejaVuSans-Bold.ttf", 13)
    f_reg   = _dv("DejaVuSans.ttf",      13)
    f_small = _dv("DejaVuSans.ttf",      11)
    f_val   = _dv("DejaVuSans-Bold.ttf", 12)

    img  = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img)

    # ── X anchors ───────────────────────────────────────────────
    # [PAD][VAL_W][BAR_W][CTR_W][BAR_W][VAL_W][PAD]
    xv1  = PAD                        # left edge of P1 value column
    xb1  = xv1 + VAL_W               # left edge of P1 bar track
    xctr = xb1 + BAR_W               # left edge of center label (= right edge of P1 bar)
    xb2  = xctr + CTR_W              # left edge of P2 bar track
    xv2  = xb2 + BAR_W              # left edge of P2 value column

    y = PAD // 2

    # column centers for each player (used for headshots + names)
    c1 = (xb1 + xctr) // 2
    c2 = (xb2 + xv2 + VAL_W) // 2

    # ── Title ───────────────────────────────────────────────────
    draw.text((W // 2, y + TITLE_H // 2), f"{year_str} Percentile Comparison",
              font=f_title, fill=TEXT, anchor="mm")
    y += TITLE_H

    # ── Headshots ───────────────────────────────────────────────
    if p1_headshot:
        hs = _circle_headshot(p1_headshot, HEAD, p1_secondary)
        if hs:
            img.paste(hs, (c1 - HEAD // 2, y), hs)
    if p2_headshot:
        hs = _circle_headshot(p2_headshot, HEAD, p2_secondary)
        if hs:
            img.paste(hs, (c2 - HEAD // 2, y), hs)
    draw.text((W // 2, y + HEAD_H // 2), "vs", font=f_reg, fill=DIM, anchor="mm")
    y += HEAD_H

    # ── Player name header ──────────────────────────────────────
    draw.text((c1, y + NAMES_H // 2), p1_label, font=f_bold, fill=P1_NAME, anchor="mm")
    draw.text((c2, y + NAMES_H // 2), p2_label, font=f_bold, fill=P2_NAME, anchor="mm")
    y += NAMES_H

    # ── Sections ─────────────────────────────────────────────────
    for cat_name, rows in sections:
        draw.rectangle([PAD, y, W - PAD, y + CAT_H], fill=CAT_BG)
        draw.text((W // 2, y + CAT_H // 2), cat_name.upper(),
                  font=f_bold, fill=(180, 185, 215), anchor="mm")
        y += CAT_H

        for j, (label, v1, v2, *_) in enumerate(rows):
            # row tint
            if j % 2 == 0:
                draw.rectangle([PAD, y, W - PAD, y + ROW_H], fill=ROW_ALT)

            bar_top    = y + 5
            bar_bottom = y + ROW_H - 5

            # bar tracks
            draw.rounded_rectangle([xb1,  bar_top, xctr - 1, bar_bottom], radius=3, fill=TRACK)
            draw.rounded_rectangle([xb2,  bar_top, xv2  - 1, bar_bottom], radius=3, fill=TRACK)

            if mode == "absolute":
                # Each side's bar extends from the center outward, sized to its own
                # percentile value (not the gap). Color signals who wins the row:
                # blue = better percentile, red = worse, gray = tied.
                v1n, v2n = (v1 or 0), (v2 or 0)
                if v1n > v2n:
                    col1, col2 = WIN_COL, LOSE_COL
                elif v2n > v1n:
                    col1, col2 = LOSE_COL, WIN_COL
                else:
                    col1 = col2 = TIE_COL
                len1 = max(1, round(v1n / 100 * BAR_W))
                len2 = max(1, round(v2n / 100 * BAR_W))
                if v1:
                    draw.rounded_rectangle([xctr - len1, bar_top, xctr - 1, bar_bottom],
                                           radius=3, fill=col1)
                if v2:
                    draw.rounded_rectangle([xb2, bar_top, xb2 + len2, bar_bottom],
                                           radius=3, fill=col2)
            else:
                # Bar shows the percentile difference on the winning player's side
                diff = (v1 or 0) - (v2 or 0)
                if diff != 0:
                    blen = max(1, round(abs(diff) / 100 * BAR_W))
                    if diff > 0:
                        draw.rounded_rectangle([xctr - blen, bar_top, xctr - 1, bar_bottom],
                                               radius=3, fill=P1_COL, outline=p1_secondary, width=2)
                    else:
                        draw.rounded_rectangle([xb2, bar_top, xb2 + blen, bar_bottom],
                                               radius=3, fill=P2_COL, outline=p2_secondary, width=2)

            # percentile values
            col1 = _pct_color(v1) if v1 else DIM
            col2 = _pct_color(v2) if v2 else DIM
            draw.text((xv1 + VAL_W - 3, y + ROW_H // 2), str(v1) if v1 else "—",
                      font=f_val, fill=col1, anchor="rm")
            draw.text((xv2 + 3, y + ROW_H // 2), str(v2) if v2 else "—",
                      font=f_val, fill=col2, anchor="lm")

            # stat label (centered in center column)
            draw.text(((xctr + xb2) // 2, y + ROW_H // 2), label,
                      font=f_reg, fill=TEXT, anchor="mm")

            y += ROW_H

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

def _blend(fg: Tuple[int, int, int], bg: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(int(round(bg[i] + (fg[i] - bg[i]) * t)) for i in range(3))


_LIB_DIR = "/usr/share/fonts/truetype/liberation2/"
_QS_DIR = "/usr/share/fonts/truetype/quicksand/"


def _lib(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans (a clean Arial-alike) — used for anything that needs to stay legible small."""
    try:
        return ImageFont.truetype(_LIB_DIR + ("LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"), size)
    except Exception:
        return ImageFont.load_default()


def _qs(size: int) -> ImageFont.FreeTypeFont:
    """Quicksand Bold — a rounder, more distinctive display face for names/headline numbers."""
    try:
        return ImageFont.truetype(_QS_DIR + "Quicksand-Bold.ttf", size)
    except Exception:
        return _lib(True, size)


def _fit_font(draw: "ImageDraw.ImageDraw", text: str, loader, max_w: int, start_size: int, min_size: int) -> "ImageFont.FreeTypeFont":
    """Shrink a font (via `loader(size)`) until `text` fits within max_w."""
    size = start_size
    while size > min_size:
        font = loader(size)
        if draw.textlength(text, font=font) <= max_w:
            return font
        size -= 2
    return loader(min_size)


def generate_player_card_image(
    player_name: str,
    team_abbrev: str,
    stat_type: str,             # 'hitting' or 'pitching'
    years_label: str,
    is_career: bool,
    bio_line: str,
    league_label: str,          # "MLB" or "MiLB"
    level_label: Optional[str], # e.g. "AAA" for a MiLB player, else None
    headline: List[Tuple[str, str]],
    grid: List[Tuple[str, str]],
    multi_rows: Optional[List[Tuple[str, ...]]] = None,  # (season, team, v1, v2, v3, v4) per row when >1 season
    headshot_bytes: Optional[bytes] = None,
) -> io.BytesIO:
    """Render a portrait, digital-trading-card-style stat card for a single player.

    Drawn at 3x scale and downsampled with LANCZOS at the end — PIL doesn't anti-alias
    ellipses/rounded rects on its own, so this is what keeps the ring and card corners smooth
    instead of jagged.
    """
    SS = 3  # supersampling factor

    def S(v: float) -> int:
        return round(v * SS)

    W = S(480)
    PAD = S(20)

    primary, secondary = _team_colors(team_abbrev)
    ACCENT = _readable(secondary, floor=140)
    BG_TOP = primary
    BG_BOTTOM = (10, 10, 16)
    TEXT = (240, 240, 248)
    DIM = (200, 203, 220)

    f_league  = _lib(True, S(15))
    f_team    = _lib(True, S(19))
    f_bio     = _lib(False, S(19))
    f_ribbon  = _lib(True, S(18))
    f_hval    = _qs(S(36))
    f_hlbl    = _lib(True, S(14))
    f_gval    = _qs(S(22))
    f_glbl    = _lib(False, S(18))
    f_footer  = _lib(False, S(11))

    TOP_BAR   = S(44)
    HEAD      = S(160)
    HEAD_H    = HEAD + S(24)
    NAME_H    = S(52)
    BIO_H     = S(28)
    GAP       = S(14)
    RIBBON_H  = S(38)
    HEADLINE_H = S(116)
    FOOTER_H  = S(30)

    CELL_GAP = S(10)
    TABLE_ROW_H = S(46)
    TABLE_COL_GAP = S(16)

    if multi_rows:
        ROW_H = S(32)
        stats_area_h = ROW_H * (len(multi_rows) + 1)
    else:
        rows_per_col = -(-len(grid) // 2)  # two-column label|value table, split evenly
        stats_area_h = rows_per_col * TABLE_ROW_H

    total_h = (PAD + TOP_BAR + HEAD_H + NAME_H + BIO_H + GAP + RIBBON_H + GAP +
               (0 if multi_rows else HEADLINE_H + GAP) + stats_area_h + GAP + FOOTER_H + PAD)

    img = Image.new("RGB", (W, total_h), BG_BOTTOM)
    draw = ImageDraw.Draw(img, "RGBA")

    # Vertical gradient background.
    fade_end = min(total_h, HEAD_H + TOP_BAR + PAD + S(220))
    for row in range(total_h):
        t = min(1.0, row / max(1, fade_end))
        draw.line([(0, row), (W, row)], fill=_blend(BG_TOP, BG_BOTTOM, 1 - t))

    y = PAD

    # Top bar: league label left, team badge right.
    draw.text((PAD, y), league_label.upper(), font=f_league, fill=_readable(secondary, floor=160))
    badge_text = team_abbrev or "—"
    bw = draw.textlength(badge_text, font=f_team) + S(20)
    badge_h = S(30)
    draw.rounded_rectangle([W - PAD - bw, y - S(4), W - PAD, y - S(4) + badge_h], radius=badge_h // 2, fill=(0, 0, 0, 110), outline=ACCENT, width=S(1))
    draw.text((W - PAD - bw / 2, y - S(4) + badge_h / 2), badge_text, font=f_team, fill=TEXT, anchor="mm")
    y += TOP_BAR

    # Headshot.
    cx = W // 2
    if headshot_bytes:
        hs = _circle_headshot(headshot_bytes, HEAD, ACCENT, gap=S(6), ring_width=S(3))
        if hs:
            img.paste(hs, (cx - HEAD // 2, y), hs)
    else:
        draw.ellipse([cx - HEAD // 2, y, cx + HEAD // 2, y + HEAD], outline=ACCENT, width=S(3))
    y += HEAD_H

    # Player name (auto-shrink to fit).
    name_font = _fit_font(draw, player_name, _qs, W - 2 * PAD, S(38), S(20))
    draw.text((cx, y + NAME_H // 2), player_name, font=name_font, fill=TEXT, anchor="mm")
    y += NAME_H

    draw.text((cx, y + BIO_H // 2), bio_line, font=f_bio, fill=DIM, anchor="mm")
    y += BIO_H + GAP

    # Ribbon: season / stat type / level.
    ribbon_label = f"{'CAREER' if is_career else years_label} • {stat_type.upper()}"
    if level_label:
        ribbon_label += f" • {level_label}"
    draw.rounded_rectangle([PAD, y, W - PAD, y + RIBBON_H], radius=S(8), fill=_blend(ACCENT, (0, 0, 0), 0.35))
    draw.text((cx, y + RIBBON_H // 2), ribbon_label, font=f_ribbon, fill=(15, 15, 20), anchor="mm")
    y += RIBBON_H + GAP

    if not multi_rows:
        # Headline stat boxes.
        n = len(headline) or 1
        box_w = (W - 2 * PAD - (n - 1) * CELL_GAP) // n
        bx = PAD
        for label, value in headline:
            draw.rounded_rectangle([bx, y, bx + box_w, y + HEADLINE_H], radius=S(10), fill=(255, 255, 255, 18), outline=ACCENT, width=S(1))
            draw.text((bx + box_w / 2, y + HEADLINE_H * 0.40), str(value), font=f_hval, fill=TEXT, anchor="mm")
            draw.text((bx + box_w / 2, y + HEADLINE_H * 0.78), label, font=f_hlbl, fill=ACCENT, anchor="mm")
            bx += box_w + CELL_GAP
        y += HEADLINE_H + GAP

        # Detail table: two columns of label/value rows, alternating row shading. The value
        # sits right after the widest label in its column (not stretched to the far edge) so
        # each row reads as one tight unit instead of two words with a wide gap between them.
        col_w = (W - 2 * PAD - TABLE_COL_GAP) // 2
        left, right = grid[:rows_per_col], grid[rows_per_col:]
        table_top = y
        for col_idx, col_data in enumerate((left, right)):
            col_x = PAD + col_idx * (col_w + TABLE_COL_GAP)
            max_label_w = max((draw.textlength(label, font=f_glbl) for label, _ in col_data), default=0)
            value_x = col_x + S(12) + max_label_w + S(18)
            ry = table_top
            for i, (label, value) in enumerate(col_data):
                if i % 2 == 0:
                    draw.rectangle([col_x, ry, col_x + col_w, ry + TABLE_ROW_H], fill=(255, 255, 255, 10))
                draw.text((col_x + S(12), ry + TABLE_ROW_H // 2), label, font=f_glbl, fill=DIM, anchor="lm")
                draw.text((value_x, ry + TABLE_ROW_H // 2), str(value), font=f_gval, fill=TEXT, anchor="lm")
                ry += TABLE_ROW_H
        y = table_top + stats_area_h
    else:
        headers = ("YEAR", "TM") + tuple(h[0] for h in headline)
        n_cols = len(headers)
        col_w = (W - 2 * PAD) // n_cols
        rx = PAD
        for h in headers:
            draw.text((rx + col_w / 2, y + ROW_H // 2), h, font=f_glbl, fill=ACCENT, anchor="mm")
            rx += col_w
        y += ROW_H
        for i, row in enumerate(multi_rows):
            if i % 2 == 0:
                draw.rectangle([PAD, y, W - PAD, y + ROW_H], fill=(255, 255, 255, 10))
            rx = PAD
            for val in row:
                draw.text((rx + col_w / 2, y + ROW_H // 2), str(val), font=f_gval, fill=TEXT, anchor="mm")
                rx += col_w
            y += ROW_H

    # Footer.
    y = total_h - PAD - FOOTER_H // 2
    draw.line([(PAD, y - FOOTER_H // 2 + S(2)), (W - PAD, y - FOOTER_H // 2 + S(2))], fill=(255, 255, 255, 30))
    draw.text((cx, y), "MLB Stats API", font=f_footer, fill=DIM, anchor="mm")

    img = img.resize((W // SS, total_h // SS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_compare_stats_image(
    p1_label: str, p2_label: str,
    year_str: str, stat_type: str,
    rows: list,  # (label, v1, v2, direction) — direction: 1 higher-better, -1 lower-better, 0 neutral
    p1_team: Optional[str] = None, p2_team: Optional[str] = None,
    p1_headshot: Optional[bytes] = None, p2_headshot: Optional[bytes] = None,
) -> io.BytesIO:
    """Render a dark-mode side-by-side stat comparison table, highlighting whichever
    player has the better value in each row.

    Drawn at 3x scale and downsampled with LANCZOS at the end for smooth headshot rings
    and row corners, matching the /stats card renderer.
    """
    SS = 3

    def S(v: float) -> int:
        return round(v * SS)

    PAD     = S(18)
    VAL_W   = S(92)
    CTR_W   = S(120)
    W       = 2 * PAD + 2 * VAL_W + CTR_W   # keeps the table centered — no leftover margin

    TITLE_H = S(46)
    HEAD    = S(72)
    HEAD_H  = HEAD + S(6)
    NAMES_H = S(34)
    ROW_H   = S(34)

    total_h = TITLE_H + HEAD_H + NAMES_H + len(rows) * ROW_H + PAD

    BG      = (16, 18, 27)
    ROW_ALT = (20, 23, 35)
    TEXT    = (224, 224, 235)
    DIM     = (110, 115, 140)
    LABEL_C = (185, 190, 218)

    p1_primary, p1_secondary = _team_colors(p1_team)
    p2_primary, p2_secondary = _team_colors(p2_team)
    P1_NAME = _readable(p1_primary)
    P2_NAME = _readable(p2_primary)
    # Lightened so dark navy/black team colors (e.g. NYY, PIT) still show up as a
    # visible highlight against the dark background instead of blending into it.
    P1_HILITE = _readable(p1_primary, floor=90)
    P2_HILITE = _readable(p2_primary, floor=90)

    f_title = _qs(S(19))
    f_reg   = _lib(False, S(15))
    f_val   = _qs(S(17))

    img  = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img, "RGBA")

    xv1  = PAD                 # left edge of P1 value column
    xctr = xv1 + VAL_W         # left edge of center label column
    xb2  = xctr + CTR_W        # left edge of P2 value column
    xend = xb2 + VAL_W         # right edge of P2 value column

    c1 = (xv1 + xctr) // 2
    c2 = (xb2 + xend) // 2

    y = PAD // 2
    draw.text((W // 2, y + TITLE_H // 2),
              f"{year_str} {'Hitting' if stat_type == 'hitting' else 'Pitching'} Comparison",
              font=f_title, fill=TEXT, anchor="mm")
    y += TITLE_H

    if p1_headshot:
        hs = _circle_headshot(p1_headshot, HEAD, p1_secondary, gap=S(6), ring_width=S(3))
        if hs:
            img.paste(hs, (c1 - HEAD // 2, y), hs)
    if p2_headshot:
        hs = _circle_headshot(p2_headshot, HEAD, p2_secondary, gap=S(6), ring_width=S(3))
        if hs:
            img.paste(hs, (c2 - HEAD // 2, y), hs)
    draw.text((W // 2, y + HEAD_H // 2), "vs", font=f_reg, fill=DIM, anchor="mm")
    y += HEAD_H

    max_name_w = W // 2 - PAD - S(10)
    p1_font = _fit_font(draw, p1_label, lambda s: _lib(True, s), max_name_w, S(15), S(9))
    p2_font = _fit_font(draw, p2_label, lambda s: _lib(True, s), max_name_w, S(15), S(9))
    draw.text((c1, y + NAMES_H // 2), p1_label, font=p1_font, fill=P1_NAME, anchor="mm")
    draw.text((c2, y + NAMES_H // 2), p2_label, font=p2_font, fill=P2_NAME, anchor="mm")
    y += NAMES_H

    def _num(v):
        if v is None:
            return None
        try:
            return float(str(v).replace('%', ''))
        except ValueError:
            return None

    for j, (label, v1, v2, direction) in enumerate(rows):
        row_bg = ROW_ALT if j % 2 == 0 else BG
        if j % 2 == 0:
            draw.rectangle([PAD, y, W - PAD, y + ROW_H], fill=row_bg)

        winner = 0
        if direction != 0:
            n1, n2 = _num(v1), _num(v2)
            if n1 is not None and n2 is not None and n1 != n2:
                if direction == 1:
                    winner = 1 if n1 > n2 else 2
                else:
                    winner = 1 if n1 < n2 else 2

        if winner == 1:
            draw.rectangle([xv1, y, xctr - 1, y + ROW_H - 1], fill=_blend(P1_HILITE, row_bg, 0.45))
        elif winner == 2:
            draw.rectangle([xb2, y, xend - 1, y + ROW_H - 1], fill=_blend(P2_HILITE, row_bg, 0.45))

        draw.text((xctr - S(8), y + ROW_H // 2), str(v1) if v1 is not None else "—",
                  font=f_val, fill=TEXT, anchor="rm")
        draw.text((xb2 + S(8), y + ROW_H // 2), str(v2) if v2 is not None else "—",
                  font=f_val, fill=TEXT, anchor="lm")
        draw.text(((xctr + xb2) // 2, y + ROW_H // 2), label,
                  font=f_reg, fill=LABEL_C, anchor="mm")

        y += ROW_H

    img = img.resize((W // SS, total_h // SS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# Define colors based on result
COLORS = {
    'Ball': (46, 204, 113),      # Green
    'Strike': (231, 76, 60),     # Red
    'Foul': (231, 76, 60),      # Red
    'Hit': (155, 89, 182),      # Purple
    'In play': (155, 89, 182),   # Purple
}

DEFAULT_COLOR = (52, 152, 219) # Blue for unknown

def get_color_for_desc(desc: str):
    d = desc.lower()
    if 'ball' in d: return COLORS['Ball']
    if 'strike' in d or 'foul' in d: return COLORS['Strike']
    if 'in play' in d or 'hit' in d: return COLORS['In play']
    return DEFAULT_COLOR

def generate_pitch_plot(pitches, stand: str = "R") -> io.BytesIO:

    # canvas size - taller to accommodate high pitches
    width, height = 1550, 1350
    # The zone area will be on the left, legend on the right
    zone_area_width = 850

    
    img = Image.new('RGB', (width, height), color=(18, 25, 33)) # Dark background
    draw = ImageDraw.Draw(img)
    
    if not pitches:
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer

    # Determine strike zone dims
    sz_top = pitches[0].sz_top or 3.5
    sz_bot = pitches[0].sz_bot or 1.5
    
    # Match MLB Gameday: one uniform scale on both axes (true physical
    # proportions, ~58.8 px/ft at Gameday's resolution). Zone width is the
    # 17" plate, ball markers are real baseball size (14/83 of zone width).
    zone_px_w = 332          # 4x Gameday's 83 px
    ball_r = 28              # 4x Gameday's 14 px diameter
    scale = zone_px_w / (2 * 0.708)  # px per foot, both axes

    # Anchor the vertical center of the strike zone at a fixed pixel so
    # batters with short/tall zones don't push the plot off-canvas
    base_y = 560 + ((sz_top + sz_bot) / 2) * scale

    def get_x(px):
        center_x = zone_area_width // 2
        return center_x + (px * scale)

    def get_y(pz):
        return base_y - (pz * scale)

    # Ground plane in fake perspective (catcher's view, like Gameday): depth d
    # in feet toward the viewer maps to pixels below ground level, heavily
    # foreshortened, with lines fanning outward slightly as they get closer
    ground_y = get_y(0)
    box_front_d = 0.708 + 3.0  # deepest chalk: 3 ft in front of plate center
    # px per foot of depth, flattened further if a tall zone leaves little room
    depth_scale = min(48, (height - 20 - ground_y) / box_front_d)
    spread = 0.03      # lateral spread per foot of depth

    def ground_pt(x_ft, d_ft):
        return (zone_area_width // 2 + x_ft * scale * (1 + spread * d_ft),
                ground_y + d_ft * depth_scale)

    # Home plate flat on the ground: 17" back edge at d=0, 8.5" parallel
    # sides, point toward the catcher (17" total depth)
    plate_w_feet = 0.708
    draw.polygon([
        ground_pt(-plate_w_feet, 0),
        ground_pt(plate_w_feet, 0),
        ground_pt(plate_w_feet, 0.708),
        ground_pt(0, 1.417),
        ground_pt(-plate_w_feet, 0.708),
    ], fill=(180, 180, 185))

    # Batter's boxes: inner chalk line 6" outside the plate, running from 3 ft
    # behind to 3 ft in front of the plate's center; the front line runs
    # outward to the edge only, so no chalk crosses in front of home plate
    box_color = (65, 80, 95)
    box_inner = plate_w_feet + 0.5
    box_back_d = 0.708 - 3.0
    for side in (-1, 1):
        inner_back = ground_pt(side * box_inner, box_back_d)
        inner_front = ground_pt(side * box_inner, box_front_d)
        outer_x = 10 if side < 0 else zone_area_width - 10
        draw.line([inner_back, inner_front], fill=box_color, width=4)
        draw.line([inner_front, (outer_x, inner_front[1])], fill=box_color, width=4)

    # Draw 3x3 strike zone
    zx_left = get_x(-0.708)
    zx_right = get_x(0.708)
    zy_top = get_y(sz_top)
    zy_bot = get_y(sz_bot)
    
    # Outer box - High contrast. PIL draws outline width inward, so expand
    # the rect by half the line width to center it on the true zone boundary
    draw.rectangle([zx_left - 4, zy_top - 4, zx_right + 4, zy_bot + 4], outline=(200, 200, 200), width=9)
    
    # Internal lines for 3x3
    v_step = (zx_right - zx_left) / 3
    draw.line([zx_left + v_step, zy_top, zx_left + v_step, zy_bot], fill=(120, 120, 120), width=4)
    draw.line([zx_left + 2*v_step, zy_top, zx_left + 2*v_step, zy_bot], fill=(120, 120, 120), width=4)
    h_step = (zy_bot - zy_top) / 3
    draw.line([zx_left, zy_top + h_step, zx_right, zy_top + h_step], fill=(120, 120, 120), width=4)
    draw.line([zx_left, zy_top + 2*h_step, zx_right, zy_top + 2*h_step], fill=(120, 120, 120), width=4)

    # Load fonts - Ultra Large for 1450x1350 resolution
    # Try different font paths for Windows/Linux compatibility
    def get_font(size, bold=False):
        if bold:
            fonts = ["arialbd.ttf", "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
        else:
            fonts = ["arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
            
        for f in fonts:
            try:
                return ImageFont.truetype(f, size)
            except:
                continue
        return ImageFont.load_default()

    font_title = get_font(56, bold=True)
    font_large = get_font(64)
    font_small = get_font(42)
    font_bold = get_font(42, bold=True)

    # Legend layout: single column, rows shrink to fit the pitch count.
    # Two-line rows need ~100px; below that switch to compact one-line rows
    # with proportionally scaled fonts/markers (floored at 60% size)
    legend_top = 100
    row_h = min(140, (height - 2 * legend_top) / len(pitches))
    compact = row_h < 100
    lg_scale = max(0.6, row_h / 140)
    rl = int(35 * lg_scale) if compact else 35
    font_lg_bold = get_font(int(42 * lg_scale), bold=True) if compact else font_bold
    font_lg_small = get_font(int(42 * lg_scale)) if compact else font_small

    # Draw Batter indicator
    bat_color = (60, 70, 80)
    bat_y = get_y(sz_top) - 120
    if stand == "R":
        draw.text((get_x(-0.708) - 200, bat_y), "RHB", fill=bat_color, font=font_title)
    else:
        draw.text((get_x(0.708) + 60, bat_y), "LHB", fill=bat_color, font=font_title)






    # Plot pitches
    for i, p in enumerate(pitches):
        px, py = get_x(p.px), get_y(p.pz)
        color = get_color_for_desc(p.description)
        
        # Draw circle
        r = ball_r
        draw.ellipse([px-r, py-r, px+r, py+r], fill=color, outline=(255, 255, 255), width=4)
        
        # Draw number
        num_str = str(p.number)
        bbox = draw.textbbox((0, 0), num_str, font=font_bold)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((px - tw/2, py - th/2 - 5), num_str, fill=(255, 255, 255), font=font_bold)
        
        # Legend (single column on the right)
        lx = 880
        ly = legend_top + i * row_h

        if compact:
            # One line per pitch: ● Description · speed type        count
            lcy = ly + row_h / 2
            lcx = lx + rl
            draw.ellipse([lcx-rl, lcy-rl, lcx+rl, lcy+rl], fill=color, outline=(255, 255, 255), width=3)
            draw.text((lcx, lcy), num_str, fill=(255, 255, 255), font=font_lg_bold, anchor="mm")

            count_x = width - 60
            draw.text((count_x, lcy), p.count, fill=(200, 200, 200), font=font_lg_small, anchor="rm")

            text_x = lx + 2 * rl + 30
            detail = f" · {p.speed} mph {p.type}"
            max_w = count_x - draw.textlength(p.count, font=font_lg_small) - 40 - text_x
            desc = p.description
            while desc and draw.textlength(desc + "…", font=font_lg_bold) + draw.textlength(detail, font=font_lg_small) > max_w:
                desc = desc[:-1].rstrip()
            if desc != p.description:
                desc += "…"
            dw = draw.textlength(desc, font=font_lg_bold)
            draw.text((text_x, lcy), desc, fill=(255, 255, 255), font=font_lg_bold, anchor="lm")
            draw.text((text_x + dw, lcy), detail, fill=(180, 180, 180), font=font_lg_small, anchor="lm")
        else:
            # Two-line rows: description + count, speed/type below
            lcx, lcy = lx + rl, ly + 45
            draw.ellipse([lcx-rl, lcy-rl, lcx+rl, lcy+rl], fill=color, outline=(255, 255, 255), width=4)
            draw.text((lcx, lcy), num_str, fill=(255, 255, 255), font=font_bold, anchor="mm")

            draw.text((lx + 100, ly), f"{p.description}", fill=(255, 255, 255), font=font_bold)
            draw.text((width - 60, ly), f"{p.count}", fill=(200, 200, 200), font=font_small, anchor="ra")
            draw.text((lx + 100, ly + 50), f"{p.speed} mph {p.type}", fill=(180, 180, 180), font=font_small)

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer


def _zone_color(value: float, chart_type: str) -> tuple:
    """Map a stat value to a blue→white→red heatmap color."""
    # (lo, mid, hi) per chart type — mid maps to white
    ranges = {
        'ba':    (0.000, 0.250, 0.500),
        'slg':   (0.000, 0.400, 0.800),
        'obp':   (0.000, 0.300, 0.600),
        'woba':  (0.000, 0.300, 0.600),
        'xba':   (0.000, 0.250, 0.500),
        'xslg':  (0.000, 0.400, 0.800),
        'xwoba': (0.000, 0.300, 0.600),
        'whiff_percent': (0.0, 0.5, 1.0),
        'swing_percent': (0.0, 0.5, 1.0),
    }
    lo, mid, hi = ranges.get(chart_type, (0.0, 0.250, 0.500))
    if value <= mid:
        t = max(0.0, (value - lo) / (mid - lo) if mid > lo else 0.0)  # 0→1 as lo→mid
        r = int(255 * t)
        g = int(255 * t)
        b = 255
    else:
        t = min(1.0, (value - mid) / (hi - mid) if hi > mid else 1.0)  # 0→1 as mid→hi
        r = 255
        g = int(255 * (1 - t))
        b = int(255 * (1 - t))
    return (r, g, b)


def generate_zone_plot(data: dict) -> io.BytesIO:
    """Render a Baseball Savant-style batting zone heatmap."""
    cells = data['cells']
    player_name = data['player_name']
    year = data['year']
    chart_type = data['chart_type']

    label_map = {
        'ba': 'BA', 'slg': 'SLG', 'obp': 'OBP', 'woba': 'wOBA',
        'xba': 'xBA', 'xslg': 'xSLG', 'xwoba': 'xwOBA',
        'whiff_percent': 'Whiff%', 'swing_percent': 'Swing%',
    }
    chart_label = label_map.get(chart_type, chart_type.upper())

    # Canvas
    W, H = 900, 1000
    bg = (18, 25, 33)
    img = Image.new('RGB', (W, H), color=bg)
    draw = ImageDraw.Draw(img)

    def get_font(size, bold=False):
        candidates = (
            ["arialbd.ttf", "DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
            if bold else
            ["arial.ttf", "DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
        )
        for f in candidates:
            try:
                return ImageFont.truetype(f, size)
            except:
                continue
        return ImageFont.load_default()

    font_title  = get_font(36, bold=True)
    font_sub    = get_font(26)
    font_cell   = get_font(22, bold=True)
    font_legend = get_font(20)

    # Title
    title = f"{player_name}  ·  {chart_label} Zone Profile  ·  {year}"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, 18), title, fill=(230, 230, 230), font=font_title)

    # Grid dimensions — px in [-2, 2], pz in [0.25, 4.25], bucket 0.5
    px_vals  = sorted(set(float(c['px']) for c in cells))
    pz_vals  = sorted(set(float(c['pz']) for c in cells), reverse=True)  # top→bottom
    n_cols, n_rows = len(px_vals), len(pz_vals)

    # Plot area
    plot_x0, plot_y0 = 60, 80
    plot_x1, plot_y1 = W - 60, H - 120
    cell_w = (plot_x1 - plot_x0) / n_cols
    cell_h = (plot_y1 - plot_y0) / n_rows

    px_to_col = {v: i for i, v in enumerate(px_vals)}
    pz_to_row = {v: i for i, v in enumerate(pz_vals)}

    # Build lookup
    lookup = {}
    for c in cells:
        lookup[(float(c['px']), float(c['pz']))] = c

    def cell_rect(col, row):
        x0 = plot_x0 + col * cell_w
        y0 = plot_y0 + row * cell_h
        return x0, y0, x0 + cell_w, y0 + cell_h

    # Draw cells
    for (px, pz), c in lookup.items():
        col = px_to_col[px]
        row = pz_to_row[pz]
        x0, y0, x1, y1 = cell_rect(col, row)

        raw = c.get('ba') if chart_type == 'ba' else c.get(chart_type)
        if raw is None:
            fill = (40, 48, 58)   # no-data grey
            text_val = "—"
        else:
            val = float(raw)
            fill = _zone_color(val, chart_type)
            if chart_type in ('whiff_percent', 'swing_percent'):
                text_val = f"{val:.0%}"
            else:
                text_val = f"{val:.3f}".lstrip('0') or '.000'

        draw.rectangle([x0 + 1, y0 + 1, x1 - 1, y1 - 1], fill=fill)

        # Cell label
        bbox = draw.textbbox((0, 0), text_val, font=font_cell)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # Dark text on light cells, light text on dark cells
        brightness = 0.299*fill[0] + 0.587*fill[1] + 0.114*fill[2]
        text_color = (20, 20, 20) if brightness > 160 else (240, 240, 240)
        draw.text((cx - tw / 2, cy - th / 2), text_val, fill=text_color, font=font_cell)

    # Strike zone overlay — 4 cols wide (centers -1.25 to 1.25), 5 rows tall (centers 1.25 to 3.25)
    # Box edges sit at the outer boundary of the outermost included cells.
    sz_col_lo = px_vals.index(-0.75)   # leftmost included column
    sz_col_hi = px_vals.index(0.75)    # rightmost included column
    sz_row_hi = pz_vals.index(2.75)    # topmost included row (pz_vals is descending)
    sz_row_lo = pz_vals.index(1.25)    # bottommost included row
    sz_x0 = plot_x0 + sz_col_lo * cell_w
    sz_x1 = plot_x0 + (sz_col_hi + 1) * cell_w
    sz_y0 = plot_y0 + sz_row_hi * cell_h
    sz_y1 = plot_y0 + (sz_row_lo + 1) * cell_h
    draw.rectangle([sz_x0, sz_y0, sz_x1, sz_y1], outline=(100, 220, 100), width=4)

    # Plate
    plate_y = plot_y1 + 18
    mid_x = (plot_x0 + plot_x1) / 2
    pw = cell_w * 1.416  # ~17 inches
    draw.polygon([
        (mid_x, plate_y + 22),
        (mid_x - pw / 2, plate_y + 8),
        (mid_x - pw / 2, plate_y - 8),
        (mid_x + pw / 2, plate_y - 8),
        (mid_x + pw / 2, plate_y + 8),
    ], fill=(180, 180, 185))

    # Legend bar (bottom)
    bar_x0, bar_x1 = plot_x0, plot_x1
    bar_y = H - 55
    bar_h = 18
    steps = 100
    step_w = (bar_x1 - bar_x0) / steps
    for i in range(steps):
        t = i / steps
        lo, hi = {'ba': (0.0, 0.400), 'slg': (0.0, 0.800)}.get(chart_type, (0.0, 0.500))
        color = _zone_color(lo + t * (hi - lo), chart_type)
        draw.rectangle([bar_x0 + i * step_w, bar_y, bar_x0 + (i + 1) * step_w, bar_y + bar_h], fill=color)
    draw.rectangle([bar_x0, bar_y, bar_x1, bar_y + bar_h], outline=(100, 100, 100), width=1)
    lo_label = "0.000"
    hi_label = {"ba": ".400", "slg": ".800", "whiff_percent": "100%", "swing_percent": "100%"}.get(chart_type, ".500")
    draw.text((bar_x0, bar_y + bar_h + 4), lo_label, fill=(160, 160, 160), font=font_legend)
    bbox = draw.textbbox((0, 0), hi_label, font=font_legend)
    draw.text((bar_x1 - (bbox[2] - bbox[0]), bar_y + bar_h + 4), hi_label, fill=(160, 160, 160), font=font_legend)
    mid_label = chart_label
    bbox = draw.textbbox((0, 0), mid_label, font=font_legend)
    draw.text(((bar_x0 + bar_x1) / 2 - (bbox[2] - bbox[0]) / 2, bar_y + bar_h + 4), mid_label, fill=(160, 160, 160), font=font_legend)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# Event → (label, dot color) for spray chart legend/coloring.
_SPRAY_EVENT_STYLE = {
    'single':     ('1B', (70, 150, 230)),
    'double':     ('2B', (240, 170, 40)),
    'triple':     ('3B', (180, 90, 220)),
    'home_run':   ('HR', (220, 50, 50)),
}
_SPRAY_OUT_COLOR = (120, 128, 138)


# Angle (degrees from center field, negative = toward left field line) for each
# MLB Stats API fieldInfo measurement, used to shape the outfield wall.
_WALL_POINTS = [
    ('leftLine', -45), ('left', -30), ('leftCenter', -15),
    ('center', 0),
    ('rightCenter', 15), ('right', 30), ('rightLine', 45),
]


def _spray_field_font(size, bold=False):
    candidates = (
        ["arialbd.ttf", "DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        if bold else
        ["arial.ttf", "DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for f in candidates:
        try:
            return ImageFont.truetype(f, size)
        except:
            continue
    return ImageFont.load_default()


def _spray_field_layout(W, field_top, side_margin, bottom_reserve, max_plot_height, field_info, event_points_ft):
    """Compute canvas height and plate/scale for a spray-chart field, sized to fit both
    the real (or generic) outfield wall and every plotted point without clipping."""
    wall_points_ft = None
    if field_info:
        try:
            wall_points_ft = [(angle, float(field_info[key])) for key, angle in _WALL_POINTS if field_info.get(key)]
        except (TypeError, ValueError):
            wall_points_ft = None

    generic_radius_ft = 400.0
    if wall_points_ft:
        raw_pts_ft = [(d * math.sin(math.radians(a)), d * math.cos(math.radians(a))) for a, d in wall_points_ft]
    else:
        raw_pts_ft = [(generic_radius_ft * math.sin(math.radians(a)), generic_radius_ft * math.cos(math.radians(a)))
                      for a in (-45, 0, 45)]
    raw_pts_ft.extend(event_points_ft)

    # The painted field extends past the wall/foul lines by the foul-ground apron; keep it in frame.
    lines = dict(wall_points_ft or {})
    line_ft = max(lines.get(-45) or generic_radius_ft, lines.get(45) or generic_radius_ft)
    apron_edge = (line_ft + _FOUL_GROUND_FT)
    raw_pts_ft.append((apron_edge * math.sin(math.radians(_FOUL_SPREAD_DEG)),
                       apron_edge * math.cos(math.radians(_FOUL_SPREAD_DEG))))

    margin_ft = 38   # room for the wall distance labels
    max_dx_ft = max(abs(x) for x, y in raw_pts_ft) + margin_ft
    max_dy_ft = max(y for x, y in raw_pts_ft) + margin_ft

    avail_half_width = W / 2 - side_margin
    scale = min(avail_half_width / max_dx_ft, max_plot_height / max_dy_ft)

    plate_x = W / 2
    plate_y = field_top + max_dy_ft * scale
    H = int(plate_y + bottom_reserve)

    return {
        'wall_points_ft': wall_points_ft,
        'generic_radius_ft': generic_radius_ft,
        'scale': scale,
        'plate_x': plate_x,
        'plate_y': plate_y,
        'H': H,
    }


# Ballpark surface colors, tuned for the dark chart background.
_FIELD_GRASS       = (36, 82, 54)
_FIELD_GRASS_LIGHT = (41, 89, 59)   # mown band, alternates with _FIELD_GRASS
_FIELD_FOUL        = (30, 40, 50)   # foul ground / backstop apron
_FIELD_DIRT        = (128, 91, 62)
_FIELD_TRACK       = (110, 78, 54)
_FIELD_CHALK       = (232, 236, 240)
_FIELD_WALL        = (196, 204, 214)

# Real-field dimensions (feet) used to lay out the infield.
_BASE_PATH_FT      = 90.0
_MOUND_CENTER_FT   = 59.0    # from the back of home plate, along center field
_MOUND_RADIUS_FT   = 9.0
_INFIELD_ARC_FT    = 95.0    # dirt arc radius, measured from the mound center
_HOME_CIRCLE_FT    = 13.0
_BASE_PATH_WIDTH_FT = 7.0
_WARNING_TRACK_FT  = 16.0
_FOUL_GROUND_FT    = 22.0    # apron drawn outside the wall / foul lines
_BACKSTOP_FT       = 46.0
_FOUL_SPREAD_DEG   = 52.0    # apron half-angle, past the 45° foul lines


def _draw_spray_field(img, draw, layout, font_distance):
    """Paint the ballpark — grass, warning track, infield dirt, basepaths, mound, bases,
    foul lines and outfield wall — then label the wall distances."""
    scale, plate_x, plate_y = layout['scale'], layout['plate_x'], layout['plate_y']
    wall_points_ft, generic_radius_ft = layout['wall_points_ft'], layout['generic_radius_ft']

    max_range_ft = max(d for _, d in wall_points_ft) if wall_points_ft else generic_radius_ft

    def wall_dist_at(angle_deg):
        """Wall distance for any angle, linearly interpolated between the park's measurements."""
        if not wall_points_ft:
            return generic_radius_ft
        pts = sorted(wall_points_ft)
        if angle_deg <= pts[0][0]:
            return pts[0][1]
        if angle_deg >= pts[-1][0]:
            return pts[-1][1]
        for (a0, d0), (a1, d1) in zip(pts, pts[1:]):
            if a0 <= angle_deg <= a1:
                t = 0.0 if a1 == a0 else (angle_deg - a0) / (a1 - a0)
                return d0 + t * (d1 - d0)
        return pts[-1][1]

    # The field is painted on a supersampled layer so the curves and chalk lines come out smooth.
    SS = 2
    layer = Image.new('RGBA', (img.width * SS, img.height * SS), (0, 0, 0, 0))
    fd = ImageDraw.Draw(layer)

    def pt(x_ft, y_ft):
        return ((plate_x + x_ft * scale) * SS, (plate_y - y_ft * scale) * SS)

    def polar(angle_deg, dist_ft):
        rad = math.radians(angle_deg)
        return pt(dist_ft * math.sin(rad), dist_ft * math.cos(rad))

    def circle(cx_ft, cy_ft, r_ft, **kw):
        x0, y0 = pt(cx_ft - r_ft, cy_ft + r_ft)
        x1, y1 = pt(cx_ft + r_ft, cy_ft - r_ft)
        fd.ellipse([x0, y0, x1, y1], **kw)

    angles = [a / 2 for a in range(-90, 91)]   # -45°..45° in half-degree steps
    wall_ft = [(a, wall_dist_at(a)) for a in angles]

    # Foul ground: the fair region pushed out past the wall and widened past each foul
    # line, plus a rounded apron behind the plate.
    spread = int(_FOUL_SPREAD_DEG * 2)
    apron = [(a / 2, wall_dist_at(min(max(a / 2, -45), 45)) + _FOUL_GROUND_FT)
             for a in range(-spread, spread + 1)]
    fd.polygon([pt(0, 0)] + [polar(a, d) for a, d in apron], fill=_FIELD_FOUL)
    circle(0, 0, _BACKSTOP_FT, fill=_FIELD_FOUL)

    # Grass out to the wall, with alternating mown wedges for depth.
    fd.polygon([pt(0, 0)] + [polar(a, d) for a, d in wall_ft], fill=_FIELD_GRASS)
    for i in range(0, len(wall_ft) - 1):
        if (i // 20) % 2:
            a0, d0 = wall_ft[i]
            a1, d1 = wall_ft[i + 1]
            fd.polygon([pt(0, 0), polar(a0, d0), polar(a1, d1)], fill=_FIELD_GRASS_LIGHT)

    # Warning track: a band just inside the wall.
    track_outer = [polar(a, d) for a, d in wall_ft]
    track_inner = [polar(a, max(d - _WARNING_TRACK_FT, 1)) for a, d in reversed(wall_ft)]
    fd.polygon(track_outer + track_inner, fill=_FIELD_TRACK)

    # Infield dirt: the arc swept 95 ft around the mound, cut off by the two foul lines.
    #   t along a foul line where it meets that arc (law of cosines, foul line at 45°).
    proj = _MOUND_CENTER_FT * math.cos(math.radians(45))
    foul_cut_ft = proj + math.sqrt(max(_INFIELD_ARC_FT ** 2 - (_MOUND_CENTER_FT ** 2 - proj ** 2), 0.0))
    arc_pts = []
    for deg in range(-180, 181, 2):
        rad = math.radians(deg)
        x = _INFIELD_ARC_FT * math.sin(rad)
        y = _MOUND_CENTER_FT + _INFIELD_ARC_FT * math.cos(rad)
        if y > 0 and abs(math.degrees(math.atan2(x, y))) <= 45:
            arc_pts.append((math.degrees(math.atan2(x, y)), x, y))
    arc_pts.sort()   # sweep left foul line → right foul line
    fd.polygon([pt(0, 0), polar(-45, foul_cut_ft)] +
               [pt(x, y) for _, x, y in arc_pts] +
               [polar(45, foul_cut_ft)], fill=_FIELD_DIRT)

    # Grass inside the basepaths — the infield diamond, inset so the paths read as dirt.
    half = _BASE_PATH_FT / math.sqrt(2)
    diamond = [(0.0, 0.0), (half, half), (0.0, 2 * half), (-half, half)]
    cx_d, cy_d = 0.0, half
    shrink = 1 - _BASE_PATH_WIDTH_FT / (half / math.sqrt(2))   # centroid→edge distance
    fd.polygon([pt(cx_d + (x - cx_d) * shrink, cy_d + (y - cy_d) * shrink) for x, y in diamond],
               fill=_FIELD_GRASS)

    # Home plate circle and pitcher's mound.
    circle(0, 0, _HOME_CIRCLE_FT, fill=_FIELD_DIRT)
    circle(0, _MOUND_CENTER_FT, _MOUND_RADIUS_FT, fill=_FIELD_DIRT)
    rub_x, rub_y = pt(-1.0, _MOUND_CENTER_FT + 1.5)
    rub_x1, rub_y1 = pt(1.0, _MOUND_CENTER_FT + 0.7)
    fd.rectangle([rub_x, rub_y, max(rub_x1, rub_x + SS), max(rub_y1, rub_y + SS)], fill=_FIELD_CHALK)

    # Foul lines out to the wall, and the bases.
    chalk_w = max(int(0.9 * scale * SS), 2)
    for side in (-45, 45):
        fd.line([pt(0, 0), polar(side, wall_dist_at(side))], fill=_FIELD_CHALK, width=chalk_w)

    # Bases are bags whose edges run parallel to the basepaths, so in this diamond
    # layout (where the paths run at 45°) each bag is drawn rotated 45° from the
    # field axes — a diamond in x/y-ft terms, corners pointing up/down/left/right.
    # First/third sit with their foul-line-side corner ON the foul line (bag entirely
    # in fair territory), so their centers are nudged toward second base by one
    # half-diagonal rather than centered on the line.
    base_half = max(1.4 * scale * SS, 3)
    second_px = pt(0.0, 2 * half)
    for bx, by in ((half, half), (0.0, 2 * half), (-half, half)):
        bcx, bcy = pt(bx, by)
        if (bx, by) != (0.0, 2 * half):
            dx, dy = second_px[0] - bcx, second_px[1] - bcy
            dist = math.hypot(dx, dy)
            bcx += dx / dist * base_half
            bcy += dy / dist * base_half
        fd.polygon([(bcx, bcy - base_half), (bcx + base_half, bcy),
                    (bcx, bcy + base_half), (bcx - base_half, bcy)], fill=_FIELD_CHALK)
    hx, hy = pt(0, 0)
    fd.polygon([(hx - base_half, hy - base_half), (hx + base_half, hy - base_half),
                (hx + base_half, hy), (hx, hy + base_half), (hx - base_half, hy)], fill=_FIELD_CHALK)

    # Outfield wall.
    fd.line([polar(a, d) for a, d in wall_ft], fill=_FIELD_WALL, width=max(int(2 * SS), 2), joint='curve')

    flat = layer.resize(img.size, Image.LANCZOS)
    img.paste(flat, (0, 0), flat)

    # Wall distance labels, offset outward along each measurement's radial direction.
    if wall_points_ft:
        for angle, dist in wall_points_ft:
            rad = math.radians(angle)
            lx = plate_x + (dist + 32) * math.sin(rad) * scale
            ly = plate_y - (dist + 32) * math.cos(rad) * scale
            label = f"{dist:.0f}'"
            bbox = draw.textbbox((0, 0), label, font=font_distance)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text((lx - tw / 2, ly - th / 2), label, fill=(170, 178, 188), font=font_distance)


def generate_spray_chart(data: dict) -> io.BytesIO:
    """Render a batted-ball spray chart for a batter, shaped to their home park's real wall distances."""
    events = data['events']
    player_name = data['player_name']
    year = data['year']
    venue_name = data.get('venue_name')
    field_info = data.get('field_info')

    W = 1200
    side_margin = 50
    field_top = 140
    bottom_reserve = 160   # legend + padding below home plate
    max_plot_height = 850  # caps canvas height when the batted-ball spread is narrow

    event_points_ft = []
    for e in events:
        try:
            event_points_ft.append((float(e['hc_x_ft']), float(e['hc_y_ft'])))
        except (KeyError, TypeError, ValueError):
            continue

    layout = _spray_field_layout(W, field_top, side_margin, bottom_reserve, max_plot_height, field_info, event_points_ft)
    scale, plate_x, plate_y, H = layout['scale'], layout['plate_x'], layout['plate_y'], layout['H']

    bg = (18, 25, 33)
    img = Image.new('RGB', (W, H), color=bg)
    draw = ImageDraw.Draw(img)

    font_title    = _spray_field_font(40, bold=True)
    font_sub      = _spray_field_font(26)
    font_legend   = _spray_field_font(22)
    font_distance = _spray_field_font(19, bold=True)

    title = f"{player_name}  ·  Spray Chart  ·  {year}"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, 20), title, fill=(230, 230, 230), font=font_title)

    subtitle = f"{len(events)} batted ball{'s' if len(events) != 1 else ''}"
    if venue_name:
        subtitle += f"  ·  {venue_name}"
    bbox = draw.textbbox((0, 0), subtitle, font=font_sub)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, 68), subtitle, fill=(150, 150, 150), font=font_sub)

    def to_canvas(hc_x_ft, hc_y_ft):
        return plate_x + hc_x_ft * scale, plate_y - hc_y_ft * scale

    _draw_spray_field(img, draw, layout, font_distance)

    # Plot each batted ball
    for e in events:
        try:
            x_ft = float(e['hc_x_ft'])
            y_ft = float(e['hc_y_ft'])
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = to_canvas(x_ft, y_ft)
        outcome = e.get('events') or ''
        label, color = _SPRAY_EVENT_STYLE.get(outcome, (None, _SPRAY_OUT_COLOR))
        r = 6 if label else 4
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=(18, 25, 33), width=1)

    # Legend
    legend_items = [('1B', _SPRAY_EVENT_STYLE['single'][1]),
                     ('2B', _SPRAY_EVENT_STYLE['double'][1]),
                     ('3B', _SPRAY_EVENT_STYLE['triple'][1]),
                     ('HR', _SPRAY_EVENT_STYLE['home_run'][1]),
                     ('Out/Other', _SPRAY_OUT_COLOR)]
    lx = 40
    ly = H - 34
    for label, color in legend_items:
        draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=color)
        draw.text((lx + 20, ly - 10), label, fill=(200, 200, 200), font=font_legend)
        bbox = draw.textbbox((0, 0), label, font=font_legend)
        lx += 20 + (bbox[2] - bbox[0]) + 30

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


_GAME_SPRAY_HIT_EVENTS = {'single', 'double', 'triple', 'home_run'}
_GAME_SPRAY_OUT_COLOR = (150, 150, 150)
_GAME_SPRAY_OUTCOME_COLORS = {
    'single': (57, 135, 229),    # blue
    'double': (217, 89, 38),     # orange
    'triple': (25, 158, 112),    # aqua
    'home_run': (237, 161, 0),   # gold
}
_GAME_SPRAY_OUTCOME_LEGEND = [
    ('Single', _GAME_SPRAY_OUTCOME_COLORS['single'], 'filled'),
    ('Double', _GAME_SPRAY_OUTCOME_COLORS['double'], 'filled'),
    ('Triple', _GAME_SPRAY_OUTCOME_COLORS['triple'], 'filled'),
    ('HR', _GAME_SPRAY_OUTCOME_COLORS['home_run'], 'ring'),
    ('Out/Other', _GAME_SPRAY_OUT_COLOR, 'hollow'),
]


def _spray_ordinal(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    suffix = 'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return f"{n}{suffix}"


def _spray_wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def generate_game_spray_chart(data: dict) -> io.BytesIO:
    """
    Render a spray chart of batted balls.
    Team-level charts (data['color_by'] == 'team') color dots by batting team.
    Player-level charts (data['color_by'] == 'outcome') show one player's batted balls,
    colored by result (single/double/triple/HR/out).
    """
    events = data['events']
    away, home = data['away'], data['home']
    venue_name = data.get('venue_name')
    field_info = data.get('field_info')
    game_label = data.get('game_label', '')
    color_by = data.get('color_by', 'team')
    player_name = data.get('player_name')
    is_pitcher = data.get('is_pitcher')
    game_date = data.get('game_date', '')
    scoreboard = data.get('scoreboard') or {}
    linescore = scoreboard.get('linescore') or {}
    ls_teams = linescore.get('teams') or {}
    away_runs = ls_teams.get('away', {}).get('runs')
    home_runs = ls_teams.get('home', {}).get('runs')

    score_str = None
    if away_runs is not None and home_runs is not None:
        score_str = f"{away} {away_runs} – {home} {home_runs}"

    state_str = game_label
    if game_label and 'progress' in game_label.lower():
        ordinal = linescore.get('currentInningOrdinal')
        half = linescore.get('inningState') or linescore.get('inningHalf')
        if ordinal:
            state_str = f"{half + ' ' if half else ''}{ordinal}"

    away_color = _readable(_team_colors(away)[0])
    home_color = _readable(_team_colors(home)[0])
    # Keep the two teams visually distinct if both primaries lightened toward the same pale shade.
    if _luminance(away_color) > 100 and _luminance(home_color) > 100 and \
            sum(abs(a - h) for a, h in zip(away_color, home_color)) < 60:
        home_color = _readable(_team_colors(home)[1] or home_color, floor=110)

    W = 1200
    side_margin = 50
    field_top = 150
    bottom_reserve = 170
    max_plot_height = 850

    event_points_ft = []
    for e in events:
        try:
            event_points_ft.append((float(e['hc_x_ft']), float(e['hc_y_ft'])))
        except (KeyError, TypeError, ValueError):
            continue

    layout = _spray_field_layout(W, field_top, side_margin, bottom_reserve, max_plot_height, field_info, event_points_ft)
    scale, plate_x, plate_y, H = layout['scale'], layout['plate_x'], layout['plate_y'], layout['H']

    bg = (18, 25, 33)

    # For a single hitter's own batted balls, add a numbered play-by-play panel on the right,
    # matching each dot on the field to its description and Statcast metrics.
    show_play_list = color_by == 'outcome' and is_pitcher is False and bool(player_name)
    panel_w = 460 if show_play_list else 0
    play_entries = []
    if show_play_list:
        font_panel_hdr  = _spray_field_font(20, bold=True)
        font_panel_desc = _spray_field_font(18)
        font_panel_stat = _spray_field_font(16)
        panel_text_w = panel_w - 60
        panel_top = 110
        entry_gap = 18

        measure_draw = ImageDraw.Draw(Image.new('RGB', (10, 10)))
        y_cursor = panel_top
        for i, e in enumerate(events, 1):
            inning = e.get('inning')
            half = (e.get('half_inning') or '').lower()
            inning_label = f"{'Top' if half == 'top' else 'Bot'} {_spray_ordinal(inning)}" if inning else ""
            pitcher_name = e.get('pitcher_name') or ''
            header = f"{i}.  {inning_label}" + (f" vs {pitcher_name}" if pitcher_name else "")
            des = e.get('des') or e.get('result') or ''
            desc_lines = _spray_wrap_text(measure_draw, des, font_panel_desc, panel_text_w)

            stat_parts = []
            ev, la, dist, xba = e.get('hit_speed'), e.get('hit_angle'), e.get('hit_distance'), e.get('xba')
            if ev not in (None, ''):
                stat_parts.append(f"{ev} mph")
            if la not in (None, ''):
                stat_parts.append(f"{la}°")
            if dist not in (None, ''):
                stat_parts.append(f"{dist} ft")
            if xba not in (None, ''):
                stat_parts.append(f"xBA {xba}")
            stat_line = "  ·  ".join(stat_parts)

            entry_height = 26 + len(desc_lines) * 22 + (22 if stat_line else 0) + entry_gap
            play_entries.append({
                'num': i, 'header': header, 'desc_lines': desc_lines, 'stat_line': stat_line,
            })
            y_cursor += entry_height

        H = max(H, y_cursor + 40)

    img = Image.new('RGB', (W + panel_w, H), color=bg)
    draw = ImageDraw.Draw(img)

    font_title    = _spray_field_font(40, bold=True)
    font_sub      = _spray_field_font(26)
    font_legend   = _spray_field_font(22)
    font_distance = _spray_field_font(19, bold=True)

    if player_name:
        role = "Batted Balls Allowed" if is_pitcher else "Batted Balls"
        title = f"{player_name}  ·  {role}"
    else:
        title = f"{away} @ {home}  ·  Spray Chart"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, 20), title, fill=(230, 230, 230), font=font_title)

    subtitle_parts = []
    if player_name:
        subtitle_parts.append(f"{away} @ {home}")
    if score_str:
        subtitle_parts.append(score_str)
    if state_str:
        subtitle_parts.append(state_str)
    if game_date:
        subtitle_parts.append(game_date)
    subtitle_parts.append(f"{len(events)} batted ball{'s' if len(events) != 1 else ''}")
    if venue_name:
        subtitle_parts.append(venue_name)
    subtitle = "  ·  ".join(subtitle_parts)
    max_sub_width = W - 80
    bbox = draw.textbbox((0, 0), subtitle, font=font_sub)
    if bbox[2] - bbox[0] > max_sub_width and venue_name and len(subtitle_parts) > 1:
        # Drop the venue first if the line is too wide for the canvas.
        subtitle = "  ·  ".join(subtitle_parts[:-1])
        bbox = draw.textbbox((0, 0), subtitle, font=font_sub)
    while bbox[2] - bbox[0] > max_sub_width and font_sub.size > 16:
        font_sub = _spray_field_font(font_sub.size - 2)
        bbox = draw.textbbox((0, 0), subtitle, font=font_sub)
    draw.text(((W - (bbox[2] - bbox[0])) // 2, 68), subtitle, fill=(150, 150, 150), font=font_sub)

    def to_canvas(hc_x_ft, hc_y_ft):
        return plate_x + hc_x_ft * scale, plate_y - hc_y_ft * scale

    _draw_spray_field(img, draw, layout, font_distance)

    font_badge = _spray_field_font(15, bold=True) if show_play_list else None

    # Plot each batted ball — filled = hit, hollow = out/other, larger ring = home run.
    for i, e in enumerate(events, 1):
        try:
            x_ft = float(e['hc_x_ft'])
            y_ft = float(e['hc_y_ft'])
        except (KeyError, TypeError, ValueError):
            continue
        cx, cy = to_canvas(x_ft, y_ft)
        outcome = (e.get('events') or '').lower().replace(' ', '_')
        is_hr = outcome == 'home_run'
        is_hit = outcome in _GAME_SPRAY_HIT_EVENTS

        if color_by == 'outcome':
            color = _GAME_SPRAY_OUTCOME_COLORS.get(outcome, _GAME_SPRAY_OUT_COLOR)
            if is_hr:
                draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=color, outline=(255, 255, 255), width=2)
            elif is_hit:
                draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=color, outline=(18, 25, 33), width=1)
            else:
                draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=_GAME_SPRAY_OUT_COLOR, width=2)
        else:
            team_batting = e.get('team_batting') or ''
            color = home_color if team_batting == home else away_color
            if is_hr:
                draw.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], fill=color, outline=(255, 215, 0), width=2)
            elif is_hit:
                draw.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=color, outline=(18, 25, 33), width=1)
            else:
                draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=color, width=2)

        if show_play_list:
            bx, by = cx + 13, cy - 13
            num_str = str(i)
            bbox = draw.textbbox((0, 0), num_str, font=font_badge)
            r = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2 + 4
            draw.ellipse([bx - r, by - r, bx + r, by + r], fill=(18, 25, 33), outline=(230, 230, 230), width=1)
            draw.text((bx - (bbox[2] - bbox[0]) / 2, by - (bbox[3] - bbox[1]) / 2 - bbox[1]), num_str,
                       fill=(230, 230, 230), font=font_badge)

    if color_by == 'outcome':
        # Legend — one row, colored by batted-ball result.
        lx = 40
        ly = H - 40
        for label, color, kind in _GAME_SPRAY_OUTCOME_LEGEND:
            if kind == 'filled':
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=color)
            elif kind == 'hollow':
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], outline=color, width=2)
            else:
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=color, outline=(255, 255, 255), width=2)
            draw.text((lx + 20, ly - 10), label, fill=(200, 200, 200), font=font_legend)
            bbox = draw.textbbox((0, 0), label, font=font_legend)
            lx += 20 + (bbox[2] - bbox[0]) + 30
    else:
        # Legend — team colors, then marker meaning.
        lx = 40
        ly = H - 66
        for label, color in ((away, away_color), (home, home_color)):
            draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=color)
            draw.text((lx + 20, ly - 10), label, fill=(200, 200, 200), font=font_legend)
            bbox = draw.textbbox((0, 0), label, font=font_legend)
            lx += 20 + (bbox[2] - bbox[0]) + 40

        lx = 40
        ly = H - 34
        marker_items = [('Hit', 'filled'), ('Out/Other', 'hollow'), ('HR', 'gold_ring')]
        for label, kind in marker_items:
            neutral = (170, 178, 188)
            if kind == 'filled':
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=neutral)
            elif kind == 'hollow':
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], outline=neutral, width=2)
            else:
                draw.ellipse([lx, ly - 6, lx + 12, ly + 6], fill=neutral, outline=(255, 215, 0), width=2)
            draw.text((lx + 20, ly - 10), label, fill=(200, 200, 200), font=font_legend)
            bbox = draw.textbbox((0, 0), label, font=font_legend)
            lx += 20 + (bbox[2] - bbox[0]) + 30

    if show_play_list:
        panel_x = W + 30
        draw.line([(W, 0), (W, H)], fill=(45, 55, 65), width=1)
        y = panel_top
        for entry in play_entries:
            draw.text((panel_x, y), entry['header'], fill=(230, 230, 230), font=font_panel_hdr)
            y += 26
            for line in entry['desc_lines']:
                draw.text((panel_x, y), line, fill=(190, 190, 190), font=font_panel_desc)
                y += 22
            if entry['stat_line']:
                draw.text((panel_x, y), entry['stat_line'], fill=(140, 148, 158), font=font_panel_stat)
                y += 22
            y += entry_gap

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def generate_rolling_xwoba_chart(
    points: list,
    player_name: str,
    team_abbrev: str,
    window: int,
    lg_avg: float = 0.315,
) -> io.BytesIO:
    """Render a rolling xwOBA line chart styled after Baseball Savant.

    points: chronologically ordered list of {'date': str, 'xwoba': float}
    """
    W, H = 720, 360
    PAD_T  = 62
    PAD_B  = 32
    PAD_L  = 58
    PAD_R  = 72   # room for "LG AVG" label

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    # Y range — snap to nice bounds around the data
    raw_vals = [p['xwoba'] for p in points]
    lo = max(0.100, math.floor((min(raw_vals) - 0.05) * 10) / 10)
    hi = min(0.700, math.ceil( (max(raw_vals) + 0.05) * 10) / 10)
    # Always include lg_avg in range
    lo = min(lo, math.floor((lg_avg - 0.05) * 10) / 10)
    hi = max(hi, math.ceil( (lg_avg + 0.05) * 10) / 10)

    # ── Colors ──────────────────────────────────────────────────
    BG          = (255, 255, 255)
    PLAYER_COL  = (210, 35, 35)     # Savant red
    AVG_COL     = (80, 80, 80)
    GRID_COL    = (210, 210, 210)
    TEXT_COL    = (40, 40, 40)
    DIM_COL     = (130, 130, 130)
    CYAN_DOT    = (80, 190, 200)    # decorative dots under title

    # ── Fonts ───────────────────────────────────────────────────
    f_bold  = _dv("DejaVuSans-Bold.ttf", 15)
    f_reg   = _dv("DejaVuSans.ttf",      15)
    f_axis  = _dv("DejaVuSans.ttf",      11)
    f_label = _dv("DejaVuSans.ttf",      10)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # ── Coordinate helpers ───────────────────────────────────────
    n = len(points)

    def xp(i):
        return PAD_L + (i / max(n - 1, 1)) * plot_w

    def yp(v):
        return PAD_T + plot_h - ((v - lo) / (hi - lo)) * plot_h

    # ── Title ────────────────────────────────────────────────────
    bold_part = f"{window} PAs"
    rest_part = " Rolling xwOBA"
    bb = draw.textbbox((0, 0), bold_part, font=f_bold)
    bold_w = bb[2] - bb[0]
    rb = draw.textbbox((0, 0), rest_part, font=f_reg)
    rest_w = rb[2] - rb[0]
    tx = (W - bold_w - rest_w) // 2
    ty = 12
    draw.text((tx,            ty), bold_part, font=f_bold, fill=TEXT_COL)
    draw.text((tx + bold_w,   ty), rest_part, font=f_reg,  fill=TEXT_COL)

    # Decorative cyan dots under title
    dot_y  = ty + 24
    dot_r  = 3
    dot_gap = 10
    n_dots = 9
    dot_start = W // 2 - (n_dots * dot_gap) // 2
    for di in range(n_dots):
        cx = dot_start + di * dot_gap
        draw.ellipse([cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r], fill=CYAN_DOT)

    # ── Grid lines ───────────────────────────────────────────────
    grid_vals = [v / 100 for v in range(int(lo * 100), int(hi * 100) + 1, 10)]
    for gv in grid_vals:
        y = yp(gv)
        # Dashed line
        x = PAD_L
        while x < PAD_L + plot_w:
            draw.line([(x, y), (min(x + 6, PAD_L + plot_w), y)], fill=GRID_COL, width=1)
            x += 11
        # Y-axis label
        draw.text((PAD_L - 6, y), f"{gv:.3f}", font=f_axis, fill=DIM_COL, anchor="rm")

    # ── League average dashed line ────────────────────────────────
    ly = yp(lg_avg)
    x = PAD_L
    while x < PAD_L + plot_w:
        draw.line([(x, ly), (min(x + 10, PAD_L + plot_w), ly)], fill=AVG_COL, width=1)
        x += 16
    draw.text((PAD_L + plot_w + 6, ly), "LG AVG", font=f_label, fill=AVG_COL, anchor="lm")

    # ── Player line ───────────────────────────────────────────────
    if n > 1:
        line_pts = [(xp(i), yp(p['xwoba'])) for i, p in enumerate(points)]
        draw.line(line_pts, fill=PLAYER_COL, width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_index_chart(series: list, tz_offset_secs: int = 0) -> io.BytesIO:
    """Overlay several intraday series normalized to % change from prev close.

    series: list of {"label": str, "color": (r,g,b), "points": [(ts, pct)], "last": pct}
    All series share a single % y-axis and a 0% baseline.
    """
    from datetime import datetime, timezone, timedelta

    S = 2  # supersample, then downscale for anti-aliasing
    W, H = 760 * S, 320 * S
    PAD_T, PAD_B, PAD_L, PAD_R = 16 * S, 26 * S, 14 * S, 52 * S
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    BG       = (30, 31, 34)
    GRID_COL = (55, 57, 62)
    DIM_COL  = (160, 160, 165)
    ZERO_COL = (150, 150, 150)

    f_axis = _dv("DejaVuSans.ttf", 11 * S)
    f_lbl  = _dv("DejaVuSans-Bold.ttf", 12 * S)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Time span across all series; y span symmetric around 0
    all_ts  = [ts for s in series for ts, _ in s["points"]]
    all_pct = [p for s in series for _, p in s["points"]]
    t0, t1 = min(all_ts), max(all_ts)
    t_span = max(t1 - t0, 1)
    maxabs = max((abs(p) for p in all_pct), default=0.5) or 0.5
    hi = maxabs * 1.18
    lo = -hi

    def xp(ts):
        return PAD_L + (ts - t0) / t_span * plot_w

    def yp(v):
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    # Horizontal grid + % labels at ~nice steps
    step = (hi - lo) / 4
    mag = 10 ** math.floor(math.log10(step))
    for nice in (1, 2, 2.5, 5, 10):
        if mag * nice >= step:
            step = mag * nice
            break
    gv = math.ceil(lo / step) * step
    while gv <= hi:
        y = yp(gv)
        draw.line([(PAD_L, y), (PAD_L + plot_w, y)], fill=GRID_COL, width=S)
        draw.text((PAD_L + plot_w + 6 * S, y), f"{gv:+.1f}%", font=f_axis, fill=DIM_COL, anchor="lm")
        gv += step

    # X-axis hour labels in exchange-local time
    tz = timezone(timedelta(seconds=tz_offset_secs))
    hour_dt = datetime.fromtimestamp(t0, tz=tz).replace(minute=0, second=0, microsecond=0)
    if hour_dt.timestamp() < t0:
        hour_dt += timedelta(hours=1)
    while hour_dt.timestamp() <= t1:
        x = xp(hour_dt.timestamp())
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID_COL, width=S)
        label = hour_dt.strftime("%I%p").lstrip("0").lower()
        draw.text((x, PAD_T + plot_h + 6 * S), label, font=f_axis, fill=DIM_COL, anchor="ma")
        hour_dt += timedelta(hours=1)

    # 0% baseline (dashed)
    y0 = yp(0)
    x = PAD_L
    while x < PAD_L + plot_w:
        draw.line([(x, y0), (min(x + 6 * S, PAD_L + plot_w), y0)], fill=ZERO_COL, width=S)
        x += 11 * S

    # Each series line
    for s in series:
        pts = [(xp(ts), yp(p)) for ts, p in s["points"]]
        if len(pts) > 1:
            draw.line(pts, fill=_readable(s["color"]), width=2 * S, joint="curve")

    # Legend (top-left): colored swatch + label + last % change
    ly = PAD_T + 6 * S
    for s in series:
        col = _readable(s["color"])
        draw.rectangle([PAD_L + 6 * S, ly + 3 * S, PAD_L + 18 * S, ly + 13 * S], fill=col)
        draw.text((PAD_L + 24 * S, ly), f"{s['label']}  {s['last']:+.2f}%", font=f_lbl, fill=col)
        ly += 18 * S

    img = img.resize((W // S, H // S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_market_chart(series: list, tz_offset_secs: int = 0, range_label: str = "") -> io.BytesIO:
    """Overlay several 0-100% probability series (e.g. prediction-market outcomes)
    against a real (non-bucketed) time axis spanning days to months.

    series: list of {"label": str, "color": (r,g,b), "points": [(ts, pct 0-100)], "last": pct}
    Unlike generate_index_chart (intraday, symmetric around 0%), this scales
    the y-axis to the data's own 0-100 range and picks x-axis tick spacing
    from the overall time span, so it works for multi-day/week/month history.
    """
    from datetime import datetime, timezone, timedelta

    S = 2
    W, H = 760 * S, 320 * S
    PAD_T, PAD_B, PAD_L, PAD_R = 16 * S, 26 * S, 14 * S, 52 * S
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    BG       = (30, 31, 34)
    GRID_COL = (55, 57, 62)
    DIM_COL  = (160, 160, 165)

    f_axis = _dv("DejaVuSans.ttf", 11 * S)
    f_lbl  = _dv("DejaVuSans-Bold.ttf", 12 * S)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    all_ts = [ts for s in series for ts, _ in s["points"]]
    all_v  = [v for s in series for _, v in s["points"]]
    t0, t1 = min(all_ts), max(all_ts)
    t_span = max(t1 - t0, 1)

    lo = max(0.0, min(all_v) - 5)
    hi = min(100.0, max(all_v) + 5)
    if hi - lo < 10:
        mid = (hi + lo) / 2
        lo, hi = max(0.0, mid - 5), min(100.0, mid + 5)

    def xp(ts):
        return PAD_L + (ts - t0) / t_span * plot_w

    def yp(v):
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    # Horizontal grid + % labels at ~4 nice steps
    step = (hi - lo) / 4
    mag = 10 ** math.floor(math.log10(step)) if step > 0 else 1
    for nice in (1, 2, 2.5, 5, 10):
        if mag * nice >= step:
            step = mag * nice
            break
    gv = math.ceil(lo / step) * step
    while gv <= hi:
        y = yp(gv)
        draw.line([(PAD_L, y), (PAD_L + plot_w, y)], fill=GRID_COL, width=S)
        draw.text((PAD_L + plot_w + 6 * S, y), f"{gv:.0f}%", font=f_axis, fill=DIM_COL, anchor="lm")
        gv += step

    # X-axis ticks — spacing adapts to the overall time span
    tz = timezone(timedelta(seconds=tz_offset_secs))
    span_days = t_span / 86400
    if span_days <= 1.5:
        unit, fmt = timedelta(hours=3), "%I%p"
    elif span_days <= 14:
        unit, fmt = timedelta(days=1), "%-m/%-d"
    elif span_days <= 90:
        unit, fmt = timedelta(days=7), "%-m/%-d"
    else:
        unit, fmt = timedelta(days=30), "%b"

    cur = datetime.fromtimestamp(t0, tz=tz)
    end = datetime.fromtimestamp(t1, tz=tz)
    while cur <= end:
        x = xp(cur.timestamp())
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID_COL, width=S)
        draw.text((x, PAD_T + plot_h + 6 * S), cur.strftime(fmt).lstrip("0").lower(),
                   font=f_axis, fill=DIM_COL, anchor="ma")
        cur += unit

    # Each series line
    for s in series:
        pts = [(xp(ts), yp(v)) for ts, v in s["points"]]
        if len(pts) > 1:
            draw.line(pts, fill=_readable(s["color"]), width=2 * S, joint="curve")

    # Legend (top-left): colored swatch + label + current value
    ly = PAD_T + 6 * S
    for s in series:
        col = _readable(s["color"])
        draw.rectangle([PAD_L + 6 * S, ly + 3 * S, PAD_L + 18 * S, ly + 13 * S], fill=col)
        draw.text((PAD_L + 24 * S, ly), f"{s['label']}  {s['last']:.0f}%", font=f_lbl, fill=col)
        ly += 18 * S

    if range_label:
        draw.text((PAD_L + plot_w - 6 * S, PAD_T + 4 * S), range_label,
                   font=f_axis, fill=DIM_COL, anchor="ra")

    img = img.resize((W // S, H // S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _price_axis_ticks(points: list, tz, range_key: str, max_ticks: int = 9) -> list:
    """Pick x-axis tick (index, label) pairs from price points for the given range.

    Charts plot against point index (not real time) so overnight/weekend gaps
    don't stretch the line; ticks mark where the calendar bucket changes.
    """
    from datetime import datetime

    def bucket(dt):
        if range_key == "1H":
            return (dt.year, dt.month, dt.day, dt.hour, dt.minute // 15)
        if range_key == "1D":
            return (dt.year, dt.month, dt.day, dt.hour)
        if range_key in ("5D", "30D"):
            return (dt.year, dt.month, dt.day)
        if range_key in ("6M", "1Y"):
            return (dt.year, dt.month)
        return (dt.year,)  # 5Y

    def label(dt):
        if range_key == "1H":
            return dt.strftime("%I:%M%p").lstrip("0").lower()
        if range_key == "1D":
            return dt.strftime("%I%p").lstrip("0").lower()
        if range_key in ("5D", "30D"):
            return dt.strftime("%-m/%-d")
        if range_key in ("6M", "1Y"):
            return dt.strftime("%b")
        return dt.strftime("%Y")

    ticks, prev = [], None
    for i, (ts, _) in enumerate(points):
        dt = datetime.fromtimestamp(ts, tz=tz)
        b = bucket(dt)
        if b != prev:
            ticks.append((i, label(dt)))
            prev = b
    if len(ticks) > max_ticks:
        stepn = math.ceil(len(ticks) / max_ticks)
        ticks = ticks[::stepn]
    return ticks


def generate_price_chart(
    points: list,
    baseline: float,
    tz_offset_secs: int = 0,
    range_key: str = "1D",
    range_label: str = "1D",
    symbol: str = "",
) -> io.BytesIO:
    """Render a price line chart vs. a baseline, with a price/high/low table.

    points:    chronologically ordered list of (unix_ts, price)
    baseline:  reference value (prev close for 1D, range-start price otherwise)
    range_key: one of 1H/1D/5D/30D/6M/1Y/5Y — controls x-axis tick granularity
    symbol:    ticker shown in the table header so the image stands alone
    """
    from datetime import timezone, timedelta

    W, H = 720, 300
    PAD_T = 16
    PAD_B = 28
    PAD_L = 14
    PAD_R = 64   # room for y-axis price labels

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    prices = [p for _, p in points]
    up = prices[-1] >= baseline

    BG       = (30, 31, 34)        # Discord dark theme background
    LINE_COL = (35, 197, 94) if up else (239, 68, 68)
    FILL_COL = (35, 197, 94, 40) if up else (239, 68, 68, 40)
    PREV_COL = (150, 150, 150)
    GRID_COL = (55, 57, 62)
    DIM_COL  = (160, 160, 165)
    TXT_COL  = (225, 225, 228)

    f_axis = _dv("DejaVuSans.ttf", 11)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Y range — include baseline, pad 5%
    lo = min(min(prices), baseline)
    hi = max(max(prices), baseline)
    span = (hi - lo) or max(hi * 0.01, 0.01)
    lo -= span * 0.05
    hi += span * 0.05

    n = len(points)

    def xp(i):
        return PAD_L + (i / (n - 1) * plot_w if n > 1 else 0)

    def yp(v):
        return PAD_T + plot_h - (v - lo) / (hi - lo) * plot_h

    # Horizontal grid + price labels at ~5 nice levels
    step = span / 4
    mag = 10 ** math.floor(math.log10(step))
    for nice in (1, 2, 2.5, 5, 10):
        if mag * nice >= step:
            step = mag * nice
            break
    gv = math.ceil(lo / step) * step
    while gv <= hi:
        y = yp(gv)
        draw.line([(PAD_L, y), (PAD_L + plot_w, y)], fill=GRID_COL, width=1)
        draw.text((PAD_L + plot_w + 6, y), f"{gv:,.2f}", font=f_axis, fill=DIM_COL, anchor="lm")
        gv += step

    # X-axis labels in exchange-local time, by calendar-bucket change
    tz = timezone(timedelta(seconds=tz_offset_secs))
    for i, lbl in _price_axis_ticks(points, tz, range_key):
        x = xp(i)
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID_COL, width=1)
        draw.text((x, PAD_T + plot_h + 6), lbl, font=f_axis, fill=DIM_COL, anchor="ma")

    # Baseline dashed reference line
    py = yp(baseline)
    x = PAD_L
    while x < PAD_L + plot_w:
        draw.line([(x, py), (min(x + 6, PAD_L + plot_w), py)], fill=PREV_COL, width=1)
        x += 11

    # Filled area between line and baseline, then the price line on top
    line_pts = [(xp(i), yp(p)) for i, p in enumerate(prices)]
    if len(line_pts) > 1:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.polygon(line_pts + [(line_pts[-1][0], py), (line_pts[0][0], py)], fill=FILL_COL)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw.line(line_pts, fill=LINE_COL, width=2)
        lx, ly = line_pts[-1]
        draw.ellipse([lx - 3, ly - 3, lx + 3, ly + 3], fill=LINE_COL)

    # Stats table: ticker + range label, then current price / high / low.
    f_lbl = _dv("DejaVuSans.ttf", 12)
    f_hdr = _dv("DejaVuSans-Bold.ttf", 12)
    header = f"{symbol} · {range_label}" if symbol else range_label
    rows = [("Price", prices[-1]), ("High", max(prices)), ("Low", min(prices))]
    lbl_w = max(draw.textlength(r[0], font=f_lbl) for r in rows)
    val_w = max(draw.textlength(f"{r[1]:,.2f}", font=f_lbl) for r in rows)
    gap, padx, pady, lh = 14, 8, 6, 16
    box_w = max(padx * 2 + lbl_w + gap + val_w, padx * 2 + draw.textlength(header, font=f_hdr))
    box_h = pady * 2 + lh * (len(rows) + 1)

    # Place the box in whichever corner overlaps the price line the least.
    m = 6
    corners = [
        (PAD_L + m, PAD_T + m),                              # top-left
        (PAD_L + plot_w - box_w - m, PAD_T + m),             # top-right
        (PAD_L + m, PAD_T + plot_h - box_h - m),             # bottom-left
        (PAD_L + plot_w - box_w - m, PAD_T + plot_h - box_h - m),  # bottom-right
    ]

    def overlap(bx, by):
        return sum(1 for px, py_ in line_pts
                   if bx <= px <= bx + box_w and by <= py_ <= by + box_h)

    bx, by = min(corners, key=lambda c: overlap(*c))
    bx, by = int(bx), int(by)

    tbl = Image.new("RGBA", (int(box_w), int(box_h)), (20, 21, 24, 205))
    img.paste(Image.alpha_composite(
        img.crop((bx, by, bx + int(box_w), by + int(box_h))).convert("RGBA"), tbl
    ).convert("RGB"), (bx, by))
    draw.text((bx + padx, by + pady), header, font=f_hdr, fill=TXT_COL)
    for j, (lbl, val) in enumerate(rows):
        ty = by + pady + lh * (j + 1)
        draw.text((bx + padx, ty), lbl, font=f_lbl, fill=DIM_COL)
        draw.text((bx + box_w - padx, ty), f"{val:,.2f}", font=f_lbl, fill=TXT_COL, anchor="ra")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generate_winprob_chart(
    wp: list,
    inning_ticks: list,
    team_abbr: str,
    opp_abbr: str,
    header: str,
    team_color: Tuple[int, int, int],
    opp_color: Tuple[int, int, int],
    swing: Optional[Tuple[int, str]] = None,
) -> io.BytesIO:
    """Render a win-probability chart from the queried team's perspective.

    wp:            the team's win probability (0-100) in play order
    inning_ticks:  list of (play_index, label) marking inning boundaries
    swing:         optional (play_index, description) for the biggest WPA play
    """
    # Render at SxS supersampling, then downscale with LANCZOS so the diagonal
    # win-prob line, fills and text come out anti-aliased.
    S = 2
    W, H = 760 * S, 320 * S
    PAD_T, PAD_B, PAD_L, PAD_R = 16 * S, 26 * S, 14 * S, 44 * S

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    BG       = (30, 31, 34)
    GRID_COL = (55, 57, 62)
    DIM_COL  = (160, 160, 165)
    TXT_COL  = (228, 228, 232)
    LINE_COL = (240, 240, 245)

    f_axis = _dv("DejaVuSans.ttf", 11 * S)
    f_lbl  = _dv("DejaVuSans-Bold.ttf", 12 * S)
    f_note = _dv("DejaVuSans.ttf", 11 * S)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    n = max(len(wp), 1)
    wp = [max(0.0, min(100.0, v)) for v in wp]

    def xp(i):
        return PAD_L + (i / (n - 1) * plot_w if n > 1 else 0)

    def yp(v):
        return PAD_T + plot_h - (v / 100.0) * plot_h

    # Horizontal grid at 0/25/50/75/100. Labels are symmetric about the 50%
    # midline: each extreme means the team on that side has a 100% chance.
    for gv in (0, 25, 50, 75, 100):
        y = yp(gv)
        draw.line([(PAD_L, y), (PAD_L + plot_w, y)], fill=GRID_COL, width=S)
        lbl_val = gv if gv >= 50 else 100 - gv
        draw.text((PAD_L + plot_w + 6 * S, y), f"{lbl_val}", font=f_axis, fill=DIM_COL, anchor="lm")

    # Inning gridlines + labels
    for idx, lbl in inning_ticks:
        x = xp(idx)
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID_COL, width=S)
        draw.text((x, PAD_T + plot_h + 6 * S), lbl, font=f_axis, fill=DIM_COL, anchor="ma")

    # Two-tone fill to the 50% midline: team color when favored, opponent below.
    def _mix(c, bg, t):  # t = weight of color
        return tuple(int(round(c[k] * t + bg[k] * (1 - t))) for k in range(3))
    team_fill = _mix(team_color, BG, 0.55)
    opp_fill  = _mix(opp_color, BG, 0.45)
    y50 = yp(50)
    for px in range(plot_w + 1):
        frac = px / plot_w * (n - 1) if n > 1 else 0
        i0 = int(frac)
        i1 = min(i0 + 1, n - 1)
        v = wp[i0] + (wp[i1] - wp[i0]) * (frac - i0)
        x = PAD_L + px
        draw.line([(x, y50), (x, yp(v))], fill=team_fill if v >= 50 else opp_fill, width=1)

    # 50% midline (dashed) then the win-prob line
    x = PAD_L
    while x < PAD_L + plot_w:
        draw.line([(x, y50), (min(x + 6 * S, PAD_L + plot_w), y50)], fill=(120, 122, 128), width=S)
        x += 11 * S
    pts = [(xp(i), yp(v)) for i, v in enumerate(wp)]
    if len(pts) > 1:
        draw.line(pts, fill=LINE_COL, width=2 * S, joint="curve")
        lx, ly = pts[-1]
        draw.ellipse([lx - 3 * S, ly - 3 * S, lx + 3 * S, ly + 3 * S], fill=LINE_COL)

        # Label the end of the line with the current (leading team's) win prob
        last_v = wp[-1]
        fav_abbr = team_abbr if last_v >= 50 else opp_abbr
        fav_col = team_color if last_v >= 50 else opp_color
        disp = last_v if last_v >= 50 else 100 - last_v
        elabel = f"{fav_abbr} {disp:.0f}%"
        ew = draw.textlength(elabel, font=f_lbl)
        eh = 16 * S
        ex1 = lx - 7 * S
        ex0 = ex1 - ew - 9 * S
        ey = max(PAD_T + eh // 2, min(PAD_T + plot_h - eh // 2, ly))
        draw.rounded_rectangle(
            [ex0, ey - eh // 2, ex1, ey + eh // 2],
            radius=4 * S, fill=_mix(fav_col, BG, 0.85),
        )
        draw.text(((ex0 + ex1) / 2, ey), elabel, font=f_lbl, fill=TXT_COL, anchor="mm")

    # Biggest-swing marker + annotation
    if swing is not None:
        s_idx, s_desc = swing
        s_idx = max(0, min(s_idx, n - 1))
        sx, sy = xp(s_idx), yp(wp[s_idx])
        draw.ellipse([sx - 4 * S, sy - 4 * S, sx + 4 * S, sy + 4 * S], outline=(255, 214, 10), width=2 * S)
        note = s_desc if len(s_desc) <= 54 else s_desc[:51] + "..."
        tw = draw.textlength(note, font=f_note)
        nx = min(max(sx - tw / 2, PAD_L + 2 * S), PAD_L + plot_w - tw - 2 * S)
        # place above the dot, or below if near the top
        ny = sy - 18 * S if sy - 18 * S > PAD_T + 14 * S else sy + 8 * S
        draw.text((nx, ny), note, font=f_note, fill=(255, 214, 10))

    # Header + legend (top-left): team color = team favored, opp color below 50%
    draw.text((PAD_L + 6 * S, PAD_T + 4 * S), header, font=f_lbl, fill=TXT_COL)
    ly0 = PAD_T + 22 * S
    for k, (ab, col) in enumerate(((team_abbr, team_color), (opp_abbr, opp_color))):
        yy = ly0 + k * 16 * S
        draw.rectangle([PAD_L + 6 * S, yy + 2 * S, PAD_L + 16 * S, yy + 12 * S], fill=_mix(col, BG, 0.8))
        draw.text((PAD_L + 22 * S, yy), ab, font=f_axis, fill=DIM_COL)

    img = img.resize((W // S, H // S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
