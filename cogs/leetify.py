"""
Leetify Cog - CS2 Stats via Leetify Public API
=====================================================
Commands:
  /leetify link <steam64_id>      – Link your Discord account to a Steam ID
  /leetify profile [user]         – View profile ratings and ranks
  /leetify last [user]            – Detailed breakdown of the most recent match
  /leetify matches [user]         – Last 5 matches at a glance
  /leetify setchannel             – Set the channel to auto-post new matches
  /leetify addwatch <steam64_id>  – Watch a Steam ID for new matches (posts when detected)
  /leetify removewatch <steam64_id> – Stop watching a Steam ID
  /leetify watchlist              – Show all watched Steam IDs in this server

Background Task:
  Every 3 minutes the bot polls watched Steam IDs and posts a match-recap
  embed whenever a new match is detected.
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import aiosqlite
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Tuple, Dict, Any, List
from config import Config

logger = logging.getLogger("MegaBot.Leetify")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
LEETIFY_BASE   = "https://api-public.cs-prod.leetify.com"
LEETIFY_COLOR  = 0xFF2566   # Leetify's hot-pink brand colour
POLL_INTERVAL  = 3           # minutes between polls

# Friendly map name mapping
MAP_NAMES: Dict[str, str] = {
    "de_dust2":    "Dust II",
    "de_mirage":   "Mirage",
    "de_inferno":  "Inferno",
    "de_nuke":     "Nuke",
    "de_ancient":  "Ancient",
    "de_anubis":   "Anubis",
    "de_vertigo":  "Vertigo",
    "de_train":    "Train",
    "de_overpass": "Overpass",
    "de_cache":    "Cache",
    "de_cbble":    "Cobblestone",
    "de_tuscan":   "Tuscan",
}


def friendly_map(raw: str) -> str:
    return MAP_NAMES.get(raw, raw.replace("de_", "").title())


def outcome_emoji(outcome: str) -> str:
    return {"win": "🟢 Win", "loss": "🔴 Loss", "tie": "🟡 Tie"}.get(outcome, "❔ ?")


def rating_bar(value: float, length: int = 10) -> str:
    """Visual bar for a 0–1 rating."""
    filled = round(value * length)
    return "█" * filled + "░" * (length - filled)


def format_rating(r: float) -> str:
    return f"{r:.2f} {rating_bar(r)}"


def pct(numerator, denominator) -> str:
    if not denominator:
        return "0%"
    return f"{numerator / denominator * 100:.0f}%"


def fv(text: str) -> str:
    """Truncate a field value to Discord's 1024-char limit."""
    return text[:1021] + "..." if len(text) > 1024 else text


# ──────────────────────────────────────────────
# Database helpers (thin wrappers over aiosqlite)
# ──────────────────────────────────────────────
DB_PATH = "data/database.db"


async def _db_execute(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


async def _db_fetchone(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchone()


async def _db_fetchall(sql: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(sql, params) as cur:
            return await cur.fetchall()


async def init_leetify_tables():
    """Create the Leetify tables if they don't already exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leetify_links (
                discord_id  INTEGER PRIMARY KEY,
                steam64_id  TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leetify_watch (
                guild_id        INTEGER NOT NULL,
                steam64_id      TEXT    NOT NULL,
                channel_id      INTEGER,
                last_match_id   TEXT,
                PRIMARY KEY (guild_id, steam64_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leetify_channels (
                guild_id    INTEGER PRIMARY KEY,
                channel_id  INTEGER NOT NULL
            )
        """)
        await db.commit()


# ──────────────────────────────────────────────
# Leetify API client
# ──────────────────────────────────────────────
class LeetifyClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._headers = {"_leetify_key": api_key}

    async def _get(self, path: str, params: dict = None) -> Optional[Any]:
        url = f"{LEETIFY_BASE}{path}"
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._headers, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"Leetify API {resp.status} for {path}")
                    return None
        except Exception as e:
            logger.error(f"Leetify API error ({path}): {e}")
            return None

    async def get_profile(self, steam64_id: str) -> Optional[dict]:
        return await self._get("/v3/profile", {"steam64_id": steam64_id})

    async def get_matches(self, steam64_id: str) -> Optional[List[dict]]:
        return await self._get("/v3/profile/matches", {"steam64_id": steam64_id})

    async def get_match(self, match_id: str) -> Optional[dict]:
        return await self._get(f"/v2/matches/{match_id}")

    async def validate_key(self) -> bool:
        resp = await self._get("/api-key/validate")
        return resp is not None


# ──────────────────────────────────────────────
# Embed builders
# ──────────────────────────────────────────────
def build_profile_embed(profile: dict, steam64_id: str) -> discord.Embed:
    name  = profile.get("name", "Unknown")
    ranks = profile.get("ranks", {})
    stats = profile.get("stats", {})
    rating = profile.get("rating", {})

    embed = discord.Embed(
        title=f"📊 {name} — Leetify Profile",
        url=f"https://leetify.com/app/profile/{steam64_id}",
        color=LEETIFY_COLOR,
    )

    # ── Ranks ──
    rank_lines = []
    if ranks.get("premier"):
        rank_lines.append(f"🏅 Premier: **{ranks['premier']:,}**")
    if ranks.get("faceit"):
        rank_lines.append(f"🔶 FACEIT: Level **{ranks['faceit']}** ({ranks.get('faceit_elo', '?')} ELO)")
    if ranks.get("leetify"):
        rank_lines.append(f"⭐ Leetify Rating: **{ranks['leetify']:.0f}**")
    if rank_lines:
        embed.add_field(name="Ranks", value=fv("\n".join(rank_lines)), inline=False)

    # ── Leetify sub-ratings ──
    r_lines = []
    for key, label in [("aim","Aim"),("positioning","Positioning"),("utility","Utility"),
                        ("clutch","Clutch"),("opening","Opening")]:
        val = rating.get(key)
        if val is not None:
            r_lines.append(f"`{label:<13}` {val:.2f}")
    if r_lines:
        embed.add_field(name="Ratings (0–1 scale)", value=fv("\n".join(r_lines)), inline=False)

    # ── Aggregate stats ──
    acc    = stats.get("accuracy_enemy_spotted")
    hs_acc = stats.get("accuracy_head")
    react  = stats.get("reaction_time_ms")
    wins   = profile.get("winrate")
    total  = profile.get("total_matches")

    stat_lines = []
    if wins    is not None: stat_lines.append(f"Win Rate: **{wins*100:.1f}%**")
    if total   is not None: stat_lines.append(f"Total Matches: **{total:,}**")
    if acc     is not None: stat_lines.append(f"Accuracy (spotted): **{acc:.1f}%**")
    if hs_acc  is not None: stat_lines.append(f"HS Accuracy: **{hs_acc:.1f}%**")
    if react   is not None: stat_lines.append(f"Reaction Time: **{react:.0f} ms**")
    if stat_lines:
        embed.add_field(name="Aggregate Stats", value=fv("\n".join(stat_lines)), inline=False)

    embed.set_footer(text=f"Steam64: {steam64_id} • Leetify Public API")
    return embed


def build_match_embed(match: dict, steam64_id: str, title_prefix: str = "Last Match") -> discord.Embed:
    """Full match recap embed for a single player."""
    map_name   = friendly_map(match.get("map_name", "?"))
    finished   = match.get("finished_at", "")
    match_id   = match.get("id", "")
    source     = match.get("data_source", "matchmaking")

    # Find the requesting player's stats row
    player_stats: Optional[dict] = None
    all_stats: List[dict] = match.get("stats", [])
    for s in all_stats:
        if str(s.get("steam64_id", "")) == str(steam64_id):
            player_stats = s
            break

    # Determine match outcome / score from team scores
    outcome = "?"
    score_str = "? – ?"
    team_scores = match.get("team_scores", [])

    if player_stats:
        my_team = player_stats.get("initial_team_number")
        scores = {ts["team_number"]: ts["score"] for ts in team_scores}
        my_score    = scores.get(my_team, 0)
        enemy_teams = [t for t in scores if t != my_team]
        enemy_score = scores.get(enemy_teams[0], 0) if enemy_teams else 0
        score_str   = f"{my_score} – {enemy_score}"

        if my_score > enemy_score:
            outcome = "win"
        elif my_score < enemy_score:
            outcome = "loss"
        else:
            outcome = "tie"

    colour_map = {"win": 0x00C853, "loss": 0xD32F2F, "tie": 0xFFC107}
    embed = discord.Embed(
        title=f"🎮 {title_prefix} — {map_name}",
        description=(
            f"{outcome_emoji(outcome)}  {score_str}  •  "
            f"{'MM' if source == 'matchmaking' else 'FACEIT'}  •  "
            f"<t:{_iso_to_ts(finished)}:R>"
        ),
        color=colour_map.get(outcome, LEETIFY_COLOR),
        url=f"https://leetify.com/app/match-details/{match_id}" if match_id else None,
    )

    if player_stats:
        k  = player_stats.get("total_kills", 0)
        d  = player_stats.get("total_deaths", 0)
        a  = player_stats.get("total_assists", 0)
        kd = player_stats.get("kd_ratio", k / max(d, 1))
        adr_val   = player_stats.get("dpr", 0)          # dpr = damage per round = ADR
        hs_kills  = player_stats.get("total_hs_kills", 0)
        hs_pct    = pct(hs_kills, k)
        rating    = player_stats.get("leetify_rating")
        rating_ct = player_stats.get("ct_leetify_rating")
        rating_t  = player_stats.get("t_leetify_rating")
        rounds    = player_stats.get("rounds_count", 0)
        mvps      = player_stats.get("mvps", 0)
        score     = player_stats.get("score", 0)
        react_s   = player_stats.get("reaction_time")   # in seconds

        # ── Core stats ──
        embed.add_field(
            name="Core Stats",
            value=fv(
                f"```\n"
                f"K / D / A   : {k} / {d} / {a}\n"
                f"K/D Ratio   : {kd:.2f}\n"
                f"ADR         : {adr_val:.1f}\n"
                f"HS%         : {hs_pct}\n"
                f"Reaction    : {react_s*1000:.0f} ms\n"
                f"MVP Coins   : {mvps}\n"
                f"Score       : {score}\n"
                f"Rounds      : {rounds}\n"
                f"```"
            ),
            inline=False,
        )

        # ── Leetify ratings ──
        if rating is not None:
            rating_lines = [f"Overall : `{rating:.4f}`"]
            if rating_ct is not None:
                rating_lines.append(f"CT Side : `{rating_ct:.4f}`")
            if rating_t is not None:
                rating_lines.append(f"T Side  : `{rating_t:.4f}`")
            embed.add_field(name="Leetify Ratings", value=fv("\n".join(rating_lines)), inline=False)

        # ── Multi-kills ──
        mk_parts = []
        for mk, label in [(5,"5K"),(4,"4K"),(3,"3K"),(2,"2K")]:
            val = player_stats.get(f"multi{mk}k", 0)
            if val:
                mk_parts.append(f"{label}: **{val}**")
        if mk_parts:
            embed.add_field(name="Multi-kills", value=fv("  ".join(mk_parts)), inline=False)

        # ── Accuracy & utility ──
        acc      = player_stats.get("accuracy_enemy_spotted")
        spray    = player_stats.get("spray_accuracy")
        flashes  = player_stats.get("flashbang_thrown", 0)
        fa       = player_stats.get("flash_assist", 0)
        smokes   = player_stats.get("smoke_thrown", 0)

        acc_lines = []
        if acc   is not None: acc_lines.append(f"Accuracy (spotted): **{acc*100:.1f}%**")
        if spray is not None: acc_lines.append(f"Spray accuracy: **{spray*100:.1f}%**")
        if acc_lines:
            embed.add_field(name="Accuracy", value=fv("\n".join(acc_lines)), inline=True)

        util_lines = [
            f"Flashes: **{flashes}** (assists: {fa})",
            f"Smokes: **{smokes}**",
        ]
        embed.add_field(name="Utility", value=fv("\n".join(util_lines)), inline=True)

        # ── Trades ──
        trade_k_pct = player_stats.get("trade_kills_success_percentage")
        trade_d_pct = player_stats.get("traded_deaths_success_percentage")
        if trade_k_pct is not None or trade_d_pct is not None:
            trade_lines = []
            if trade_k_pct is not None:
                trade_lines.append(f"Trade kills: **{trade_k_pct*100:.0f}%** success")
            if trade_d_pct is not None:
                trade_lines.append(f"Traded deaths: **{trade_d_pct*100:.0f}%** success")
            embed.add_field(name="Trade Stats", value=fv("\n".join(trade_lines)), inline=False)

    embed.set_footer(text=f"Match ID: {match_id}  •  Leetify Public API")
    return embed


def _match_player_outcome(match: dict, steam64_id: str):
    """Return (player_stats, outcome, score_str) from a full match object."""
    all_stats  = match.get("stats", [])
    team_scores = match.get("team_scores", [])

    player_stats = next(
        (s for s in all_stats if str(s.get("steam64_id", "")) == str(steam64_id)), None
    )

    outcome   = "?"
    score_str = "? – ?"

    if player_stats and team_scores:
        my_team     = player_stats.get("initial_team_number")
        scores      = {ts["team_number"]: ts["score"] for ts in team_scores}
        my_score    = scores.get(my_team, 0)
        enemy_score = next((v for k, v in scores.items() if k != my_team), 0)
        score_str   = f"{my_score} – {enemy_score}"
        if my_score > enemy_score:
            outcome = "win"
        elif my_score < enemy_score:
            outcome = "loss"
        else:
            outcome = "tie"

    return player_stats, outcome, score_str


def build_matches_embed(matches: List[dict], steam64_id: str, player_name: str) -> discord.Embed:
    """Summary of recent matches (one line each)."""
    embed = discord.Embed(
        title=f"🏁 Recent Matches — {player_name}",
        color=LEETIFY_COLOR,
    )

    for i, m in enumerate(matches[:10]):
        map_name  = friendly_map(m.get("map_name", "?"))
        finished  = _iso_to_ts(m.get("finished_at", ""))
        source    = "MM" if m.get("data_source", "matchmaking") == "matchmaking" else "FACEIT"

        player_stats, outcome, score_str = _match_player_outcome(m, steam64_id)

        kd_str  = ""
        adr_str = ""
        rating  = None
        if player_stats:
            k       = player_stats.get("total_kills", 0)
            d       = player_stats.get("total_deaths", 0)
            adr_val = player_stats.get("dpr", 0)
            kd      = player_stats.get("kd_ratio", k / max(d, 1))
            rating  = player_stats.get("leetify_rating")
            kd_str  = f"K/D: **{kd:.2f}**  ADR: **{adr_val:.0f}**"

        line = (
            f"{outcome_emoji(outcome)}  **{map_name}**  `{score_str}`  {source}"
            + (f"  •  {kd_str}" if kd_str else "")
            + (f"  •  Rating: `{rating:.3f}`" if rating is not None else "")
            + (f"  •  <t:{finished}:R>" if finished else "")
        )
        embed.add_field(name=f"#{i+1}", value=fv(line), inline=False)

    embed.set_footer(text=f"Steam64: {steam64_id}  •  Leetify Public API")
    return embed


def _iso_to_ts(iso: str) -> int:
    """Convert ISO8601 string to unix timestamp (int), returns 0 on fail."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


# ──────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────
class Leetify(commands.Cog):
    """CS2 stats powered by the Leetify Public API."""

    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.client = LeetifyClient(Config.LEETIFY_API_KEY)
        self._poll_task_started = False

    async def cog_load(self):
        await init_leetify_tables()
        if not self._poll_task_started:
            self.poll_new_matches.start()
            self._poll_task_started = True
        logger.info("Leetify cog loaded — polling every %d minutes", POLL_INTERVAL)

    def cog_unload(self):
        self.poll_new_matches.cancel()

    # ── Helper: resolve steam64 for a user ──────────────────────────────
    async def _resolve_steam(self, discord_user: discord.User | discord.Member) -> Optional[str]:
        row = await _db_fetchone(
            "SELECT steam64_id FROM leetify_links WHERE discord_id = ?",
            (discord_user.id,),
        )
        return row[0] if row else None

    # ── Helper: get name quickly from Steam64 via profile ───────────────
    async def _get_name(self, steam64_id: str) -> str:
        profile = await self.client.get_profile(steam64_id)
        if profile:
            return profile.get("name", steam64_id)
        return steam64_id

    # ─────────────────────────────────────────
    # Slash commands
    # ─────────────────────────────────────────
    leetify_group = app_commands.Group(name="leetify", description="CS2 stats via Leetify")

    @leetify_group.command(name="link", description="Link your Discord account to a Steam64 ID")
    @app_commands.describe(steam64_id="Your 17-digit Steam64 ID")
    async def link(self, interaction: discord.Interaction, steam64_id: str):
        await interaction.response.defer(ephemeral=True)
        if not re.fullmatch(r"\d{17}", steam64_id):
            await interaction.followup.send("❌ Please provide a valid 17-digit Steam64 ID.", ephemeral=True)
            return
        # Verify the account exists on Leetify
        profile = await self.client.get_profile(steam64_id)
        if not profile:
            await interaction.followup.send(
                "❌ Could not find that Steam ID on Leetify. Make sure you've logged into Leetify at least once.",
                ephemeral=True,
            )
            return
        await _db_execute(
            "INSERT OR REPLACE INTO leetify_links (discord_id, steam64_id) VALUES (?, ?)",
            (interaction.user.id, steam64_id),
        )
        name = profile.get("name", steam64_id)
        await interaction.followup.send(
            f"✅ Linked your account to **{name}** (`{steam64_id}`).", ephemeral=True
        )

    @leetify_group.command(name="profile", description="View a player's Leetify profile")
    @app_commands.describe(user="Discord user (must be linked) or leave blank for yourself",
                           steam64_id="Or enter a raw Steam64 ID")
    async def profile(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        steam64_id: Optional[str] = None,
    ):
        await interaction.response.defer()
        steam_id = await self._resolve_target(interaction, user, steam64_id)
        if not steam_id:
            return

        profile_data = await self.client.get_profile(steam_id)
        if not profile_data:
            await interaction.followup.send("❌ Could not fetch profile. Check the Steam64 ID or try again later.")
            return

        embed = build_profile_embed(profile_data, steam_id)
        await interaction.followup.send(embed=embed)

    @leetify_group.command(name="last", description="Show your last match stats (ADR, K/D, HS%, etc.)")
    @app_commands.describe(user="Discord user (must be linked) or leave blank for yourself",
                           steam64_id="Or enter a raw Steam64 ID")
    async def last(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        steam64_id: Optional[str] = None,
    ):
        await interaction.response.defer()
        steam_id = await self._resolve_target(interaction, user, steam64_id)
        if not steam_id:
            return

        matches = await self.client.get_matches(steam_id)
        if not matches:
            await interaction.followup.send("❌ No matches found for that player.")
            return

        # /v3/profile/matches already returns full match objects
        embed = build_match_embed(matches[0], steam_id, "Last Match")
        await interaction.followup.send(embed=embed)

    @leetify_group.command(name="matches", description="Show your last 10 matches")
    @app_commands.describe(user="Discord user (must be linked) or leave blank for yourself",
                           steam64_id="Or enter a raw Steam64 ID")
    async def matches(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        steam64_id: Optional[str] = None,
    ):
        await interaction.response.defer()
        steam_id = await self._resolve_target(interaction, user, steam64_id)
        if not steam_id:
            return

        match_list = await self.client.get_matches(steam_id)
        if not match_list:
            await interaction.followup.send("❌ No matches found for that player.")
            return

        name = await self._get_name(steam_id)
        embed = build_matches_embed(match_list, steam_id, name)
        await interaction.followup.send(embed=embed)

    @leetify_group.command(name="setchannel", description="Set the channel for auto-posting new match recaps")
    @app_commands.describe(channel="The text channel to post match recaps in")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def setchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await _db_execute(
            "INSERT OR REPLACE INTO leetify_channels (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild_id, channel.id),
        )
        await interaction.response.send_message(
            f"✅ Match recaps will be posted in {channel.mention}.", ephemeral=True
        )

    @leetify_group.command(name="addwatch", description="Watch a Steam ID for new matches (auto-posts)")
    @app_commands.describe(steam64_id="The 17-digit Steam64 ID to watch")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def addwatch(self, interaction: discord.Interaction, steam64_id: str):
        await interaction.response.defer(ephemeral=True)
        if not re.fullmatch(r"\d{17}", steam64_id):
            await interaction.followup.send("❌ Invalid Steam64 ID.", ephemeral=True)
            return

        # Get current latest match ID as baseline
        matches = await self.client.get_matches(steam64_id)
        last_id = matches[0].get("id") if matches else None

        await _db_execute(
            """INSERT OR REPLACE INTO leetify_watch
               (guild_id, steam64_id, last_match_id)
               VALUES (?, ?, ?)""",
            (interaction.guild_id, steam64_id, last_id),
        )
        name = await self._get_name(steam64_id)
        await interaction.followup.send(
            f"✅ Now watching **{name}** (`{steam64_id}`) for new matches.", ephemeral=True
        )

    @leetify_group.command(name="removewatch", description="Stop watching a Steam ID")
    @app_commands.describe(steam64_id="Steam64 ID to remove")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def removewatch(self, interaction: discord.Interaction, steam64_id: str):
        await _db_execute(
            "DELETE FROM leetify_watch WHERE guild_id = ? AND steam64_id = ?",
            (interaction.guild_id, steam64_id),
        )
        await interaction.response.send_message(f"✅ Removed `{steam64_id}` from watch list.", ephemeral=True)

    @leetify_group.command(name="watchlist", description="Show all Steam IDs being watched in this server")
    async def watchlist(self, interaction: discord.Interaction):
        rows = await _db_fetchall(
            "SELECT steam64_id, last_match_id FROM leetify_watch WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        if not rows:
            await interaction.response.send_message("No Steam IDs are being watched in this server.", ephemeral=True)
            return
        lines = [f"`{r[0]}`" for r in rows]
        embed = discord.Embed(title="👀 Watched Steam IDs", description="\n".join(lines), color=LEETIFY_COLOR)
        embed.set_footer(text="Use /leetify removewatch to stop watching")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @leetify_group.command(name="debug", description="Dump raw API data for the first match (owner only)")
    @app_commands.describe(steam64_id="Steam64 ID to inspect")
    async def debug(self, interaction: discord.Interaction, steam64_id: str):
        await interaction.response.defer(ephemeral=True)
        import json
        matches = await self.client.get_matches(steam64_id)
        if not matches:
            await interaction.followup.send("No matches returned.", ephemeral=True)
            return
        first = matches[0]
        raw = json.dumps(first, indent=2)
        # Send as a file if too long
        if len(raw) > 1900:
            import io
            await interaction.followup.send(
                file=discord.File(io.BytesIO(raw.encode()), filename="match_debug.json"),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"```json\n{raw}\n```", ephemeral=True)

    # ─────────────────────────────────────────
    # Background polling task
    # ─────────────────────────────────────────
    @tasks.loop(minutes=POLL_INTERVAL)
    async def poll_new_matches(self):
        """Check every watched Steam ID for a new match and post a recap."""
        rows = await _db_fetchall("SELECT guild_id, steam64_id, last_match_id FROM leetify_watch")
        for guild_id, steam64_id, last_match_id in rows:
            try:
                await self._check_and_post(guild_id, steam64_id, last_match_id)
            except Exception as e:
                logger.error(f"Poll error for {steam64_id}: {e}")

    @poll_new_matches.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _check_and_post(self, guild_id: int, steam64_id: str, last_match_id: Optional[str]):
        matches = await self.client.get_matches(steam64_id)
        if not matches:
            return

        latest = matches[0]
        latest_id = latest.get("id")
        if not latest_id or latest_id == last_match_id:
            return  # No new match

        # Update last seen
        await _db_execute(
            "UPDATE leetify_watch SET last_match_id = ? WHERE guild_id = ? AND steam64_id = ?",
            (latest_id, guild_id, steam64_id),
        )

        # Find the channel
        chan_row = await _db_fetchone("SELECT channel_id FROM leetify_channels WHERE guild_id = ?", (guild_id,))
        if not chan_row:
            return  # No output channel configured
        channel = self.bot.get_channel(chan_row[0])
        if not channel:
            return

        # /v3/profile/matches already returns full match objects — use directly
        name = await self._get_name(steam64_id)
        embed = build_match_embed(latest, steam64_id, f"New Match — {name}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to post match recap: {e}")

    # ─────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────
    async def _resolve_target(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member],
        steam64_id: Optional[str],
    ) -> Optional[str]:
        """Return a Steam64 ID from the various input options, or send an error."""
        if steam64_id:
            if not re.fullmatch(r"\d{17}", steam64_id):
                await interaction.followup.send("❌ Invalid Steam64 ID (must be 17 digits).")
                return None
            return steam64_id

        target = user or interaction.user
        sid = await self._resolve_steam(target)
        if not sid:
            who = "you are" if target == interaction.user else f"{target.display_name} is"
            await interaction.followup.send(
                f"❌ {who} not linked to a Leetify account. "
                f"Use `/leetify link <steam64_id>` first."
            )
            return None
        return sid


# ──────────────────────────────────────────────
# Compact embed for when full match data is unavailable
# ──────────────────────────────────────────────
def _build_compact_match_embed(summary: dict, steam64_id: str) -> discord.Embed:
    map_name = friendly_map(summary.get("map_name", "?"))
    outcome  = summary.get("outcome", "?")
    finished = _iso_to_ts(summary.get("finished_at", ""))
    rating   = summary.get("leetify_rating")
    score    = summary.get("score") or []
    source   = summary.get("data_source", "matchmaking")

    score_str = " – ".join(str(s) for s in score) if isinstance(score, list) else "?"
    colour_map = {"win": 0x00C853, "loss": 0xD32F2F, "tie": 0xFFC107}
    embed = discord.Embed(
        title=f"🎮 Last Match — {map_name}",
        description=(
            f"{outcome_emoji(outcome)}  {score_str}  •  "
            f"{'MM' if source == 'matchmaking' else 'FACEIT'}  •  "
            + (f"<t:{finished}:R>" if finished else "")
        ),
        color=colour_map.get(outcome, LEETIFY_COLOR),
    )

    lines = []
    if rating is not None:
        lines.append(f"⭐ Leetify Rating: **{rating:.2f}** {rating_bar(rating)}")

    for key, label in [
        ("accuracy_enemy_spotted", "Accuracy (spotted)"),
        ("accuracy_head",         "HS Accuracy"),
        ("preaim",                "Pre-aim"),
        ("reaction_time_ms",      "Reaction Time (ms)"),
    ]:
        val = summary.get(key)
        if val is not None:
            if "ms" in label:
                lines.append(f"• {label}: **{val:.0f}**")
            else:
                lines.append(f"• {label}: **{val*100:.1f}%**")

    if lines:
        embed.add_field(name="Stats", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"Steam64: {steam64_id}  •  Full stats require /v2/matches")
    return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(Leetify(bot))
    logger.info("Leetify cog registered")
