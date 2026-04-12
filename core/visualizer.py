import io
from PIL import Image, ImageDraw, ImageFont
from typing import List
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

def generate_pitch_plot(pitches) -> io.BytesIO:
    # canvas size - larger for higher resolution
    width, height = 1200, 900
    # The zone area will be on the left, legend on the right
    zone_area_width = 700
    
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
    
    # Scale constants
    # pX scale: pixels per foot
    x_scale = 220 
    # pZ scale: pixels per foot
    z_scale = 180
    
    def get_x(px):
        center_x = zone_area_width // 2
        return center_x + (px * x_scale)

    def get_y(pz):
        # Anchor at 800px from top (ground level roughly)
        base_y = 800
        return base_y - (pz * z_scale)

    # Draw ground line
    draw.line([50, 800, zone_area_width - 50, 800], fill=(50, 60, 70), width=4)

    # Draw Plate
    plate_y = 810
    plate_w_feet = 0.708 # Half width
    draw.polygon([
        (get_x(0), plate_y + 30),
        (get_x(-plate_w_feet), plate_y + 10),
        (get_x(-plate_w_feet), plate_y - 10),
        (get_x(plate_w_feet), plate_y - 10),
        (get_x(plate_w_feet), plate_y + 10)
    ], fill=(180, 180, 185))

    # Draw 3x3 strike zone
    zx_left = get_x(-0.708)
    zx_right = get_x(0.708)
    zy_top = get_y(sz_top)
    zy_bot = get_y(sz_bot)
    
    # Outer box - Thicker line
    draw.rectangle([zx_left, zy_top, zx_right, zy_bot], outline=(200, 200, 200), width=6)
    
    # Internal lines for 3x3 - Thicker
    v_step = (zx_right - zx_left) / 3
    draw.line([zx_left + v_step, zy_top, zx_left + v_step, zy_bot], fill=(120, 120, 120), width=2)
    draw.line([zx_left + 2*v_step, zy_top, zx_left + 2*v_step, zy_bot], fill=(120, 120, 120), width=2)
    h_step = (zy_bot - zy_top) / 3
    draw.line([zx_left, zy_top + h_step, zx_right, zy_top + h_step], fill=(120, 120, 120), width=2)
    draw.line([zx_left, zy_top + 2*h_step, zx_right, zy_top + 2*h_step], fill=(120, 120, 120), width=2)

    # Load fonts - Larger sizes
    try:
        # Try some common windows font paths, fallback to default
        font_path = "arial.ttf"
        font_bold_path = "arialbd.ttf"
        
        font_title = ImageFont.truetype(font_bold_path, 36)
        font_large = ImageFont.truetype(font_path, 28)
        font_small = ImageFont.truetype(font_path, 22)
        font_bold = ImageFont.truetype(font_bold_path, 26)
    except:
        font_title = font_large = font_small = font_bold = ImageFont.load_default()

    # Plot pitches
    for i, p in enumerate(pitches):
        px, py = get_x(p.px), get_y(p.pz)
        color = get_color_for_desc(p.description)
        
        # Draw circle - Larger radius
        r = 25
        draw.ellipse([px-r, py-r, px+r, py+r], fill=color, outline=(255, 255, 255), width=2)
        
        # Draw number
        num_str = str(p.number)
        bbox = draw.textbbox((0, 0), num_str, font=font_small)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((px - tw/2, py - th/2 - 4), num_str, fill=(255, 255, 255), font=font_small)
        
        # Legend (on the right)
        lx = 750
        ly = 80 + (i * 90)
        
        # In case too many pitches, start a second column
        if ly > height - 100:
            lx += 220
            ly = 80 + ((i - 8) * 90)
        
        # Pitch marker in legend
        draw.ellipse([lx, ly+10, lx+45, ly+55], fill=color, outline=(255, 255, 255), width=2)
        draw.text((lx + 15, ly + 18), num_str, fill=(255, 255, 255), font=font_small)
        
        # Result and Count
        draw.text((lx + 65, ly), f"{p.description}", fill=(255, 255, 255), font=font_bold)
        draw.text((width - 80, ly), f"{p.count}", fill=(200, 200, 200), font=font_small)
        
        # Speed and Type
        draw.text((lx + 65, ly + 35), f"{p.speed} mph {p.type}", fill=(180, 180, 180), font=font_small)

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer
