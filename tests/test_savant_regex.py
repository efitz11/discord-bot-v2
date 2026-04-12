import aiohttp
import asyncio
import re
import json

async def main():
    headers = {"User-Agent": "Mozilla/5.0"}
    url = "https://baseballsavant.mlb.com/savant-player/669221"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as res:
            text = await res.text()
            # Extract just the statcast array
            # It usually looks like: statcast: [{"year":..., ...}],
            match = re.search(r"statcast:\s*(\[.*?\]),\s*\n", text, re.DOTALL)
            if match:
                statcast_json = match.group(1)
                try:
                    data = json.loads(statcast_json)
                    print(f"SUCCESS! Found {len(data)} statcast years.")
                    print("Keys in latest:", list(data[-1].keys())[:10])
                except Exception as e:
                    print("Failed:", e)
                    print(statcast_json[:200])

if __name__ == "__main__":
    asyncio.run(main())
