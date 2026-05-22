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
from PIL import Image, ImageOps, ImageEnhance
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

LEAGUE_CHANNEL_ID = None
RESULTS_CHANNEL_ID = None

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
        self.mode = mode
        self.gametype = gametype
        self.perks = perks
        self.link = link
        self.joined_users = []
        self.locked = False
        self.view_message = None
        self.created_at = datetime.datetime.utcnow()
        self.code = generate_league_code()

    async def auto_close(self, guild):
        await asyncio.sleep(18000)
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


# ── IMPROVED SCOREBOARD PARSER ──

def preprocess_img(img):
    gray = img.convert('L')
    resized = gray.resize((gray.width * 3, gray.height * 3), Image.Resampling.LANCZOS)
    enhanced = ImageEnhance.Contrast(resized).enhance(2.8)
    return enhanced


def get_row_team(row_strip):
    rw, rh = row_strip.size
    green_votes = 0
    red_votes = 0

    sample_y = rh // 2
    step = max(1, int(rw * 0.05))
    for sample_x in range(int(rw * 0.05), int(rw * 0.60), step):
        if sample_x >= rw:
            continue
        pixel = row_strip.getpixel((sample_x, sample_y))
        r, g, b = pixel[:3]

        if g > 75 and g > r + 15 and g > b + 15:
            green_votes += 1
        elif r > 65 and r > g + 12:
            red_votes += 1

    if green_votes == 0 and red_votes == 0:
        return None
    return "green" if green_votes >= red_votes else "red"


def clean_player_name(raw_text):
    """Improved name cleaning"""
    text = raw_text.strip()
    
    # Remove common UI noise
    text = re.sub(r'(?i)\b(name|device|ping|kd|kills|deaths|victory|defeat|rewards|stats|all|omall|lts|ts)\b', '', text)
    
    # Keep letters, numbers, underscores, hyphens, and Korean characters
    text = re.sub(r'[^a-zA-Z0-9_\-가-힣]', '', text)
    
    if len(text) < 2:
        return ""
        
    return text.strip()


def find_best_existing_key(name_lower, master_stats, threshold=3):
    def edit_distance(a, b):
        if abs(len(a) - len(b)) > threshold:
            return threshold + 1
        dp = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            new_dp = [i + 1]
            for j, cb in enumerate(b):
                new_dp.append(min(dp[j] + (ca != cb), dp[j + 1] + 1, new_dp[-1] + 1))
            dp = new_dp
        return dp[-1]

    for existing_key in master_stats:
        if existing_key.startswith(name_lower) or name_lower.startswith(existing_key):
            return existing_key
        if edit_distance(existing_key, name_lower) <= threshold:
            return existing_key
    return None


@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    if RESULTS_CHANNEL_ID and message.channel.id == RESULTS_CHANNEL_ID:
        if message.attachments:
            valid_attachments = [
                a for a in message.attachments
                if any(a.filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'webp'])
            ]

            if not valid_attachments:
                return

            round_count = len(valid_attachments)
            processing_msg = await message.reply(
                f"Processing leaderboard stats from {round_count} match screenshot(s)... 🔄"
            )

            master_stats = {}

            try:
                for attachment in valid_attachments:
                    image_bytes = await attachment.read()
                    orig_img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
                    w, h = orig_img.size

                    # Crop to leaderboard area
                    crop_left   = int(w * 0.02)
                    crop_right  = int(w * 0.98)
                    crop_top    = int(h * 0.20)
                    crop_bottom = int(h * 0.89)

                    board_img = orig_img.crop((crop_left, crop_top, crop_right, crop_bottom))
                    bw, bh = board_img.size

                    enhanced_board = preprocess_img(board_img)
                    hocr_data = pytesseract.image_to_data(
                        enhanced_board, output_type=pytesseract.Output.DICT, lang='eng'
                    )

                    row_centers = []
                    for idx, text in enumerate(hocr_data['text']):
                        if '/' in text or (text.isdigit() and int(text) < 100):
                            y_top    = hocr_data['top'][idx] / 3.0
                            y_height = hocr_data['height'][idx] / 3.0
                            center_y = y_top + (y_height / 2)

                            if bh * 0.07 < center_y < bh * 0.96:
                                if not any(abs(center_y - e) < (bh * 0.04) for e in row_centers):
                                    row_centers.append(center_y)

                    row_centers.sort()

                    # Fallback positions
                    if len(row_centers) < 3:
                        row_centers = [bh * 0.15, bh * 0.28, bh * 0.42, bh * 0.55, bh * 0.69, bh * 0.83, bh * 0.96]

                    for row_y in row_centers:
                        box_radius = int(bh * 0.06)
                        y1 = max(0, int(row_y - box_radius))
                        y2 = min(bh, int(row_y + box_radius))

                        row_strip = board_img.crop((0, y1, bw, y2))
                        rw, rh = row_strip.size

                        detected_team = get_row_team(row_strip)
                        if detected_team is None:
                            continue

                        # Score extraction
                        score_box  = row_strip.crop((int(rw * 0.68), 0, rw, rh))
                        prep_score = preprocess_img(score_box)
                        score_text = pytesseract.image_to_string(prep_score, config='--psm 6').strip()

                        score_match = re.search(r'(\d+)\s*[\/\|:.\s-]\s*(\d+)', score_text)
                        if not score_match:
                            continue

                        kills = int(score_match.group(1))
                        deaths = int(score_match.group(2))

                        # Name extraction - improved area
                        name_box = row_strip.crop((int(rw * 0.02), 0, int(rw * 0.48), rh))
                        prep_name = preprocess_img(name_box)
                        name_raw = pytesseract.image_to_string(prep_name, config='--psm 7').strip()

                        player_name = clean_player_name(name_raw)

                        if not player_name or len(player_name) < 2:
                            continue

                        # Fuzzy deduplication
                        lookup_key = player_name.lower()
                        existing = find_best_existing_key(lookup_key, master_stats)

                        if existing:
                            master_stats[existing]["kills"] += kills
                            master_stats[existing]["deaths"] += deaths
                        else:
                            master_stats[lookup_key] = {
                                "display_name": player_name,
                                "kills": kills,
                                "deaths": deaths,
                                "team_type": detected_team,
                            }

                # Build embed
                if master_stats:
                    green_team = []
                    red_team = []

                    for data in master_stats.values():
                        k = data["kills"]
                        d = max(1, data["deaths"])
                        kdr = round(k / d, 2)
                        payload = {
                            "name": data["display_name"],
                            "kills": k,
                            "deaths": data["deaths"],
                            "kdr": kdr,
                        }
                        if data["team_type"] == "green":
                            green_team.append(payload)
                        else:
                            red_team.append(payload)

                    green_team.sort(key=lambda x: (x['kills'], x['kdr']), reverse=True)
                    red_team.sort(key=lambda x: (x['kills'], x['kdr']), reverse=True)

                    embed_desc = f"🏆 **Match Results (Cumulative Stats across {round_count} round(s))**\n\n"

                    embed_desc += "🔵 **Green Team:**\n"
                    if green_team:
                        for idx, p in enumerate(green_team):
                            medal = " 👑" if idx == 0 else ""
                            embed_desc += f"• **{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                    else:
                        embed_desc += "*No players detected*\n"

                    embed_desc += "\n🔴 **Red Team:**\n"
                    if red_team:
                        for idx, p in enumerate(red_team):
                            medal = " 🥈" if idx == 0 else ""
                            embed_desc += f"• **{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                    else:
                        embed_desc += "*No players detected*\n"

                    embed = discord.Embed(
                        description=embed_desc,
                        color=discord.Color.blue()   # Changed to Blue
                    )
                    await processing_msg.edit(content=None, embed=embed)
                else:
                    await processing_msg.edit(
                        content="❌ Unable to extract stats. Try clearer screenshots."
                    )

            except Exception as e:
                logger.error(f"OCR execution failure: {e}")
                await processing_msg.edit(content="⚠️ Parser error occurred.")

            return

    await bot.process_commands(message)


# ── /setchannel COMMAND ──
@bot.tree.command(name="setchannel", description="Set target channel for league commands or match results output")
@app_commands.describe(type="Choose whether this channel is for hosting leagues or displaying match results")
@app_commands.choices(type=[
    app_commands.Choice(name="league", value="league"),
    app_commands.Choice(name="results", value="results")
])
async def setchannel(interaction: Interaction, type: app_commands.Choice[str]):
    global LEAGUE_CHANNEL_ID, RESULTS_CHANNEL_ID

    if type.value == "league":
        LEAGUE_CHANNEL_ID = interaction.channel.id
        await interaction.response.send_message(
            f"✅ {interaction.channel.mention} is now set as the **League Hosting** channel.",
            ephemeral=True
        )
    elif type.value == "results":
        RESULTS_CHANNEL_ID = interaction.channel.id
        await interaction.response.send_message(
            f"✅ {interaction.channel.mention} is now set as the **Match Results** channel.",
            ephemeral=True
        )


# ── /league COMMAND ──
@bot.tree.command(name="league", description="Create a league lobby")
@app_commands.describe(
    mode="2s, 3s, or 4s (auto-sets player count)",
    gametype="War or Swift",
    perks="Perks on or off",
    rank="Required Rank (Any or Custom Roles Setup)",
    link="Game link"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="2s", value="2s"),
        app_commands.Choice(name="3s", value="3s"),
        app_choices.Choice(name="4s", value="4s"),
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
        app_commands.Choice(name=label, value=code) for code, label in rank_labels.items()
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
    if LEAGUE_CHANNEL_ID and interaction.channel.id != LEAGUE_CHANNEL_ID:
        await interaction.response.send_message(
            "❌ This command can only be used in the designated League Hosting channel.",
            ephemeral=True
        )
        return

    creator = interaction.user
    if creator.id in league_lobbies:
        await interaction.response.send_message(
            "❌ You already have an active league lobby.", ephemeral=True
        )
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
            lobby = league_lobbies.get(lobby.owner_id)  # Fixed reference
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
                        f"❌ You need rank {lobby.required_rank} or higher.", ephemeral=True)
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


# ── MANAGEMENT COMMANDS ──

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


@bot.tree.command(name="aclhelp", description="Show all ACL League Bot commands")
async def help_command(interaction: Interaction):
    embed = discord.Embed(
        title="📚 ACL League Bot — Commands",
        color=discord.Color.gold()
    )
    embed.add_field(name="📌 Setup", value="`/setchannel [type: league/results]` — Configure channel modes", inline=False)
    embed.add_field(
        name="🎮 League Commands",
        value=(
            "`/league` — Host a league\n"
            "`/closelobby` — Close your league\n"
            "`/cancelled` — Cancel league\n"
            "`/leave` — Leave league\n"
            "`/status` — Show joined players\n"
            "`/team` — Generate teams\n"
            "`/addleagueplayer` — Add player\n"
            "`/kickleagueplayer` — Kick player"
        ),
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.critical("❌ DISCORD_TOKEN not set!")
    exit()
bot.run(token) 
