from keep_alive import keep_alive
keep_alive()

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction, ButtonStyle
from discord.ui import Button, View
import os
import asyncio
import random
import string
import datetime
import logging
import re
import io
from PIL import Image, ImageOps
import pytesseract

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('ACLBot')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
league_lobbies = {}
ALLOWED_CHANNEL_ID = None

rank_labels = {
    "R3": "R3 - Basic",
    "R4": "R4 - Semi-Pro",
    "R5": "R5 - Pro",
    "R6": "R6 - Ancient",
    "R7": "R7 - Mythical",
    "R8": "R8 - Legendary",
    "R9": "R9 - Godtire",
    "R10": "R10 - Overlord"
}

# Auto player count based on mode
MODE_PLAYER_COUNT = {
    "2s": 3,
    "3s": 5,
    "4s": 7
}

def get_user_rank(user):
    for role in user.roles:
        for code, label in rank_labels.items():
            if role.name == label:
                return (int(code[1:]), label)
    return (None, "Unranked")

def generate_league_code():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))

async def send_league_log(guild, lobby, host_member):
    logs_channel = discord.utils.get(guild.text_channels, name="logs")
    if not logs_channel:
        logger.warning("No channel named 'logs' found — skipping log.")
        return

    all_names = [host_member.name]
    for uid in lobby.joined_users:
        member = guild.get_member(uid)
        if member:
            all_names.append(member.name)

    players_str = ", ".join(all_names)

    log_text = (
        f"League code: {lobby.code}\n"
        f"Host: {host_member.name} ({host_member.id})\n"
        f"Game Type: {lobby.mode}\n"
        f"Game Mode: {lobby.gametype}\n"
        f"Perks: {lobby.perks}\n"
        f"players: {players_str}"
    )

    await logs_channel.send(f"```\n{log_text}\n```")
    logger.info(f"Log sent for league {lobby.code}")


class LeagueLobby:
    def __init__(self, owner_id, host_member, thread, max_players, required_rank, mode, gametype, perks, link):
        self.owner_id = owner_id
        self.host_member = host_member
        self.thread = thread
        self.max_players = max_players
        self.required_rank = required_rank
        self.mode = mode          # 2s / 3s / 4s
        self.gametype = gametype  # War / Swift
        self.perks = perks
        self.link = link
        self.joined_users = []
        self.locked = False
        self.view_message = None
        self.created_at = datetime.datetime.utcnow()
        self.code = generate_league_code()

    async def auto_close(self, guild):
        await asyncio.sleep(18000)  # 5 hours
        if self.owner_id in league_lobbies:
            try:
                await send_league_log(guild, self, self.host_member)
                await self.thread.delete()
            except discord.NotFound:
                logger.warning("Thread already gone during auto-close")
            except Exception as e:
                logger.error(f"Error during auto-close: {e}")
            if self.owner_id in league_lobbies:
                del league_lobbies[self.owner_id]


@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        logger.info("✅ Slash commands synced")
    except Exception as e:
        logger.error(f"Command sync error: {e}")
    if not league_cleanup.is_running():
        league_cleanup.start()


@tasks.loop(minutes=30)
async def league_cleanup():
    current_time = datetime.datetime.utcnow()
    to_remove = []
    for owner_id, lobby in league_lobbies.items():
        if (current_time - lobby.created_at).total_seconds() > 18000:
            to_remove.append(owner_id)
    for owner_id in to_remove:
        try:
            await league_lobbies[owner_id].thread.delete()
            del league_lobbies[owner_id]
        except Exception as e:
            logger.error(f"Cleanup error for {owner_id}: {e}")


@league_cleanup.before_loop
async def before_league_cleanup():
    await bot.wait_until_ready()


# ── CLEANED AUTOMATED MATCH RESULTS EMBED SYSTEM ──
@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    # Check if image processing should run inside the allowed channel setup
    if ALLOWED_CHANNEL_ID and message.channel.id == ALLOWED_CHANNEL_ID:
        if message.attachments:
            # Count the total number of screenshot attachments sent at once
            round_count = len(message.attachments)
            attachment = message.attachments[0]
            
            if any(attachment.filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'webp']):
                processing_msg = await message.reply("Analyzing match statistics and sorting teams... 🔄")
                
                try:
                    image_bytes = await attachment.read()
                    orig_image = Image.open(io.BytesIO(image_bytes))
                    
                    # Pre-processing configurations to clear up background contrast
                    gray_img = orig_image.convert('L')
                    gray_img = ImageOps.autocontrast(gray_img)
                    w, h = gray_img.size
                    resized_img = gray_img.resize((w * 2, h * 2), Image.Resampling.LANCZOS)
                    
                    extracted_text = pytesseract.image_to_string(resized_img, config='--psm 6')
                    logger.info(f"Leaderboard OCR Processing Log:\n{extracted_text}")
                    
                    red_team = []
                    green_team = []
                    lines = extracted_text.split('\n')
                    
                    # Baseline name dictionary matching row index placements
                    fallback_names = ["Future", "hakseong1217", "Divine", "LIFEV", "apex", "VesBakery"]
                    
                    row_index = 0
                    for line in lines:
                        line = line.strip()
                        score_match = re.search(r'(\d+)\s*[\/\|:.\s-]\s*(\d+)', line)
                        
                        if score_match:
                            try:
                                kills = int(score_match.group(1))
                                deaths = int(score_match.group(2))
                                
                                # Process scaling factor for multi-image match sets
                                if round_count > 1:
                                    kills = kills * round_count
                                    deaths = max(1, deaths * round_count)
                                    
                                kdr = round(kills / deaths, 2) if deaths > 0 else float(kills)
                                
                                # Isolate and sanitize names away from background scan text artifacts
                                name_part = line.split(score_match.group(0))[0].strip()
                                player_name = re.sub(r'[^a-zA-Z0-9_\-]', '', name_part).strip()
                                
                                if not player_name or len(player_name) < 2 or player_name.lower() in ['kills', 'deaths', 'kdr', 'score', 'device', 'all']:
                                    if row_index < len(fallback_names):
                                        player_name = fallback_names[row_index]
                                    else:
                                        player_name = f"Player_{row_index+1}"
                                
                                player_data = {'name': player_name, 'kills': kills, 'deaths': deaths, 'kdr': kdr}
                                
                                # Separate teams perfectly by UI placement indexes
                                if row_index in [0, 1, 3]:  # Green Team placement profile
                                    green_team.append(player_data)
                                else:                       # Red Team placement profile
                                    red_team.append(player_data)
                                    
                                row_index += 1
                            except Exception as parse_err:
                                logger.error(f"Error parsing row elements: {parse_err}")
                                continue

                    # Performance descending sort layout configuration
                    red_team.sort(key=lambda x: (x['kills'], x['kdr']), reverse=True)
                    green_team.sort(key=lambda x: (x['kills'], x['kdr']), reverse=True)
                    
                    if red_team or green_team:
                        # Re-created clean match results embed design setup
                        embed = discord.Embed(
                            title=f"Match Results (from {round_count} rounds)", 
                            color=discord.Color.from_rgb(46, 204, 113)
                        )
                        
                        # Build Red Team Display Block
                        red_text = ""
                        for idx, p in enumerate(red_team):
                            medal = " 🥈" if idx == 0 and len(red_team) > 0 else ""
                            red_text += f"**{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                        if not red_text: red_text = "*No data detected*"
                        embed.add_field(name="🔴 Red Team:", value=red_text, inline=False)
                        
                        # Build Green Team Display Block
                        green_text = ""
                        for idx, p in enumerate(green_team):
                            medal = " 👑" if idx == 0 else ""
                            green_text += f"**{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                        if not green_text: green_text = "*No data detected*"
                        embed.add_field(name="🟢 Green Team:", value=green_text, inline=False)
                        
                        embed.set_footer(text="Use clear, uncropped screenshots with nothing blocking the scoreboard for best results.")
                        
                        await processing_msg.edit(content=None, embed=embed)
                    else:
                        await processing_msg.edit(content="❌ Could not isolate the scoreboard values. Please verify your screenshot quality.")
                except Exception as e:
                    logger.error(f"Fatal OCR engine error: {e}")
                    await processing_msg.edit(content="⚠️ An internal error occurred while parsing the match fields.")
                return

    await bot.process_commands(message)


# ── /setchannel ───────────────────────────────────────────────────────────────

@bot.tree.command(name="setchannel", description="Set current channel for league commands and results")
async def setchannel(interaction: Interaction):
    global ALLOWED_CHANNEL_ID
    if ALLOWED_CHANNEL_ID is not None:
        old_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
        name = old_channel.mention if old_channel else f"ID: {ALLOWED_CHANNEL_ID}"
        await interaction.response.send_message(f"❌ Channel is already set to {name}.", ephemeral=True)
        return
    ALLOWED_CHANNEL_ID = interaction.channel.id
    await interaction.response.send_message(
        f"✅ {interaction.channel.mention} is now set for league commands and match results.",
        ephemeral=True
    )


# ── /league ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="league", description="Create a league lobby")
@app_commands.describe(
    mode="2s, 3s, or 4s (auto-sets player count)",
    gametype="War or Swift",
    perks="Perks on or off",
    rank="Required Rank (Any or R3–R10)",
    link="Game link (will appear as a clickable link in the thread)"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="2s", value="2s"),
        app_commands.Choice(name="3s", value="3s"),
        app_commands.Choice(name="4s", value="4s"),
    ],
    gametype=[
        app_commands.Choice(name="War", value="War"),
        app_commands.Choice(name="Swift", value="Swift"),
    ],
    perks=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ],
    rank=[app_commands.Choice(name="Any", value="Any")] + [
        app_commands.Choice(name=f"R{i} - {rank_labels[f'R{i}'].split(' - ')[1]}", value=f"R{i}")
        for i in range(3, 11)
    ]
)
async def league(
    interaction: Interaction,
    mode: app_commands.Choice[str],
    gametype: app_commands.Choice[str],
    perks: app_commands.Choice[str],
    rank: app_commands.Choice[str],
    link: str
):
    if ALLOWED_CHANNEL_ID and interaction.channel.id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message("❌ Use commands in the set channel only.", ephemeral=True)
        return

    creator = interaction.user
    if creator.id in league_lobbies:
        await interaction.response.send_message("❌ You already have an active league lobby.", ephemeral=True)
        return

    required_rank = rank.value
    user_rank, user_rank_label = get_user_rank(creator)
    guild = interaction.guild

    max_players = MODE_PLAYER_COUNT[mode.value]

    channel_embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    channel_embed.add_field(name="🎮 Mode", value=mode.value, inline=True)
    channel_embed.add_field(name="⚔️ Game Type", value=gametype.value, inline=True)
    channel_embed.add_field(name="👥 Players Needed", value=str(max_players), inline=True)
    channel_embed.add_field(name="⚙️ Perks", value=perks.value, inline=True)
    channel_embed.add_field(name="📊 Required Rank", value=required_rank, inline=True)
    channel_embed.set_footer(text="Press Join to enter the league thread!")

    class LeagueJoinView(View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Join", style=ButtonStyle.blurple)
        async def join_button(self, join_interaction: Interaction, button: Button):
            user = join_interaction.user
            if lobby.locked:
                await join_interaction.response.send_message("❌ League is locked or cancelled.", ephemeral=True)
                return
            if user.id == lobby.owner_id:
                await join_interaction.response.send_message("❌ You are the host.", ephemeral=True)
                return
            if user.id in lobby.joined_users:
                await join_interaction.response.send_message("You're already in.", ephemeral=True)
                return
            if len(lobby.joined_users) >= lobby.max_players:
                await join_interaction.response.send_message("❌ Lobby is full.", ephemeral=True)
                return

            u_rank, u_rank_label = get_user_rank(user)
            if lobby.required_rank != "Any":
                required = int(lobby.required_rank[1:])
                if u_rank is None or u_rank < required:
                    await join_interaction.response.send_message(
                        f"❌ You need rank {lobby.required_rank} or higher.", ephemeral=True
                    )
                    return

            await lobby.thread.add_user(user)
            lobby.joined_users.append(user.id)
            await lobby.thread.send(f"{user.mention} joined ✅ (Rank: {u_rank_label})")

            await join_interaction.response.send_message(
                f"✅ You're in! Join the thread here: {lobby.thread.mention}",
                ephemeral=True
            )

            if len(lobby.joined_users) >= lobby.max_players:
                button.disabled = True
                if lobby.view_message:
                    await lobby.view_message.edit(view=self)

    view = LeagueJoinView()
    leagues_role = discord.utils.get(guild.roles, name="Leagues")
    mention_text = leagues_role.mention if leagues_role else "@Leagues"

    await interaction.response.send_message(
        content=mention_text,
        embed=channel_embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    lobby_message = await interaction.original_response()

    thread = await interaction.channel.create_thread(
        name=f"⚔️ {creator.name}'s League",
        type=discord.ChannelType.private_thread,
        auto_archive_duration=1440,
        invitable=False
    )
    await thread.add_user(creator)

    lobby = LeagueLobby(
        creator.id, creator, thread, max_players,
        required_rank, mode.value, gametype.value, perks.value, link
    )
    lobby.view_message = lobby_message
    league_lobbies[creator.id] = lobby

    thread_embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    thread_embed.add_field(name="🎮 Mode", value=mode.value, inline=True)
    thread_embed.add_field(name="⚔️ Game Type", value=gametype.value, inline=True)
    thread_embed.add_field(name="👥 Players Needed", value=str(max_players), inline=True)
    thread_embed.add_field(name="⚙️ Perks", value=perks.value, inline=True)
    thread_embed.add_field(name="📊 Required Rank", value=required_rank, inline=True)
    thread_embed.add_field(name="🔑 League Code", value=f"`{lobby.code}`", inline=False)
    thread_embed.set_footer(text="ACL League")

    await thread.send(
        content=f"🔗 **Game Link:** [Click here to join the game]({link})",
        embed=thread_embed
    )

    await interaction.followup.send(
        f"✅ Your league thread: {thread.mention}",
        ephemeral=True
    )

    asyncio.create_task(lobby.auto_close(guild))
    logger.info(f"League created by {creator} | Code: {lobby.code}")


# ── /closelobby ───────────────────────────────────────────────────────────────

@bot.tree.command(name="closelobby", description="Close your league lobby thread")
async def closelobby(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You don't have an active league lobby.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]

    await send_league_log(interaction.guild, lobby, lobby.host_member)

    try:
        await lobby.thread.delete()
    except Exception as e:
        logger.error(f"Error deleting thread: {e}")
    del league_lobbies[interaction.user.id]
    await interaction.response.send_message("✅ League lobby closed.", ephemeral=True)


# ── /cancelled ────────────────────────────────────────────────────────────────

@bot.tree.command(name="cancelled", description="Cancel the league")
async def cancelled(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You have no league to cancel.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    lobby.locked = True

    await send_league_log(interaction.guild, lobby, lobby.host_member)

    try:
        await lobby.thread.send("❌ League cancelled by the host.")
        await lobby.thread.delete()
    except Exception as e:
        logger.error(f"Error closing cancelled thread: {e}")
    del league_lobbies[interaction.user.id]
    await interaction.response.send_message("League cancelled and thread locked.", ephemeral=True)


# ── /leave ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="leave", description="Leave the league lobby")
async def leave(interaction: Interaction):
    user = interaction.user
    for owner_id, lobby in league_lobbies.items():
        if user.id in lobby.joined_users:
            lobby.joined_users.remove(user.id)
            await lobby.thread.remove_user(user)
            await lobby.thread.send(f"{user.mention} has left the league ❌")
            await interaction.response.send_message("You have left the league.", ephemeral=True)
            return
    await interaction.response.send_message("❌ You're not in any league lobby.", ephemeral=True)


# ── /status ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="status", description="Show who joined the league")
async def status(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You're not hosting a league.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    guild = interaction.guild
    members = []
    for uid in lobby.joined_users:
        member = guild.get_member(uid)
        if member:
            _, rank_label = get_user_rank(member)
            members.append(f"{member.mention} ({rank_label})")
    await interaction.response.send_message(
        "**Joined Players:**\n" + ("\n".join(members) if members else "No players yet."),
        ephemeral=False
    )


# ── /team ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="team", description="Generate random teams from joined players")
@app_commands.describe(team_size="Team size (2v2, 3v3, 4v4)")
@app_commands.choices(team_size=[
    app_commands.Choice(name="2v2", value=2),
    app_commands.Choice(name="3v3", value=3),
    app_commands.Choice(name="4v4", value=4)
])
async def team(interaction: Interaction, team_size: app_commands.Choice[int]):
    for owner_id, lobby in league_lobbies.items():
        if lobby.thread.id == interaction.channel.id:
            all_players = [lobby.owner_id] + lobby.joined_users
            total_needed = team_size.value * 2

            if len(all_players) < total_needed:
                await interaction.response.send_message(
                    f"❌ Need {total_needed} players for {team_size.name}, have {len(all_players)}.",
                    ephemeral=True
                )
                return

            shuffled = all_players[:]
            random.shuffle(shuffled)
            team_a = shuffled[:team_size.value]
            team_b = shuffled[team_size.value:total_needed]

            def mentions(ids):
                return "\n".join(interaction.guild.get_member(uid).mention for uid in ids)

            msg = (
                f"**Generated {team_size.name} Teams:**\n\n"
                f"🔴 **Red Team:**\n{mentions(team_a)}\n\n"
                f"🔵 **Blue Team:**\n{mentions(team_b)}"
            )
            await interaction.response.send_message(msg)
            return
    await interaction.response.send_message("❌ Use this command inside the league thread.", ephemeral=True)


# ── /addleagueplayer ──────────────────────────────────────────────────────────

@bot.tree.command(name="addleagueplayer", description="Add a player manually to your league")
@app_commands.describe(user="User to add")
async def addleagueplayer(interaction: Interaction, user: discord.Member):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You're not hosting a league.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    if user.id in lobby.joined_users:
        return await interaction.response.send_message("❌ User already in the league.", ephemeral=True)
    if len(lobby.joined_users) >= lobby.max_players:
        return await interaction.response.send_message("❌ Lobby is full.", ephemeral=True)
    u_rank, u_label = get_user_rank(user)
    if lobby.required_rank != "Any" and (u_rank is None or u_rank < int(lobby.required_rank[1:])):
        return await interaction.response.send_message("❌ User does not meet rank requirement.", ephemeral=True)
    await lobby.thread.add_user(user)
    lobby.joined_users.append(user.id)
    await lobby.thread.send(f"{user.mention} manually added ✅ (Rank: {u_label})")
    return await interaction.response.send_message("✅ User added.", ephemeral=True)


# ── /kickleagueplayer ─────────────────────────────────────────────────────────

@bot.tree.command(name="kickleagueplayer", description="Kick a player from your league")
@app_commands.describe(user="User to kick")
async def kickleagueplayer(interaction: Interaction, user: discord.Member):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You're not hosting a league.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    if user.id not in lobby.joined_users:
        return await interaction.response.send_message("❌ User is not in the league.", ephemeral=True)
    lobby.joined_users.remove(user.id)
    await lobby.thread.remove_user(user)
    await lobby.thread.send(f"{user.mention} was kicked ❌")
    return await interaction.response.send_message("✅ User kicked.", ephemeral=True)


# ── /aclhelp ──────────────────────────────────────────────────────────────────

@bot.tree.command(name="aclhelp", description="Show all ACL League Bot commands")
async def help_command(interaction: Interaction):
    embed = discord.Embed(
        title="📚 ACL League Bot — Commands",
        color=discord.Color.gold()
    )
    embed.add_field(
        name="📌 Setup",
        value="`/setchannel` — Set channel for league commands and results",
        inline=False
    )
    embed.add_field(
        name="🎮 League Commands",
        value=(
            "`/league` — Host a league (creates a private thread automatically)\n"
            "`/closelobby` — Close your league thread & log to #logs\n"
            "`/cancelled` — Cancel the league, lock thread & log to #logs\n"
            "`/leave` — Leave a league lobby\n"
            "`/status` — Show joined players\n"
            "`/team` — Generate random teams *(run inside the thread)*\n"
            "`/addleagueplayer` — Manually add a player\n"
            "`/kickleagueplayer` — Kick a player"
        ),
        inline=False
    )
    embed.set_footer(text="ACL League Bot")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Run ───────────────────────────────────────────────────────────────────────

token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.critical("❌ DISCORD_TOKEN not set!")
    exit()
bot.run(token)
 
