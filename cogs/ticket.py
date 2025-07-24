import discord
import json
import asyncio
import pytz
import sqlite3
from datetime import datetime
import chat_exporter
import io
from discord.ext import commands
from discord import Option, slash_command, Embed, Colour, File
from discord.ext.commands import has_permissions

# --- CONFIGURATION ---
# It's recommended to load configuration from a file or environment variables
# For this example, we'll keep it simple.
# Make sure you have a config.json file with these keys.
with open("config.json", mode="r") as config_file:
    config = json.load(config_file)

GUILD_ID = config["guild_id"]
TICKET_CHANNEL_ID = config["ticket_channel_id"]
LOG_CHANNEL_ID = config["log_channel_id"]
TIMEZONE = config["timezone"]
EMBED_TITLE = config["embed_title"]
EMBED_DESCRIPTION = config["embed_description"]
# You can add more categories and roles by extending this list
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


# --- DATABASE SETUP ---
conn = sqlite3.connect('tickets.db')
cur = conn.cursor()

def setup_database():
    """Initializes the database and creates tables if they don't exist."""
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER,
            status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP,
            claimed_by INTEGER,
            rating INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()

# --- HELPER FUNCTIONS ---
def is_staff():
    """Check if the user has a staff role."""
    async def predicate(ctx):
        staff_roles = [v["team_role_id"] for k, v in TICKET_CATEGORIES.items()]
        return any(role.id in staff_roles for role in ctx.author.roles)
    return commands.check(predicate)

def convert_to_unix_timestamp(date_string):
    """Converts a date string to a Unix timestamp."""
    try:
        date_format = "%Y-%m-%d %H:%M:%S"
        dt_obj = datetime.strptime(date_string, date_format)
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
        self.add_item(self.create_ticket_select())

    def create_ticket_select(self):
        """Creates the select menu for ticket options."""
        options = [
            discord.SelectOption(
                label=details["label"],
                description=details["description"],
                emoji=details["emoji"],
                value=key
            ) for key, details in TICKET_CATEGORIES.items()
        ]
        return discord.ui.Select(
            custom_id="ticket_creation_select",
            placeholder="Choose a ticket reason...",
            options=options
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Checks if the user can create a ticket."""
        user_id = interaction.user.id
        cur.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,))
        if cur.fetchone():
            await interaction.response.send_message("You are blocked from creating tickets.", ephemeral=True)
            return False

        cur.execute("SELECT 1 FROM tickets WHERE user_id = ? AND status = 'open'", (user_id,))
        if cur.fetchone():
            await interaction.response.send_message("You already have an open ticket.", ephemeral=True)
            return False
        return True

    @discord.ui.select(custom_id="ticket_creation_select")
    async def select_callback(self, select: discord.ui.Select, interaction: discord.Interaction):
        await self.create_ticket(select.values[0], interaction)

    async def create_ticket(self, ticket_type: str, interaction: discord.Interaction):
        """Creates a new ticket channel."""
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        guild = interaction.guild
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
            channel_name = f"ticket-{interaction.user.name}-{ticket_type}"
            channel = await category.create_text_channel(name=channel_name, overwrites=overwrites)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permissions to create a channel.", ephemeral=True)
            return

        created_at = datetime.now(pytz.timezone(TIMEZONE))
        cur.execute(
            "INSERT INTO tickets (user_id, channel_id, status, created_at) VALUES (?, ?, 'open', ?)",
            (user_id, channel.id, created_at)
        )
        conn.commit()

        embed = Embed(
            title=f"Ticket for {interaction.user.display_name}",
            description=f"Welcome! A staff member will be with you shortly.\nReason: **{category_details['label']}**",
            color=Colour.blue()
        )
        await channel.send(content=f"{interaction.user.mention} {team_role.mention}", embed=embed, view=TicketControlView(self.bot))
        await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


class TicketControlView(discord.ui.View):
    """A view with buttons to control a ticket (close, claim)."""
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Closes the ticket."""
        await interaction.response.defer()
        channel = interaction.channel
        
        cur.execute("SELECT user_id FROM tickets WHERE channel_id = ?", (channel.id,))
        ticket_owner_id = cur.fetchone()

        if not ticket_owner_id:
             return await interaction.followup.send("This is not a valid ticket channel.", ephemeral=True)
        
        ticket_owner_id = ticket_owner_id[0]

        # Allow staff or the ticket owner to close
        if interaction.user.id != ticket_owner_id and not any(role.id in [v["team_role_id"] for k, v in TICKET_CATEGORIES.items()] for role in interaction.user.roles):
            return await interaction.followup.send("You do not have permission to close this ticket.", ephemeral=True)

        await self.archive_and_log(interaction)
        await channel.delete()

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.green, custom_id="ticket_claim")
    async def claim_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Allows a staff member to claim a ticket."""
        await interaction.response.defer(ephemeral=True)
        
        if not any(role.id in [v["team_role_id"] for k, v in TICKET_CATEGORIES.items()] for role in interaction.user.roles):
            return await interaction.followup.send("Only staff members can claim tickets.", ephemeral=True)

        cur.execute("UPDATE tickets SET claimed_by = ? WHERE channel_id = ?", (interaction.user.id, interaction.channel.id))
        conn.commit()

        embed = Embed(
            description=f"This ticket has been claimed by {interaction.user.mention}.",
            color=Colour.gold()
        )
        await interaction.channel.send(embed=embed)
        await interaction.followup.send("You have claimed this ticket.", ephemeral=True)
        button.disabled = True
        await interaction.message.edit(view=self)

    async def archive_and_log(self, interaction: discord.Interaction):
        """Archives the ticket and sends a log message."""
        channel = interaction.channel
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)

        if not log_channel:
            return

        cur.execute("SELECT id, user_id, created_at FROM tickets WHERE channel_id = ?", (channel.id,))
        ticket_data = cur.fetchone()
        if not ticket_data:
            return

        ticket_id, user_id, created_at = ticket_data
        ticket_creator = interaction.guild.get_member(user_id)
        closed_at = datetime.now(pytz.timezone(TIMEZONE))

        # Generate transcript
        transcript = await chat_exporter.export(channel, bot=self.bot)
        transcript_file = File(io.BytesIO(transcript.encode()), filename=f"transcript-{channel.name}.html")

        embed = Embed(title="Ticket Closed", color=Colour.red())
        embed.add_field(name="Ticket ID", value=ticket_id, inline=True)
        embed.add_field(name="Opened By", value=ticket_creator.mention if ticket_creator else f"ID: {user_id}", inline=True)
        embed.add_field(name="Closed By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Created At", value=f"<t:{convert_to_unix_timestamp(created_at.strftime('%Y-%m-%d %H:%M:%S'))}:f>", inline=False)
        embed.add_field(name="Closed At", value=f"<t:{int(closed_at.timestamp())}:f>", inline=False)

        await log_channel.send(embed=embed, file=transcript_file)
        
        # Ask for rating
        if ticket_creator:
            try:
                rating_view = TicketRatingView(ticket_id)
                await ticket_creator.send("Thank you for contacting us! Please rate your support experience:", view=rating_view)
            except discord.Forbidden:
                pass # User has DMs disabled

        cur.execute("UPDATE tickets SET status = 'closed', closed_at = ? WHERE channel_id = ?", (closed_at, channel.id))
        conn.commit()


class TicketRatingView(discord.ui.View):
    """A view for users to rate their support experience."""
    def __init__(self, ticket_id: int):
        super().__init__(timeout=180) # 3 minute timeout
        self.ticket_id = ticket_id

    @discord.ui.button(label="⭐", style=discord.ButtonStyle.primary, custom_id="rate_1")
    async def rate_1(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.submit_rating(1, interaction)

    @discord.ui.button(label="⭐⭐", style=discord.ButtonStyle.primary, custom_id="rate_2")
    async def rate_2(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.submit_rating(2, interaction)

    @discord.ui.button(label="⭐⭐⭐", style=discord.ButtonStyle.primary, custom_id="rate_3")
    async def rate_3(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.submit_rating(3, interaction)

    @discord.ui.button(label="⭐⭐⭐⭐", style=discord.ButtonStyle.primary, custom_id="rate_4")
    async def rate_4(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.submit_rating(4, interaction)

    @discord.ui.button(label="⭐⭐⭐⭐⭐", style=discord.ButtonStyle.primary, custom_id="rate_5")
    async def rate_5(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.submit_rating(5, interaction)

    async def submit_rating(self, rating: int, interaction: discord.Interaction):
        """Submits the rating to the database."""
        cur.execute("UPDATE tickets SET rating = ? WHERE id = ?", (rating, self.ticket_id))
        conn.commit()
        await interaction.response.send_message(f"You rated this ticket {rating} stars. Thank you for your feedback!", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)


# --- COG CLASS ---

class AdvancedTicketSystem(commands.Cog):
    """An advanced ticket system with multiple features."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        setup_database()
        self.bot.add_view(TicketLaunchView(bot))
        self.bot.add_view(TicketControlView(bot))


    @commands.Cog.listener()
    async def on_ready(self):
        print(f"{self.__class__.__name__} cog has been loaded.")

    # --- SLASH COMMANDS ---

    @slash_command(name="setup-tickets", description="Sets up the ticket creation panel in the current channel.", guild_ids=[GUILD_ID])
    @has_permissions(administrator=True)
    async def setup_tickets(self, ctx: discord.ApplicationContext):
        """Sends the ticket creation panel."""
        embed = Embed(
            title=EMBED_TITLE,
            description=EMBED_DESCRIPTION,
            color=Colour.blue()
        )
        await ctx.send(embed=embed, view=TicketLaunchView(self.bot))
        await ctx.respond("Ticket panel has been set up.", ephemeral=True)

    @slash_command(name="add-user", description="Adds a user to the current ticket.", guild_ids=[GUILD_ID])
    @is_staff()
    async def add_user(self, ctx: discord.ApplicationContext, user: Option(discord.Member, "The user to add")):
        """Adds a user to a ticket."""
        if "ticket-" not in ctx.channel.name:
            return await ctx.respond("This is not a ticket channel.", ephemeral=True)
        
        await ctx.channel.set_permissions(user, read_messages=True, send_messages=True)
        await ctx.respond(f"{user.mention} has been added to the ticket.", ephemeral=True)

    @slash_command(name="remove-user", description="Removes a user from the current ticket.", guild_ids=[GUILD_ID])
    @is_staff()
    async def remove_user(self, ctx: discord.ApplicationContext, user: Option(discord.Member, "The user to remove")):
        """Removes a user from a ticket."""
        if "ticket-" not in ctx.channel.name:
            return await ctx.respond("This is not a ticket channel.", ephemeral=True)

        await ctx.channel.set_permissions(user, overwrite=None)
        await ctx.respond(f"{user.mention} has been removed from the ticket.", ephemeral=True)

    @slash_command(name="block-user", description="Blocks a user from creating new tickets.", guild_ids=[GUILD_ID])
    @is_staff()
    async def block_user(self, ctx: discord.ApplicationContext, user: Option(discord.Member, "The user to block")):
        """Blocks a user from creating tickets."""
        cur.execute("INSERT OR IGNORE INTO blocked_users (user_id) VALUES (?)", (user.id,))
        conn.commit()
        await ctx.respond(f"{user.mention} has been blocked from creating tickets.", ephemeral=True)

    @slash_command(name="unblock-user", description="Unblocks a user from creating new tickets.", guild_ids=[GUILD_ID])
    @is_staff()
    async def unblock_user(self, ctx: discord.ApplicationContext, user: Option(discord.Member, "The user to unblock")):
        """Unblocks a user from creating tickets."""
        cur.execute("DELETE FROM blocked_users WHERE user_id = ?", (user.id,))
        conn.commit()
        await ctx.respond(f"{user.mention} has been unblocked.", ephemeral=True)


def setup(bot):
    bot.add_cog(AdvancedTicketSystem(bot))

