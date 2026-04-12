import asyncio
import discord
from core.mlb_client import MLBClient

async def main():
    client = MLBClient()
    stats = await client.get_player_percentiles('juan soto')
    if stats:
        embed = discord.Embed()
        stats.apply_to_embed(embed)
        for field in embed.fields:
            print("==========", field.name, "==========")
            print(field.value)
    else:
        print("NONE WAS RETURNED")
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
