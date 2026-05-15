
from keep_alive import keep_alive
keep_alive()

import discord
from discord.ext import commands
from discord import app_commands, Interaction, ButtonStyle
from discord.ui import Button, View
import os
import asyncio
import random
from typing import List
import re
from PIL import Image
import pytesseract
import aiohttp
import io

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
lobby_data = {}
ALLOWED_CHANNEL_ID = None
END_CHANNEL_ID = None

# [... جميع أوامرك الأصلية هنا بدون أي تغيير ...]

# ===== الأمر /end المعدل فقط =====
@bot.tree.command(name="end", description="End the match by analyzing last 10 screenshots in temp channel")
async def end(interaction: Interaction):
    if interaction.user.id not in lobby_data:
        print("[END] User not hosting a league.")
        return await interaction.response.send_message("❌ You are not hosting a league.", ephemeral=True)

    await interaction.response.defer()
    print("[END] Processing /end command...")

    temp_channel = lobby_data[interaction.user.id]["channel"]

    messages = []
    async for msg in temp_channel.history(limit=50):
        messages.append(msg)

    print(f"[END] Fetched {len(messages)} messages from temp channel.")

    images_data = []
    texts = []

    for msg in messages:
        for att in msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                data = await att.read()
                image = Image.open(io.BytesIO(data))
                text = pytesseract.image_to_string(image)
                print(f"[END] OCR Text from image (first 100 chars): {text[:100]!r}")

                if re.search(r"\b(KD|Kills|Deaths|Win|Lose|Score|Player|Stats|Match|Victory|Defeat)\b", text, re.IGNORECASE):
                    images_data.append(data)
                    texts.append(text)
                    print(f"[END] Added image with valid match keywords. Total images collected: {len(images_data)}")

                if len(images_data) >= 10:
                    break
        if len(images_data) >= 10:
            break

    if not images_data:
        print("[END] No valid match result images found.")
        return await interaction.followup.send("❌ No valid match result images found in last 50 messages.")

    kd_data = []
    for t in texts:
        found_stats = re.findall(r"([A-Za-z0-9_]+)[^\S\r\n]*[:\-]?[^\S\r\n]*(\d+)\s*/\s*(\d+)", t)
        print(f"[END] Found player stats in text: {found_stats}")
        for u, k_str, d_str in found_stats:
            k, d = int(k_str), int(d_str)
            kd = round(k / d, 2) if d != 0 else float(k)
            kd_data.append((u, k, d, kd))

    if not kd_data:
        print("[END] No valid player stats detected in images.")
        return await interaction.followup.send("❌ No valid player stats detected in images.")

    kd_data.sort(key=lambda x: x[3], reverse=True)
    for idx, item in enumerate(kd_data):
        medal = "👑" if idx == 0 else "🥈" if idx == 1 else "🥉" if idx == 2 else ""
        kd_data[idx] = (*item, medal)

    half = len(kd_data) // 2
    summary = "🎮 Minion players who joined the league\n\n"
    summary += "**🏆 Win team:**\n" + "".join(f"{u}: {k}/{d} ({kd} KD) {m}\n" for u, k, d, kd, m in kd_data[:half])
    summary += "\n**💔 Lose team:**\n" + "".join(f"{u}: {k}/{d} ({kd} KD) {m}\n" for u, k, d, kd, m in kd_data[half:])

    chan = bot.get_channel(END_CHANNEL_ID)
    if chan:
        print("[END] Sending match summary and images to end channel.")
        await chan.send(summary)
        for i, data in enumerate(images_data, 1):
            file = discord.File(io.BytesIO(data), filename=f"result_{i}.png")
            await chan.send(file=file)
        print("[END] Sent all results.")
    else:
        print("[END] End channel not set or not found.")
        await interaction.followup.send("❌ End channel is not set or not found.")

# ===== تشغيل البوت =====
token = os.getenv("DISCORD_TOKEN")
if not token:
    print("❌ DISCORD_TOKEN not set!")
    exit()

bot.run(token)
