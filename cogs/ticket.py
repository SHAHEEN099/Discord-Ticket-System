#storage mode is json 
import discord
import json
import asyncio
import pytz
from datetime import datetime
import chat_exporter
import io
from discord.ext import commands
from discord import Embed, Colour, File, app_commands # Import app_commands for describing command options
from discord.ext.commands import has_permissions
import os

# --- CONFIGURATION ---
# Ensure you have a ticket_config.json file in the same directory
with open("ticket_config.json", mode="r") as config_file:
    config = json.load(config_file)

GUILD_ID = config["guild_id"]
TICKET_CHANNEL_ID = config["ticket_channel_id"]
LOG_CHANNEL_ID = config["log_channel_id"]
TIMEZONE = config["timezone"]
EMBED_TITLE = config["embed_title"]
EMBED_DESCRIPTION = config["embed_description"]
TICKET_CATEGORIES = {
    "support1": {
        "category_id": config["category_id_1"],
        "team_role_id": config["team_role_id_1"],
        "label": "General Support",
        "description": "For general help and questions.",
        "emoji": "❓"
    },
    "support2": {
        "category_id": config["category_id_2"],
        "team_role_id": config["team_role_id_2"],
        "label": "Technical Support",
        "description": "For technical issues and bug reports.",
        "emoji": "📛"
    }
}

# --- JSON DATABASE SETUP ---
DB_FILE = "tickets.json"

def load_data():
    """Loads data from the JSON file. If the file doesn't exist, creates it."""
    if not os.path.exists(DB_FILE):
        return {"tickets": {}, "blocked_users": [], "next_ticket_id": 1}
    try:
        with open(DB_FILE, "r") as f:
            content = f.read()
            if not content:
                return {"tickets": {}, "blocked_users": [], "next_ticket_id": 1}
            return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError):
        return {"tickets": {}, "blocked_users": [], "next_ticket_id": 1}

def save_data(data):
    """Saves data to the JSON file."""
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

# --- HELPER FUNCTIONS ---
def is_staff():
    """Check if the user has a staff role."""
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False
        staff_roles = [v["team_role_id"] for k, v in TICKET_CATEGORIES.items()]
        return any(role.id in staff_roles for role in ctx.author.roles)
    return commands.check(predicate)

def convert_to_unix_timestamp(date_string):
    """Converts a date string to a Unix timestamp."""
    try:
        dt_obj = datetime.fromisoformat(date_string) if isinstance(date_string, str) else date_string
        if dt_obj.tzinfo is None:
            tz = pytz.timezone(TIMEZONE)
            dt_obj = tz.localize(dt_obj)
        return int(dt_obj.timestamp())
    except (ValueError, TypeError):
        return int(datetime.now().timestamp())

# --- UI VIEWS ---

class TicketLaunchView(discord.ui.View):
    """A view to launch the ticket creation process."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=details["label"],
                description=details["description"],
                emoji=details["emoji"],
                value=key
            ) for key, details in TICKET_CATEGORIES.items()
        ]
        select = discord.ui.Select(
            custom_id="ticket_creation_select",
            placeholder="Choose a ticket reason...",
            options=options
        )
        select.callback = self.select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        data = load_data()
        user_id = interaction.user.id
        if user_id in data["blocked_users"]:
            await interaction.response.send_message("You are blocked from creating tickets.", ephemeral=True)
            return False
        for ticket in data["tickets"].values():
            if ticket.get("user_id") == user_id and ticket.get("status") == "open":
                await interaction.response.send_message("You already have an open ticket.", ephemeral=True)
                return False
        return True

    async def select_callback(self, interaction: discord.Interaction):
        await self.create_ticket(interaction.data['values'][0], interaction)

    async def create_ticket(self, ticket_type: str, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_data()
        user_id, guild = interaction.user.id, interaction.guild
        category_details = TICKET_CATEGORIES[ticket_type]
        category = self.bot.get_channel(category_details["category_id"])
        team_role = guild.get_role(category_details["team_role_id"])

        if not category:
            await interaction.followup.send("Ticket category not found. Please contact an admin.", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            team_role: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        try:
            channel = await category.create_text_channel(f"ticket-{interaction.user.name}", overwrites=overwrites)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permissions to create a channel.", ephemeral=True)
            return

        ticket_id = data["next_ticket_id"]
        data["tickets"][str(channel.id)] = {
            "id": ticket_id, "user_id": user_id, "status": "open",
            "created_at": datetime.now(pytz.timezone(TIMEZONE)).isoformat(),
            "closed_at": None, "claimed_by": None, "rating": None
        }
        data["next_ticket_id"] += 1
        save_data(data)

        embed = Embed(title=f"Ticket #{ticket_id}", description=f"Welcome! A staff member will be with you shortly.\nReason: **{category_details['label']}**", color=Colour.blue())
        await channel.send(content=f"{interaction.user.mention} {team_role.mention}", embed=embed, view=TicketControlView(self.bot))
        await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ticket_data = load_data()["tickets"].get(str(interaction.channel.id))
        if not ticket_data:
            return await interaction.followup.send("This is not a valid ticket channel.", ephemeral=True)
        
        is_staff_member = any(r.id in [v["team_role_id"] for v in TICKET_CATEGORIES.values()] for r in interaction.user.roles)
        if interaction.user.id != ticket_data["user_id"] and not is_staff_member:
            return await interaction.followup.send("You do not have permission to close this ticket.", ephemeral=True)

        await self.archive_and_log(interaction, ticket_data)
        await interaction.channel.delete()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_staff_member = any(r.id in [v["team_role_id"] for v in TICKET_CATEGORIES.values()] for r in interaction.user.roles)
        if not is_staff_member:
            return await interaction.response.send_message("Only staff members can claim tickets.", ephemeral=True)

        data = load_data()
        ticket_data = data["tickets"].get(str(interaction.channel.id))
        if ticket_data:
            ticket_data["claimed_by"] = interaction.user.id
            save_data(data)
            await interaction.channel.send(embed=Embed(description=f"This ticket has been claimed by {interaction.user.mention}.", color=Colour.gold()))
            await interaction.response.send_message("You have claimed this ticket.", ephemeral=True)
            button.disabled = True
            await interaction.message.edit(view=self)
        else:
            await interaction.response.send_message("Could not find ticket data.", ephemeral=True)

    async def archive_and_log(self, interaction: discord.Interaction, ticket_data: dict):
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel: return

        user_id, ticket_id = ticket_data["user_id"], ticket_data["id"]
        created_at = datetime.fromisoformat(ticket_data["created_at"])
        ticket_creator = interaction.guild.get_member(user_id)
        closed_at = datetime.now(pytz.timezone(TIMEZONE))

        transcript = await chat_exporter.export(interaction.channel, bot=self.bot)
        transcript_file = File(io.BytesIO(transcript.encode()), filename=f"transcript-{interaction.channel.name}.html")

        embed = Embed(title="Ticket Closed", color=Colour.red())
        embed.add_field(name="Ticket ID", value=ticket_id, inline=True)
        embed.add_field(name="Opened By", value=ticket_creator.mention if ticket_creator else f"ID: {user_id}", inline=True)
        embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Created At", value=f"<t:{convert_to_unix_timestamp(created_at)}:f>", inline=False)
        embed.add_field(name="Closed At", value=f"<t:{int(closed_at.timestamp())}:f>", inline=False)
        await log_channel.send(embed=embed, file=transcript_file)
        
        if ticket_creator:
            try:
                await ticket_creator.send("Please rate your support experience:", view=TicketRatingView(str(interaction.channel.id)))
            except discord.Forbidden: pass

        data = load_data()
        if str(interaction.channel.id) in data["tickets"]:
            data["tickets"][str(interaction.channel.id)]["status"] = "closed"
            data["tickets"][str(interaction.channel.id)]["closed_at"] = closed_at.isoformat()
            save_data(data)

class TicketRatingView(discord.ui.View):
    def __init__(self, channel_id_str: str):
        super().__init__(timeout=180)
        self.channel_id_str = channel_id_str

    async def submit_rating(self, rating: int, interaction: discord.Interaction):
        data = load_data()
        if self.channel_id_str in data["tickets"]:
            data["tickets"][self.channel_id_str]["rating"] = rating
            save_data(data)
            await interaction.response.send_message(f"You rated this ticket {rating} stars. Thank you!", ephemeral=True)
            for item in self.children: item.disabled = True
            await interaction.message.edit(view=self)

    @discord.ui.button(label="⭐", style=discord.ButtonStyle.primary)
    async def rate_1(self, interaction: discord.Interaction, button: discord.ui.Button): await self.submit_rating(1, interaction)
    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.primary)
    async def rate_2(self, interaction: discord.Interaction, button: discord.ui.Button): await self.submit_rating(2, interaction)
    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.primary)
    async def rate_3(self, interaction: discord.Interaction, button: discord.ui.Button): await self.submit_rating(3, interaction)
    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.primary)
    async def rate_4(self, interaction: discord.Interaction, button: discord.ui.Button): await self.submit_rating(4, interaction)
    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.primary)
    async def rate_5(self, interaction: discord.Interaction, button: discord.ui.Button): await self.submit_rating(5, interaction)

# --- COG CLASS ---

class AdvancedTicketSystem(commands.Cog, name="tickets"):
    """An advanced ticket system with multiple features using JSON storage."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(TicketLaunchView(bot))
        self.bot.add_view(TicketControlView(bot))

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(TicketLaunchView(self.bot))
        self.bot.add_view(TicketControlView(self.bot))
        print(f"{self.__class__.__name__} cog has been loaded and views are registered.")

    # --- HYBRID COMMANDS ---

    @commands.hybrid_command(name="setup-tickets", description="Sets up the ticket creation panel.", guild=discord.Object(id=GUILD_ID))
    @has_permissions(administrator=True)
    async def setup_tickets(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        embed = Embed(title=EMBED_TITLE, description=EMBED_DESCRIPTION, color=Colour.blue())
        await ctx.channel.send(embed=embed, view=TicketLaunchView(self.bot))
        await ctx.send("Ticket panel has been set up.", ephemeral=True)

    @commands.hybrid_command(name="add-user", description="Adds a user to the current ticket.", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(user="The user to add")
    @is_staff()
    async def add_user(self, ctx: commands.Context, user: discord.Member):
        await ctx.defer(ephemeral=True)
        if "ticket-" not in ctx.channel.name:
            return await ctx.send("This is not a ticket channel.", ephemeral=True)
        await ctx.channel.set_permissions(user, read_messages=True, send_messages=True)
        await ctx.send(f"{user.mention} has been added to the ticket.", ephemeral=True)

    @commands.hybrid_command(name="remove-user", description="Removes a user from the current ticket.", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(user="The user to remove")
    @is_staff()
    async def remove_user(self, ctx: commands.Context, user: discord.Member):
        await ctx.defer(ephemeral=True)
        if "ticket-" not in ctx.channel.name:
            return await ctx.send("This is not a ticket channel.", ephemeral=True)
        await ctx.channel.set_permissions(user, overwrite=None)
        await ctx.send(f"{user.mention} has been removed from the ticket.", ephemeral=True)

    @commands.hybrid_command(name="block-user", description="Blocks a user from creating new tickets.", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(user="The user to block")
    @is_staff()
    async def block_user(self, ctx: commands.Context, user: discord.Member):
        await ctx.defer(ephemeral=True)
        data = load_data()
        if user.id not in data["blocked_users"]:
            data["blocked_users"].append(user.id)
            save_data(data)
        await ctx.send(f"{user.mention} has been blocked from creating tickets.", ephemeral=True)

    @commands.hybrid_command(name="unblock-user", description="Unblocks a user from creating new tickets.", guild=discord.Object(id=GUILD_ID))
    @app_commands.describe(user="The user to unblock")
    @is_staff()
    async def unblock_user(self, ctx: commands.Context, user: discord.Member):
        await ctx.defer(ephemeral=True)
        data = load_data()
        if user.id in data["blocked_users"]:
            data["blocked_users"].remove(user.id)
            save_data(data)
        await ctx.send(f"{user.mention} has been unblocked.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdvancedTicketSystem(bot), guilds=[discord.Object(id=GUILD_ID)])
