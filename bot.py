import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import io
import os
from dotenv import load_dotenv
load_dotenv()
import time

# =============================================
# CONFIG — fill these in
# =============================================
TOKEN = os.environ.get("TOKEN")                  # <-- your bot token
SERVER_ID = 1499364118141075557            # <-- your server ID
TRADE_CATEGORY_NAME = "Trades"             # <-- category name for trade channels
REPORT_CHANNEL_ID = 1499364119097245740     # <-- channel ID where reports are sent (admin channel)
# =============================================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

trades = {}
user_to_trade = {}
channel_to_trade = {}


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


async def get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    for cat in guild.categories:
        if cat.name == TRADE_CATEGORY_NAME:
            return cat
    return await guild.create_category(TRADE_CATEGORY_NAME)


async def cleanup_trade(trade_id: str):
    trade = trades.get(trade_id)
    if not trade:
        return
    user_to_trade.pop(trade.get("user1_id"), None)
    user_to_trade.pop(trade.get("user2_id"), None)
    ch = trade.get("channel")
    if ch:
        channel_to_trade.pop(ch.id, None)
    trades.pop(trade_id, None)


# ─────────────────────────────────────────────
# VIEW: Trade request (accept / decline) — sent via DM
# ─────────────────────────────────────────────
class TradeRequestView(discord.ui.View):
    def __init__(self, initiator: discord.Member, target: discord.Member, guild: discord.Guild):
        super().__init__(timeout=120)
        self.initiator = initiator
        self.target = target
        self.guild = guild

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This request is not for you.", ephemeral=True)
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="You accepted the trade request. Setting up the channel...", view=self)

        # Notify initiator
        try:
            await self.initiator.send(f"**{self.target.display_name}** accepted your trade request. Setting up the channel...")
        except:
            pass

        await create_trade_channel(self.initiator, self.target, self.guild)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("This request is not for you.", ephemeral=True)
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="You declined the trade request.", view=self)

        try:
            await self.initiator.send(f"**{self.target.display_name}** declined your trade request.")
        except:
            pass


# ─────────────────────────────────────────────
# VIEW: Confirm trade buttons
# ─────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, trade_id: str):
        super().__init__(timeout=600)
        self.trade_id = trade_id

    @discord.ui.button(label="Confirm trade", style=discord.ButtonStyle.green, custom_id="confirm_trade")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = trades.get(self.trade_id)
        if not trade:
            await interaction.response.send_message("Trade not found.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid not in [trade["user1_id"], trade["user2_id"]]:
            await interaction.response.send_message("You are not part of this trade.", ephemeral=True)
            return

        if trade["confirmed"].get(uid):
            await interaction.response.send_message("You already confirmed.", ephemeral=True)
            return

        trade["confirmed"][uid] = True
        confirmed_count = sum(trade["confirmed"].values())

        await interaction.response.send_message(
            f"**{interaction.user.display_name}** confirmed the trade. ({confirmed_count}/2)",
            ephemeral=False
        )

        if all(trade["confirmed"].values()):
            await complete_trade(self.trade_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, custom_id="cancel_trade")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        trade = trades.get(self.trade_id)
        if not trade:
            await interaction.response.send_message("Trade already finished.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid not in [trade["user1_id"], trade["user2_id"]]:
            await interaction.response.send_message("You are not part of this trade.", ephemeral=True)
            return

        ch: discord.TextChannel = trade["channel"]
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** cancelled the trade."
        )
        await cleanup_trade(self.trade_id)
        await ch.send("Trade has been cancelled. This channel is now closed.")


# ─────────────────────────────────────────────
# VIEW: Report modal trigger
# ─────────────────────────────────────────────
class ReportReasonSelect(discord.ui.Select):
    def __init__(self, reported_user: discord.Member, reporter: discord.Member, trade_id: str):
        self.reported_user = reported_user
        self.reporter = reporter
        self.trade_id = trade_id
        options = [
            discord.SelectOption(label="File is not working", value="not_working"),
            discord.SelectOption(label="File contains a different build", value="wrong_build"),
            discord.SelectOption(label="Other reason", value="other"),
        ]
        super().__init__(placeholder="Select a reason...", options=options)

    async def callback(self, interaction: discord.Interaction):
        reason = self.values[0]
        modal = ReportDetailModal(self.reported_user, self.reporter, reason, self.trade_id)
        await interaction.response.send_modal(modal)


class ReportView(discord.ui.View):
    def __init__(self, reported_user: discord.Member, reporter: discord.Member, trade_id: str):
        super().__init__(timeout=300)
        self.add_item(ReportReasonSelect(reported_user, reporter, trade_id))


# ─────────────────────────────────────────────
# MODAL: Report detail
# ─────────────────────────────────────────────
class ReportDetailModal(discord.ui.Modal, title="Report a player"):
    def __init__(self, reported_user: discord.Member, reporter: discord.Member, reason: str, trade_id: str):
        super().__init__()
        self.reported_user = reported_user
        self.reporter = reporter
        self.reason = reason
        self.trade_id = trade_id

    details = discord.ui.TextInput(
        label="Describe the issue in detail",
        style=discord.TextStyle.paragraph,
        placeholder="Explain what happened...",
        required=True,
        max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        reason_labels = {
            "not_working": "File is not working",
            "wrong_build": "File contains a different build",
            "other": "Other reason",
        }

        report_channel = bot.get_channel(REPORT_CHANNEL_ID)
        if not report_channel:
            await interaction.response.send_message("Could not find the report channel. Contact an admin.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Trade Report",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Reporter", value=f"{self.reporter.mention} ({self.reporter.id})", inline=True)
        embed.add_field(name="Reported player", value=f"{self.reported_user.mention} ({self.reported_user.id})", inline=True)
        embed.add_field(name="Reason", value=reason_labels.get(self.reason, self.reason), inline=False)
        embed.add_field(name="Details", value=self.details.value, inline=False)

        trade = trades.get(self.trade_id)
        if trade:
            ch = trade.get("channel")
            if ch:
                embed.add_field(name="Trade channel", value=ch.mention, inline=False)

        await report_channel.send(embed=embed)
        await interaction.response.send_message("Your report has been submitted to the admins.", ephemeral=True)


# ─────────────────────────────────────────────
# VIEW: Post-trade actions (report button)
# ─────────────────────────────────────────────
class PostTradeView(discord.ui.View):
    def __init__(self, trade_id: str, user1: discord.Member, user2: discord.Member):
        super().__init__(timeout=None)
        self.trade_id = trade_id
        self.user1 = user1
        self.user2 = user2

    @discord.ui.button(label="Report a player", style=discord.ButtonStyle.danger)
    async def report(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid == self.user1.id:
            reported = self.user2
        elif uid == self.user2.id:
            reported = self.user1
        else:
            await interaction.response.send_message("You are not part of this trade.", ephemeral=True)
            return

        view = ReportView(reported, interaction.user, self.trade_id)
        await interaction.response.send_message(
            f"You are reporting **{reported.display_name}**. Select a reason:",
            view=view,
            ephemeral=True
        )


# ─────────────────────────────────────────────
# CREATE TRADE CHANNEL
# ─────────────────────────────────────────────
async def create_trade_channel(initiator: discord.Member, target: discord.Member, guild: discord.Guild):
    if initiator.id in user_to_trade or target.id in user_to_trade:
        return

    category = await get_or_create_category(guild)
    trade_id = f"{initiator.id}_{target.id}_{int(time.time())}"
    channel_name = f"trade-{initiator.display_name[:10]}-{target.display_name[:10]}".lower().replace(" ", "-")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        initiator: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, read_message_history=True),
        target: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, manage_channels=True, read_message_history=True),
    }

    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        topic=f"Trade between {initiator.display_name} and {target.display_name}"
    )

    trades[trade_id] = {
        "user1_id": initiator.id,
        "user2_id": target.id,
        "file1": None,
        "file2": None,
        "confirmed": {initiator.id: False, target.id: False},
        "channel": channel,
        "guild_id": guild.id,
    }
    user_to_trade[initiator.id] = trade_id
    user_to_trade[target.id] = trade_id
    channel_to_trade[channel.id] = trade_id

    embed_welcome = discord.Embed(
        title="Build File Trade",
        color=discord.Color.blurple()
    )
    embed_welcome.add_field(name="Participants", value=f"{initiator.mention}\n{target.mention}", inline=False)
    embed_welcome.add_field(
        name="How it works",
        value=(
            "1. Send your .build file to the bot via DM\n"
            "2. The bot notifies both players when both files are uploaded\n"
            "3. Both players click Confirm — files are exchanged via DM\n"
            "4. After the trade you can report the other player if needed"
        ),
        inline=False
    )
    embed_welcome.add_field(
        name="Important",
        value="Your file is only visible to the bot. The other player only sees the file name and size.",
        inline=False
    )

    for member, partner in [(initiator, target), (target, initiator)]:
        try:
            await member.send(
                f"Your trade channel with **{partner.display_name}** is ready: {channel.mention}\n"
                f"Send your `.build` file here in DM to upload it privately."
            )
        except:
            pass

    await channel.send(
        content=f"{initiator.mention} {target.mention} — your trade channel is ready. Check your DMs for upload instructions.",
        embed=embed_welcome
    )


# ─────────────────────────────────────────────
# COMPLETE TRADE
# ─────────────────────────────────────────────
async def complete_trade(trade_id: str):
    trade = trades.get(trade_id)
    if not trade:
        return

    ch: discord.TextChannel = trade["channel"]
    guild: discord.Guild = ch.guild

    user1 = guild.get_member(trade["user1_id"])
    user2 = guild.get_member(trade["user2_id"])
    f1 = trade["file1"]
    f2 = trade["file2"]

    errors = []

    try:
        bytes1 = await f1["attachment"].read()
        await user2.send(
            f"Trade complete. Here is the file from **{user1.display_name}**:",
            file=discord.File(fp=io.BytesIO(bytes1), filename=f1["name"])
        )
    except Exception as e:
        errors.append(f"File from {user1.display_name}: {e}")

    try:
        bytes2 = await f2["attachment"].read()
        await user1.send(
            f"Trade complete. Here is the file from **{user2.display_name}**:",
            file=discord.File(fp=io.BytesIO(bytes2), filename=f2["name"])
        )
    except Exception as e:
        errors.append(f"File from {user2.display_name}: {e}")

    embed_done = discord.Embed(
        title="Trade complete",
        description="Files have been sent to both players via DM.",
        color=discord.Color.green()
    )
    embed_done.add_field(name=f"File from {user1.display_name}", value=f"`{f1['name']}` - {format_size(f1['size'])}", inline=False)
    embed_done.add_field(name=f"File from {user2.display_name}", value=f"`{f2['name']}` - {format_size(f2['size'])}", inline=False)

    if errors:
        embed_done.add_field(name="Errors", value="\n".join(errors), inline=False)

    post_view = PostTradeView(trade_id, user1, user2)

    await ch.send(
        content="If you have an issue with the received file, use the button below to report the other player.",
        embed=embed_done,
        view=post_view
    )

    # Remove from active trades but keep channel
    user_to_trade.pop(trade["user1_id"], None)
    user_to_trade.pop(trade["user2_id"], None)
    trades.pop(trade_id, None)


# ─────────────────────────────────────────────
# SLASH COMMAND /trade
# ─────────────────────────────────────────────
@bot.tree.command(name="trade", description="Send a trade request to another player", guild=discord.Object(id=SERVER_ID))
@app_commands.describe(user="The player you want to trade with")
async def trade_cmd(interaction: discord.Interaction, user: discord.Member):
    initiator = interaction.user
    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    if user.bot:
        await interaction.response.send_message("You cannot trade with a bot.", ephemeral=True)
        return
    if user.id == initiator.id:
        await interaction.response.send_message("You cannot trade with yourself.", ephemeral=True)
        return
    if initiator.id in user_to_trade:
        await interaction.response.send_message("You already have an active trade.", ephemeral=True)
        return
    if user.id in user_to_trade:
        await interaction.response.send_message(f"**{user.display_name}** is already in another trade.", ephemeral=True)
        return

    # Send trade request via DM to target
    try:
        view = TradeRequestView(initiator, user, guild)
        await user.send(
            f"**{initiator.display_name}** wants to trade .build files with you.",
            view=view
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"Could not send a DM to **{user.display_name}**. They may have DMs disabled.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Trade request sent to **{user.display_name}**. Waiting for them to accept.",
        ephemeral=True
    )


# ─────────────────────────────────────────────
# HANDLE DM MESSAGES — file uploads
# ─────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if not isinstance(message.channel, discord.DMChannel):
        await bot.process_commands(message)
        return

    uid = message.author.id

    if uid not in user_to_trade:
        return

    trade_id = user_to_trade[uid]
    trade = trades.get(trade_id)
    if not trade:
        return

    if not message.attachments:
        return

    slot = "file1" if uid == trade["user1_id"] else "file2"

    if trade[slot] is not None:
        await message.channel.send("You already uploaded your file. Waiting for the other player.")
        return

    attachment = message.attachments[0]

    trade[slot] = {
        "name": attachment.filename,
        "size": attachment.size,
        "attachment": attachment,
    }

    size_str = format_size(attachment.size)

    embed_sender = discord.Embed(title="File uploaded", color=discord.Color.blue())
    embed_sender.add_field(name="File name", value=f"`{attachment.filename}`", inline=True)
    embed_sender.add_field(name="Size", value=size_str, inline=True)
    embed_sender.set_footer(text="Waiting for the other player to upload their file...")
    await message.channel.send(embed=embed_sender)

    ch: discord.TextChannel = trade["channel"]
    embed_channel = discord.Embed(title="File uploaded", color=discord.Color.blue())
    embed_channel.add_field(name="Player", value=message.author.display_name, inline=True)
    embed_channel.add_field(name="File name", value=f"`{attachment.filename}`", inline=True)
    embed_channel.add_field(name="Size", value=size_str, inline=True)
    embed_channel.set_footer(text="File is hidden until both players confirm the trade.")
    try:
        await ch.send(embed=embed_channel)
    except:
        pass

    other_id = trade["user2_id"] if uid == trade["user1_id"] else trade["user1_id"]
    try:
        guild = bot.get_guild(trade["guild_id"])
        other_member = guild.get_member(other_id)
        embed_notify = discord.Embed(title="Partner uploaded their file", color=discord.Color.orange())
        embed_notify.add_field(name="From", value=message.author.display_name, inline=True)
        embed_notify.add_field(name="File name", value=f"`{attachment.filename}`", inline=True)
        embed_notify.add_field(name="Size", value=size_str, inline=True)
        embed_notify.set_footer(text="Send your file in DM to continue.")
        await other_member.send(embed=embed_notify)
    except:
        pass

    if trade["file1"] and trade["file2"]:
        f1 = trade["file1"]
        f2 = trade["file2"]

        guild = bot.get_guild(trade["guild_id"])
        user1 = guild.get_member(trade["user1_id"])
        user2 = guild.get_member(trade["user2_id"])

        embed_ready = discord.Embed(
            title="Both files uploaded",
            description="Review the details and confirm the trade.",
            color=discord.Color.green()
        )
        embed_ready.add_field(name=f"File from {user1.display_name if user1 else 'Player 1'}", value=f"`{f1['name']}` - {format_size(f1['size'])}", inline=False)
        embed_ready.add_field(name=f"File from {user2.display_name if user2 else 'Player 2'}", value=f"`{f2['name']}` - {format_size(f2['size'])}", inline=False)
        embed_ready.set_footer(text="Both players must confirm to complete the trade.")

        confirm_view = ConfirmView(trade_id)
        mentions = f"{user1.mention if user1 else ''} {user2.mention if user2 else ''}"

        try:
            await ch.send(content=mentions, embed=embed_ready, view=confirm_view)
        except:
            pass


# ─────────────────────────────────────────────
# SLASH COMMAND /canceltrade (admin)
# ─────────────────────────────────────────────
@bot.tree.command(name="canceltrade", description="[Admin] Cancel the trade in this channel", guild=discord.Object(id=SERVER_ID))
@app_commands.checks.has_permissions(manage_channels=True)
async def cancel_trade_admin(interaction: discord.Interaction):
    trade_id = channel_to_trade.get(interaction.channel.id)
    if not trade_id:
        await interaction.response.send_message("This is not an active trade channel.", ephemeral=True)
        return

    await interaction.response.send_message("Admin cancelled the trade.")
    await cleanup_trade(trade_id)
    await interaction.channel.send("Trade has been cancelled by an admin. This channel is now closed.")

# ─────────────────────────────────────────────
# READY
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user} (ID: {bot.user.id})")
    try:
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        guild = discord.Object(id=SERVER_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands to guild.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


bot.run(TOKEN)

