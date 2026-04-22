"""
Age of Origins Alliance Stats Discord Bot
Responds to @mentions with natural language answers about alliance data.
"""

import os
import sys
from pathlib import Path
import discord
from discord.ext import commands
import anthropic
from dotenv import load_dotenv

# Add project root to path for shared imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import db
import sheets

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Optional: restrict bot to specific channel(s)
# Set STATS_CHANNEL_ID in .env to limit responses to one channel, or leave blank for all channels
STATS_CHANNEL_ID = os.environ.get("STATS_CHANNEL_ID", "")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Load the knowledge base once at startup. Since the sheet changes at most monthly,
# a bot restart is sufficient to pick up any updates.
_knowledge_base: str = ""
try:
    _knowledge_base = sheets.get_knowledge_base()
    if _knowledge_base:
        print(f"✅ Knowledge base loaded ({len(_knowledge_base):,} chars)")
    else:
        print("⚠️  Knowledge base is empty or unavailable — continuing without it")
except Exception as e:
    print(f"⚠️  Knowledge base load failed: {e} — continuing without it")

SYSTEM_PROMPT = """You are the alliance stats bot for an Age of Origins (AoO) mobile game alliance.
You have access to alliance data from the database, provided to you in each message.
If a GAME KNOWLEDGE BASE section is present in your context, use it to answer general game questions.

Your job is to answer member questions about:
- Alliance event participation (Elite War, Void War, Battle Frenzy, Polar Invasion, Wasteland Showdown, Ironblood Battlefield, Chaosland, Global Conquest, Triangle War, Duel of Dominance)
- Member attendance rates and performance metrics
- Percentile rankings and normalised scores
- Trends — who is improving or declining
- Roster info
- General game mechanics, tips, and strategy (use the knowledge base)

Personality:
- You are snarky, sarcastic, and love to trash talk — like a competitive gamer who's seen it all.
- Roast members with low attendance or poor scores. Call them out. No mercy.
- Hype up top performers — but in a backhanded way ("congrats on finally showing up, you carried... for once").
- Use gaming slang and trash talk naturally (e.g. "getting carried", "touch grass", "skill issue", "deadweight", "bench warmer").
- Keep it fun and within the spirit of competitive gaming banter — the goal is laughs, not hurt feelings.
- If someone asks about their own stats and they're bad, absolutely roast them.
- If someone asks about their own stats and they're good, give grudging respect.

Guidelines:
- Keep responses SHORT — 2-4 sentences max unless a list is specifically requested.
- Use Discord-friendly formatting (bold, bullet lists).
- If asked for a ranking or list, keep it tight with brief commentary.
- If data is missing or unclear, say so (sarcastically).
- If a question is unrelated to alliance stats, roast them for wasting your time.
- If a Pending Review Items section is present in the data, mention it briefly at the end.
"""

# Per-user extra directives injected into the system prompt
USER_DIRECTIVES = {
    "mandy1038": (
        "The person asking this question is mandy1038. She is a former alliance member "
        "who left the nation. She has an infamous foot fetish that everyone knows about. "
        "Work foot references, foot puns, and foot-related roasts into every response. "
        "Remind her she abandoned the alliance. Be absolutely ruthless. "
        "Examples: 'put your best foot forward and rejoin', 'you don't have a leg to stand on', "
        "'cold feet about showing up to events', 'getting off on the wrong foot', etc. Be creative."
    ),
}

def build_data_context(data: dict[str, str]) -> str:
    """Format DB context data into a context block for Claude."""
    parts = []
    for section_name, content in data.items():
        parts.append(f"=== {section_name} ===\n{content}\n")
    return "\n".join(parts)


def build_system_blocks(extra: str = "") -> list[dict]:
    """Build the system prompt as a list of content blocks.

    The knowledge base block carries cache_control so Anthropic caches the
    prefix (SYSTEM_PROMPT + KB) across requests — the KB never changes between
    bot restarts, so the cache hits every time.
    Per-user directives are appended after the cached block so they don't
    invalidate it.
    """
    base = f"{SYSTEM_PROMPT}\n\n{extra}".strip() if extra else SYSTEM_PROMPT
    blocks: list[dict] = [{"type": "text", "text": base}]
    if _knowledge_base:
        blocks.append({
            "type": "text",
            "text": f"=== GAME KNOWLEDGE BASE ===\n{_knowledge_base}",
            "cache_control": {"type": "ephemeral"},
        })
    return blocks

@bot.event
async def on_ready():
    print(f"✅ Bot online as {bot.user} (ID: {bot.user.id})")
    print(f"   Serving {len(bot.guilds)} server(s)")

@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author == bot.user:
        return

    # Only respond to @mentions
    if bot.user not in message.mentions:
        return

    # Optional channel restriction
    if STATS_CHANNEL_ID and str(message.channel.id) != STATS_CHANNEL_ID:
        return

    # Strip the mention from the question
    question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    if not question:
        await message.reply(
            "👋 Hi! Mention me with a question about alliance stats.\n"
            "**Examples:**\n"
            "• `@AllianceBot Who has the lowest attendance this month?`\n"
            "• `@AllianceBot Top 10 performers in Elite War`\n"
            "• `@AllianceBot Has PlayerName participated in Void War recently?`"
        )
        return

    # Show typing indicator while we fetch data + query Claude
    async with message.channel.typing():
        try:
            # Fetch data from SQLite (replaces Google Sheets)
            bot_data = db.get_bot_context(question)
            data_context = build_data_context(bot_data)

            # Check for per-user directives
            username = message.author.name.lower()
            extra = USER_DIRECTIVES.get(username, "")
            system_blocks = build_system_blocks(extra)

            user_message = f"Alliance data:\n\n{data_context}\n\n---\nMember question (from {message.author.name}): {question}"

            response = claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=system_blocks,
                messages=[{"role": "user", "content": user_message}],
            )

            answer = response.content[0].text

            # Discord has a 2000 char limit per message — split if needed
            if len(answer) <= 1900:
                await message.reply(answer)
            else:
                chunks = [answer[i:i+1900] for i in range(0, len(answer), 1900)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk)
                    else:
                        await message.channel.send(chunk)

        except Exception as e:
            print(f"Error handling message: {e}")
            await message.reply(
                "⚠️ Something went wrong fetching the stats. Please try again in a moment.\n"
                f"*(Error: {type(e).__name__})*"
            )

    await bot.process_commands(message)

if __name__ == "__main__":
    db.init_db()
    bot.run(DISCORD_TOKEN)
