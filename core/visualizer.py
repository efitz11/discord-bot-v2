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


def generate_compare_percentiles_image(
    p1_label: str, p2_label: str,
    year_str: str, stat_type: str,
    sections: list,
) -> io.BytesIO:
    """Render a side-by-side percentile comparison chart styled after Baseball Savant."""

    # ── Layout ──────────────────────────────────────────────────
    W        = 760
    PAD      = 18
    VAL_W    = 34    # fixed-width column for the percentile number
    CTR_W    = 164   # center column for the stat label
    BAR_W    = (W - 2 * PAD - 2 * VAL_W - CTR_W) // 2   # ≈ 245 px per bar

    TITLE_H  = 52
    NAMES_H  = 38
    CAT_H    = 34
    ROW_H    = 31

    n_rows = sum(len(rows) for _, rows in sections)
    n_cats = len(sections)
    total_h = TITLE_H + NAMES_H + n_cats * CAT_H + n_rows * ROW_H + PAD

    # ── Colors ──────────────────────────────────────────────────
    BG       = (16, 18, 27)
    CAT_BG   = (26, 30, 48)
    ROW_ALT  = (20, 23, 35)
    TRACK    = (34, 38, 56)
    TEXT     = (224, 224, 235)
    DIM      = (110, 115, 140)
    P1_COL   = (100, 180, 255)   # blue  — left player
    P2_COL   = (255, 145, 85)    # orange — right player

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

    # ── Title ───────────────────────────────────────────────────
    draw.text((W // 2, y + TITLE_H // 2), f"{year_str} Percentile Comparison",
              font=f_title, fill=TEXT, anchor="mm")
    y += TITLE_H

    # ── Player name header ──────────────────────────────────────
    draw.text((xb1, y + NAMES_H // 2), p1_label, font=f_bold, fill=P1_COL, anchor="lm")
    draw.text((W // 2,  y + NAMES_H // 2), "vs",       font=f_reg,  fill=DIM,    anchor="mm")
    draw.text((xv2 + VAL_W - 2, y + NAMES_H // 2), p2_label, font=f_bold, fill=P2_COL, anchor="rm")
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
                                           radius=3, fill=P1_COL)
                else:
                    draw.rounded_rectangle([xb2, bar_top, xb2 + blen, bar_bottom],
                                           radius=3, fill=P2_COL)

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


def generate_intraday_chart(
    points: list,
    prev_close: float,
    tz_offset_secs: int = 0,
) -> io.BytesIO:
    """Render an intraday price line chart vs. previous close.

    points: chronologically ordered list of (unix_ts, price)
    """
    from datetime import datetime, timezone, timedelta

    W, H = 720, 300
    PAD_T = 16
    PAD_B = 28
    PAD_L = 14
    PAD_R = 64   # room for y-axis price labels

    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    prices = [p for _, p in points]
    up = prices[-1] >= prev_close

    BG       = (30, 31, 34)        # Discord dark theme background
    LINE_COL = (35, 197, 94) if up else (239, 68, 68)
    FILL_COL = (35, 197, 94, 40) if up else (239, 68, 68, 40)
    PREV_COL = (150, 150, 150)
    GRID_COL = (55, 57, 62)
    DIM_COL  = (160, 160, 165)

    f_axis = _dv("DejaVuSans.ttf", 11)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Y range — include prev_close, pad 5%
    lo = min(min(prices), prev_close)
    hi = max(max(prices), prev_close)
    span = (hi - lo) or max(hi * 0.01, 0.01)
    lo -= span * 0.05
    hi += span * 0.05

    t0, t1 = points[0][0], points[-1][0]
    t_span = max(t1 - t0, 1)

    def xp(ts):
        return PAD_L + (ts - t0) / t_span * plot_w

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

    # X-axis hour labels in exchange-local time
    tz = timezone(timedelta(seconds=tz_offset_secs))
    first_dt = datetime.fromtimestamp(t0, tz=tz)
    hour_dt = first_dt.replace(minute=0, second=0, microsecond=0)
    if hour_dt < first_dt:
        hour_dt += timedelta(hours=1)
    while hour_dt.timestamp() <= t1:
        x = xp(hour_dt.timestamp())
        draw.line([(x, PAD_T), (x, PAD_T + plot_h)], fill=GRID_COL, width=1)
        label = hour_dt.strftime("%I%p").lstrip("0").lower()
        draw.text((x, PAD_T + plot_h + 6), label, font=f_axis, fill=DIM_COL, anchor="ma")
        hour_dt += timedelta(hours=1)

    # Previous-close dashed baseline
    py = yp(prev_close)
    x = PAD_L
    while x < PAD_L + plot_w:
        draw.line([(x, py), (min(x + 6, PAD_L + plot_w), py)], fill=PREV_COL, width=1)
        x += 11

    # Filled area between line and baseline, then the price line on top
    line_pts = [(xp(ts), yp(p)) for ts, p in points]
    if len(line_pts) > 1:
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.polygon(line_pts + [(line_pts[-1][0], py), (line_pts[0][0], py)], fill=FILL_COL)
        img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"), (0, 0))
        draw.line(line_pts, fill=LINE_COL, width=2)
        lx, ly = line_pts[-1]
        draw.ellipse([lx - 3, ly - 3, lx + 3, ly + 3], fill=LINE_COL)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
