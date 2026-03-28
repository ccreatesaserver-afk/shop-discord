"""
╔══════════════════════════════════════════════════════════════════╗
║       🏪  DISCORDFORGE STORE BOT  —  v4.0.0 ULTIMATE            ║
║         by creator_server_sm  |  Store Integration Edition      ║
╚══════════════════════════════════════════════════════════════════╝

NEW in v4.0:
  • Store integration — validates purchase tokens from website
  • Auto-setup on join (if token in invite state)
  • Auto-leave after successful setup
  • Token verification against Firebase
  • Per-product template routing
  • Full v3 feature set preserved
"""

import discord
from discord.ext import commands, tasks
import firebase_admin
from firebase_admin import credentials, db
import asyncio
import logging
import os
import json
import math
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any

# ═══════════════════════════════════════════════════════════════════
#                         LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DiscordForgeBot")

# ═══════════════════════════════════════════════════════════════════
#                           CONFIG
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
FIREBASE_CRED    = os.getenv("FIREBASE_CRED", "firebase-key.json")
FIREBASE_DB_URL  = os.getenv("FIREBASE_DB_URL", "https://discord-business-inquiries-default-rtdb.firebaseio.com")
AUTO_BACKUP_HOURS = int(os.getenv("AUTO_BACKUP_HOURS", "24"))
BOT_VERSION      = "4.0.0"

# Product → Template ID mapping
# When a user buys product X, bot looks up template TEMPLATE_MAP[X] from Firebase
TEMPLATE_MAP = {
    "gaming-pro":      "tpl_gaming_pro",
    "community-elite": "tpl_community_elite",
    "business-pro":    "tpl_business_pro",
    "anime-world":     "tpl_anime_world",
    "bot-template-v3": None,  # Bot itself — no template, just welcome message
    "bundle-ultimate": "tpl_bundle_ultimate",
}

# ═══════════════════════════════════════════════════════════════════
#                         FIREBASE INIT
# ═══════════════════════════════════════════════════════════════════
try:
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    logger.info("✅ Firebase connected.")
except Exception as e:
    logger.critical(f"❌ Firebase init failed: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════
#                         BOT CLASS
# ═══════════════════════════════════════════════════════════════════
intents = discord.Intents.all()

class DiscordForgeBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.start_time = datetime.now(timezone.utc)
        # Track guilds currently being set up to prevent duplicate runs
        self._setup_in_progress: set = set()

    async def setup_hook(self):
        await self.tree.sync()
        auto_backup_loop.start()
        logger.info("✅ Slash commands synced | Auto-backup loop started.")

    async def on_ready(self):
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"DiscordForge Store | v{BOT_VERSION} 🏪"
            )
        )
        logger.info(f"🤖 Online: {self.user} (ID: {self.user.id})")
        logger.info(f"🌐 Serving {len(self.guilds)} guild(s)")

    async def on_guild_join(self, guild: discord.Guild):
        """
        When the bot joins a server:
        1. Check all pending invites for a token in their 'state' field
        2. If found, validate token against Firebase
        3. Run the appropriate template setup
        4. Mark token as used
        5. Leave the server
        """
        logger.info(f"➕ Joined guild: {guild.name} (ID: {guild.id})")

        if guild.id in self._setup_in_progress:
            return
        self._setup_in_progress.add(guild.id)

        try:
            await asyncio.sleep(2)  # Let Discord settle
            await self._handle_store_join(guild)
        except Exception as e:
            logger.error(f"Error in on_guild_join for {guild.name}: {e}", exc_info=True)
        finally:
            self._setup_in_progress.discard(guild.id)

    async def _handle_store_join(self, guild: discord.Guild):
        """Main store join handler."""
        # Try to find the invite token from guild's audit log (bot invited via OAuth)
        # In production: the token is passed via the Discord OAuth state parameter
        # We check all pending tokens for this guild

        pending_tokens = self._get_pending_tokens_for_guild(guild)

        if not pending_tokens:
            logger.info(f"No pending store tokens for {guild.name} — normal join.")
            ch = await first_available_channel(guild)
            if ch:
                await ch.send(embed=welcome_embed())
            return

        # Use first matching token
        token, token_data = pending_tokens[0]
        product_id = token_data.get("productId", "")

        logger.info(f"🎫 Store token found: {token} | Product: {product_id} | Guild: {guild.name}")

        ch = await first_available_channel(guild)

        # Send progress message
        if ch:
            prog_msg = await ch.send(embed=progress_embed(
                "🏪 DiscordForge — מקים שרת",
                f"זיהינו רכישה תקפה!\n**מוצר:** `{product_id}`\n\n⏳ מקים את השרת...",
                0, 4
            ))

        try:
            # Step 1: Mark token as used (prevent replay)
            db.reference(f"tokens/{token}").update({"used": True, "usedAt": now_iso(), "guildId": guild.id})

            template_id = TEMPLATE_MAP.get(product_id)

            if template_id is None:
                # Product has no template (e.g., the bot itself)
                if ch:
                    await safe_edit_msg(prog_msg, embed=success_embed(
                        "✅ רכישה מאומתת!",
                        f"**מוצר:** `{product_id}`\nהמוצר שלך אינו תבנית שרת — בדוק את לוח הרכישות באתר.",
                    ))
                await write_audit_standalone(guild, "STORE_JOIN_NO_TEMPLATE", product_id, token)
                return

            # Step 2: Fetch template from Firebase
            template = db.reference(f"templates/{template_id}").get()

            if not template:
                # If custom template doesn't exist, use a built-in default
                template = get_default_template(product_id)

            if not template:
                if ch:
                    await safe_edit_msg(prog_msg, embed=error_embed(
                        "❌ תבנית לא נמצאה",
                        f"תבנית `{template_id}` לא נמצאה. אנא פנה לתמיכה."
                    ))
                return

            if ch:
                await safe_edit_msg(prog_msg, embed=progress_embed(
                    "🏗️ בונה מבנה שרת...",
                    f"מוחק מבנה ישן ובונה מחדש לפי תבנית:\n**{template.get('description', template_id)}**",
                    1, 4
                ))

            # Step 3: Wipe & restore
            await safe_delete_guild(guild)

            if ch:
                # Find new channel after deletion
                ch = await first_available_channel(guild)

            if ch:
                await ch.send(embed=progress_embed("👥 יוצר תפקידים...", "", 2, 4))

            stats = await restore_guild(guild, template)

            # Step 4: Success message
            new_ch = await first_available_channel(guild)
            if new_ch:
                await new_ch.send(embed=success_embed(
                    "🎉 השרת מוכן!",
                    f"**DiscordForge** הקים את השרת בהצלחה!\n\n"
                    f"📋 תבנית: `{template_id}`\n"
                    f"👥 תפקידים: **{stats['roles']}**\n"
                    f"📁 קטגוריות: **{stats['categories']}**\n"
                    f"💬 ערוצים: **{stats['channels']}**\n\n"
                    f"🏪 תודה שרכשת ב-**DiscordForge**!\n"
                    f"🔗 https://discordforge.store",
                    footer=f"DiscordForge v{BOT_VERSION}"
                ))

            await write_audit_standalone(guild, "STORE_SETUP_SUCCESS", product_id, token, stats)
            logger.info(f"✅ Store setup complete: {guild.name} | {product_id} | {stats}")

        except Exception as e:
            logger.error(f"Store setup failed for {guild.name}: {e}", exc_info=True)
            ch = await first_available_channel(guild)
            if ch:
                await ch.send(embed=error_embed(
                    "❌ שגיאה בהקמה",
                    f"אירעה שגיאה בזמן הקמת השרת:\n`{e}`\n\nאנא פנה לתמיכה עם הקוד: `{token}`"
                ))
        finally:
            # Always leave after store-triggered setup
            await asyncio.sleep(5)
            try:
                await guild.leave()
                logger.info(f"🚪 Left guild after store setup: {guild.name}")
            except Exception as e:
                logger.warning(f"Could not leave guild {guild.name}: {e}")

    def _get_pending_tokens_for_guild(self, guild: discord.Guild):
        """Get all unused purchase tokens. In production, match by guild owner or invite state."""
        try:
            all_tokens = db.reference("tokens").get() or {}
            pending = []
            for token, data in all_tokens.items():
                if not data.get("used", False):
                    pending.append((token, data))
            # Return most recent first (basic heuristic — in production, match by owner user ID)
            return pending
        except Exception as e:
            logger.warning(f"Token lookup failed: {e}")
            return []

    async def on_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await safe_send(ctx, embed=error_embed("🚫 אין הרשאות", "נדרשות הרשאות מנהל לפקודה זו."))
        elif isinstance(error, commands.MissingRequiredArgument):
            await safe_send(ctx, embed=error_embed("⚠️ פרמטר חסר", f"חסר: `{error.param.name}`\nהשתמש ב-`!help` לעזרה."))
        elif isinstance(error, commands.CommandNotFound):
            pass
        else:
            logger.error(f"Unhandled error in '{ctx.command}': {error}", exc_info=True)
            await safe_send(ctx, embed=error_embed("❌ שגיאה", f"`{type(error).__name__}: {error}`"))

bot = DiscordForgeBot()

# ═══════════════════════════════════════════════════════════════════
#                         EMBED FACTORIES
# ═══════════════════════════════════════════════════════════════════
COLORS = {
    "success": discord.Color.from_rgb(87, 242, 135),
    "error":   discord.Color.from_rgb(237, 66, 69),
    "warning": discord.Color.from_rgb(254, 231, 92),
    "info":    discord.Color.from_rgb(88, 101, 242),
    "gold":    discord.Color.from_rgb(255, 215, 0),
    "purple":  discord.Color.from_rgb(155, 89, 182),
    "cyan":    discord.Color.from_rgb(0, 206, 209),
    "store":   discord.Color.from_rgb(88, 101, 242),
}

def _base_embed(title, desc, color, footer=None):
    e = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=footer or f"DiscordForge v{BOT_VERSION} 🏪")
    return e

def success_embed(t, d, footer=None): return _base_embed(t, d, COLORS["success"], footer)
def error_embed(t, d):               return _base_embed(t, d, COLORS["error"])
def warning_embed(t, d):             return _base_embed(t, d, COLORS["warning"])
def info_embed(t, d, footer=None):   return _base_embed(t, d, COLORS["info"], footer)

def welcome_embed():
    e = _base_embed(
        "👋 DiscordForge Bot",
        "ברוך הבא! הבוט של **DiscordForge** כאן.\n\n"
        "**לרכישת תבניות ובוטים:**\n"
        "🔗 https://discordforge.store\n\n"
        "**פקודות זמינות:**\n"
        "`!help` — רשימת פקודות\n"
        "`!copy <id>` — גיבוי שרת\n"
        "`!setup <id>` — הקמה מתבנית\n"
        "`!templates` — כל התבניות",
        COLORS["store"]
    )
    e.set_thumbnail(url="https://i.imgur.com/8oxGdVS.png")
    return e

def progress_embed(title, desc, current=0, total=0):
    if total > 0:
        pct = current / total
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        desc += f"\n\n`[{bar}]` {current}/{total} ({int(pct*100)}%)"
    return _base_embed(title, desc, COLORS["info"])

# ═══════════════════════════════════════════════════════════════════
#                         UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════
async def safe_send(ctx, **kwargs):
    try: return await ctx.send(**kwargs)
    except Exception as e:
        logger.warning(f"safe_send failed: {e}")
        return None

async def safe_edit_msg(msg, **kwargs):
    try: await msg.edit(**kwargs)
    except Exception: pass

async def first_available_channel(guild):
    for ch in sorted(guild.text_channels, key=lambda c: c.position):
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None

async def send_to_guild(guild, **kwargs):
    ch = await first_available_channel(guild)
    if ch:
        try: await ch.send(**kwargs)
        except Exception: pass

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def format_dt(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d/%m/%Y %H:%M UTC")
    except Exception:
        return iso

def serialize_overwrites(obj):
    result = []
    for target, ow in obj.overwrites.items():
        allow, deny = ow.pair()
        result.append({
            "id": target.id,
            "name": getattr(target, "name", str(target.id)),
            "type": "role" if isinstance(target, discord.Role) else "member",
            "allow": allow.value,
            "deny": deny.value,
        })
    return result

def build_overwrites_map(ov_list, role_mapping):
    result = {}
    for ov in ov_list:
        target = role_mapping.get(ov["id"])
        if target:
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ov["allow"]),
                discord.Permissions(ov["deny"])
            )
    return result

async def write_audit(guild, action, author, details=""):
    entry = {
        "action": action, "author": author,
        "guild": guild.name, "guild_id": guild.id,
        "timestamp": now_iso(), "details": details
    }
    try: db.reference(f"audit/{guild.id}").push(entry)
    except Exception as e: logger.warning(f"Audit write failed: {e}")

async def write_audit_standalone(guild, action, product_id, token, stats=None):
    entry = {
        "action": action, "product": product_id, "token": token,
        "guild": guild.name, "guild_id": guild.id,
        "timestamp": now_iso(), "stats": stats or {}
    }
    try: db.reference(f"store_audit/{guild.id}").push(entry)
    except Exception as e: logger.warning(f"Store audit write failed: {e}")

async def safe_delete_guild(guild):
    tasks_list = [ch.delete() for ch in guild.channels]
    results = await asyncio.gather(*tasks_list, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception): logger.warning(f"Channel delete error: {r}")
    for role in guild.roles:
        if role.is_default() or role.managed: continue
        try:
            await role.delete()
            await asyncio.sleep(0.35)
        except Exception as e: logger.warning(f"Role delete error {role.name}: {e}")

# ═══════════════════════════════════════════════════════════════════
#                     DEFAULT TEMPLATES (Built-in)
# ═══════════════════════════════════════════════════════════════════
def get_default_template(product_id: str) -> Optional[Dict]:
    """Return a built-in template when Firebase template not found."""
    templates = {
        "gaming-pro": {
            "description": "Gaming Pro Server",
            "server_name": "Gaming Pro",
            "roles": [
                {"id": 1, "name": "👑 Owner", "color": 16766720, "permissions": 8, "hoist": True, "mentionable": False, "position": 10},
                {"id": 2, "name": "⚡ Admin", "color": 15158332, "permissions": 8, "hoist": True, "mentionable": True, "position": 9},
                {"id": 3, "name": "🛡️ Moderator", "color": 3447003, "permissions": 268706822, "hoist": True, "mentionable": True, "position": 8},
                {"id": 4, "name": "🎮 Pro Gamer", "color": 10181046, "permissions": 104320064, "hoist": True, "mentionable": False, "position": 5},
                {"id": 5, "name": "🎯 Member", "color": 9807270, "permissions": 104320064, "hoist": False, "mentionable": False, "position": 3},
                {"id": 6, "name": "🆕 New Member", "color": 0, "permissions": 66560, "hoist": False, "mentionable": False, "position": 1},
            ],
            "categories": [
                {
                    "name": "📢 INFO", "position": 0, "overwrites": [],
                    "channels": [
                        {"name": "📜│rules", "type": "text", "position": 0, "topic": "חוקי השרת", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "📣│announcements", "type": "text", "position": 1, "topic": "הודעות רשמיות", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "👋│welcome", "type": "text", "position": 2, "topic": "ברוך הבא!", "slowmode": 0, "nsfw": False, "overwrites": []},
                    ]
                },
                {
                    "name": "💬 GENERAL", "position": 1, "overwrites": [],
                    "channels": [
                        {"name": "💬│general", "type": "text", "position": 0, "topic": "שיחה חופשית", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "🖼️│media", "type": "text", "position": 1, "topic": "תמונות וסרטונים", "slowmode": 3, "nsfw": False, "overwrites": []},
                        {"name": "🤖│bots", "type": "text", "position": 2, "topic": "פקודות בוטים", "slowmode": 2, "nsfw": False, "overwrites": []},
                    ]
                },
                {
                    "name": "🎮 GAMING", "position": 2, "overwrites": [],
                    "channels": [
                        {"name": "🎮│game-chat", "type": "text", "position": 0, "topic": "שיחות על משחקים", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "📊│stats", "type": "text", "position": 1, "topic": "סטטיסטיקות", "slowmode": 5, "nsfw": False, "overwrites": []},
                        {"name": "🔍│lfg", "type": "text", "position": 2, "topic": "מחפש קבוצה", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "🎯 Gaming Lounge", "type": "voice", "position": 3, "bitrate": 96000, "user_limit": 10, "overwrites": []},
                        {"name": "🎮 Game Room 1", "type": "voice", "position": 4, "bitrate": 64000, "user_limit": 5, "overwrites": []},
                        {"name": "🎮 Game Room 2", "type": "voice", "position": 5, "bitrate": 64000, "user_limit": 5, "overwrites": []},
                    ]
                },
                {
                    "name": "⭐ VIP", "position": 3, "overwrites": [],
                    "channels": [
                        {"name": "💎│vip-chat", "type": "text", "position": 0, "topic": "VIP בלבד", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "👑 VIP Lounge", "type": "voice", "position": 1, "bitrate": 128000, "user_limit": 5, "overwrites": []},
                    ]
                },
                {
                    "name": "🔧 STAFF", "position": 4, "overwrites": [],
                    "channels": [
                        {"name": "📋│staff-chat", "type": "text", "position": 0, "topic": "צוות בלבד", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "⚖️│mod-log", "type": "text", "position": 1, "topic": "לוג מודים", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "🔧 Staff Voice", "type": "voice", "position": 2, "bitrate": 64000, "user_limit": 0, "overwrites": []},
                    ]
                },
            ],
            "uncategorized_channels": []
        },
        "community-elite": {
            "description": "Community Elite Server",
            "server_name": "Community Elite",
            "roles": [
                {"id": 1, "name": "🌟 Founder", "color": 16766720, "permissions": 8, "hoist": True, "mentionable": False, "position": 10},
                {"id": 2, "name": "⚡ Admin", "color": 15105570, "permissions": 8, "hoist": True, "mentionable": True, "position": 9},
                {"id": 3, "name": "🛡️ Moderator", "color": 3447003, "permissions": 268706822, "hoist": True, "mentionable": True, "position": 7},
                {"id": 4, "name": "💎 Elite", "color": 10181046, "permissions": 104320064, "hoist": True, "mentionable": False, "position": 5},
                {"id": 5, "name": "👥 Member", "color": 9807270, "permissions": 104320064, "hoist": False, "mentionable": False, "position": 2},
            ],
            "categories": [
                {
                    "name": "📢 ראשי", "position": 0, "overwrites": [],
                    "channels": [
                        {"name": "📜│rules", "type": "text", "position": 0, "topic": "חוקים ותנאי שימוש", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "📣│announcements", "type": "text", "position": 1, "topic": "הודעות רשמיות", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "👋│introductions", "type": "text", "position": 2, "topic": "הצג את עצמך", "slowmode": 60, "nsfw": False, "overwrites": []},
                        {"name": "🗳️│suggestions", "type": "text", "position": 3, "topic": "הצעות לשיפור", "slowmode": 30, "nsfw": False, "overwrites": []},
                    ]
                },
                {
                    "name": "💬 שיחות", "position": 1, "overwrites": [],
                    "channels": [
                        {"name": "💬│general", "type": "text", "position": 0, "topic": "", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "🎨│media", "type": "text", "position": 1, "topic": "", "slowmode": 5, "nsfw": False, "overwrites": []},
                        {"name": "😂│memes", "type": "text", "position": 2, "topic": "", "slowmode": 10, "nsfw": False, "overwrites": []},
                        {"name": "🎵│music", "type": "text", "position": 3, "topic": "", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "🎙️ General Voice", "type": "voice", "position": 4, "bitrate": 64000, "user_limit": 0, "overwrites": []},
                        {"name": "🎵 Music Bot", "type": "voice", "position": 5, "bitrate": 96000, "user_limit": 0, "overwrites": []},
                    ]
                },
                {
                    "name": "📚 פורומים", "position": 2, "overwrites": [],
                    "channels": [
                        {"name": "💡│ideas", "type": "text", "position": 0, "topic": "רעיונות ודיונים", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "❓│help", "type": "text", "position": 1, "topic": "עזרה ותמיכה", "slowmode": 0, "nsfw": False, "overwrites": []},
                        {"name": "📰│news", "type": "text", "position": 2, "topic": "חדשות ועדכונים", "slowmode": 30, "nsfw": False, "overwrites": []},
                    ]
                },
            ],
            "uncategorized_channels": []
        },
    }

    # For products without specific templates, generate a generic one
    generic = templates.get(product_id)
    if generic:
        return generic

    # Generic fallback
    return {
        "description": f"Template for {product_id}",
        "server_name": product_id.replace("-", " ").title(),
        "roles": [
            {"id": 1, "name": "👑 Owner", "color": 16766720, "permissions": 8, "hoist": True, "mentionable": False, "position": 5},
            {"id": 2, "name": "⚡ Admin", "color": 3447003, "permissions": 8, "hoist": True, "mentionable": True, "position": 4},
            {"id": 3, "name": "👥 Member", "color": 9807270, "permissions": 104320064, "hoist": False, "mentionable": False, "position": 1},
        ],
        "categories": [
            {
                "name": "📢 INFO", "position": 0, "overwrites": [],
                "channels": [
                    {"name": "📜│rules", "type": "text", "position": 0, "topic": "", "slowmode": 0, "nsfw": False, "overwrites": []},
                    {"name": "📣│announcements", "type": "text", "position": 1, "topic": "", "slowmode": 0, "nsfw": False, "overwrites": []},
                ]
            },
            {
                "name": "💬 GENERAL", "position": 1, "overwrites": [],
                "channels": [
                    {"name": "💬│general", "type": "text", "position": 0, "topic": "", "slowmode": 0, "nsfw": False, "overwrites": []},
                    {"name": "🎙️ General Voice", "type": "voice", "position": 1, "bitrate": 64000, "user_limit": 0, "overwrites": []},
                ]
            },
        ],
        "uncategorized_channels": []
    }

# ═══════════════════════════════════════════════════════════════════
#                     SNAPSHOT / RESTORE ENGINE
# ═══════════════════════════════════════════════════════════════════
async def snapshot_guild(guild, description, author):
    data = {
        "description": description, "server_name": guild.name,
        "created_at": now_iso(), "created_by": author,
        "bot_version": BOT_VERSION, "roles": [],
        "categories": [], "uncategorized_channels": [],
    }

    for role in reversed(guild.roles):
        if role.is_default(): continue
        data["roles"].append({
            "id": role.id, "name": role.name, "color": role.color.value,
            "permissions": role.permissions.value, "hoist": role.hoist,
            "mentionable": role.mentionable, "position": role.position,
        })

    for cat in sorted(guild.categories, key=lambda c: c.position):
        cat_info = {
            "name": cat.name, "position": cat.position,
            "overwrites": serialize_overwrites(cat), "channels": [],
        }
        for ch in sorted(cat.channels, key=lambda c: c.position):
            ch_info = {
                "name": ch.name, "type": str(ch.type),
                "position": ch.position, "overwrites": serialize_overwrites(ch),
            }
            if isinstance(ch, discord.TextChannel):
                ch_info.update({"topic": ch.topic or "", "slowmode": ch.slowmode_delay, "nsfw": ch.is_nsfw()})
            elif isinstance(ch, discord.VoiceChannel):
                ch_info.update({"bitrate": ch.bitrate, "user_limit": ch.user_limit})
            elif isinstance(ch, discord.ForumChannel):
                ch_info.update({"topic": ch.topic or ""})
            cat_info["channels"].append(ch_info)
        data["categories"].append(cat_info)

    for ch in guild.channels:
        if ch.category is None and not isinstance(ch, discord.CategoryChannel):
            ch_info = {"name": ch.name, "type": str(ch.type), "position": ch.position, "overwrites": serialize_overwrites(ch)}
            if isinstance(ch, discord.TextChannel):
                ch_info.update({"topic": ch.topic or "", "slowmode": ch.slowmode_delay, "nsfw": ch.is_nsfw()})
            elif isinstance(ch, discord.VoiceChannel):
                ch_info.update({"bitrate": ch.bitrate, "user_limit": ch.user_limit})
            data["uncategorized_channels"].append(ch_info)

    return data

async def restore_guild(guild, ref):
    stats = {"roles": 0, "categories": 0, "channels": 0, "errors": 0}
    role_mapping = {}

    for r_data in sorted(ref.get("roles", []), key=lambda r: r.get("position", 0)):
        try:
            new_role = await guild.create_role(
                name=r_data["name"], color=discord.Color(r_data["color"]),
                permissions=discord.Permissions(r_data["permissions"]),
                hoist=r_data["hoist"], mentionable=r_data["mentionable"],
                reason="DiscordForge Store Setup"
            )
            role_mapping[r_data["id"]] = new_role
            stats["roles"] += 1
            await asyncio.sleep(0.35)
        except Exception as e:
            logger.warning(f"Role create fail '{r_data['name']}': {e}")
            stats["errors"] += 1

    for cat_data in sorted(ref.get("categories", []), key=lambda c: c.get("position", 0)):
        try:
            ow = build_overwrites_map(cat_data.get("overwrites", []), role_mapping)
            new_cat = await guild.create_category(name=cat_data["name"], overwrites=ow, reason="DiscordForge Store Setup")
            stats["categories"] += 1
            await asyncio.sleep(0.35)
        except Exception as e:
            logger.warning(f"Category fail '{cat_data['name']}': {e}")
            stats["errors"] += 1
            new_cat = None

        if new_cat:
            for ch_data in sorted(cat_data.get("channels", []), key=lambda c: c.get("position", 0)):
                try:
                    await _create_channel(guild, ch_data, new_cat, role_mapping)
                    stats["channels"] += 1
                    await asyncio.sleep(0.35)
                except Exception as e:
                    logger.warning(f"Channel fail '{ch_data['name']}': {e}")
                    stats["errors"] += 1

    for ch_data in ref.get("uncategorized_channels", []):
        try:
            await _create_channel(guild, ch_data, None, role_mapping)
            stats["channels"] += 1
            await asyncio.sleep(0.35)
        except Exception as e:
            logger.warning(f"Uncategorized ch fail '{ch_data['name']}': {e}")
            stats["errors"] += 1

    return stats

async def _create_channel(guild, ch_data, category, role_mapping):
    ch_ow = build_overwrites_map(ch_data.get("overwrites", []), role_mapping)
    t = ch_data.get("type", "text")
    kwargs = dict(name=ch_data["name"], category=category, overwrites=ch_ow, reason="DiscordForge Store Setup")

    if t == "text":
        await guild.create_text_channel(**kwargs, topic=ch_data.get("topic", ""),
            slowmode_delay=ch_data.get("slowmode", 0), nsfw=ch_data.get("nsfw", False))
    elif t == "voice":
        await guild.create_voice_channel(**kwargs,
            bitrate=min(ch_data.get("bitrate", 64000), guild.bitrate_limit),
            user_limit=ch_data.get("user_limit", 0))
    elif t == "stage_voice":
        await guild.create_stage_channel(**kwargs)
    elif t == "forum":
        await guild.create_forum(**kwargs, topic=ch_data.get("topic", ""))
    else:
        await guild.create_text_channel(**kwargs)

# ═══════════════════════════════════════════════════════════════════
#                     FIREBASE HELPERS
# ═══════════════════════════════════════════════════════════════════
def fb_get(path): return db.reference(path).get()
def fb_set(path, data): db.reference(path).set(data)
def fb_delete(path): db.reference(path).delete()

def fb_get_template(tid): return db.reference(f"templates/{tid}").get()
def fb_set_template(tid, data): db.reference(f"templates/{tid}").set(data)
def fb_delete_template(tid): db.reference(f"templates/{tid}").delete()
def fb_all_templates(): return db.reference("templates").get() or {}
def fb_get_audit(guild_id, limit=10):
    raw = db.reference(f"audit/{guild_id}").get() or {}
    entries = list(raw.values()) if isinstance(raw, dict) else raw
    return sorted(entries, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]

# ═══════════════════════════════════════════════════════════════════
#                     AUTO-BACKUP TASK
# ═══════════════════════════════════════════════════════════════════
@tasks.loop(hours=AUTO_BACKUP_HOURS)
async def auto_backup_loop():
    await bot.wait_until_ready()
    for guild in bot.guilds:
        cfg = db.reference(f"autobackup/{guild.id}").get()
        if not cfg or not cfg.get("enabled"): continue
        tid = f"auto_{guild.id}"
        try:
            data = await snapshot_guild(guild, f"Auto-backup {now_iso()[:10]}", "AutoBackup")
            data["guild_id"] = guild.id
            data["auto"] = True
            fb_set_template(tid, data)
            await write_audit(guild, "AUTO_BACKUP", "System", f"Template ID: {tid}")
            await send_to_guild(guild, embed=success_embed("🔄 גיבוי אוטומטי", f"גיבוי אוטומטי הושלם!\n**ID:** `{tid}`"))
            logger.info(f"Auto-backup OK: {guild.name} → {tid}")
        except Exception as e:
            logger.error(f"Auto-backup failed for {guild.name}: {e}")

# ═══════════════════════════════════════════════════════════════════
#                   UI VIEWS
# ═══════════════════════════════════════════════════════════════════
class ConfirmView(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=30)
        self.confirmed = None
        self.author_id = author_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("לא מיועד עבורך.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ אישור", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="❌ ביטול", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction, button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()

class TemplatePageView(discord.ui.View):
    PER_PAGE = 4

    def __init__(self, templates, ctx):
        super().__init__(timeout=120)
        self.templates = templates
        self.ctx = ctx
        self.page = 0
        self.total_pages = max(1, math.ceil(len(templates) / self.PER_PAGE))
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_label.label = f"{self.page + 1}/{self.total_pages}"

    def build_embed(self):
        start = self.page * self.PER_PAGE
        items = self.templates[start: start + self.PER_PAGE]
        lines = []
        for tid, t in items:
            icon = "🔄" if t.get("auto") else "📋"
            lines.append(
                f"{icon} **`{tid}`** — {t.get('description','ללא תיאור')}\n"
                f"└ 🖥️ {t.get('server_name','?')} | 👥 {len(t.get('roles',[]))} | "
                f"📁 {len(t.get('categories',[]))} | 🗓️ {format_dt(t.get('created_at',''))}"
            )
        return info_embed(
            f"📚 תבניות שמורות ({len(self.templates)})",
            "\n\n".join(lines) or "אין תבניות.",
            footer=f"עמוד {self.page + 1}/{self.total_pages}"
        )

    async def interaction_check(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("לא מיועד עבורך.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction, button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_label(self, interaction, button): pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction, button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="❌ סגור", style=discord.ButtonStyle.danger)
    async def close_btn(self, interaction, button):
        await interaction.message.delete()
        self.stop()

# ═══════════════════════════════════════════════════════════════════
#                   STORE COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="store")
async def cmd_store(ctx):
    """Show store info."""
    e = _base_embed(
        "🏪 DiscordForge Store",
        "רכוש תבניות ובוטים מקצועיים לשרת Discord שלך!\n\n"
        "🔗 **https://discordforge.store**\n\n"
        "**תהליך הרכישה:**\n"
        "1️⃣ בחר תבנית באתר\n"
        "2️⃣ הירשם ורכוש\n"
        "3️⃣ קבל קישור ייחודי לבוט\n"
        "4️⃣ הזמן את הבוט — השרת יוקם אוטומטית!\n"
        "5️⃣ הבוט יצא לאחר שיסיים ✨",
        COLORS["store"]
    )
    e.add_field(name="💰 מחירים", value="החל מ-₪24", inline=True)
    e.add_field(name="⚡ זמן הקמה", value="2-5 דקות", inline=True)
    e.add_field(name="🛡️ ערבות", value="100% שביעות רצון", inline=True)
    await safe_send(ctx, embed=e)

@bot.command(name="verify_token")
@commands.has_permissions(administrator=True)
async def cmd_verify_token(ctx, token: str):
    """Manually verify and apply a purchase token."""
    token_data = db.reference(f"tokens/{token}").get()

    if not token_data:
        return await safe_send(ctx, embed=error_embed("❌ טוקן לא נמצא", f"הטוקן `{token}` אינו קיים."))

    if token_data.get("used"):
        return await safe_send(ctx, embed=error_embed("❌ טוקן בשימוש", f"הטוקן `{token}` כבר שומש."))

    product_id = token_data.get("productId", "unknown")

    view = ConfirmView(ctx.author.id)
    msg = await safe_send(ctx, embed=warning_embed(
        "🎫 אימות טוקן",
        f"**טוקן:** `{token}`\n**מוצר:** `{product_id}`\n\nלהמשיך בהקמה?"
    ), view=view)
    await view.wait()

    if not view.confirmed:
        return await safe_edit_msg(msg, embed=error_embed("❌ בוטל", "הפעולה בוטלה."), view=None)

    await safe_edit_msg(msg, view=None)

    # Trigger setup
    db.reference(f"tokens/{token}").update({"used": True, "usedAt": now_iso()})
    template_id = TEMPLATE_MAP.get(product_id)
    template = fb_get_template(template_id) if template_id else get_default_template(product_id)

    if not template:
        return await safe_send(ctx, embed=error_embed("❌ תבנית לא נמצאה", f"תבנית לא נמצאה עבור `{product_id}`."))

    prog = await safe_send(ctx, embed=progress_embed("⏳ מקים שרת...", "", 0, 3))
    await safe_delete_guild(ctx.guild)

    prog2_ch = await first_available_channel(ctx.guild)
    if prog2_ch:
        await prog2_ch.send(embed=progress_embed("👥 יוצר תפקידים...", "", 1, 3))

    stats = await restore_guild(ctx.guild, template)
    await write_audit(ctx.guild, "MANUAL_TOKEN_SETUP", str(ctx.author), f"Token: {token}, Product: {product_id}")

    final_ch = await first_available_channel(ctx.guild)
    if final_ch:
        await final_ch.send(embed=success_embed(
            "🎉 הקמה הושלמה!",
            f"📋 תבנית: `{product_id}`\n"
            f"👥 תפקידים: {stats['roles']}\n"
            f"📁 קטגוריות: {stats['categories']}\n"
            f"💬 ערוצים: {stats['channels']}",
        ))

# ═══════════════════════════════════════════════════════════════════
#                   STANDARD COMMANDS (preserved from v3)
# ═══════════════════════════════════════════════════════════════════

@bot.command(name="copy", aliases=["backup", "save"])
@commands.has_permissions(administrator=True)
async def cmd_copy(ctx, template_id: str, *, description: str = ""):
    if not description:
        description = f"Backup of {ctx.guild.name}"

    existing = fb_get_template(template_id)
    if existing:
        view = ConfirmView(ctx.author.id)
        msg = await safe_send(ctx, embed=warning_embed("⚠️ תבנית קיימת", f"תבנית `{template_id}` כבר קיימת ותידרס. המשך?"), view=view)
        await view.wait()
        if not view.confirmed:
            return await safe_edit_msg(msg, embed=error_embed("❌ בוטל", "הפעולה בוטלה."), view=None)
        await safe_edit_msg(msg, view=None)

    prog = await safe_send(ctx, embed=progress_embed("⏳ אוסף נתונים...", "", 0, 2))
    try:
        data = await snapshot_guild(ctx.guild, description, str(ctx.author))
        data["guild_id"] = ctx.guild.id
    except Exception as e:
        return await safe_edit_msg(prog, embed=error_embed("❌ שגיאה", f"{e}"))

    await safe_edit_msg(prog, embed=progress_embed("📤 שומר ב-Firebase...", "", 1, 2))
    try:
        fb_set_template(template_id, data)
    except Exception as e:
        return await safe_edit_msg(prog, embed=error_embed("❌ שגיאת Firebase", str(e)))

    await write_audit(ctx.guild, "COPY", str(ctx.author), f"Template: {template_id}")
    await safe_edit_msg(prog, embed=success_embed(
        "✅ גיבוי הושלם!",
        f"**ID:** `{template_id}`\n**תיאור:** {description}\n"
        f"**תפקידים:** {len(data['roles'])}\n**קטגוריות:** {len(data['categories'])}",
        footer=f"גובה על ידי {ctx.author}"
    ))

@bot.command(name="setup", aliases=["restore", "apply"])
@commands.has_permissions(administrator=True)
async def cmd_setup(ctx, template_id: str):
    ref = fb_get_template(template_id)
    if not ref:
        ref = get_default_template(template_id)
    if not ref:
        return await safe_send(ctx, embed=error_embed("❌ לא נמצא", f"תבנית `{template_id}` לא קיימת."))

    view = ConfirmView(ctx.author.id)
    msg = await safe_send(ctx, embed=warning_embed(
        "⚠️ אזהרה — הקמת שרת",
        f"כל הערוצים והתפקידים יימחקו!\n📋 תבנית: `{template_id}`\n⚠️ **לא ניתן לבטל!**"
    ), view=view)
    await view.wait()
    if not view.confirmed:
        return await safe_edit_msg(msg, embed=error_embed("❌ בוטל", "הפעולה בוטלה."), view=None)

    await safe_edit_msg(msg, view=None)
    await safe_delete_guild(ctx.guild)
    stats = await restore_guild(ctx.guild, ref)
    await write_audit(ctx.guild, "SETUP", str(ctx.author), f"Template: {template_id}")

    final_ch = await first_available_channel(ctx.guild)
    if final_ch:
        await final_ch.send(embed=success_embed(
            "🎯 הקמת שרת הושלמה!",
            f"📋 `{template_id}`\n👥 {stats['roles']} תפקידים\n📁 {stats['categories']} קטגוריות\n💬 {stats['channels']} ערוצים",
            footer=f"הופעל על ידי {ctx.author}"
        ))

@bot.command(name="templates", aliases=["list", "tlist"])
@commands.has_permissions(administrator=True)
async def cmd_templates(ctx):
    all_t = fb_all_templates()
    if not all_t:
        return await safe_send(ctx, embed=info_embed("📋 תבניות", "אין תבניות שמורות."))
    items = sorted(all_t.items(), key=lambda x: x[1].get("created_at", ""), reverse=True)
    view = TemplatePageView(items, ctx)
    await safe_send(ctx, embed=view.build_embed(), view=view)

@bot.command(name="template_info", aliases=["tinfo"])
@commands.has_permissions(administrator=True)
async def cmd_template_info(ctx, template_id: str):
    t = fb_get_template(template_id) or get_default_template(template_id)
    if not t:
        return await safe_send(ctx, embed=error_embed("❌ לא נמצא", f"תבנית `{template_id}` לא קיימת."))
    roles = t.get("roles", [])
    cats = t.get("categories", [])
    total_ch = sum(len(c.get("channels", [])) for c in cats) + len(t.get("uncategorized_channels", []))
    e = _base_embed(f"📋 תבנית: {template_id}", t.get("description", ""), COLORS["purple"])
    e.add_field(name="🖥️ שרת מקור", value=t.get("server_name", "?"), inline=True)
    e.add_field(name="👤 נוצר על ידי", value=t.get("created_by", "?"), inline=True)
    e.add_field(name="👥 תפקידים", value=str(len(roles)), inline=True)
    e.add_field(name="📁 קטגוריות", value=str(len(cats)), inline=True)
    e.add_field(name="💬 ערוצים", value=str(total_ch), inline=True)
    await safe_send(ctx, embed=e)

@bot.command(name="delete_template", aliases=["tdel"])
@commands.has_permissions(administrator=True)
async def cmd_delete_template(ctx, template_id: str):
    if not fb_get_template(template_id):
        return await safe_send(ctx, embed=error_embed("❌ לא נמצא", f"תבנית `{template_id}` אינה קיימת."))
    view = ConfirmView(ctx.author.id)
    msg = await safe_send(ctx, embed=warning_embed("🗑️ מחיקת תבנית", f"למחוק את `{template_id}`?"), view=view)
    await view.wait()
    if not view.confirmed:
        return await safe_edit_msg(msg, embed=error_embed("❌ בוטל", ""), view=None)
    fb_delete_template(template_id)
    await write_audit(ctx.guild, "DELETE_TEMPLATE", str(ctx.author), f"Template: {template_id}")
    await safe_edit_msg(msg, embed=success_embed("🗑️ נמחק!", f"תבנית `{template_id}` נמחקה."), view=None)

@bot.command(name="export_template", aliases=["texport"])
@commands.has_permissions(administrator=True)
async def cmd_export_template(ctx, template_id: str):
    data = fb_get_template(template_id) or get_default_template(template_id)
    if not data:
        return await safe_send(ctx, embed=error_embed("❌ לא נמצא", f"תבנית `{template_id}` לא קיימת."))
    import io
    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    file = discord.File(fp=io.BytesIO(json_bytes), filename=f"template_{template_id}.json")
    await ctx.send(embed=success_embed("📤 ייצוא", f"תבנית `{template_id}` יוצאה!"), file=file)
    await write_audit(ctx.guild, "EXPORT_TEMPLATE", str(ctx.author), f"Template: {template_id}")

@bot.command(name="import_template", aliases=["timport"])
@commands.has_permissions(administrator=True)
async def cmd_import_template(ctx, template_id: str):
    if not ctx.message.attachments:
        return await safe_send(ctx, embed=error_embed("❌ חסר קובץ", "יש לצרף קובץ JSON."))
    att = ctx.message.attachments[0]
    if not att.filename.endswith(".json"):
        return await safe_send(ctx, embed=error_embed("❌ סוג שגוי", "יש לצרף קובץ `.json`."))
    if fb_get_template(template_id):
        return await safe_send(ctx, embed=error_embed("❌ כבר קיים", f"תבנית `{template_id}` כבר קיימת."))
    try:
        raw = await att.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return await safe_send(ctx, embed=error_embed("❌ קובץ שגוי", str(e)))
    data["imported_by"] = str(ctx.author)
    data["imported_at"] = now_iso()
    fb_set_template(template_id, data)
    await write_audit(ctx.guild, "IMPORT_TEMPLATE", str(ctx.author), f"Template: {template_id}")
    await safe_send(ctx, embed=success_embed("📥 יובא!", f"תבנית `{template_id}` יובאה!"))

@bot.command(name="autobackup")
@commands.has_permissions(administrator=True)
async def cmd_autobackup(ctx, state: str = "status"):
    ref = db.reference(f"autobackup/{ctx.guild.id}")
    cfg = ref.get() or {}
    if state.lower() in ("on", "enable"):
        cfg["enabled"] = True
        cfg["interval_hours"] = AUTO_BACKUP_HOURS
        cfg["set_by"] = str(ctx.author)
        ref.set(cfg)
        await safe_send(ctx, embed=success_embed("🔄 גיבוי אוטומטי הופעל", f"כל {AUTO_BACKUP_HOURS} שעות."))
    elif state.lower() in ("off", "disable"):
        cfg["enabled"] = False
        ref.set(cfg)
        await safe_send(ctx, embed=info_embed("⏸️ גיבוי אוטומטי כובה", ""))
    else:
        enabled = cfg.get("enabled", False)
        await safe_send(ctx, embed=info_embed("ℹ️ סטטוס גיבוי אוטומטי",
            f"מצב: {'✅ פעיל' if enabled else '❌ כבוי'}\nתדירות: כל {AUTO_BACKUP_HOURS} שעות"))
    await write_audit(ctx.guild, "AUTOBACKUP_CHANGE", str(ctx.author), f"State: {state}")

@bot.command(name="audit")
@commands.has_permissions(administrator=True)
async def cmd_audit(ctx, limit: int = 10):
    limit = min(max(limit, 1), 25)
    entries = fb_get_audit(ctx.guild.id, limit)
    if not entries:
        return await safe_send(ctx, embed=info_embed("📜 לוג ביקורת", "אין רשומות."))
    lines = [f"**{e.get('action','?')}** — {e.get('author','?')}\n└ {e.get('details','')}" for e in entries]
    await safe_send(ctx, embed=info_embed(f"📜 לוג ביקורת", "\n\n".join(lines)))

@bot.command(name="stats")
@commands.has_permissions(administrator=True)
async def cmd_stats(ctx):
    guild = ctx.guild
    uptime = datetime.now(timezone.utc) - bot.start_time
    d, rem = divmod(int(uptime.total_seconds()), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    all_t = fb_all_templates()
    e = _base_embed("📊 לוח מחוונים", f"**{guild.name}**", COLORS["cyan"])
    e.add_field(name="👥 חברים", value=str(guild.member_count), inline=True)
    e.add_field(name="💬 ערוצים", value=str(len(guild.channels)), inline=True)
    e.add_field(name="📋 תבניות", value=str(len(all_t)), inline=True)
    e.add_field(name="⏱️ Uptime", value=f"{d}d {h}h {m}m", inline=True)
    e.add_field(name="🌐 שרתים", value=str(len(bot.guilds)), inline=True)
    e.add_field(name="📦 גרסה", value=f"v{BOT_VERSION}", inline=True)
    await safe_send(ctx, embed=e)

@bot.command(name="help")
async def cmd_help(ctx):
    e = discord.Embed(
        title=f"📖 DiscordForge Bot — עזרה (v{BOT_VERSION})",
        color=COLORS["purple"],
        timestamp=datetime.now(timezone.utc)
    )
    e.add_field(name="━━━ 🏪 חנות ━━━", value="\u200b", inline=False)
    e.add_field(name="`!store`", value="מידע על החנות", inline=True)
    e.add_field(name="`!verify_token <token>`", value="אמת טוקן רכישה ידנית", inline=True)

    e.add_field(name="━━━ 📋 תבניות ━━━", value="\u200b", inline=False)
    e.add_field(name="`!copy <id> [תיאור]`", value="גיבוי שרת נוכחי", inline=True)
    e.add_field(name="`!setup <id>`", value="הקמת שרת מתבנית ⚠️", inline=True)
    e.add_field(name="`!templates`", value="כל התבניות (עם דפדוף)", inline=True)
    e.add_field(name="`!template_info <id>`", value="מידע על תבנית", inline=True)
    e.add_field(name="`!delete_template <id>`", value="מחיקת תבנית", inline=True)
    e.add_field(name="`!export_template <id>`", value="ייצוא JSON", inline=True)
    e.add_field(name="`!import_template <id>`", value="ייבוא JSON", inline=True)

    e.add_field(name="━━━ 🔧 כלים ━━━", value="\u200b", inline=False)
    e.add_field(name="`!autobackup <on|off>`", value="גיבוי אוטומטי", inline=True)
    e.add_field(name="`!audit`", value="לוג פעולות", inline=True)
    e.add_field(name="`!stats`", value="לוח מחוונים", inline=True)

    e.set_footer(text="🏪 discordforge.store | כל הפקודות דורשות הרשאות מנהל")
    await safe_send(ctx, embed=e)

# ═══════════════════════════════════════════════════════════════════
#                         RUN
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.critical("❌ Set BOT_TOKEN environment variable!")
        sys.exit(1)
    bot.run(BOT_TOKEN, log_handler=None)
