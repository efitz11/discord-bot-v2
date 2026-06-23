import io
import math
import asyncio
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from core.visualizer import generate_price_chart, generate_index_chart
from core.utils import ET_ZONE


MAP_ZOOM = 8     # base map zoom — each tile ~155km
RADAR_ZOOM = 7   # max zoom level RainViewer radar supports
TILE_SIZE = 256
SCALE = 2 ** (MAP_ZOOM - RADAR_ZOOM)  # 4: one radar tile = 4×4 base-map tiles
OUTPUT_SIZE = 768  # final image size
GRID = 5  # fetch 5×5 tiles then crop to center on the exact query point


def _lat_lon_to_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Global pixel position of (lat, lon) at the given zoom level."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    gx = (lon + 180.0) / 360.0 * n * TILE_SIZE
    gy = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return gx, gy


STOCK_INDEXES = [
    ("^DJI", "Dow Jones"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "Nasdaq"),
]

# (symbol, legend label, line color) for the overlaid intraday index chart
STOCK_INDEX_CHART = [
    ("^DJI", "Dow", (94, 151, 246)),
    ("^GSPC", "S&P 500", (75, 201, 122)),
    ("^IXIC", "Nasdaq", (240, 170, 76)),
]


def _abbrev_number(n) -> str:
    """1234567890 -> '1.2B'"""
    if n is None:
        return "-"
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}{suffix}"
    return f"{n:,.0f}"


def _desc_to_icon(d: str) -> str:
    d = d.lower()
    if "thunder" in d:
        return "⛈️"
    elif "snow" in d or "blizzard" in d:
        return "❄️"
    elif "rain" in d or "drizzle" in d or "shower" in d:
        return "🌧️"
    elif "overcast" in d or "cloudy" in d:
        return "☁️"
    elif "partly" in d or "mist" in d or "fog" in d:
        return "⛅"
    elif "sunny" in d or "clear" in d:
        return "☀️"
    return "🌡️"


class ExtendedSlash(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def _yahoo_crumb(self, session, force: bool = False) -> tuple[str, str] | None:
        """Yahoo quote API needs a session cookie + crumb token; cache them."""
        if not force and getattr(self, "_crumb_cache", None):
            return self._crumb_cache
        try:
            async with session.get("https://fc.yahoo.com", headers={"User-Agent": "Mozilla/5.0"}) as resp:
                cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
            if not cookie:
                return None
            async with session.get(
                "https://query1.finance.yahoo.com/v1/test/getcrumb",
                headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie},
            ) as resp:
                crumb = (await resp.text()).strip()
            if not crumb or "<" in crumb:
                return None
        except Exception:
            return None
        self._crumb_cache = (cookie, crumb)
        return self._crumb_cache

    async def _fetch_quotes(self, session, symbols: list[str]) -> list[dict]:
        """Fetch quotes from Yahoo Finance's v7 quote endpoint (free, no API key)."""
        for attempt in range(2):
            auth = await self._yahoo_crumb(session, force=attempt > 0)
            if not auth:
                return []
            cookie, crumb = auth
            url = (
                "https://query1.finance.yahoo.com/v7/finance/quote"
                f"?symbols={urllib.parse.quote(','.join(symbols))}&crumb={urllib.parse.quote(crumb)}"
            )
            try:
                async with session.get(url, headers={"User-Agent": "Mozilla/5.0", "Cookie": cookie}) as resp:
                    if resp.status in (401, 403):  # stale crumb — refresh and retry once
                        continue
                    if resp.status != 200:
                        return []
                    data = await resp.json(content_type=None)
                return data.get("quoteResponse", {}).get("result") or []
            except Exception:
                return []
        return []

    # range_key -> (Yahoo interval, Yahoo range)
    CHART_RANGES = {
        "1H":  ("1m",  "1d"),   # fetched as 1d/1m, then sliced to the last hour
        "1D":  ("5m",  "1d"),
        "5D":  ("15m", "5d"),
        "30D": ("60m", "1mo"),
        "6M":  ("1d",  "6mo"),
        "1Y":  ("1d",  "1y"),
        "5Y":  ("1wk", "5y"),
    }

    # range_key -> readable label for the text line
    RANGE_LABELS = {
        "1H": "Past Hour", "5D": "Past 5 Days", "30D": "Past 30 Days",
        "6M": "Past 6 Months", "1Y": "Past Year", "5Y": "Past 5 Years",
    }

    async def _fetch_chart_points(self, session, symbol: str, range_key: str):
        """Fetch (points, meta) of (unix_ts, close) for a symbol/range from Yahoo,
        or (None, None) on failure."""
        interval, yrange = self.CHART_RANGES.get(range_key, self.CHART_RANGES["1D"])
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?interval={interval}&range={yrange}&includePrePost=false"
        )
        try:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json(content_type=None)
            result = (data.get("chart", {}).get("result") or [None])[0]
            if not result:
                return None, None
            meta = result.get("meta", {})
            timestamps = result.get("timestamp") or []
            closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
            points = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
            return points, meta
        except Exception as e:
            print(f"[stock] chart fetch error for {symbol} ({range_key}): {e}")
            return None, None

    async def _price_chart(self, session, symbol: str, range_key: str):
        """Build a price chart for the given range.

        Returns (chart_buf, stats) where stats is {"baseline", "last"} for the
        range, or (None, None) on failure.
        """
        try:
            points, meta = await self._fetch_chart_points(session, symbol, range_key)
            if not points:
                return None, None

            # 1H: keep only the last 60 minutes of available data
            if range_key == "1H" and points:
                cutoff = points[-1][0] - 3600
                sliced = [p for p in points if p[0] >= cutoff]
                if len(sliced) >= 2:
                    points = sliced

            if len(points) < 2:
                return None, None

            # Baseline: previous close for the intraday view, else the range-start price
            if range_key == "1D" and meta.get("chartPreviousClose") is not None:
                baseline = meta["chartPreviousClose"]
            else:
                baseline = points[0][1]

            stats = {"baseline": baseline, "last": points[-1][1]}
            loop = asyncio.get_event_loop()
            chart = await loop.run_in_executor(
                None, generate_price_chart, points, baseline,
                meta.get("gmtoffset", 0), range_key, range_key, symbol,
            )
            return chart, stats
        except Exception as e:
            print(f"[stock] price chart error for {symbol} ({range_key}): {e}")
            return None, None

    async def _index_intraday_chart(self, session) -> io.BytesIO | None:
        """Build an intraday chart overlaying the major indexes, each normalized
        to % change from its previous close so they share one axis."""
        async def one(sym, label, color):
            points, meta = await self._fetch_chart_points(session, sym, "1D")
            if not points or len(points) < 2:
                return None
            prev = meta.get("chartPreviousClose")
            if prev is None:
                prev = points[0][1]
            pct = [(ts, (p / prev - 1.0) * 100.0) for ts, p in points]
            return {"label": label, "color": color, "points": pct,
                    "last": pct[-1][1], "gmt": meta.get("gmtoffset", 0)}

        results = await asyncio.gather(*(one(s, l, c) for s, l, c in STOCK_INDEX_CHART))
        series = [r for r in results if r]
        if len(series) < 2:
            return None
        tz_off = series[0]["gmt"]
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, generate_index_chart, series, tz_off)

    @app_commands.command(name="stock", description="Stock quote for a ticker, or the major indexes if omitted")
    @app_commands.describe(
        ticker="Ticker symbol (e.g. AAPL); omit for Dow/S&P 500/Nasdaq",
        range="Chart time range (default 1D)",
    )
    @app_commands.choices(range=[
        app_commands.Choice(name="1 Hour",  value="1H"),
        app_commands.Choice(name="1 Day",   value="1D"),
        app_commands.Choice(name="5 Days",  value="5D"),
        app_commands.Choice(name="30 Days", value="30D"),
        app_commands.Choice(name="6 Months", value="6M"),
        app_commands.Choice(name="1 Year",  value="1Y"),
        app_commands.Choice(name="5 Years", value="5Y"),
    ])
    async def stock(self, interaction: discord.Interaction, ticker: str = None,
                    range: app_commands.Choice[str] = None):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()

        if ticker:
            symbol = ticker.strip().upper()
            quotes = await self._fetch_quotes(session, [symbol])
            if not quotes or quotes[0].get("regularMarketPrice") is None:
                await interaction.followup.send(f"Could not find a quote for **{symbol}**.")
                return
            q = quotes[0]
            symbol = q.get("symbol", symbol)
            name = q.get("longName") or q.get("shortName") or symbol
            price = q["regularMarketPrice"]
            change = q.get("regularMarketChange", 0.0)
            pct = q.get("regularMarketChangePercent", 0.0)

            def quote_line(label: str, prefix: str) -> str | None:
                p = q.get(f"{prefix}Price")
                if p is None:
                    return None
                t = q.get(f"{prefix}Time")
                ts = datetime.fromtimestamp(t, tz=ET_ZONE).strftime("%Y-%m-%d %H:%M:%S") if t else "?"
                return (
                    f"{label}: {p:,.2f} ({q.get(f'{prefix}Change', 0.0):+,.2f},"
                    f" {q.get(f'{prefix}ChangePercent', 0.0):+.2f}%) ({ts})"
                )

            range_key = range.value if range else "1D"
            chart, range_stats = await self._price_chart(session, symbol, range_key)

            state = q.get("marketState", "")
            lines = [quote_line("Market Hours", "regularMarket")]
            if state.startswith("PRE"):
                pre = quote_line("Premarket", "preMarket")
                if pre:
                    lines.insert(0, pre)
            elif state.startswith("POST") or state == "CLOSED":
                post = quote_line("After Hours", "postMarket")
                if post:
                    lines.insert(0, post)
            # Range performance line (skip for the 1D intraday view, already shown above)
            if range_key != "1D" and range_stats and range_stats["baseline"]:
                base = range_stats["baseline"]
                last_p = range_stats["last"]
                r_chg = last_p - base
                r_pct = r_chg / base * 100 if base else 0.0
                label = self.RANGE_LABELS.get(range_key, range_key)
                lines.append(f"{label}: {last_p:,.2f} ({r_chg:+,.2f}, {r_pct:+.2f}%)")
            lines += [
                f"Day volume: {_abbrev_number(q.get('regularMarketVolume'))}"
                f" ({_abbrev_number(q.get('averageDailyVolume10Day'))} 10 day avg)",
                f"Day range: {q.get('regularMarketDayLow', 0):,.2f} - {q.get('regularMarketDayHigh', 0):,.2f}",
                f"52w range: {q.get('fiftyTwoWeekLow', 0):,.2f} - {q.get('fiftyTwoWeekHigh', 0):,.2f}",
            ]
            last = []
            if q.get("marketCap") is not None:
                last.append(f"Market Cap: {_abbrev_number(q['marketCap'])}")
            if q.get("trailingPE") is not None:
                last.append(f"P/E: {q['trailingPE']:.2f}")
            if last:
                lines.append(", ".join(last))

            embed = discord.Embed(
                title=f"{name} ({symbol})",
                url=f"https://finance.yahoo.com/quote/{urllib.parse.quote(symbol)}",
                description=f"```{chr(10).join(lines)}```",
                color=discord.Color.green() if change >= 0 else discord.Color.red(),
            )
            if chart:
                embed.set_image(url="attachment://chart.png")
                await interaction.followup.send(embed=embed, file=discord.File(chart, filename="chart.png"))
            else:
                await interaction.followup.send(embed=embed)
            return

        # No ticker — summary of the major indexes
        quotes = await self._fetch_quotes(session, [sym for sym, _ in STOCK_INDEXES])
        by_symbol = {q.get("symbol"): q for q in quotes}
        lines = []
        for sym, label in STOCK_INDEXES:
            q = by_symbol.get(sym)
            if not q or q.get("regularMarketPrice") is None:
                continue
            lines.append(
                f"{label:<10}{q['regularMarketPrice']:>12,.2f}"
                f"{q.get('regularMarketChangePercent', 0.0):>+8.2f}%"
            )
        if not lines:
            await interaction.followup.send("Could not fetch index data right now.")
            return

        embed = discord.Embed(
            title="📊 Market Summary",
            description=f"```{chr(10).join(lines)}```",
            color=discord.Color.blurple(),
        )
        chart = await self._index_intraday_chart(session)
        if chart:
            embed.set_image(url="attachment://indexes.png")
            await interaction.followup.send(embed=embed, file=discord.File(chart, filename="indexes.png"))
        else:
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="weather", description="Get the current weather for a location")
    @app_commands.describe(location="City, zip code, or address (e.g. Washington DC, 20001)")
    async def weather(self, interaction: discord.Interaction, location: str):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()

        # Pre-geocode through Nominatim so wttr.in doesn't pick a foreign location.
        # Zip codes: hard-restrict to US. Everything else: soft-bias toward US.
        is_us_zip = location.strip().replace('-', '').isdigit() and len(location.strip()) in (5, 9)
        geo_bias = "countrycodes=us" if is_us_zip else "viewbox=-125,24,-66,50&bounded=0"
        geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(location)}&format=json&limit=1&{geo_bias}"
        geo_name = None
        try:
            async with session.get(geo_url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                geo_data = await resp.json() if resp.status == 200 else []
            if geo_data:
                wttr_query = urllib.parse.quote(f"{geo_data[0]['lat']},{geo_data[0]['lon']}")
                geo_name = geo_data[0].get('display_name', '').split(',')[0].strip() or None
            else:
                wttr_query = urllib.parse.quote(location)
        except Exception:
            wttr_query = urllib.parse.quote(location)

        url = f"https://wttr.in/{wttr_query}?format=j1"
        try:
            async with session.get(url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Could not fetch weather for **{location}**.")
                    return
                data = await resp.json(content_type=None)
        except Exception as e:
            await interaction.followup.send(f"Error fetching weather: {e}")
            return

        current = data.get("current_condition", [{}])[0]
        area = data.get("nearest_area", [{}])[0]
        hourly = data.get("weather", [{}])[0].get("hourly", [])

        wttr_name = area.get("areaName", [{}])[0].get("value", location)
        region = area.get("region", [{}])[0].get("value", "")
        country = area.get("country", [{}])[0].get("value", "")
        wttr_location = wttr_name
        if region and region != wttr_name:
            wttr_location += f", {region}"
        if country and country not in ("United States of America", ""):
            wttr_location += f", {country}"
        area_name = geo_name or wttr_name
        location_str = area_name
        if region and region != area_name:
            location_str += f", {region}"
        if country and country not in ("United States of America", ""):
            location_str += f", {country}"

        desc = current.get("weatherDesc", [{}])[0].get("value", "Unknown")
        temp_f = current.get("temp_F", "?")
        temp_c = current.get("temp_C", "?")
        feels_f = current.get("FeelsLikeF", "?")
        feels_c = current.get("FeelsLikeC", "?")
        humidity = current.get("humidity", "?")
        wind_mph = current.get("windspeedMiles", "?")
        wind_dir = current.get("winddir16Point", "")
        uv = current.get("uvIndex", "?")
        visibility = current.get("visibility", "?")
        precip = current.get("precipInches", "0.0")

        icon = _desc_to_icon(desc)

        wind_str = f"{wind_mph} mph {wind_dir}".strip()

        embed = discord.Embed(
            title=f"{icon} {desc} — {location_str}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Temperature", value=f"{temp_f}°F / {temp_c}°C", inline=True)
        embed.add_field(name="Feels Like", value=f"{feels_f}°F / {feels_c}°C", inline=True)
        embed.add_field(name="Humidity", value=f"{humidity}%", inline=True)
        embed.add_field(name="Wind", value=wind_str, inline=True)
        embed.add_field(name="UV Index", value=str(uv), inline=True)
        embed.add_field(name="Visibility", value=f"{visibility} mi", inline=True)
        if float(precip) > 0:
            embed.add_field(name="Precipitation", value=f"{precip} in", inline=True)

        hourly_by_time = {h.get("time"): h for h in hourly}
        forecast_parts = []
        for label, time_key in [("Morning", "900"), ("Noon", "1200"), ("Evening", "1800"), ("Night", "2100")]:
            h = hourly_by_time.get(time_key)
            if h:
                h_desc = h.get("weatherDesc", [{}])[0].get("value", "")
                h_icon = _desc_to_icon(h_desc)
                h_temp = h.get("tempF", "?")
                forecast_parts.append(f"`{label:<7}` {h_icon} {h_desc} · {h_temp}°F")
        if forecast_parts:
            embed.add_field(name="Today's Forecast", value="\n".join(forecast_parts), inline=False)

        if geo_name and wttr_location != location_str:
            embed.set_footer(text=f"Nearest station: {wttr_location}")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="forecast", description="3-day weather forecast for a location")
    @app_commands.describe(location="City, zip code, or address (e.g. Washington DC, 20001)")
    async def forecast(self, interaction: discord.Interaction, location: str):
        await interaction.response.defer()

        session = await self.bot.mlb_client.get_session()

        is_us_zip = location.strip().replace('-', '').isdigit() and len(location.strip()) in (5, 9)
        geo_bias = "countrycodes=us" if is_us_zip else "viewbox=-125,24,-66,50&bounded=0"
        geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(location)}&format=json&limit=1&{geo_bias}"
        geo_name = None
        try:
            async with session.get(geo_url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                geo_data = await resp.json() if resp.status == 200 else []
            if geo_data:
                wttr_query = urllib.parse.quote(f"{geo_data[0]['lat']},{geo_data[0]['lon']}")
                geo_name = geo_data[0].get('display_name', '').split(',')[0].strip() or None
            else:
                wttr_query = urllib.parse.quote(location)
        except Exception:
            wttr_query = urllib.parse.quote(location)

        url = f"https://wttr.in/{wttr_query}?format=j1"
        try:
            async with session.get(url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"Could not fetch forecast for **{location}**.")
                    return
                data = await resp.json(content_type=None)
        except Exception as e:
            await interaction.followup.send(f"Error fetching forecast: {e}")
            return

        area = data.get("nearest_area", [{}])[0]
        wttr_name = area.get("areaName", [{}])[0].get("value", location)
        region    = area.get("region", [{}])[0].get("value", "")
        country   = area.get("country", [{}])[0].get("value", "")
        wttr_location = wttr_name
        if region and region != wttr_name:
            wttr_location += f", {region}"
        if country and country not in ("United States of America", ""):
            wttr_location += f", {country}"
        area_name = geo_name or wttr_name
        location_str = area_name
        if region and region != area_name:
            location_str += f", {region}"
        if country and country not in ("United States of America", ""):
            location_str += f", {country}"

        days = data.get("weather", [])
        embed = discord.Embed(title=f"📅 3-Day Forecast — {location_str}", color=discord.Color.blue())

        for i, day in enumerate(days[:3]):
            if i == 0:
                day_label = "Today"
            elif i == 1:
                day_label = "Tomorrow"
            else:
                try:
                    day_label = datetime.strptime(day.get("date", ""), "%Y-%m-%d").strftime("%A")
                except Exception:
                    day_label = day.get("date", f"Day {i+1}")

            max_f  = day.get("maxtempF", "?")
            min_f  = day.get("mintempF", "?")

            desc = day.get("hourly", [{}])[4].get("weatherDesc", [{}])[0].get("value", "")
            icon = _desc_to_icon(desc)
            value = f"{icon} {desc}\nHigh **{max_f}°F** · Low **{min_f}°F**"
            embed.add_field(name=day_label, value=value, inline=False)

        if geo_name and wttr_location != location_str:
            embed.set_footer(text=f"Nearest station: {wttr_location}")

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="radar", description="Show a weather radar map for a location (default: Nationals Park)")
    @app_commands.describe(location="City, zip code, or address (e.g. Washington DC, 20001); omit for default")
    async def radar(self, interaction: discord.Interaction, location: str = None):
        await interaction.response.defer()

        if not location:
            location = os.getenv("RADAR_DEFAULT_LOCATION", "Nationals Park")

        session = await self.bot.mlb_client.get_session()

        # 1. Geocode via Nominatim; bias to US for bare zip codes
        is_us_zip = location.strip().replace('-', '').isdigit() and len(location.strip()) in (5, 9)
        geo_bias = "countrycodes=us" if is_us_zip else "viewbox=-125,24,-66,50&bounded=0"
        geo_url = f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(location)}&format=json&limit=1&{geo_bias}"
        try:
            async with session.get(geo_url, headers={"User-Agent": "discord-bot/1.0"}) as resp:
                geo_data = await resp.json() if resp.status == 200 else []
        except Exception as e:
            await interaction.followup.send(f"Error geocoding location: {e}")
            return

        if not geo_data:
            await interaction.followup.send(f"Could not find location: **{location}**")
            return

        lat = float(geo_data[0]['lat'])
        lon = float(geo_data[0]['lon'])
        display_name = geo_data[0].get('display_name', location).split(',')[0].strip()

        # 2. Get latest RainViewer radar frame
        try:
            async with session.get("https://api.rainviewer.com/public/weather-maps.json") as resp:
                rv_data = await resp.json() if resp.status == 200 else {}
        except Exception as e:
            await interaction.followup.send(f"Error fetching radar data: {e}")
            return

        past_frames = rv_data.get('radar', {}).get('past', [])
        if not past_frames:
            await interaction.followup.send("Radar data is currently unavailable.")
            return

        rv_host = rv_data['host']
        frame = past_frames[-1]
        rv_path = frame['path']
        from datetime import timezone
        radar_age_secs = int(datetime.now(tz=timezone.utc).timestamp()) - frame['time']
        radar_age_mins = radar_age_secs // 60
        radar_ts = f"{radar_age_mins} minute{'s' if radar_age_mins != 1 else ''} ago"

        # 3. Compute center tile and exact sub-tile pixel position of the query point
        gx, gy = _lat_lon_to_pixel(lat, lon, MAP_ZOOM)  # global pixel at MAP_ZOOM
        cx = int(gx // TILE_SIZE)
        cy = int(gy // TILE_SIZE)

        # Fetch a GRID×GRID tile canvas so we have enough room to crop centered on (gx, gy)
        half = GRID // 2  # 2 for a 5×5 grid
        map_offsets = [(dx, dy) for dy in range(-half, half + 1) for dx in range(-half, half + 1)]

        # Radar: find which RADAR_ZOOM tiles cover the full GRID×GRID map extent
        rx_min = (cx - half) // SCALE
        rx_max = (cx + half) // SCALE
        ry_min = (cy - half) // SCALE
        ry_max = (cy + half) // SCALE
        radar_offsets = [(rx, ry) for ry in range(ry_min, ry_max + 1) for rx in range(rx_min, rx_max + 1)]

        # Use a dedicated session with a higher connector limit for concurrent tile fetching
        connector = aiohttp.TCPConnector(limit=50)
        async with aiohttp.ClientSession(connector=connector) as tile_session:
            async def fetch(url):
                try:
                    async with tile_session.get(url, headers={"User-Agent": "discord-bot/1.0"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            return await resp.read()
                except Exception:
                    pass
                return None

            map_tasks = [fetch(f"https://tile.openstreetmap.org/{MAP_ZOOM}/{cx+dx}/{cy+dy}.png") for dx, dy in map_offsets]
            radar_tasks = [fetch(f"{rv_host}{rv_path}/256/{RADAR_ZOOM}/{rx}/{ry}/2/1_1.png") for rx, ry in radar_offsets]

            map_results, radar_results = await asyncio.gather(
                asyncio.gather(*map_tasks),
                asyncio.gather(*radar_tasks),
            )

        # 4. Build composite in executor
        loop = asyncio.get_event_loop()

        def build_image():
            canvas_size = TILE_SIZE * GRID  # 1280×1280

            # Stitch base map
            base_img = Image.new('RGBA', (canvas_size, canvas_size), (180, 180, 180, 255))
            for i, (dx, dy) in enumerate(map_offsets):
                if map_results[i]:
                    tile = Image.open(io.BytesIO(map_results[i])).convert('RGBA')
                    base_img.paste(tile, ((dx + half) * TILE_SIZE, (dy + half) * TILE_SIZE))

            # Stitch radar tiles at RADAR_ZOOM, scale up and align with base map canvas
            r_cols = rx_max - rx_min + 1
            r_rows = ry_max - ry_min + 1
            radar_canvas = Image.new('RGBA', (r_cols * TILE_SIZE, r_rows * TILE_SIZE), (0, 0, 0, 0))
            for i, (rx, ry) in enumerate(radar_offsets):
                if radar_results[i]:
                    tile = Image.open(io.BytesIO(radar_results[i]))
                    if tile.mode == 'P':  # palette-mode = RainViewer error tile, skip it
                        continue
                    radar_canvas.paste(tile.convert('RGBA'), ((rx - rx_min) * TILE_SIZE, (ry - ry_min) * TILE_SIZE))

            radar_scaled = radar_canvas.resize(
                (r_cols * TILE_SIZE * SCALE, r_rows * TILE_SIZE * SCALE),
                Image.NEAREST
            )

            # Crop radar to align with the base map canvas top-left corner
            crop_x_px = ((cx - half) - rx_min * SCALE) * TILE_SIZE
            crop_y_px = ((cy - half) - ry_min * SCALE) * TILE_SIZE
            radar_overlay = radar_scaled.crop((crop_x_px, crop_y_px, crop_x_px + canvas_size, crop_y_px + canvas_size))

            composited = Image.alpha_composite(base_img, radar_overlay)

            # Crop the large canvas to OUTPUT_SIZE centered on the exact query pixel
            qx = int(gx - (cx - half) * TILE_SIZE)
            qy = int(gy - (cy - half) * TILE_SIZE)
            half_out = OUTPUT_SIZE // 2
            left = max(0, min(qx - half_out, canvas_size - OUTPUT_SIZE))
            top  = max(0, min(qy - half_out, canvas_size - OUTPUT_SIZE))
            composited = composited.crop((left, top, left + OUTPUT_SIZE, top + OUTPUT_SIZE))

            buf = io.BytesIO()
            composited.convert('RGB').save(buf, format='JPEG', quality=85)
            buf.seek(0)
            return buf

        try:
            buf = await loop.run_in_executor(None, build_image)
        except Exception as e:
            print(f"[radar] build_image error: {e}")
            await interaction.followup.send("Error generating radar image.")
            return

        embed = discord.Embed(
            title=f"🌧️ Radar — {display_name}",
            color=discord.Color.blue()
        )
        embed.set_image(url="attachment://radar.jpg")
        embed.set_footer(text=f"Base map: OpenStreetMap · Radar: RainViewer · Updated {radar_ts}")

        await interaction.followup.send(embed=embed, file=discord.File(buf, filename="radar.jpg"))


async def setup(bot):
    await bot.add_cog(ExtendedSlash(bot))
