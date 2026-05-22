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
from PIL import Image, ImageEnhance
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


# ═══════════════════════════════════════════════════════════════
#  SCOREBOARD PARSER  —  pixel-row scan + column-aware OCR
# ═══════════════════════════════════════════════════════════════

def preprocess_for_ocr(img):
    """Upscale 3× and boost contrast for better Tesseract accuracy."""
    gray = img.convert('L')
    big  = gray.resize((gray.width * 3, gray.height * 3), Image.Resampling.LANCZOS)
    return ImageEnhance.Contrast(big).enhance(3.0)


def detect_player_rows_by_pixel(board_img):
    """
    Scan the board image row by row to find colored (green/red) player rows.
    Returns list of (y_center, team) for each detected player row.
    This works for BOTH English and Korean layouts since it doesn't rely
    on text headers — only on the green/red background color of rows.
    """
    bw, bh = board_img.size
    row_colors = []

    # Sample every pixel row, vote on its dominant color
    for y in range(bh):
        green = red = neutral = 0
        step = max(1, bw // 20)
        for x in range(int(bw * 0.02), int(bw * 0.70), step):
            try:
                r, g, b = board_img.getpixel((x, y))[:3]
            except Exception:
                continue
            if g > 80 and g > r + 12 and g > b + 12:
                green += 1
            elif r > 55 and r > g + 12:
                red += 1
            else:
                neutral += 1
        total = green + red + neutral
        if total == 0:
            row_colors.append('neutral')
        elif green > total * 0.25:
            row_colors.append('green')
        elif red > total * 0.25:
            row_colors.append('red')
        else:
            row_colors.append('neutral')

    # Merge consecutive same-color rows into bands → each band = one player row
    bands = []
    i = 0
    while i < len(row_colors):
        color = row_colors[i]
        if color == 'neutral':
            i += 1
            continue
        j = i
        while j < len(row_colors) and row_colors[j] == color:
            j += 1
        band_h = j - i
        # Ignore tiny bands (noise) — player rows are usually > 3% of board height
        if band_h >= max(3, int(bh * 0.03)):
            bands.append((i + band_h // 2, color))   # (y_center, team)
        i = j

    return bands


def clean_name(raw):
    """Remove non-ASCII, UI keywords, and illegal username chars."""
    text = re.sub(r'[^\x00-\x7F]+', '', raw)
    # Strip whole-word UI labels only — preserve single-char prefixes like 'x'
    text = re.sub(
        r'(?i)(^|\s)(name|device|ping|kd|kills|deaths|victory|defeat|'
        r'rewards|stats|results|omall|mmall|ammall|oomall|lts|ts)(\s|$)',
        ' ', text
    )
    text = re.sub(r'[^a-zA-Z0-9_\-]', '', text)
    return text.strip()


def ocr_row(board_img, y_center, row_half, bw):
    """
    OCR a single player row. Returns (player_name, kills, deaths) or None.
    Crops the row from the board, runs OCR, then uses X-coordinates to
    separate the name (left zone) from the KD score (right zone).
    """
    y1 = max(0, y_center - row_half)
    y2 = min(board_img.height, y_center + row_half)
    row_img = board_img.crop((0, y1, bw, y2))
    rw, rh  = row_img.size

    proc = preprocess_for_ocr(row_img)
    data = pytesseract.image_to_data(proc, output_type=pytesseract.Output.DICT)

    words = []
    for i, txt in enumerate(data['text']):
        txt = txt.strip()
        if not txt or data['conf'][i] < 10:
            continue
        x  = data['left'][i]  / 3
        ww = data['width'][i] / 3
        cx = x + ww / 2
        words.append({'text': txt, 'cx': cx})

    if not words:
        return None

    # KD score is in the rightmost ~30% of the row
    SCORE_X_MIN = rw * 0.68
    # Name is in the leftmost ~55%
    NAME_X_MAX  = rw * 0.55

    kd_re        = re.compile(r'^(\d{1,3})\s*[\/\|]\s*(\d{1,3})$')
    kd_token     = None
    has_dash     = False

    right_words = sorted([w for w in words if w['cx'] >= SCORE_X_MIN],
                         key=lambda w: w['cx'])

    # Pattern 1: "8/5" as one token
    for w in right_words:
        m = kd_re.match(w['text'])
        if m:
            kd_token = (int(m.group(1)), int(m.group(2)))
            break

    # Pattern 2: "8" "5" as two adjacent digit tokens
    if kd_token is None:
        for j in range(len(right_words) - 1):
            a, b_ = right_words[j], right_words[j + 1]
            if a['text'].isdigit() and b_['text'].isdigit():
                kd_token = (int(a['text']), int(b_['text']))
                break

    # Pattern 3: dash row "- / -"
    if kd_token is None:
        row_text = ' '.join(w['text'] for w in words)
        if re.search(r'-\s*[\/\|]\s*-', row_text):
            has_dash = True

    if has_dash or kd_token is None:
        return None   # no valid score → skip

    kills, deaths = kd_token

    # Name: left-most tokens in left zone
    name_words = sorted([w for w in words if w['cx'] < NAME_X_MAX],
                        key=lambda w: w['cx'])
    raw_name    = ' '.join(w['text'] for w in name_words)
    player_name = clean_name(raw_name)

    if not player_name or len(player_name) < 2:
        return None

    return (player_name, kills, deaths)


def parse_scoreboard(orig_img):
    """
    Main entry point. Returns list of dicts:
        { display_name, kills, deaths, team_type }

    Uses pixel-row scanning to find player rows — works for both
    English (VICTORY/DEFEAT with NAME header) and Korean (승리/패배
    with no text headers) scoreboards.
    """
    w, h = orig_img.size

    # ── Crop: remove outer UI chrome, keep the player table area ─────────
    # Use a generous vertical range — pixel scanner will ignore non-colored rows
    left   = int(w * 0.02)
    right  = int(w * 0.98)
    top    = int(h * 0.18)   # capture from just below the top of the card
    bottom = int(h * 0.95)

    board = orig_img.crop((left, top, right, bottom))
    bw, bh = board.size

    # ── Detect player row positions by color ──────────────────────────────
    bands = detect_player_rows_by_pixel(board)
    logger.info(f"Pixel scan found {len(bands)} colored bands")

    if not bands:
        return []

    # Estimate half-height of a typical row from band spacing or band height
    # Use 45% of average band-to-band gap, minimum 8px
    if len(bands) > 1:
        gaps = [bands[i+1][0] - bands[i][0] for i in range(len(bands)-1)]
        avg_gap = sum(gaps) / len(gaps)
        row_half = max(8, int(avg_gap * 0.45))
    else:
        row_half = max(8, int(bh * 0.07))

    players = []
    for (y_center, team) in bands:
        result = ocr_row(board, y_center, row_half, bw)
        if result is None:
            continue
        player_name, kills, deaths = result
        players.append({
            'display_name': player_name,
            'kills':        kills,
            'deaths':       deaths,
            'team_type':    team,
        })
        logger.info(f"  → {player_name} | {kills}/{deaths} | {team}")

    return players


def find_best_existing_key(name_lower, master_stats, threshold=3):
    """
    Fuzzy dedup: merge OCR variants of the same player name across rounds.
    Prefix match or edit-distance <= threshold → treat as same player.
    When merging, keep whichever team had MORE votes (majority wins).
    """
    def ed(a, b):
        if abs(len(a) - len(b)) > threshold:
            return threshold + 1
        dp = list(range(len(b) + 1))
        for ca in a:
            ndp = [dp[0] + 1]
            for j, cb in enumerate(b):
                ndp.append(min(dp[j] + (ca != cb), dp[j+1] + 1, ndp[-1] + 1))
            dp = ndp
        return dp[-1]

    for k in master_stats:
        if k.startswith(name_lower) or name_lower.startswith(k):
            return k
        if ed(k, name_lower) <= threshold:
            return k
    return None


# ═══════════════════════════════════════════════════════════════
#  on_message  —  drives the OCR pipeline
# ═══════════════════════════════════════════════════════════════

@bot.event
async def on_message(message):
    if message.author == bot.user or not message.guild:
        return

    if RESULTS_CHANNEL_ID and message.channel.id == RESULTS_CHANNEL_ID:
        valid_attachments = [
            a for a in message.attachments
            if any(a.filename.lower().endswith(ext)
                   for ext in ['png', 'jpg', 'jpeg', 'webp'])
        ]

        if not valid_attachments:
            await bot.process_commands(message)
            return

        round_count = len(valid_attachments)
        processing_msg = await message.reply(
            f"Processing leaderboard stats from {round_count} match screenshot(s)... 🔄"
        )

        # key -> { display_name, kills, deaths, green_votes, red_votes }
        master_stats = {}

        try:
            for attachment in valid_attachments:
                image_bytes = await attachment.read()
                orig_img    = Image.open(io.BytesIO(image_bytes)).convert('RGB')

                players = parse_scoreboard(orig_img)
                logger.info(f"[{attachment.filename}] detected {len(players)} players: "
                            f"{[p['display_name'] for p in players]}")

                for p in players:
                    key      = p['display_name'].lower()
                    best_key = find_best_existing_key(key, master_stats)

                    if best_key:
                        master_stats[best_key]['kills']  += p['kills']
                        master_stats[best_key]['deaths'] += p['deaths']
                        if p['team_type'] == 'green':
                            master_stats[best_key]['green_votes'] += 1
                        else:
                            master_stats[best_key]['red_votes'] += 1
                    else:
                        master_stats[key] = {
                            'display_name': p['display_name'],
                            'kills':        p['kills'],
                            'deaths':       p['deaths'],
                            'green_votes':  1 if p['team_type'] == 'green' else 0,
                            'red_votes':    1 if p['team_type'] == 'red'   else 0,
                        }

            # ── Build result embed ──────────────────────────────────────
            if master_stats:
                green_team, red_team = [], []

                for data in master_stats.values():
                    k    = data['kills']
                    d    = max(1, data['deaths'])
                    kdr  = round(k / d, 2)
                    team = 'green' if data['green_votes'] >= data['red_votes'] else 'red'
                    entry = {
                        'name':   data['display_name'],
                        'kills':  k,
                        'deaths': data['deaths'],
                        'kdr':    kdr,
                    }
                    if team == 'green':
                        green_team.append(entry)
                    else:
                        red_team.append(entry)

                green_team.sort(key=lambda x: (x['kills'], x['kdr']), reverse=True)
                red_team.sort(  key=lambda x: (x['kills'], x['kdr']), reverse=True)

                desc  = f"🏆 **Match Results (Cumulative Stats across {round_count} round(s))**\n\n"
                desc += "🟢 **Green Team:**\n"
                if green_team:
                    for i, p in enumerate(green_team):
                        medal = " 👑" if i == 0 else ""
                        desc += f"• **{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                else:
                    desc += "*No players detected*\n"

                desc += "\n🔴 **Red Team:**\n"
                if red_team:
                    for i, p in enumerate(red_team):
                        medal = " 🥈" if i == 0 else ""
                        desc += f"• **{p['name']}**: {p['kills']}/{p['deaths']} ({p['kdr']} KD){medal}\n"
                else:
                    desc += "*No players detected*\n"

                embed = discord.Embed(
                    description=desc,
                    color=discord.Color.from_rgb(46, 204, 113)
                )
                await processing_msg.edit(content=None, embed=embed)

            else:
                await processing_msg.edit(
                    content="❌ No player data detected. Make sure the Stats tab is selected and the screenshot is clear."
                )

        except Exception as e:
            logger.error(f"OCR failure: {e}", exc_info=True)
            await processing_msg.edit(
                content="⚠️ An unexpected internal parser error occurred."
            )
        return

    await bot.process_commands(message)


# ═══════════════════════════════════════════════════════════════
#  /setchannel
# ═══════════════════════════════════════════════════════════════

@bot.tree.command(name="setchannel",
                  description="Set target channel for league commands or match results output")
@app_commands.describe(type="Choose whether this channel is for hosting leagues or displaying match results")
@app_commands.choices(type=[
    app_commands.Choice(name="league",  value="league"),
    app_commands.Choice(name="results", value="results"),
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


# ═══════════════════════════════════════════════════════════════
#  /league
# ═══════════════════════════════════════════════════════════════

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
        app_commands.Choice(name="4s", value="4s"),
    ],
    gametype=[
        app_commands.Choice(name="War",   value="War"),
        app_commands.Choice(name="Swift", value="Swift"),
    ],
    perks=[
        app_commands.Choice(name="On",  value="on"),
        app_commands.Choice(name="Off", value="off"),
    ],
    rank=[app_commands.Choice(name="Any", value="Any")] + [
        app_commands.Choice(name=label, value=code)
        for code, label in rank_labels.items()
    ]
)
async def league(
    interaction: Interaction,
    mode:     app_commands.Choice[str],
    gametype: app_commands.Choice[str],
    perks:    app_commands.Choice[str],
    rank:     app_commands.Choice[str],
    link:     str
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

    required_rank         = rank.value
    user_rank, user_rank_label = get_user_rank(creator)
    guild                 = interaction.guild
    max_players           = MODE_PLAYER_COUNT[mode.value]

    channel_embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    channel_embed.add_field(name="🎮 Mode",           value=mode.value,       inline=True)
    channel_embed.add_field(name="⚔️ Game Type",      value=gametype.value,   inline=True)
    channel_embed.add_field(name="👥 Players Needed", value=str(max_players), inline=True)
    channel_embed.add_field(name="⚙️ Perks",          value=perks.value,      inline=True)
    channel_embed.add_field(name="📊 Required Rank",  value=required_rank,    inline=True)
    channel_embed.set_footer(text="Press Join to enter the league thread!")

    class LeagueJoinView(View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label="Join", style=ButtonStyle.blurple)
        async def join_button(self, join_interaction: Interaction, button: Button):
            user = join_interaction.user
            if lobby.locked:
                await join_interaction.response.send_message(
                    "❌ League is locked or cancelled.", ephemeral=True); return
            if user.id == lobby.owner_id:
                await join_interaction.response.send_message(
                    "❌ You are the host.", ephemeral=True); return
            if user.id in lobby.joined_users:
                await join_interaction.response.send_message(
                    "You're already in.", ephemeral=True); return
            if len(lobby.joined_users) >= lobby.max_players:
                await join_interaction.response.send_message(
                    "❌ Lobby is full.", ephemeral=True); return

            u_rank, u_rank_label = get_user_rank(user)
            if lobby.required_rank != "Any":
                required = int(lobby.required_rank[1:])
                if u_rank is None or u_rank < required:
                    await join_interaction.response.send_message(
                        f"❌ You need rank {lobby.required_rank} or higher.",
                        ephemeral=True); return

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

    view          = LeagueJoinView()
    leagues_role  = discord.utils.get(guild.roles, name="Leagues")
    mention_text  = leagues_role.mention if leagues_role else "@Leagues"

    await interaction.response.send_message(
        content=mention_text, embed=channel_embed, view=view,
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
    lobby.view_message    = lobby_message
    league_lobbies[creator.id] = lobby

    thread_embed = discord.Embed(
        title="🏆 ACL LEAGUE",
        description=f"**League created by:** {creator.mention} (Rank: {user_rank_label})",
        color=discord.Color.blue()
    )
    thread_embed.add_field(name="🎮 Mode",           value=mode.value,        inline=True)
    thread_embed.add_field(name="⚔️ Game Type",      value=gametype.value,    inline=True)
    thread_embed.add_field(name="👥 Players Needed", value=str(max_players),  inline=True)
    thread_embed.add_field(name="⚙️ Perks",          value=perks.value,       inline=True)
    thread_embed.add_field(name="📊 Required Rank",  value=required_rank,     inline=True)
    thread_embed.add_field(name="🔑 League Code",    value=f"`{lobby.code}`", inline=False)
    thread_embed.set_footer(text="ACL League")

    await thread.send(
        content=f"🔗 **Game Link:** [Click here to join the game]({link})",
        embed=thread_embed
    )
    await interaction.followup.send(
        f"✅ Your league thread: {thread.mention}", ephemeral=True
    )
    asyncio.create_task(lobby.auto_close(guild))


# ═══════════════════════════════════════════════════════════════
#  Management commands
# ═══════════════════════════════════════════════════════════════

@bot.tree.command(name="closelobby", description="Close your league lobby thread")
async def closelobby(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message(
            "❌ You don't have an active league lobby.", ephemeral=True); return
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
        await interaction.response.send_message(
            "❌ You have no league to cancel.", ephemeral=True); return
    lobby = league_lobbies[interaction.user.id]
    lobby.locked = True
    await send_league_log(interaction.guild, lobby, lobby.host_member)
    try:
        await lobby.thread.send("❌ League cancelled by the host.")
        await lobby.thread.delete()
    except Exception as e:
        logger.error(f"Error closing cancelled thread: {e}")
    del league_lobbies[interaction.user.id]
    await interaction.response.send_message(
        "League cancelled and thread locked.", ephemeral=True)


@bot.tree.command(name="leave", description="Leave the league lobby")
async def leave(interaction: Interaction):
    user = interaction.user
    for owner_id, lobby in league_lobbies.items():
        if user.id in lobby.joined_users:
            lobby.joined_users.remove(user.id)
            await lobby.thread.remove_user(user)
            await lobby.thread.send(f"{user.mention} has left the league ❌")
            await interaction.response.send_message(
                "You have left the league.", ephemeral=True); return
    await interaction.response.send_message(
        "❌ You're not in any league lobby.", ephemeral=True)


@bot.tree.command(name="status", description="Show who joined the league")
async def status(interaction: Interaction):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message(
            "❌ You're not hosting a league.", ephemeral=True); return
    lobby   = league_lobbies[interaction.user.id]
    guild   = interaction.guild
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
    app_commands.Choice(name="4v4", value=4),
])
async def team(interaction: Interaction, team_size: app_commands.Choice[int]):
    for owner_id, lobby in league_lobbies.items():
        if lobby.thread.id == interaction.channel.id:
            all_players  = [lobby.owner_id] + lobby.joined_users
            total_needed = team_size.value * 2
            if len(all_players) < total_needed:
                await interaction.response.send_message(
                    f"❌ Need {total_needed} players for {team_size.name}, "
                    f"have {len(all_players)}.", ephemeral=True); return
            shuffled = all_players[:]
            random.shuffle(shuffled)
            team_a = shuffled[:team_size.value]
            team_b = shuffled[team_size.value:total_needed]

            def mentions(ids):
                return "\n".join(
                    interaction.guild.get_member(uid).mention for uid in ids)

            msg = (
                f"**Generated {team_size.name} Teams:**\n\n"
                f"🔴 **Red Team:**\n{mentions(team_a)}\n\n"
                f"🔵 **Blue Team:**\n{mentions(team_b)}"
            )
            await interaction.response.send_message(msg); return
    await interaction.response.send_message(
        "❌ Use this command inside the league thread.", ephemeral=True)


@bot.tree.command(name="addleagueplayer", description="Add a player manually to your league")
@app_commands.describe(user="User to add")
async def addleagueplayer(interaction: Interaction, user: discord.Member):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message(
            "❌ You're not hosting a league.", ephemeral=True); return
    lobby = league_lobbies[interaction.user.id]
    if user.id in lobby.joined_users:
        return await interaction.response.send_message(
            "❌ User already in the league.", ephemeral=True)
    if len(lobby.joined_users) >= lobby.max_players:
        return await interaction.response.send_message(
            "❌ Lobby is full.", ephemeral=True)
    u_rank, u_label = get_user_rank(user)
    if lobby.required_rank != "Any" and (
            u_rank is None or u_rank < int(lobby.required_rank[1:])):
        return await interaction.response.send_message(
            "❌ User does not meet rank requirement.", ephemeral=True)
    await lobby.thread.add_user(user)
    lobby.joined_users.append(user.id)
    await lobby.thread.send(f"{user.mention} manually added ✅ (Rank: {u_label})")
    return await interaction.response.send_message("✅ User added.", ephemeral=True)


@bot.tree.command(name="kickleagueplayer", description="Kick a player from your league")
@app_commands.describe(user="User to kick")
async def kickleagueplayer(interaction: Interaction, user: discord.Member):
    if interaction.user.id not in league_lobbies:
        await interaction.response.send_message(
            "❌ You're not hosting a league.", ephemeral=True); return
    lobby = league_lobbies[interaction.user.id]
    if user.id not in lobby.joined_users:
        return await interaction.response.send_message(
            "❌ User is not in the league.", ephemeral=True)
    lobby.joined_users.remove(user.id)
    await lobby.thread.remove_user(user)
    await lobby.thread.send(f"{user.mention} was kicked ❌")
    return await interaction.response.send_message("✅ User kicked.", ephemeral=True)


@bot.tree.command(name="aclhelp", description="Show all ACL League Bot commands")
async def help_command(interaction: Interaction):
    embed = discord.Embed(title="📚 ACL League Bot — Commands",
                          color=discord.Color.gold())
    embed.add_field(
        name="📌 Setup",
        value="`/setchannel [type: league/results]` — Configure channel modes",
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


# ═══════════════════════════════════════════════════════════════
token = os.getenv("DISCORD_TOKEN")
if not token:
    logger.critical("❌ DISCORD_TOKEN not set!")
    exit()
bot.run(token)
 
