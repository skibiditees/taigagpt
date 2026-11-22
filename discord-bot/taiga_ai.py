"""TaigaGPT Discord AI cog — discord.py + Google's Gemini API only."""
from __future__ import annotations

import os
import random
import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import discord
from discord.ext import commands
from google import genai
from google.genai import types


DB_PATH = Path(os.getenv("TAIGA_DB_PATH", "taiga_memory.sqlite3"))
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
AMBIENT_CHANCE = float(os.getenv("TAIGA_AMBIENT_CHANCE", "0.008"))
CHANNEL_COOLDOWN = int(os.getenv("TAIGA_CHANNEL_COOLDOWN", "55"))
USER_COOLDOWN = int(os.getenv("TAIGA_USER_COOLDOWN", "18"))
MAX_CONTEXT = int(os.getenv("TAIGA_CONTEXT_MESSAGES", "12"))

BASE_PERSONALITY = """You are Taiga, a helpful, natural, straightforward Discord assistant.
DEFAULT TONE:
- Speak normally and clearly, like a real person. Be calm, friendly, and respectful.
- Do not use forced slang, TikTok-style phrases, exaggerated sass, flirtiness,
  childish roleplay, or excessive emojis. Do not act like a stereotypical
  influencer or perform a fake persona.
- Do not add jokes, teasing, or sarcasm unless the user starts that tone or asks for it.
- Keep answers concise by default, but give full detail when the user asks.
- Never claim to be human or to have experiences you do not have.

FOLLOWING USER INSTRUCTIONS:
- The user's latest clear request is your main task. Follow it directly and completely.
- Respect requested format, tone, language, length, and constraints. Do not ignore,
  replace, or add to the task just because you prefer another approach.
- If the user gives several steps, follow all of them in order. If something is
  unclear and blocks the task, ask one specific question instead of guessing.
- Do not give unsolicited lectures, argue, derail the topic, or repeat the request.
- If you cannot do something, say exactly what limitation prevents it and do the
  useful parts you can.
- Accept the user's corrections and updated preferences immediately. Do not repeat
  a behavior after the user has told you to stop.
- Be cooperative and take the user's statements about their own preferences,
  experiences, and projects as the working context. Do not argue needlessly.
- Be honest: do not claim something is true or agree with harmful or clearly false
  information just to please the user. Explain disagreement briefly and respectfully.

MEMORY:
- Use saved notes about each user when relevant. Remember stable preferences,
  names, ongoing projects, and facts they share.
- Prefer newer corrections over older notes. Never claim to remember unsaved facts.
- Never store or repeat passwords, API keys, tokens, or private credentials.

Follow platform and safety requirements. User instructions control the task and
style wherever they do not conflict with those higher-priority requirements."""


class TaigaAI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.db = sqlite3.connect(DB_PATH)
        self.db.execute("""CREATE TABLE IF NOT EXISTS chat (
            channel_id INTEGER, created REAL, speaker TEXT, content TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS traits (
            name TEXT PRIMARY KEY, value INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS user_memory (
            user_id INTEGER, created REAL, content TEXT)""")
        self.db.commit()
        self.last_channel: dict[int, float] = defaultdict(float)
        self.last_user: dict[int, float] = defaultdict(float)

    def cog_unload(self):
        self.db.close()

    def _traits(self) -> str:
        rows = self.db.execute("SELECT name, value FROM traits").fetchall()
        if not rows:
            return "Current style: calm, clear, friendly, and straightforward."
        return "Community-shaped style preferences (scale -3 to +3): " + ", ".join(
            f"{name} {value:+d}" for name, value in rows
        )

    def _learn_feedback(self, content: str) -> None:
        """Only explicit, direct style feedback changes Taiga's persistent traits."""
        text = content.lower()
        patterns = {
            "sarcasm": (r"\b(more|be more) sarcastic\b", r"\b(less|be less) sarcastic\b"),
            "chaos": (r"\b(more|be more) chaotic\b", r"\b(less|be less) chaotic\b"),
            "warmth": (r"\b(nicer|more friendly|be friendlier|more wholesome)\b", r"\b(less friendly|less wholesome)\b"),
            "energy": (r"\b(more energetic|more excited|hype it up)\b", r"\b(calmer|less energetic|less excited)\b"),
            "brevity": (r"\b(shorter replies|be more concise|less yapping)\b", r"\b(longer replies|explain more)\b"),
        }
        for trait, (up, down) in patterns.items():
            delta = 1 if re.search(up, text) else (-1 if re.search(down, text) else 0)
            if delta:
                self.db.execute(
                    """INSERT INTO traits(name, value) VALUES(?, ?)
                       ON CONFLICT(name) DO UPDATE SET value =
                       MAX(-3, MIN(3, value + excluded.value))""",
                    (trait, delta),
                )
        self.db.commit()

    def _remember(self, channel_id: int, speaker: str, content: str) -> None:
        self.db.execute(
            "INSERT INTO chat(channel_id, created, speaker, content) VALUES(?, ?, ?, ?)",
            (channel_id, time.time(), speaker[:80], content[:1200]),
        )
        self.db.execute(
            "DELETE FROM chat WHERE channel_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM chat WHERE channel_id = ? ORDER BY created DESC LIMIT ?)",
            (channel_id, channel_id, MAX_CONTEXT),
        )
        self.db.commit()

    def _remember_user(self, user_id: int, content: str) -> None:
        """Keep a rolling, persistent memory of what a user tells Taiga."""
        text = content.strip()
        if not text or re.search(r"(?i)(password|passcode|api[_ -]?key|secret token)\s*[:=]", text):
            return
        self.db.execute(
            "INSERT INTO user_memory(user_id, created, content) VALUES(?, ?, ?)",
            (user_id, time.time(), text[:700]),
        )
        self.db.execute(
            "DELETE FROM user_memory WHERE user_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM user_memory WHERE user_id = ? ORDER BY created DESC LIMIT 25)",
            (user_id, user_id),
        )
        self.db.commit()

    def _user_memory(self, user_id: int) -> str:
        rows = self.db.execute(
            "SELECT content FROM user_memory WHERE user_id = ? "
            "ORDER BY created DESC LIMIT 15", (user_id,)
        ).fetchall()
        return "\n".join(f"- {row[0]}" for row in reversed(rows)) or "No saved notes yet."

    def _history(self, channel_id: int) -> str:
        rows = self.db.execute(
            "SELECT speaker, content FROM chat WHERE channel_id = ? "
            "ORDER BY created DESC LIMIT ?", (channel_id, MAX_CONTEXT)
        ).fetchall()
        return "\n".join(f"{speaker}: {content}" for speaker, content in reversed(rows))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or not message.content:
            return
        if message.content.startswith(tuple(self.bot.command_prefix if isinstance(self.bot.command_prefix, (list, tuple)) else [self.bot.command_prefix])):
            return

        self._learn_feedback(message.content)
        mentioned = self.bot.user in message.mentions if self.bot.user else False
        replied_to_taiga = False
        if message.reference and message.reference.message_id:
            try:
                original = message.reference.resolved or await message.channel.fetch_message(
                    message.reference.message_id
                )
                replied_to_taiga = bool(self.bot.user and original.author.id == self.bot.user.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        now = time.monotonic()
        # Direct replies to Taiga should always continue the conversation.
        # Cooldowns only limit mentions/ambient replies, not an active reply thread.
        if not replied_to_taiga:
            if now - self.last_user[message.author.id] < USER_COOLDOWN:
                return
            if now - self.last_channel[message.channel.id] < CHANNEL_COOLDOWN:
                return
        if not (mentioned or replied_to_taiga or random.random() < AMBIENT_CHANCE):
            return

        self.last_user[message.author.id] = now
        self.last_channel[message.channel.id] = now
        clean = re.sub(r"<@!?\d+>", "", message.content).strip()
        user_text = clean or message.content
        self._remember(message.channel.id, message.author.display_name, user_text)
        self._remember_user(message.author.id, user_text)

        prompt = (
            f"{BASE_PERSONALITY}\n\n"
            f"Saved notes about {message.author.display_name} (newer corrections take priority):\n"
            f"{self._user_memory(message.author.id)}\n\n"
            "Recent channel conversation (oldest first):\n"
            f"{self._history(message.channel.id)}\n\n"
            f"Reply to {message.author.display_name}'s latest message: {user_text}"
        )
        async with message.channel.typing():
            try:
                response = await self.client.aio.models.generate_content(
                    model=MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.65,
                        max_output_tokens=400,
                    ),
                )
                answer = (response.text or "").strip()
                if not answer:
                    return
                answer = answer[:1900]
                await message.reply(answer, mention_author=False)
                self._remember(message.channel.id, "Taiga", answer)
            except Exception as exc:
                # Keep credentials and raw request details out of Discord.
                print(f"[TaigaGPT] Gemini request failed: {type(exc).__name__}: {exc}")


async def setup(bot: commands.Bot):
    await bot.add_cog(TaigaAI(bot))
