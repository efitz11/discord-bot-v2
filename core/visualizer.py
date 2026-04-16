import io
import colorsys
from PIL import Image, ImageDraw, ImageFont
from typing import List, Optional
import os

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
    
    # Scale constants - Balanced for elongation and coverage
    x_scale = 190 # pixels per foot
    z_scale = 240 # pixels per foot 
    
    def get_x(px):
        center_x = zone_area_width // 2
        return center_x + (px * x_scale)

    def get_y(pz):
        # Anchor at 1200px
        base_y = 1200 
        return base_y - (pz * z_scale)

    # Draw ground line
    draw.line([50, 1200, zone_area_width - 50, 1200], fill=(50, 60, 70), width=4)

    # Draw Plate
    plate_y = 1215
    plate_w_feet = 0.708 
    draw.polygon([
        (get_x(0), plate_y + 35),
        (get_x(-plate_w_feet), plate_y + 10),
        (get_x(-plate_w_feet), plate_y - 12),
        (get_x(plate_w_feet), plate_y - 12),
        (get_x(plate_w_feet), plate_y + 10)
    ], fill=(180, 180, 185))

    # Draw 3x3 strike zone
    zx_left = get_x(-0.708)
    zx_right = get_x(0.708)
    zy_top = get_y(sz_top)
    zy_bot = get_y(sz_bot)
    
    # Outer box - High contrast
    draw.rectangle([zx_left, zy_top, zx_right, zy_bot], outline=(200, 200, 200), width=9)
    
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
    if stand == "R":
        draw.text((get_x(-1.8), get_y(4.5)), "RHB", fill=bat_color, font=font_title)
    else:
        draw.text((get_x(1.2), get_y(4.5)), "LHB", fill=bat_color, font=font_title)






    # Plot pitches
    for i, p in enumerate(pitches):
        px, py = get_x(p.px), get_y(p.pz)
        color = get_color_for_desc(p.description)
        
        # Draw circle
        r = 35
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
