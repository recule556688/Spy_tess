import os
import json
import discord
from discord.ext import commands, tasks
from discord.sinks import WaveSink
import asyncio
from dotenv import load_dotenv
import whisper
import tempfile
import time

load_dotenv()
try:
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
except Exception as e:
    print(f"Error loading Discord bot token: {e}")
    exit(1)

intents = discord.Intents.default()
intents.message_content = True  # needed to read messages in some forks
bot = commands.Bot(command_prefix="!", intents=intents)

# Load Whisper model
asr_model = whisper.load_model("large-v2", device="cuda")

# Persistent settings file
SETTINGS_FILE = "settings.json"

# Load or initialize settings
if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) > 0:
    with open(SETTINGS_FILE, "r") as f:
        settings = json.load(f)
else:
    settings = {}

# Initialize auto-join state
auto_join_enabled = settings.get("auto_join_enabled", False)

# Save settings to file
def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

# Connections and state tracking
connections = {}
audio_chunks = {}
recording = {}
CHUNK_DURATION = 5

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    if auto_join_enabled:
        print("Auto-join is enabled. Ready to join voice channels.")

# Load or initialize settings
if os.path.exists(SETTINGS_FILE) and os.path.getsize(SETTINGS_FILE) > 0:
    with open(SETTINGS_FILE, "r") as f:
        settings = json.load(f)
else:
    settings = {}

# Default language setting
default_language = "fr"

@bot.slash_command(name="set_language", description="Set the default language for transcription.")
async def set_language(ctx, language: str, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    global default_language
    default_language = language
    settings["default_language"] = default_language
    save_settings()
    await ctx.respond(f"Default transcription language set to {language}.", ephemeral=ephemeral)


@bot.slash_command(name="join", description="Bot joins your current voice channel.")
async def join_slash(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    if not ctx.author.voice:
        await ctx.respond("You must be in a voice channel to use this command.", ephemeral=ephemeral)
        return

    channel = ctx.author.voice.channel
    vc = await channel.connect()
    connections[ctx.guild.id] = vc
    await ctx.respond(f"Joined {channel.name}", ephemeral=ephemeral)


@bot.slash_command(name="set_transcription_channel", description="Set the text channel for transcription messages.")
async def set_transcription_channel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    settings.setdefault(guild_id, {})["preferred_channel"] = channel.id
    save_settings()
    await ctx.respond(f"Transcription messages will now be sent to {channel.mention}")

@bot.slash_command(name="set_response_channel", description="Set the text channel for response actions.")
async def set_response_channel(ctx, channel: discord.TextChannel):
    guild_id = str(ctx.guild.id)
    settings.setdefault(guild_id, {})["response_channel"] = channel.id
    save_settings()
    await ctx.respond(f"Response actions will now be sent to {channel.mention}")



@bot.slash_command(name="start_transcribing", description="Start live transcription of voice channel.")
async def start_transcribing_slash(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    guild_id = ctx.guild.id
    if guild_id not in connections:
        await ctx.respond("I'm not in a voice channel. Use /join first.", ephemeral=ephemeral)
        return

    vc = connections[guild_id]
    recording[guild_id] = True
    audio_chunks[guild_id] = []

    vc.start_recording(
        discord.sinks.WaveSink(),
        after_recording_chunk,
        ctx.channel
    )

    start_chunk_loop(ctx.guild.id)
    await ctx.respond("**Started transcribing** in 5-second chunks...", ephemeral=ephemeral)


@bot.slash_command(name="stop_transcribing", description="Stop live transcription.")
async def stop_transcribing_slash(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    guild_id = ctx.guild.id
    if guild_id not in connections:
        await ctx.respond("I'm not in a voice channel.", ephemeral=ephemeral)
        return

    # Check if recording is active
    if recording.get(guild_id, False):
        vc = connections[guild_id]
        try:
            vc.stop_recording()
        except Exception as e:
            print(f"Error stopping recording: {e}")
        recording[guild_id] = False
        await asyncio.sleep(1)
        await ctx.respond("Stopped transcribing.", ephemeral=ephemeral)
    else:
        await ctx.respond("Not currently transcribing.", ephemeral=ephemeral)




@bot.slash_command(name="leave", description="Bot leaves the voice channel.")
async def leave_slash(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    guild_id = ctx.guild.id
    if guild_id in connections:
        vc = connections[guild_id]
        await vc.disconnect()
        del connections[guild_id]
        recording[guild_id] = False
        if guild_id in audio_chunks:
            del audio_chunks[guild_id]
        await ctx.respond("Left the voice channel.", ephemeral=ephemeral)
    else:
        await ctx.respond("I'm not connected to any voice channel.", ephemeral=ephemeral)


def start_chunk_loop(guild_id):
    """
    Start a task that periodically stops the current sink (after 5s),
    transcribes it, and re-starts if still recording.
    """
    async def chunk_loop():
        while True:
            if guild_id not in connections:
                break

            vc = connections[guild_id]

            # If the user has stopped recording, break the loop
            if not recording.get(guild_id, False):
                break

            # Wait for CHUNK_DURATION seconds
            await asyncio.sleep(CHUNK_DURATION)

            # Check if still recording using the recording dictionary
            if recording.get(guild_id, False):
                vc.stop_recording()

            # Wait a moment for the callback (after_recording_chunk) to finish writing data
            await asyncio.sleep(1)

            # Check if we should keep recording
            if guild_id in connections and recording.get(guild_id, False):
                vc.start_recording(
                    WaveSink(),
                    lambda sink, *args: after_recording_chunk(sink, guild_id, *args),  # Ensure guild_id is passed
                    None  # Ensure channel is passed correctly later
                )
            else:
                break

    bot.loop.create_task(chunk_loop())


# Define action functions
async def action_miaou(channel, user_mention):
    await channel.send(f"**Keyword 'miaou' detected** from {user_mention}!")

async def action_hello(channel, user_mention):
    await channel.send(f"**Hello detected** from {user_mention}!")

async def action_gay(channel, user_mention):
    await channel.send(f"**Gay detected** from {user_mention}!")
    await channel.send("https://imgur.com/gallery/two-mindsets-going-into-2025-0jWTrH6")


# Keyword detection function
async def detect_keywords(text, channel, user_mention):
    keywords = {
        "miaou": action_miaou,
        "hello": action_hello,
        "bonjour": action_hello,
        "gay": action_gay,
        # Add more keywords and corresponding functions here
    }

    for keyword, action in keywords.items():
        if keyword in text.lower():
            await action(channel, user_mention)

# Modify the after_recording_chunk function
async def after_recording_chunk(sink, guild_id, *args):
    guild_id = str(guild_id)
    settings_data = settings.get(guild_id, {})

    transcription_channel_id = int(settings_data.get("preferred_channel", 0))
    response_channel_id = int(settings_data.get("response_channel", 0))

    if not transcription_channel_id or not response_channel_id:
        return

    transcription_channel = bot.get_channel(transcription_channel_id)
    response_channel = bot.get_channel(response_channel_id)

    if transcription_channel is None or not transcription_channel.permissions_for(transcription_channel.guild.me).send_messages:
        return

    for user_id, audio_data in sink.audio_data.items():
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_data.file.getvalue())
                tmp_path = tmp.name

            # Use the default language for transcription
            result = asr_model.transcribe(tmp_path, language=default_language)
            text = result["text"].strip()

            if text:
                user_mention = f"<@{user_id}>"
                await transcription_channel.send(f"**{user_mention}** said: `{text}`")
                await detect_keywords(text, response_channel, user_mention)

        except Exception as e:
            print(f"Error during transcription or sending message: {e}")

        finally:
            try:
                os.remove(tmp_path)
            except Exception as e:
                print(f"Error deleting temporary file: {e}")


@bot.slash_command(name="auto_join_and_transcribe", description="Automatically join your voice channel and start transcribing.")
async def auto_join_and_transcribe(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    if not ctx.author.voice:
        await ctx.respond("You must be in a voice channel to use this command.", ephemeral=ephemeral)
        return

    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id

    # Join the voice channel
    vc = await channel.connect()
    connections[guild_id] = vc

    # Start transcribing
    recording[guild_id] = True
    audio_chunks[guild_id] = []

    vc.start_recording(
        discord.sinks.WaveSink(),
        after_recording_chunk,
        ctx.channel
    )

    start_chunk_loop(guild_id)
    await ctx.respond(f"Joined {channel.name} and started transcribing in 5-second chunks...", ephemeral=ephemeral)


@bot.event
async def on_voice_state_update(member, before, after):
    guild_id = member.guild.id

    if auto_join_enabled and after.channel and not before.channel:
        channel = after.channel

        if guild_id in connections:
            current_vc = connections[guild_id]
            if current_vc.channel == channel:
                return
            else:
                await current_vc.disconnect()

        vc = await channel.connect()
        connections[guild_id] = vc

        settings.setdefault(str(guild_id), {})["transcription_channel"] = channel.id
        save_settings()

        recording[guild_id] = True
        audio_chunks[guild_id] = []

        vc.start_recording(
            discord.sinks.WaveSink(),
            after_recording_chunk,
            channel
        )

        start_chunk_loop(guild_id)
        print(f"Joined {channel.name} and started transcribing.")

    if before.channel and not after.channel:
        channel = before.channel

        if len(channel.members) == 1 and member.guild.me in channel.members:
            if guild_id in connections:
                vc = connections[guild_id]
                if recording.get(guild_id, False):
                    vc.stop_recording()
                await vc.disconnect()
                del connections[guild_id]
                recording[guild_id] = False
                if guild_id in audio_chunks:
                    del audio_chunks[guild_id]
                print(f"Left {channel.name} as it is empty.")


@bot.slash_command(name="activate_auto_join", description="Activate auto-join and transcribe when users join a voice channel.")
async def activate_auto_join(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    global auto_join_enabled
    auto_join_enabled = True
    settings["auto_join_enabled"] = auto_join_enabled
    save_settings()
    await ctx.respond("Auto-join and transcribe activated.", ephemeral=ephemeral)


@bot.slash_command(name="deactivate_auto_join", description="Deactivate auto-join and transcribe.")
async def deactivate_auto_join(ctx, ephemeral: bool = discord.commands.Option(default=True, description="Should the response be ephemeral?")):
    global auto_join_enabled
    auto_join_enabled = False
    settings["auto_join_enabled"] = auto_join_enabled
    save_settings()
    await ctx.respond("Auto-join and transcribe deactivated.", ephemeral=ephemeral)


try:
    bot.run(TOKEN)
except Exception as e:
    print(f"An error occurred: {e}")
