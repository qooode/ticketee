# Ticketee — Discord Ticket Bot (Python + SQLite)

Ticketee is a minimal Discord ticket bot with:
- Admin-only slash commands for configuration
- Support panel message with category dropdown and modal fields
- Private ticket channels under a category with staff + opener access
- "Mark as Solved" (user) and "Confirm Close" (staff) flow
- All ticket messages (and modal submissions) saved to SQLite
- Dockerfile for Coolify deployment
 - Priority support (Low/Normal/High/Urgent) with user set + admin override

## Requirements
- Python 3.11+
- A Discord bot application with the following intents enabled:
  - Privileged: Message Content (needed to log messages in ticket channels)
  - Non-privileged: Guilds, Members

## Environment
Set these environment variables (Coolify or local):
- `DISCORD_TOKEN` (required)
- `DB_PATH` (default: `data/bot.sqlite`)
- Optional defaults (can be changed later via slash commands):
  - `SUPPORT_CONTACT_NAME`
  - `PANEL_TITLE`
  - `PANEL_DESCRIPTION`
- Optional fast command sync:
  - `GUILD_ID` (speeds up slash command registration to one guild)

## Install & Run (local)
- Create a virtualenv and install deps:
  - `python -m venv .venv && source .venv/bin/activate`
  - `pip install -r requirements.txt`
- Run: `python bot.py`

## Deploy with Docker/Coolify
- Image builds from `Dockerfile`.
- Mount a persistent volume to `/app/data` so `bot.sqlite` survives restarts.
- Set environment variables as above.

## Admin Commands
All commands are under `/admin` and require either:
- Server owner, or
- `Manage Server` permission, or
- Member has the configured staff role

Commands:
- `/admin set_support_channel <#channel>` — where the panel is posted
- `/admin set_ticket_category <category>` — parent category for ticket channels
- `/admin set_staff_role <@role>` — staff that can view and confirm close
- `/admin set_panel <title> <description> <contact_name>` — panel copy
- `/admin add_category <name> [placeholder]` — adds a ticket category
- `/admin remove_category <name>` — removes a ticket category
- `/admin add_field <category_name> <field_name> <label> [required] [style]` — add a modal field to a category
  - `style`: `short` or `paragraph` (default `short`)
- `/admin remove_field <category_name> <field_name>` — remove a modal field
- `/admin list_config` — show current config and categories/fields
- `/admin post_panel` — post the panel with the dropdown
 - `/admin set_ticket_priority` — set current ticket priority (run inside the ticket)

## How It Works
- Panel: The bot posts an embed with a dropdown of categories you configured.
- Modal: After picking a category, a modal appears with the fields you defined.
- Ticket: On submit, the bot creates a private channel: `ticket-<####>-<name>` (number first, global per server).
  - Permissions: opener + staff role can view/send, everyone else denied.
  - First message includes the submitted details and buttons.
- Priority: Users choose priority in the modal; opener or staff can change later via the "Set Priority" button or `/admin set_ticket_priority`.
- Close Flow: Opener can press "Mark as Solved"; staff must press "Confirm Close" to close and lock the channel.
- Logging: All messages in ticket channels are saved into SQLite (`messages` table), including the initial modal submission (stored as JSON content).

## Notes
- Max 25 categories appear in the dropdown (Discord API limit).
- Modals allow up to 5 text inputs (Discord API limit).
- If you restart the bot, existing panel and ticket buttons remain functional (persistent views are registered on startup). If a panel select ever stops working, just run `/admin post_panel` again.

## Troubleshooting
- Slash commands not showing: set `GUILD_ID` and restart to force a guild-only sync, or wait up to an hour for global sync.
- Messages not logged: ensure Message Content intent is enabled in the Developer Portal and in your bot code.
- Permissions: set the staff role and ticket parent category; the bot needs `Manage Channels` permission.

## Schema (SQLite)
Tables: `config`, `categories`, `fields`, `guild_counters`, `tickets`, `messages`.
- `messages.content` stores raw text or JSON (for modal submissions).
- Attachments are saved as JSON array in `attachments_json`.
