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
    # canvas size
    width, height = 800, 600
    # The zone area will be on the left, legend on the right
    zone_area_width = 450
    
    img = Image.new('RGB', (width, height), color=(18, 25, 33)) # Dark background
    draw = ImageDraw.Draw(img)
    
    if not pitches:
        # Return an empty image with a message
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        return buffer

    # Determine strike zone dims (using first pitch as representative)
    sz_top = pitches[0].sz_top or 3.5
    sz_bot = pitches[0].sz_bot or 1.5
    
    # Coordinates mapping
    # MLB coordinates pX is usually [-2, 2] feet
    # pZ is usually [0, 5] feet
    
    # Horizontal: -2 to 2 centered at 0
    # Map pX to pixels in zone_area_width
    def get_x(px):
        # Translate -1.5 -> 1.5 feet to 50 -> 400 pixels
        # 0 is centerX
        center_x = zone_area_width // 2
        scale = 150 # pixels per foot
        return center_x + (px * scale)

    def get_y(pz):
        # Translate 0 -> 5 feet to 550 -> 50 pixels (y is inverted)
        base_y = 550
        scale = 120 # pixels per foot
        return base_y - (pz * scale)

    # Draw Plate (rough estimation)
    plate_y = 560
    plate_w = 0.708 * 150 # 8.5 inches in feet * scale
    draw.polygon([
        (get_x(0), plate_y + 20),
        (get_x(-0.708), plate_y),
        (get_x(-0.708), plate_y - 10),
        (get_x(0.708), plate_y - 10),
        (get_x(0.708), plate_y)
    ], fill=(200, 200, 200))

    # Draw 3x3 strike zone
    # Plate width is 17 inches = 1.416 feet. Left edge -0.708, Right edge 0.708.
    zx_left = get_x(-0.708)
    zx_right = get_x(0.708)
    zy_top = get_y(sz_top)
    zy_bot = get_y(sz_bot)
    
    # Outer box (semi-transparent gray/white)
    draw.rectangle([zx_left, zy_top, zx_right, zy_bot], outline=(150, 150, 150), width=3)
    
    # Internal lines for 3x3
    # Vertical
    v_step = (zx_right - zx_left) / 3
    draw.line([zx_left + v_step, zy_top, zx_left + v_step, zy_bot], fill=(100, 100, 100), width=1)
    draw.line([zx_left + 2*v_step, zy_top, zx_left + 2*v_step, zy_bot], fill=(100, 100, 100), width=1)
    # Horizontal
    h_step = (zy_bot - zy_top) / 3
    draw.line([zx_left, zy_top + h_step, zx_right, zy_top + h_step], fill=(100, 100, 100), width=1)
    draw.line([zx_left, zy_top + 2*h_step, zx_right, zy_top + 2*h_step], fill=(100, 100, 100), width=1)

    # Load fonts (fallback to default)
    try:
        font_large = ImageFont.truetype("arial.ttf", 20)
        font_small = ImageFont.truetype("arial.ttf", 16)
        font_bold = ImageFont.truetype("arialbd.ttf", 18)
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
        font_bold = ImageFont.load_default()

    # Plot pitches
    for i, p in enumerate(pitches):
        px, py = get_x(p.px), get_y(p.pz)
        color = get_color_for_desc(p.description)
        
        # Draw circle
        r = 15
        draw.ellipse([px-r, py-r, px+r, py+r], fill=color, outline=(255, 255, 255), width=1)
        # Draw number
        num_str = str(p.number)
        bbox = draw.textbbox((0, 0), num_str, font=font_small)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((px - tw/2, py - th/2 - 2), num_str, fill=(255, 255, 255), font=font_small)
        
        # Legend (on the right)
        lx = 480
        ly = 50 + (i * 65)
        
        # Pitch marker in legend
        draw.ellipse([lx, ly+10, lx+30, ly+40], fill=color, outline=(255, 255, 255), width=1)
        draw.text((lx + 10, ly + 15), num_str, fill=(255, 255, 255), font=font_small)
        
        # Result and Count
        draw.text((lx + 45, ly), f"{p.description}", fill=(255, 255, 255), font=font_bold)
        draw.text((width - 80, ly), f"{p.count}", fill=(200, 200, 200), font=font_small)
        
        # Speed and Type
        draw.text((lx + 45, ly + 25), f"{p.speed} mph {p.type}", fill=(180, 180, 180), font=font_small)

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer
