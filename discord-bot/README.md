# TaigaGPT — Gemini Discord AI

A self-contained `discord.py` AI cog using Google's official `google-genai` SDK.
It does not modify or depend on the website files in the repository.

## Features
- Replies when mentioned or when someone replies to one of Taiga's messages.
- Rare ambient participation (default 0.8% chance per eligible message), with per-channel and per-user cooldowns.
- SQLite conversation context, limited to the most recent messages in each channel.
- Persistent community style tuning when members explicitly request changes such as "be more sarcastic", "less chaotic", or "shorter replies".
- Uses Gemini only for AI generation; no paid third-party AI service.

## Choose ONE way to run it

### A. Add it to your existing discord.py bot (recommended for Wispbyte)
1. Upload the contents of this `discord-bot/` folder to the folder where your existing bot runs, or add this folder to its Python import path.
2. Install dependencies: `pip install -r requirements.txt`.
3. Set `GEMINI_API_KEY` in Wispbyte's environment variables.
4. In your existing bot's startup/setup code, load the extension:
   ```py
   await bot.load_extension("taiga_ai")
   ```
   If `taiga_ai.py` is inside a package folder, use its dotted import path instead.
5. Enable **Message Content Intent** in the Discord Developer Portal (Bot → Privileged Gateway Intents) and in your bot's code.
6. Restart the existing bot.

Do not run `bot.py` at the same time as your existing bot with the same Discord token.

### B. Run this as the whole bot
1. Install Python 3.10+ and dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set `DISCORD_BOT_TOKEN` and `GEMINI_API_KEY` as environment variables (or copy `.env.example` to `.env` and fill it in).
3. Start it: `python bot.py`.

## API keys
- Create a Gemini key in Google AI Studio: https://aistudio.google.com/app/apikey
- Add both keys through Wispbyte's environment/secrets settings. Never paste real keys into chat or commit them to GitHub.
- The default model is `gemini-3.8-flash`. Google model access, free-tier quotas, and rate limits can vary by account and change over time.

## Personality evolution
Taiga's core identity stays stable. The community can nudge style traits with direct feedback, for example:
- "Taiga, be more sarcastic"
- "less chaotic"
- "be friendlier"
- "shorter replies"

These preferences persist in `taiga_memory.sqlite3`. Recent channel context is also stored there (up to the configured message count per channel). Back up that file if you want to preserve Taiga's learned style/context between hosting changes. Members should avoid posting sensitive information.

## Important
- Ambient replies are random by design. Set `TAIGA_AMBIENT_CHANCE=0` to disable them.
- Free Gemini access is subject to Google's current eligibility, quotas, and rate limits; it is not unlimited.
- Wispbyte must keep the process running for the bot to stay online. GitHub Codespaces is not a reliable 24/7 host.
