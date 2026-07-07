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


def _circle_headshot(img_bytes: bytes, size: int, ring: Tuple[int, int, int]) -> Optional[Image.Image]:
    """Crop a headshot to a circle with a team-colored ring sitting just outside it."""
    try:
        base = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception:
        return None
    gap = 6  # space between the photo edge and the ring
    inner = size - 2 * gap
    w, h = base.size
    s = min(w, h)
    base = base.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((inner, inner))
    mask = Image.new("L", (inner, inner), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, inner - 1, inner - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (gap, gap), mask)
    ImageDraw.Draw(out).ellipse([1, 1, size - 2, size - 2], outline=ring, width=3)
    return out


def generate_compare_percentiles_image(
    p1_label: str, p2_label: str,
    year_str: str, stat_type: str,
    sections: list,
    p1_team: Optional[str] = None, p2_team: Optional[str] = None,
    p1_headshot: Optional[bytes] = None, p2_headshot: Optional[bytes] = None,
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


def generate_compare_stats_image(
    p1_label: str, p2_label: str,
    year_str: str, stat_type: str,
    rows: list,  # (label, v1, v2, direction) — direction: 1 higher-better, -1 lower-better, 0 neutral
    p1_team: Optional[str] = None, p2_team: Optional[str] = None,
    p1_headshot: Optional[bytes] = None, p2_headshot: Optional[bytes] = None,
) -> io.BytesIO:
    """Render a dark-mode side-by-side stat comparison table, highlighting whichever
    player has the better value in each row."""

    PAD     = 18
    VAL_W   = 92
    CTR_W   = 120
    W       = 2 * PAD + 2 * VAL_W + CTR_W   # keeps the table centered — no leftover margin

    TITLE_H = 46
    HEAD    = 72
    HEAD_H  = HEAD + 6
    NAMES_H = 34
    ROW_H   = 30

    total_h = TITLE_H + HEAD_H + NAMES_H + len(rows) * ROW_H + PAD

    BG      = (16, 18, 27)
    ROW_ALT = (20, 23, 35)
    TEXT    = (224, 224, 235)
    DIM     = (110, 115, 140)
    LABEL_C = (180, 185, 215)

    p1_primary, p1_secondary = _team_colors(p1_team)
    p2_primary, p2_secondary = _team_colors(p2_team)
    P1_NAME = _readable(p1_primary)
    P2_NAME = _readable(p2_primary)
    # Lightened so dark navy/black team colors (e.g. NYY, PIT) still show up as a
    # visible highlight against the dark background instead of blending into it.
    P1_HILITE = _readable(p1_primary, floor=90)
    P2_HILITE = _readable(p2_primary, floor=90)

    f_title = _dv("DejaVuSans-Bold.ttf", 17)
    f_bold  = _dv("DejaVuSans-Bold.ttf", 13)
    f_reg   = _dv("DejaVuSans.ttf",      13)
    f_val   = _dv("DejaVuSans-Bold.ttf", 14)

    img  = Image.new("RGB", (W, total_h), BG)
    draw = ImageDraw.Draw(img)

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
        hs = _circle_headshot(p1_headshot, HEAD, p1_secondary)
        if hs:
            img.paste(hs, (c1 - HEAD // 2, y), hs)
    if p2_headshot:
        hs = _circle_headshot(p2_headshot, HEAD, p2_secondary)
        if hs:
            img.paste(hs, (c2 - HEAD // 2, y), hs)
    draw.text((W // 2, y + HEAD_H // 2), "vs", font=f_reg, fill=DIM, anchor="mm")
    y += HEAD_H

    draw.text((c1, y + NAMES_H // 2), p1_label, font=f_bold, fill=P1_NAME, anchor="mm")
    draw.text((c2, y + NAMES_H // 2), p2_label, font=f_bold, fill=P2_NAME, anchor="mm")
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

        draw.text((xctr - 8, y + ROW_H // 2), str(v1) if v1 is not None else "—",
                  font=f_val, fill=TEXT, anchor="rm")
        draw.text((xb2 + 8, y + ROW_H // 2), str(v2) if v2 is not None else "—",
                  font=f_val, fill=TEXT, anchor="lm")
        draw.text(((xctr + xb2) // 2, y + ROW_H // 2), label,
                  font=f_reg, fill=LABEL_C, anchor="mm")

        y += ROW_H

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

    # Draw ground line
    ground_y = get_y(0)
    draw.line([50, ground_y, zone_area_width - 50, ground_y], fill=(50, 60, 70), width=4)

    # Draw Plate
    plate_y = ground_y + 15
    plate_w_feet = 0.708 
    draw.polygon([
        (get_x(0), plate_y + 35),
        (get_x(-plate_w_feet), plate_y + 10),
        (get_x(-plate_w_feet), plate_y - 12),
        (get_x(plate_w_feet), plate_y - 12),
        (get_x(plate_w_feet), plate_y + 10)
    ], fill=(180, 180, 185))

    # Draw batter's box inner edges: 6 inches outside each edge of home plate
    box_color = (65, 80, 95)
    bx_left = get_x(-(plate_w_feet + 0.5))
    bx_right = get_x(plate_w_feet + 0.5)
    by_top = get_y(0)   # ground line / front of plate
    by_bot = height
    draw.line([bx_left, by_top, bx_left, by_bot], fill=box_color, width=4)
    draw.line([bx_right, by_top, bx_right, by_bot], fill=box_color, width=4)

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
        
        # Legend (on the right)
        lx = 880

        ly = 100 + (i * 140)
        
        # In case too many pitches, start a second column
        if ly > height - 150:
            lx += 320
            ly = 100 + ((i - 8) * 140)
        
        # Pitch marker in legend - perfect circle
        rl = 35
        lcx, lcy = lx + rl, ly + 45
        draw.ellipse([lcx-rl, lcy-rl, lcx+rl, lcy+rl], fill=color, outline=(255, 255, 255), width=4)
        # Center number in legend circle
        bbox_l = draw.textbbox((0, 0), num_str, font=font_bold)
        twl, thl = bbox_l[2] - bbox_l[0], bbox_l[3] - bbox_l[1]
        draw.text((lcx - twl/2, lcy - thl/2 - 5), num_str, fill=(255, 255, 255), font=font_bold)
        
        # Result and Count
        draw.text((lx + 100, ly), f"{p.description}", fill=(255, 255, 255), font=font_bold)
        draw.text((width - 100, ly), f"{p.count}", fill=(200, 200, 200), font=font_small)
        
        # Speed and Type
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
