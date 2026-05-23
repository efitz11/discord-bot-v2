import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    resolved = await client.resolve_player('sean murphy')
    pid = resolved['id']
    url = f"https://baseballsavant.mlb.com/savant-player/{pid}"
    session = await client.get_session()
    async with session.get(url) as resp:
        text = await resp.text()
    import re, json
    match = re.search(r"statcast:\s*(\[.*?\]),\s*\n", text, re.DOTALL)
    statcast_data = json.loads(match.group(1))
    year_stats = [s for s in statcast_data if s.get('aggregate')=='0'][-1]
    
    print([k for k in year_stats.keys() if 'percent' in k])
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
