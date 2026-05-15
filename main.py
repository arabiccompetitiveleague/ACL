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

# Configure logging
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

def get_user_rank(user):
    for role in user.roles:
        for code, label in rank_labels.items():
            if role.name == label:
                return (int(code[1:]), label)
    return (None, "Unranked")


# ── Generate a unique league code ────────────────────────────────────────────

def generate_league_code():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))


# ── Send log to #logs channel ─────────────────────────────────────────────────

async def send_league_log(guild, lobby, host_member):
    logs_channel = discord.utils.get(guild.text_channels, name="logs")
    if not logs_channel:
        logger.warning("No channel named 'logs' found — skipping log.")
        return

    # Collect player usernames (exclude host)
    player_names = []
    for uid in lobby.joined_users:
        member = guild.get_member(uid)
        if member:
            player_names.append(member.name)

    players_str = ", ".join(player_names) if player_names else "None"

    log_text = (
        f"League code: {lobby.code}\n"
        f"Host: {host_member.name} ({host_member.id})\n"
        f"Game Type: {lobby.mode}\n"
        f"Perks: {lobby.perks}\n"
        f"players: {players_str}"
    )

    # Send as a code block so clicking it selects/copies the whole thing
    await logs_channel.send(f"```\n{log_text}\n```")
    logger.info(f"League log sent for code {lobby.code}")


# ── LeagueLobby ───────────────────────────────────────────────────────────────

class LeagueLobby:
    def __init__(self, owner_id, thread, max_players, required_rank, mode, perks):
        self.owner_id = owner_id
        self.thread = thread
        self.max_players = max_players
        self.required_rank = required_rank
        self.mode = mode
        self.perks = perks
        self.joined_users = []
        self.locked = False
        self.view_message = None
        self.created_at = datetime.datetime.utcnow()
        self.view = None
        self.code = generate_league_code()   # ← unique code per lobby

    async def auto_close(self, guild, host_member):
        await asyncio.sleep(18000)  # 5 hours
        if self.owner_id in league_lobbies:
            try:
                logger.info(f"Auto-closing lobby thread for {self.owner_id}")
                await send_league_log(guild, self, host_member)
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
    league_cleanup.start()


@tasks.loop(minutes=30)
async def league_cleanup():
    logger.info("Running league cleanup task...")
    current_time = datetime.datetime.utcnow()
    to_remove = []
    for owner_id, lobby in league_lobbies.items():
        if (current_time - lobby.created_at).total_seconds() > 18000:
            to_remove.append(owner_id)
    for owner_id in to_remove:
        try:
            await league_lobbies[owner_id].thread.delete()
            del league_lobbies[owner_id]
            logger.info(f"Cleaned up expired lobby for {owner_id}")
        except Exception as e:
            logger.error(f"Cleanup error for {owner_id}: {e}")


@league_cleanup.before_loop
async def before_league_cleanup():
    await bot.wait_until_ready()


# ── /setchannel ──────────────────────────────────────────────────────────────

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
    mode="1s, 2s, 3s, or 4s",
    players="Number of players needed (host not counted)",
    perks="Perks on or off",
    rank="Required Rank (Any or R3–R10)"
)
@app_commands.choices(rank=[app_commands.Choice(name="Any", value="Any")] + [
    app_commands.Choice(name=f"R{i} - {rank_labels[f'R{i}'].split(' - ')[1]}", value=f"R{i}")
    for i in range(3, 11)
])
async def league(interaction: Interaction, mode: str, players: int, perks: str, rank: app_commands.Choice[str]):
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

    embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    embed.add_field(name="🎮 Mode", value=mode, inline=True)
    embed.add_field(name="👥 Players Needed", value=str(players), inline=True)
    embed.add_field(name="⚙️ Perks", value=perks, inline=True)
    embed.add_field(name="📊 Required Rank", value=required_rank, inline=True)
    embed.set_footer(text="Press Join to enter the league thread!")

    # ── Join button view ──────────────────────────────────────────────────────
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
            await join_interaction.response.send_message("You're in! Check the thread 👇", ephemeral=True)

            if len(lobby.joined_users) >= lobby.max_players:
                button.disabled = True
                if lobby.view_message:
                    await lobby.view_message.edit(view=self)

    view = LeagueJoinView()
    leagues_role = discord.utils.get(guild.roles, name="Leagues")
    mention_text = leagues_role.mention if leagues_role else "@Leagues"

    await interaction.response.send_message(
        content=mention_text,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    lobby_message = await interaction.original_response()

    thread = await lobby_message.create_thread(
        name=f"⚔️ {creator.name}'s League",
        auto_archive_duration=1440
    )
    await thread.add_user(creator)

    # Create lobby — now stores mode & perks too
    lobby = LeagueLobby(creator.id, thread, players, required_rank, mode, perks)
    lobby.view_message = lobby_message
    league_lobbies[creator.id] = lobby

    await thread.send(
        f"👋 Welcome, {creator.mention}!\n"
        f"**Host:** {creator.mention} (Rank: {user_rank_label})\n"
        f"**Mode:** {mode} | **Perks:** {perks} | **Rank:** {required_rank}\n"
        f"**League Code:** `{lobby.code}`\n"
        f"Players joining via the button above will appear here."
    )

    asyncio.create_task(lobby.auto_close(guild, creator))
    logger.info(f"League thread created for {creator} — {thread.mention} — Code: {lobby.code}")


# ── /closelobby ───────────────────────────────────────────────────────────────

@bot.tree.command(name="closelobby", description="Close your league lobby thread")
async def closelobby(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message("❌ You don't have an active league lobby.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    guild = interaction.guild
    host = interaction.user

    # Send log before deleting
    await send_league_log(guild, lobby, host)

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
    guild = interaction.guild
    host = interaction.user

    # Send log before deleting
    await send_league_log(guild, lobby, host)

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
            players = lobby.joined_users
            total_needed = team_size.value * 2
            if len(players) < total_needed:
                await interaction.response.send_message(
                    f"❌ Need {total_needed} players for {team_size.name}, have {len(players)}.",
                    ephemeral=True
                )
                return
            shuffled = players[:]
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
            "`/league` — Host a league (creates a thread automatically)\n"
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
 
