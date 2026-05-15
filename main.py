from keep_alive import keep_alive
keep_alive()

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction, ButtonStyle
from discord.ui import Button, View
import os
import asyncio
import random
import re
from PIL import Image
import pytesseract
import datetime
import logging
import json
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ACLBot')

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
league_lobbies = {}
ALLOWED_CHANNEL_ID = None  # For league commands and results

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

class LeagueLobby:
    def __init__(self, owner_id, temp_channel, max_players, required_rank):
        self.owner_id = owner_id
        self.temp_channel = temp_channel
        self.max_players = max_players
        self.required_rank = required_rank
        self.joined_users = []
        self.locked = False
        self.message = None
        self.created_at = datetime.datetime.utcnow()
        self.view = None

    async def auto_close(self):
        await asyncio.sleep(18000)  # 5 hours
        if self.owner_id in league_lobbies:
            try:
                logger.info(f"Auto-closing lobby for {self.owner_id}")
                await self.temp_channel.delete()
            except discord.NotFound:
                logger.warning("Channel already deleted during auto-close")
            except Exception as e:
                logger.error(f"Error deleting channel during auto-close: {str(e)}")
            if self.owner_id in league_lobbies:
                del league_lobbies[self.owner_id]
                logger.info(f"Removed lobby for {self.owner_id} from tracking")

@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("------")
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
        lobby_age = current_time - lobby.created_at
        if lobby_age.total_seconds() > 18000:  # 5 hours
            to_remove.append(owner_id)
    
    for owner_id in to_remove:
        try:
            logger.info(f"Cleaning up expired lobby for {owner_id}")
            await league_lobbies[owner_id].temp_channel.delete()
            del league_lobbies[owner_id]
        except Exception as e:
            logger.error(f"Cleanup error for {owner_id}: {str(e)}")

@league_cleanup.before_loop
async def before_league_cleanup():
    logger.info("Waiting for bot to be ready before starting cleanup task...")
    await bot.wait_until_ready()

@bot.tree.command(name="setchannel", description="Set current channel for league commands and results")
async def setchannel(interaction: Interaction):
    global ALLOWED_CHANNEL_ID
    logger.info(f"Setchannel command by {interaction.user} in #{interaction.channel}")
    if ALLOWED_CHANNEL_ID is not None:
        old_channel = bot.get_channel(ALLOWED_CHANNEL_ID)
        channel_name = old_channel.mention if old_channel else f"ID: {ALLOWED_CHANNEL_ID}"
        await interaction.response.send_message(f"❌ Channel is already set to {channel_name}.", ephemeral=True)
        return
    ALLOWED_CHANNEL_ID = interaction.channel.id
    logger.info(f"Set allowed channel to #{interaction.channel} (ID: {interaction.channel.id})")
    await interaction.response.send_message(
        f"✅ This channel ({interaction.channel.mention}) is now set for league commands and match results.",
        ephemeral=True
    )

@bot.tree.command(name="league", description="Create a league lobby")
@app_commands.describe(
    mode="1s, 2s, 3s, or 4s",
    players="Number of players needed (host not counted)",
    perks="Perks on or off",
    rank="Required Rank (Any or R3–R10)"
)
@app_commands.choices(rank=[app_commands.Choice(name="Any", value="Any")] + [
    app_commands.Choice(name=f"R{i} - {rank_labels[f'R{i}'].split(' - ')[1]}", value=f"R{i}") for i in range(3, 11)
])
async def league(interaction: Interaction, mode: str, players: int, perks: str, rank: app_commands.Choice[str]):
    logger.info(f"League command by {interaction.user} with params: mode={mode}, players={players}, perks={perks}, rank={rank.value}")
    if ALLOWED_CHANNEL_ID and interaction.channel.id != ALLOWED_CHANNEL_ID:
        logger.warning(f"Command used in wrong channel: #{interaction.channel} (Allowed: {ALLOWED_CHANNEL_ID})")
        await interaction.response.send_message("❌ Use commands in the set channel only.", ephemeral=True)
        return

    creator = interaction.user
    if creator.id in league_lobbies:
        logger.warning(f"{creator} already has an active lobby")
        await interaction.response.send_message("❌ You already have an active league lobby.", ephemeral=True)
        return

    guild = interaction.guild
    required_rank = rank.value
    user_rank, user_rank_label = get_user_rank(creator)

    category = discord.utils.get(guild.categories, name="League Lobbies")
    if not category:
        logger.info("Creating League Lobbies category")
        category = await guild.create_category("League Lobbies")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        creator: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }

    temp_channel = await category.create_text_channel(f"{creator.name}-league", overwrites=overwrites)
    logger.info(f"Created league channel: #{temp_channel.name}")

    embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    embed.add_field(name="🎮 Mode", value=mode, inline=True)
    embed.add_field(name="👥 Players Needed", value=str(players), inline=True)
    embed.add_field(name="⚙️ Perks", value=perks, inline=True)
    embed.add_field(name="📊 Required Rank", value=required_rank, inline=True)
    embed.set_footer(text="Join by pressing the Join button below!")

    class LeagueJoinView(View):
        def __init__(self):
            super().__init__(timeout=None)
        
        @discord.ui.button(label="Join", style=ButtonStyle.blurple)
        async def join_button(self, join_interaction: Interaction, button: Button):
            logger.info(f"Join button pressed by {join_interaction.user}")
            user = join_interaction.user
            if lobby.locked:
                logger.info("Join attempt on locked lobby")
                await join_interaction.response.send_message("❌ League is locked or cancelled.", ephemeral=True)
                return
            if user.id == lobby.owner_id:
                logger.info("Host tried to join own lobby")
                await join_interaction.response.send_message("❌ You are the host and cannot join your own league.", ephemeral=True)
                return
            if user.id in lobby.joined_users:
                logger.info("User already in lobby")
                await join_interaction.response.send_message("You're already in.", ephemeral=True)
                return
            if len(lobby.joined_users) >= lobby.max_players:
                logger.info("Lobby full")
                await join_interaction.response.send_message("❌ Lobby is full.", ephemeral=True)
                return

            user_rank, user_rank_label = get_user_rank(user)
            if lobby.required_rank != "Any":
                required = int(lobby.required_rank[1:])
                if user_rank is None or user_rank < required:
                    logger.info(f"User {user} rank insufficient (has {user_rank}, needs {required})")
                    await join_interaction.response.send_message(f"❌ You need rank {lobby.required_rank} or higher.", ephemeral=True)
                    return

            await lobby.temp_channel.set_permissions(user, read_messages=True, send_messages=True)
            lobby.joined_users.append(user.id)
            logger.info(f"Added {user} to lobby")
            await lobby.temp_channel.send(f"{user.mention} joined ✅ (Rank: {user_rank_label})")
            await join_interaction.response.send_message("You're in!", ephemeral=True)

            if len(lobby.joined_users) >= lobby.max_players:
                logger.info("Lobby now full, disabling join button")
                button.disabled = True
                if lobby.view:
                    await lobby.view.edit(view=self)

    view = LeagueJoinView()
    leagues_role = discord.utils.get(guild.roles, name="Leagues")
    mention_text = leagues_role.mention if leagues_role else "@Leagues"
    await interaction.response.send_message(
        content=mention_text,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
    view_message = await interaction.original_response()
    
    lobby = LeagueLobby(creator.id, temp_channel, players, required_rank)
    lobby.view = view_message
    league_lobbies[creator.id] = lobby
    
    await temp_channel.send(embed=embed)
    await temp_channel.send(f"**Host:** {creator.mention} (Rank: {user_rank_label})")
    
    # Start auto-close task for this league lobby
    asyncio.create_task(lobby.auto_close())
    logger.info(f"League lobby created for {creator} with {players} player slots")

@bot.tree.command(name="closelobby", description="Close your league lobby")
async def closelobby(interaction: Interaction):
    logger.info(f"Closelobby command by {interaction.user}")
    if interaction.user.id not in league_lobbies:
        logger.warning(f"{interaction.user} has no lobby to close")
        await interaction.response.send_message("❌ You don't have a league lobby.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    try:
        logger.info(f"Deleting lobby channel: #{lobby.temp_channel.name}")
        await lobby.temp_channel.delete()
    except Exception as e:
        logger.error(f"Error deleting channel: {str(e)}")
    if interaction.user.id in league_lobbies:
        del league_lobbies[interaction.user.id]
        logger.info("Lobby removed from tracking")
    await interaction.response.send_message("✅ League lobby closed.", ephemeral=True)

@bot.tree.command(name="cancelled", description="Cancel the league")
async def cancelled(interaction: Interaction):
    logger.info(f"Cancelled command by {interaction.user}")
    if interaction.user.id not in league_lobbies:
        logger.warning(f"{interaction.user} has no lobby to cancel")
        await interaction.response.send_message("❌ You have no league to cancel.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    lobby.locked = True
    await lobby.temp_channel.send("❌ League cancelled by the host.")
    try:
        logger.info(f"Cancelling lobby for {interaction.user}")
        await lobby.temp_channel.delete()
    except Exception as e:
        logger.error(f"Error deleting cancelled channel: {str(e)}")
    del league_lobbies[interaction.user.id]
    await interaction.response.send_message("League cancelled and chat closed.", ephemeral=True)

@bot.tree.command(name="leave", description="Leave the league lobby")
async def leave(interaction: Interaction):
    logger.info(f"Leave command by {interaction.user}")
    user = interaction.user
    for owner_id, lobby in league_lobbies.items():
        if user.id in lobby.joined_users:
            logger.info(f"Removing {user} from lobby")
            lobby.joined_users.remove(user.id)
            await lobby.temp_channel.set_permissions(user, overwrite=None)
            await lobby.temp_channel.send(f"{user.mention} has left the league ❌")
            await interaction.response.send_message("You have left the league.", ephemeral=True)
            return
    logger.info(f"{user} not in any lobby")
    await interaction.response.send_message("❌ You're not in any league lobby.", ephemeral=True)

@bot.tree.command(name="status", description="Show who joined the league")
async def status(interaction: Interaction):
    logger.info(f"Status command by {interaction.user}")
    if interaction.user.id not in league_lobbies:
        logger.warning(f"{interaction.user} not hosting a lobby")
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
        "**Joined Players:**\n" + ("\n".join(members) if members else "No players have joined yet."),
        ephemeral=False
    )

@bot.tree.command(name="team", description="Generate random teams from joined players")
@app_commands.describe(team_size="Team size (2v2, 3v3, 4v4)")
@app_commands.choices(team_size=[
    app_commands.Choice(name="2v2", value=2),
    app_commands.Choice(name="3v3", value=3),
    app_commands.Choice(name="4v4", value=4)
])
async def team(interaction: Interaction, team_size: app_commands.Choice[int]):
    logger.info(f"Team command by {interaction.user} with size {team_size.name}")
    for owner_id, lobby in league_lobbies.items():
        if lobby.temp_channel.id == interaction.channel.id:
            players = lobby.joined_users
            total_needed = team_size.value * 2
            
            if len(players) < total_needed:
                logger.info(f"Not enough players for {team_size.name} (needed: {total_needed}, have: {len(players)})")
                await interaction.response.send_message(
                    f"❌ Not enough players for {team_size.name}. Need {total_needed}, have {len(players)}.",
                    ephemeral=True
                )
                return
                
            # Shuffle and create teams
            random.shuffle(players)
            teams = []
            for i in range(0, total_needed, team_size.value):
                teams.append(players[i:i + team_size.value])
            
            # Format team message
            msg = f"**Generated {team_size.name} Teams:**\n"
            team_names = ["🔴 Red Team", "🔵 Blue Team"]
            for i, team in enumerate(teams):
                members = [interaction.guild.get_member(uid).mention for uid in team]
                msg += f"\n{team_names[i]}:\n" + "\n".join(members)
            
            logger.info(f"Generated teams: {msg}")
            await interaction.response.send_message(msg, ephemeral=False)
            return
            
    logger.warning("Team command used outside league channel")
    await interaction.response.send_message("❌ Use this in a league channel", ephemeral=True)

@bot.tree.command(name="addleagueplayer", description="Add a player manually to your league")
@app_commands.describe(user="User to add")
async def addleagueplayer(interaction: Interaction, user: discord.Member):
    logger.info(f"Addleagueplayer command by {interaction.user} for {user}")
    if interaction.user.id not in league_lobbies:
        logger.warning(f"{interaction.user} not hosting a lobby")
        await interaction.response.send_message("❌ You're not hosting a league.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    if user.id in lobby.joined_users:
        logger.info(f"{user} already in lobby")
        return await interaction.response.send_message("❌ User already in the league.", ephemeral=True)
    if len(lobby.joined_users) >= lobby.max_players:
        logger.info("Lobby full")
        return await interaction.response.send_message("❌ Lobby is full.", ephemeral=True)
    u_rank, u_label = get_user_rank(user)
    if lobby.required_rank != "Any" and (u_rank is None or u_rank < int(lobby.required_rank[1:])):
        logger.info(f"{user} rank insufficient (has {u_rank}, needs {int(lobby.required_rank[1:])})")
        return await interaction.response.send_message("❌ User does not meet rank requirement.", ephemeral=True)
    await lobby.temp_channel.set_permissions(user, read_messages=True, send_messages=True)
    lobby.joined_users.append(user.id)
    logger.info(f"Manually added {user} to lobby")
    await lobby.temp_channel.send(f"{user.mention} manually added ✅ (Rank: {u_label})")
    return await interaction.response.send_message("✅ User added.", ephemeral=True)

@bot.tree.command(name="kickleagueplayer", description="Kick a player manually from your league")
@app_commands.describe(user="User to kick")
async def kickleagueplayer(interaction: Interaction, user: discord.Member):
    logger.info(f"Kickleagueplayer command by {interaction.user} for {user}")
    if interaction.user.id not in league_lobbies:
        logger.warning(f"{interaction.user} not hosting a lobby")
        await interaction.response.send_message("❌ You're not hosting a league.", ephemeral=True)
        return
    lobby = league_lobbies[interaction.user.id]
    if user.id not in lobby.joined_users:
        logger.info(f"{user} not in lobby")
        return await interaction.response.send_message("❌ User is not in the league.", ephemeral=True)
    lobby.joined_users.remove(user.id)
    await lobby.temp_channel.set_permissions(user, overwrite=None)
    logger.info(f"Kicked {user} from lobby")
    await lobby.temp_channel.send(f"{user.mention} was kicked ❌")
    return await interaction.response.send_message("✅ User kicked.", ephemeral=True)

@bot.tree.command(name="help", description="Show all command instructions")
async def help_command(interaction: Interaction):
    logger.info(f"Help command by {interaction.user}")
    msg = """
📚 ACL League Bot Help

📌 Setup:
/setchannel - Set channel for league commands and results

🎮 League Commands:
/league - Create a league lobby
/closelobby - Close your league lobby
/cancelled - Cancel the league
/leave - Leave a league lobby
/status - Show joined players
/team - Generate random teams (2v2/3v3/4v4)
/addleagueplayer - Add a player to your league
/kickleagueplayer - Kick a player from your league
"""

token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.critical("❌ DISCORD_TOKEN not set!")
    exit()
bot.run(token) 
