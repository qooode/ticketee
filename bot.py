import os
import sqlite3
import json
import time
import re
from typing import List, Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands


DB_PATH = os.getenv("DB_PATH", os.path.join("data", "bot.sqlite"))
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID_ENV = os.getenv("GUILD_ID")

# Default panel content (env overrides used if DB values are missing)
ENV_CONTACT_NAME = os.getenv("SUPPORT_CONTACT_NAME")
ENV_PANEL_TITLE = os.getenv("PANEL_TITLE")
ENV_PANEL_DESCRIPTION = os.getenv("PANEL_DESCRIPTION")


def ensure_data_dir():
    d = os.path.dirname(DB_PATH)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    # guild-wide configuration
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS config (
            guild_id INTEGER PRIMARY KEY,
            support_channel_id INTEGER,
            ticket_category_id INTEGER,
            staff_role_id INTEGER,
            panel_title TEXT,
            panel_description TEXT,
            contact_name TEXT,
            allow_user_close INTEGER DEFAULT 1
        )
        """
    )
    # categories configured by admin
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            placeholder TEXT,
            active INTEGER DEFAULT 1
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_categories_guild ON categories(guild_id)")

    # fields per category (for the modal)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            required INTEGER DEFAULT 1,
            style TEXT DEFAULT 'short', -- 'short' | 'paragraph'
            min_length INTEGER,
            max_length INTEGER,
            order_index INTEGER DEFAULT 0
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_fields_cat ON fields(category_id)")

    # per-guild ticket number counter
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_counters (
            guild_id INTEGER PRIMARY KEY,
            next_ticket_number INTEGER DEFAULT 1
        )
        """
    )

    # tickets and messages
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number INTEGER,
            guild_id INTEGER NOT NULL,
            opener_id INTEGER NOT NULL,
            channel_id INTEGER,
            category_id INTEGER,
            status TEXT NOT NULL,
            priority TEXT DEFAULT 'Low',
            created_at INTEGER,
            closed_at INTEGER,
            admin_closer_id INTEGER
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets(channel_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            discord_message_id INTEGER,
            author_id INTEGER,
            content TEXT,
            attachments_json TEXT,
            created_at INTEGER
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id)")

    conn.commit()
    # Lightweight migrations for newly added columns
    try:
        cur.execute("PRAGMA table_info(tickets)")
        cols = {r[1] for r in cur.fetchall()}
        if "priority" not in cols:
            cur.execute("ALTER TABLE tickets ADD COLUMN priority TEXT DEFAULT 'Low'")
            conn.commit()
    except Exception:
        pass
    conn.close()


def upsert_config(guild_id: int, **kwargs):
    conn = get_conn()
    cur = conn.cursor()
    # Ensure row exists
    cur.execute("INSERT OR IGNORE INTO config(guild_id) VALUES (?)", (guild_id,))
    # Build dynamic update
    keys = []
    vals = []
    for k, v in kwargs.items():
        keys.append(f"{k} = ?")
        vals.append(v)
    if keys:
        vals.append(guild_id)
        cur.execute(f"UPDATE config SET {', '.join(keys)} WHERE guild_id = ?", vals)
    conn.commit()
    conn.close()


def get_config(guild_id: int) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM config WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    conn.close()
    cfg = {
        "support_channel_id": None,
        "ticket_category_id": None,
        "staff_role_id": None,
        "panel_title": None,
        "panel_description": None,
        "contact_name": None,
        "allow_user_close": 1,
    }
    if row:
        cfg.update(dict(row))
    # Apply ENV defaults if DB value is missing
    if not cfg.get("contact_name") and ENV_CONTACT_NAME:
        cfg["contact_name"] = ENV_CONTACT_NAME
    if not cfg.get("panel_title") and ENV_PANEL_TITLE:
        cfg["panel_title"] = ENV_PANEL_TITLE
    if not cfg.get("panel_description") and ENV_PANEL_DESCRIPTION:
        cfg["panel_description"] = ENV_PANEL_DESCRIPTION
    return cfg


def list_categories(guild_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM categories WHERE guild_id = ? AND active = 1 ORDER BY id ASC",
        (guild_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_category_by_id(cat_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM categories WHERE id = ?", (cat_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_fields_for_category(cat_id: int) -> List[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM fields WHERE category_id = ? ORDER BY order_index ASC, id ASC",
        (cat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_or_init_counter(guild_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO guild_counters(guild_id, next_ticket_number) VALUES (?,1)",
        (guild_id,),
    )
    cur.execute(
        "SELECT next_ticket_number FROM guild_counters WHERE guild_id = ?",
        (guild_id,),
    )
    row = cur.fetchone()
    next_num = int(row[0]) if row else 1
    conn.close()
    return next_num


def increment_counter(guild_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE guild_counters SET next_ticket_number = next_ticket_number + 1 WHERE guild_id = ?",
        (guild_id,),
    )
    conn.commit()
    conn.close()


def slugify_username(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:50] if s else "user"


def is_admin_like(member: discord.Member, guild_cfg: Dict[str, Any]) -> bool:
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.manage_guild:
        return True
    staff_role_id = guild_cfg.get("staff_role_id")
    if staff_role_id:
        role = discord.utils.get(member.roles, id=int(staff_role_id))
        if role is not None:
            return True
    return False


class PanelSelect(discord.ui.Select):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(
            placeholder="Choose a category",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="panel_select",
        )

    async def callback(self, interaction: discord.Interaction):
        # Read selected category id from value `cat:<id>`
        try:
            value = self.values[0]
        except Exception:
            await interaction.response.send_message(
                "No category selected.", ephemeral=True
            )
            return

        if not value.startswith("cat:"):
            await interaction.response.send_message(
                "Invalid selection.", ephemeral=True
            )
            return
        cat_id = int(value.split(":", 1)[1])
        category = get_category_by_id(cat_id)
        if not category:
            await interaction.response.send_message(
                "That category no longer exists.", ephemeral=True
            )
            return
        fields = get_fields_for_category(cat_id)
        modal = TicketModal(category, fields)
        await interaction.response.send_modal(modal)


class PanelView(discord.ui.View):
    def __init__(self, options: List[discord.SelectOption]):
        super().__init__(timeout=None)
        self.add_item(PanelSelect(options))


class PrioritySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Low", value="Low"),
            discord.SelectOption(label="Normal", value="Normal", default=True),
            discord.SelectOption(label="High", value="High"),
            discord.SelectOption(label="Urgent", value="Urgent"),
        ]
        super().__init__(placeholder="Select priority", min_values=1, max_values=1, options=options, custom_id="priority_select")

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        pr = self.values[0]
        # Validate ticket and permissions
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
        t = cur.fetchone()
        if not t:
            conn.close()
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        cfg = get_config(interaction.guild.id)
        allow = (int(t["opener_id"]) == interaction.user.id) or is_admin_like(interaction.user, cfg)  # type: ignore
        if not allow:
            conn.close()
            await interaction.response.send_message("Only opener or staff can set priority.", ephemeral=True)
            return
        cur.execute("UPDATE tickets SET priority = ? WHERE id = ?", (pr, t["id"]))
        conn.commit()
        conn.close()
        # Update topic and notify channel
        try:
            ch = interaction.channel
            if isinstance(ch, discord.TextChannel):
                await ch.edit(topic=f"Ticket #{t['ticket_number']:04d} | Priority: {pr}")
            await ch.send(f"Priority set to {pr} by {interaction.user.mention}.")  # type: ignore
        except Exception:
            pass
        await interaction.response.edit_message(content=f"Priority updated to {pr}.", view=None)


class PrioritySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(PrioritySelect())


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Mark as Solved", style=discord.ButtonStyle.primary, custom_id="ticket_mark_solved")
    async def mark_solved(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        # Fetch ticket by channel
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM tickets WHERE channel_id = ?",
            (interaction.channel_id,),
        )
        t = cur.fetchone()
        if not t:
            conn.close()
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        if int(t["opener_id"]) != interaction.user.id:
            conn.close()
            await interaction.response.send_message("Only the ticket opener can mark as solved.", ephemeral=True)
            return
        if t["status"] == "closed":
            conn.close()
            await interaction.response.send_message("Ticket already closed.", ephemeral=True)
            return
        # Update status to pending_close
        cur.execute(
            "UPDATE tickets SET status = 'pending_close' WHERE id = ?",
            (t["id"],),
        )
        conn.commit()
        conn.close()
        await interaction.response.send_message(
            "Marked as solved. Waiting for staff to confirm closing.", ephemeral=True
        )
        try:
            await interaction.channel.send(
                f"{interaction.user.mention} marked this ticket as solved. A staff member can now confirm closing.")
        except Exception:
            pass

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, custom_id="ticket_confirm_close")
    async def confirm_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        cfg = get_config(interaction.guild.id)
        if not is_admin_like(interaction.user, cfg):
            await interaction.response.send_message("You are not allowed to close this ticket.", ephemeral=True)
            return
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
        t = cur.fetchone()
        if not t:
            conn.close()
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        if t["status"] == "closed":
            conn.close()
            await interaction.response.send_message("Ticket already closed.", ephemeral=True)
            return
        cur.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ?, admin_closer_id = ? WHERE id = ?",
            (int(time.time()), interaction.user.id, t["id"]),
        )
        conn.commit()
        conn.close()

        # Lock channel for opener
        try:
            ch = interaction.channel  # type: ignore
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                opener = ch.guild.get_member(int(t["opener_id"]))
                overwrites = ch.overwrites
                if opener and isinstance(ch, discord.TextChannel):
                    overwrites[opener] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
                    await ch.edit(overwrites=overwrites, reason="Ticket closed")
            await interaction.response.send_message("Ticket closed.", ephemeral=True)
            await interaction.channel.send("This ticket is now closed. Thank you!")
        except Exception:
            await interaction.response.send_message("Ticket closed, but failed to update permissions.", ephemeral=True)

    @discord.ui.button(label="Set Priority", style=discord.ButtonStyle.secondary, custom_id="ticket_set_priority")
    async def set_priority(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return
        # Only opener or staff/admin can change
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM tickets WHERE channel_id = ?", (interaction.channel_id,))
        t = cur.fetchone()
        conn.close()
        if not t:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        cfg = get_config(interaction.guild.id)
        is_staff = is_admin_like(interaction.user, cfg)
        if not is_staff and int(t["opener_id"]) != interaction.user.id:
            await interaction.response.send_message("Only the opener or staff can change priority.", ephemeral=True)
            return

        # Show ephemeral select to choose priority
        await interaction.response.send_message("Choose a priority:", view=PrioritySelectView(), ephemeral=True)


class TicketModal(discord.ui.Modal, title="Support Ticket"):
    def __init__(self, category_row: sqlite3.Row, fields_rows: List[sqlite3.Row]):
        self.category_row = category_row
        self.fields_rows = fields_rows
        # Build inputs
        components = []
        # Always include a larger multi-line field for the main issue
        default_issue_label = "What's the issue?"
        components.append(
            discord.ui.TextInput(
                label=default_issue_label,
                custom_id="builtin:issue",
                required=True,
                style=discord.TextStyle.paragraph,
            )
        )
        # Add up to 4 additional admin-defined fields (Discord limit is 5 total)
        filtered = []
        for f in fields_rows:
            try:
                if (f["label"] or "").strip().casefold() == default_issue_label.casefold():
                    continue
            except Exception:
                pass
            filtered.append(f)
        for f in filtered[:4]:
            style = discord.TextStyle.short if (f["style"] or "short") == "short" else discord.TextStyle.paragraph
            ti = discord.ui.TextInput(
                label=f["label"],
                custom_id=f"field:{f['id']}",
                required=bool(f["required"]),
                style=style,
                min_length=f["min_length"] if f["min_length"] else None,
                max_length=f["max_length"] if f["max_length"] else None,
            )
            components.append(ti)

        super().__init__(timeout=None)
        for c in components:
            self.add_item(c)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        guild = interaction.guild
        cfg = get_config(guild.id)

        # Next ticket number (guild-wide counter)
        num = get_or_init_counter(guild.id)
        # Default priority is Low; can be changed after channel opens via button or admin command
        priority = "Low"

        # Create channel (put number first to show global sequence clearly)
        channel_name = f"ticket-{num:04d}-{slugify_username(interaction.user.display_name)}"
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        staff_role_id = cfg.get("staff_role_id")
        staff_role = guild.get_role(int(staff_role_id)) if staff_role_id else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        # Ensure the bot can see and send in the channel
        me = guild.me or await guild.fetch_member(bot.user.id)  # type: ignore
        overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        parent = None
        if cfg.get("ticket_category_id"):
            parent = guild.get_channel(int(cfg["ticket_category_id"]))
            if not isinstance(parent, discord.CategoryChannel):
                parent = None

        channel = await guild.create_text_channel(
            channel_name,
            category=parent,
            overwrites=overwrites,
            topic=f"Ticket #{num:04d} | Priority: {priority}",
            reason=f"New support ticket by {interaction.user}"
        )

        # Persist ticket row
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tickets(ticket_number, guild_id, opener_id, channel_id, category_id, status, created_at, priority)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                num,
                guild.id,
                interaction.user.id,
                channel.id,
                self.category_row["id"],
                "open",
                int(time.time()),
                priority,
            ),
        )
        ticket_id = cur.lastrowid
        # increment counter
        cur.execute(
            "UPDATE guild_counters SET next_ticket_number = next_ticket_number + 1 WHERE guild_id = ?",
            (guild.id,),
        )
        conn.commit()

        # Compose initial embed with fields
        embed = discord.Embed(
            title=f"Ticket #{num:04d} - {self.category_row['name']}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Opener", value=interaction.user.mention, inline=False)
        embed.add_field(name="Priority", value=priority, inline=True)
        for item in self.children:
            if isinstance(item, discord.ui.TextInput):
                # store as content JSON entry as well
                embed.add_field(name=item.label, value=item.value or "(blank)", inline=False)

        # Intro text
        intro = (
            "Thanks for reaching out! A staff member will respond as soon as possible.\n"
            "Use 'Set Priority' to change urgency, or 'Mark as Solved' if resolved. Staff will confirm closing."
        )

        view = TicketView()
        try:
            await channel.send(content=intro, embed=embed, view=view)
        except Exception:
            pass

        # Log modal submission as first message in DB
        content_dict = {}
        for item in self.children:
            if isinstance(item, discord.ui.TextInput):
                content_dict[item.label] = item.value
        cur.execute(
            "INSERT INTO messages(ticket_id, discord_message_id, author_id, content, attachments_json, created_at) VALUES (?,?,?,?,?,?)",
            (ticket_id, None, interaction.user.id, json.dumps(content_dict), json.dumps([]), int(time.time())),
        )
        conn.commit()
        conn.close()

        await interaction.response.send_message(
            f"Ticket created: {channel.mention}", ephemeral=True
        )


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # Needed to log ticket messages

bot = commands.Bot(command_prefix="!", intents=intents)


admin_group = app_commands.Group(name="admin", description="Admin commands")


def admin_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    cfg = get_config(interaction.guild.id)
    return is_admin_like(interaction.user, cfg)


def require_admin():
    async def predicate(interaction: discord.Interaction):
        if not admin_check(interaction):
            raise app_commands.CheckFailure("You do not have permission to use this command.")
        return True

    return app_commands.check(predicate)


@admin_group.command(name="set_support_channel", description="Set the channel where the panel will be posted")
@require_admin()
async def set_support_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    upsert_config(interaction.guild_id, support_channel_id=channel.id)
    await interaction.response.send_message(f"Support channel set to {channel.mention}", ephemeral=True)


@admin_group.command(name="set_ticket_category", description="Set the category for new ticket channels")
@require_admin()
async def set_ticket_category(interaction: discord.Interaction, category: discord.CategoryChannel):
    upsert_config(interaction.guild_id, ticket_category_id=category.id)
    await interaction.response.send_message(f"Ticket parent category set to {category.name}", ephemeral=True)


@admin_group.command(name="set_staff_role", description="Set the staff role that can see tickets and close them")
@require_admin()
async def set_staff_role(interaction: discord.Interaction, role: discord.Role):
    upsert_config(interaction.guild_id, staff_role_id=role.id)
    await interaction.response.send_message(f"Staff role set to {role.mention}", ephemeral=True)


@admin_group.command(name="set_panel", description="Set panel title/description/contact name")
@require_admin()
async def set_panel(interaction: discord.Interaction, title: str, description: str, contact_name: str):
    upsert_config(interaction.guild_id, panel_title=title, panel_description=description, contact_name=contact_name)
    await interaction.response.send_message("Panel content updated.", ephemeral=True)


@admin_group.command(name="add_category", description="Add a ticket category")
@require_admin()
async def add_category(interaction: discord.Interaction, name: str, placeholder: Optional[str] = None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO categories(guild_id, name, placeholder, active) VALUES (?,?,?,1)",
        (interaction.guild_id, name, placeholder),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"Category '{name}' added.", ephemeral=True)


@admin_group.command(name="remove_category", description="Remove a ticket category")
@require_admin()
async def remove_category(interaction: discord.Interaction, name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM categories WHERE guild_id = ? AND name = ?",
        (interaction.guild_id, name),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"Category '{name}' removed (if it existed).", ephemeral=True)


@admin_group.command(name="add_field", description="Add a modal field to a category")
@require_admin()
@app_commands.describe(style="short or paragraph")
async def add_field(
    interaction: discord.Interaction,
    category_name: str,
    field_name: str,
    label: str,
    required: bool = True,
    style: str = "short",
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM categories WHERE guild_id = ? AND name = ?",
        (interaction.guild_id, category_name),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return
    cat_id = int(row[0])
    style_val = "paragraph" if style.lower().startswith("p") else "short"
    cur.execute(
        "INSERT INTO fields(category_id, name, label, required, style) VALUES (?,?,?,?,?)",
        (cat_id, field_name, label, 1 if required else 0, style_val),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Field '{label}' added to category '{category_name}'.", ephemeral=True
    )


@admin_group.command(name="remove_field", description="Remove a modal field from a category")
@require_admin()
async def remove_field(interaction: discord.Interaction, category_name: str, field_name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM categories WHERE guild_id = ? AND name = ?",
        (interaction.guild_id, category_name),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return
    cat_id = int(row[0])
    cur.execute(
        "DELETE FROM fields WHERE category_id = ? AND name = ?",
        (cat_id, field_name),
    )
    conn.commit()
    conn.close()
    await interaction.response.send_message(
        f"Field '{field_name}' removed from category '{category_name}' (if it existed).",
        ephemeral=True,
    )


@admin_group.command(name="list_config", description="Show current config and categories")
@require_admin()
async def list_config(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    cats = list_categories(interaction.guild_id)
    lines = []
    support_ch = f"<#{cfg['support_channel_id']}>" if cfg.get('support_channel_id') else 'not set'
    staff_role = f"<@&{cfg['staff_role_id']}>" if cfg.get('staff_role_id') else 'not set'
    lines.append(f"Support channel: {support_ch}")
    lines.append(f"Ticket parent category: {cfg.get('ticket_category_id') or 'not set'}")
    lines.append(f"Staff role: {staff_role}")
    lines.append(f"Panel title: {cfg.get('panel_title') or '(default)'}")
    lines.append(f"Contact name: {cfg.get('contact_name') or '(default)'}")
    lines.append("")
    lines.append("Categories:")
    if not cats:
        lines.append("- (none)")
    else:
        for c in cats:
            fields = get_fields_for_category(int(c["id"]))
            lines.append(f"- {c['name']} ({len(fields)} fields)")
            for f in fields:
                lines.append(f"  • {f['label']} [{'required' if f['required'] else 'optional'}] {f['style']}")
    text = "\n".join(lines)
    await interaction.response.send_message(text, ephemeral=True)


@admin_group.command(name="post_panel", description="Post the support panel in the configured channel")
@require_admin()
async def post_panel(interaction: discord.Interaction):
    cfg = get_config(interaction.guild_id)
    channel_id = cfg.get("support_channel_id")
    if not channel_id:
        await interaction.response.send_message("Support channel not set.", ephemeral=True)
        return
    ch = interaction.guild.get_channel(int(channel_id))  # type: ignore
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("Configured support channel is invalid.", ephemeral=True)
        return
    # Permission pre-check to avoid failure
    me = interaction.guild.me or await interaction.guild.fetch_member(bot.user.id)  # type: ignore
    perms = ch.permissions_for(me)
    missing = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.read_message_history:
        missing.append("Read Message History")
    if not perms.embed_links:
        missing.append("Embed Links")
    if missing:
        await interaction.response.send_message(
            f"Missing channel permissions in {ch.mention}: {', '.join(missing)}",
            ephemeral=True,
        )
        return
    cats = list_categories(interaction.guild_id)
    if not cats:
        await interaction.response.send_message("Please add at least one category first.", ephemeral=True)
        return

    # Build embed
    title = cfg.get("panel_title") or "Contact Support"
    description = cfg.get("panel_description") or (
        f"Contact {cfg.get('contact_name') or 'Support'} directly for issues."
    )
    embed = discord.Embed(title=title, description=description, color=discord.Color.green())

    # Select options
    options = []
    for c in cats[:25]:  # max 25 options
        options.append(
            discord.SelectOption(
                label=c["name"],
                description=(c["placeholder"] or "")[:100],
                value=f"cat:{c['id']}",
            )
        )

    view = PanelView(options)
    try:
        await ch.send(embed=embed, view=view)
        await interaction.response.send_message("Panel posted.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            f"I don't have permission to post in {ch.mention}. Check channel/category overrides.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"Failed to post panel: {e}",
            ephemeral=True,
        )


@admin_group.command(name="set_ticket_priority", description="Set the ticket priority for the current ticket channel")
@require_admin()
@app_commands.choices(priority=[
    app_commands.Choice(name="Low", value="Low"),
    app_commands.Choice(name="Normal", value="Normal"),
    app_commands.Choice(name="High", value="High"),
    app_commands.Choice(name="Urgent", value="Urgent"),
])
async def set_ticket_priority(interaction: discord.Interaction, priority: app_commands.Choice[str]):
    if not interaction.channel or not interaction.guild:
        await interaction.response.send_message("Use this in a ticket channel.", ephemeral=True)
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, ticket_number FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
    t = cur.fetchone()
    if not t:
        conn.close()
        await interaction.response.send_message("This is not a ticket channel.", ephemeral=True)
        return
    cur.execute("UPDATE tickets SET priority = ? WHERE id = ?", (priority.value, t["id"]))
    conn.commit()
    conn.close()
    # Update channel topic if possible
    try:
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.channel.edit(topic=f"Ticket #{t['ticket_number']:04d} | Priority: {priority.value}")
    except Exception:
        pass
    await interaction.response.send_message(f"Priority set to {priority.value}.", ephemeral=True)


@bot.event
async def on_ready():
    # Register persistent views for button handling across restarts
    try:
        bot.add_view(TicketView())
        # Also register a PanelView stub so selects on old panels still work
        # We add a minimal option to satisfy the component structure; options on the message will be used
        stub_option = discord.SelectOption(label="Select", value="cat:0")
        bot.add_view(PanelView([stub_option]))
    except Exception:
        pass

    # Sync commands
    try:
        if GUILD_ID_ENV:
            guild_obj = discord.Object(id=int(GUILD_ID_ENV))
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
        else:
            await bot.tree.sync()
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # Log messages in ticket channels
    if message.author.bot:
        return
    if not message.guild:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, status FROM tickets WHERE channel_id = ?", (message.channel.id,))
    t = cur.fetchone()
    if not t or t["status"] == "closed":
        conn.close()
        return
    attachments = [
        {
            "id": a.id,
            "filename": a.filename,
            "url": a.url,
            "size": a.size,
            "content_type": a.content_type,
        }
        for a in message.attachments
    ]
    cur.execute(
        "INSERT INTO messages(ticket_id, discord_message_id, author_id, content, attachments_json, created_at) VALUES (?,?,?,?,?,?)",
        (t["id"], message.id, message.author.id, message.content, json.dumps(attachments), int(time.time())),
    )
    conn.commit()
    conn.close()


@bot.event
async def setup_hook():
    # Attach admin group
    bot.tree.add_command(admin_group)


def main():
    if not TOKEN:
        print("DISCORD_TOKEN is not set. Please set it in the environment.")
        return
    init_db()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
