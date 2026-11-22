"""Optional standalone launcher. Use this OR load taiga_ai as a cog in your existing bot."""
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.guild_messages = True

bot = commands.Bot(command_prefix=os.getenv("BOT_PREFIX", "!"), intents=intents)

@bot.event
async def setup_hook():
    await bot.load_extension("taiga_ai")

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set DISCORD_BOT_TOKEN in your environment.")
    bot.run(token)
