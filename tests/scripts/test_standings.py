import asyncio
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    sts = await client.get_standings("nl east")
    for group in sts:
        print(group.title)
        print(group.format_discord_code_block())
    
    sts_wc = await client.get_standings("al wc")
    for group in sts_wc:
        print(group.title)
        print(group.format_discord_code_block(is_wc=True))
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
