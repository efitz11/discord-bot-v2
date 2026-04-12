import asyncio, aiohttp, json, io, csv

async def main():
    session = aiohttp.ClientSession()
    
    arsenal_url = "https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats?type=pitcher&pitchType=&year=2025&team=&min=1&csv=true"
    async with session.get(arsenal_url) as resp:
        raw = await resp.read()
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        
        burnes = [r for r in rows if "burnes" in r.get("last_name, first_name","").lower()]
        print(f"=== Corbin Burnes ({len(burnes)} pitches) ===")
        for r in burnes:
            print(json.dumps(r, indent=2))
            print()
    
    await session.close()

asyncio.run(main())
