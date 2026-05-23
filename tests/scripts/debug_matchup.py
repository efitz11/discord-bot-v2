import asyncio, aiohttp

async def main():
    async with aiohttp.ClientSession() as session:
        # Blake Snell (605483)
        pid = 605483
        url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=vsPlayer&group=pitching"
        async with session.get(url) as resp:
            data = await resp.json()
            
        print(f"Stats groups: {len(data.get('stats', []))}")
        for sg in data.get('stats', []):
            print(f"Type: {sg.get('type', {}).get('displayName')} | Splits: {len(sg.get('splits', []))}")
            if sg.get('splits'):
                s = sg['splits'][0]
                print(f"First split batter: {s.get('batter', {}).get('fullName')}")
                print(f"First split stats: {s.get('stat', {}).get('plateAppearances', 0)} PA")

if __name__ == "__main__":
    asyncio.run(main())
