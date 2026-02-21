import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks
import yaml


# =========================
# Config utils
# =========================

def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Não achei '{path}'. Coloque o config.yaml na raiz do projeto.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml inválido: precisa ser um YAML de mapa (dict).")
    return cfg

def hex_to_int_color(hex_str: str) -> int:
    s = (hex_str or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        raise ValueError(f"Cor inválida: '{hex_str}'. Use #RRGGBB")
    return int(s, 16)

def norm(s: str) -> str:
    return (s or "").strip().lower()


# =========================
# Models
# =========================

@dataclass
class RoleDef:
    name: str
    color: int
    hoist: bool = False
    mentionable: bool = False

@dataclass
class ChannelDef:
    name: str
    type: str  # text | voice
    topic: Optional[str] = None
    slowmode: int = 0
    user_limit: int = 0

@dataclass
class CategoryDef:
    name: str          # display (com emoji)
    raw_name: str      # sem emoji, para matching
    emoji: str
    channels: List[ChannelDef]


# =========================
# Load config
# =========================

CONFIG = load_config("config.yaml")

SYNC_CFG = CONFIG.get("sync", {})
AGGRESSIVE_CHANNELS = bool(SYNC_CFG.get("aggressive_channels", True))
AGGRESSIVE_ROLES = bool(SYNC_CFG.get("aggressive_roles", True))
PRESERVE_CATEGORIES = set((SYNC_CFG.get("preserve_categories") or []))
PRESERVE_CHANNELS = set((SYNC_CFG.get("preserve_channels") or []))
PRESERVE_ROLES = set((SYNC_CFG.get("preserve_roles") or []))

CLAN_CFG = CONFIG.get("clan", {})
CLAN_NAME = str(CLAN_CFG.get("name", "ZombieClan")).strip()
GAME_NAME = str(CLAN_CFG.get("game", "Death Zone Online")).strip()

REG_CFG = CONFIG.get("registration", {})
ENTRY_CATEGORY_NAME = str(REG_CFG.get("category_name", "ENTRADA")).strip()
ENTRY_CATEGORY_EMOJI = str(REG_CFG.get("category_emoji", "🧟")).strip()
ENTRY_CHANNEL_NAME = str(REG_CFG.get("channel_name", "entrada")).strip()

FORCE_ON_JOIN = bool(REG_CFG.get("force_on_join", True))
PING_ON_JOIN = bool(REG_CFG.get("ping_on_join_in_channel", False))
AUDIT_INTERVAL_MIN = int(REG_CFG.get("audit_interval_min", 10))

ROLE_PENDING = str(REG_CFG.get("unregistered_role_name", "⛔ Pendente")).strip()
ROLE_MEMBER = str(REG_CFG.get("registered_role_name", "✅ Membro")).strip()

REQUIRE_MEMBER_ROLE = bool(REG_CFG.get("require_registered_role", True))
BYPASS_ROLES = set((REG_CFG.get("bypass_roles") or []))  # staff

NICK_PREFIX = str(REG_CFG.get("nickname_prefix", "") or "").strip()

UI_CFG = CONFIG.get("ui", {}) or {}
WELCOME_CATEGORY_RAW = str(UI_CFG.get("welcome_category_name", "GERAL")).strip()
WELCOME_CHANNEL_NAME = str(UI_CFG.get("welcome_channel_name", "boas-vindas")).strip()

LOGS_CATEGORY_RAW = str(UI_CFG.get("logs_category_name", "STAFF")).strip()
LOGS_CATEGORY_EMOJI = str(UI_CFG.get("logs_category_emoji", "🛠")).strip()
LOGS_CHANNEL_NAME = str(UI_CFG.get("logs_channel_name", "logs")).strip()

READ_ONLY_CATEGORY_RAW = str(UI_CFG.get("read_only_category_name", "AVISOS")).strip()
READ_ONLY_CHANNELS = set((UI_CFG.get("read_only_channels") or ["regras", "avisos"]))

SLOWMODE_OVERRIDES: Dict[str, int] = {}
for k, v in (UI_CFG.get("slowmodes") or {}).items():
    try:
        SLOWMODE_OVERRIDES[str(k).strip()] = int(v)
    except Exception:
        pass

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Variables/Secrets do host).")


# =========================
# Bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# Helpers
# =========================

def get_staff_roles(guild: discord.Guild) -> List[discord.Role]:
    if not BYPASS_ROLES:
        r = discord.utils.get(guild.roles, name="🛡️ Moderação")
        return [r] if r else []
    roles = []
    for name in BYPASS_ROLES:
        r = discord.utils.get(guild.roles, name=name)
        if r:
            roles.append(r)
    return roles

def slowmode_for_channel(name: str, default: int) -> int:
    n = str(name).strip()
    if n in SLOWMODE_OVERRIDES:
        return int(SLOWMODE_OVERRIDES[n] or 0)
    return int(default or 0)


# =========================
# Builders
# =========================

def build_role_defs(cfg: dict) -> List[RoleDef]:
    out: List[RoleDef] = []
    for r in (cfg.get("roles") or []):
        name = str(r.get("name", "")).strip()
        if not name:
            continue
        out.append(
            RoleDef(
                name=name,
                color=hex_to_int_color(r.get("color", "#95A5A6")),
                hoist=bool(r.get("hoist", False)),
                mentionable=bool(r.get("mentionable", False)),
            )
        )

    names = {x.name for x in out}
    if ROLE_MEMBER not in names:
        out.append(RoleDef(name=ROLE_MEMBER, color=hex_to_int_color("#2ECC71"), hoist=True))
    if ROLE_PENDING not in names:
        out.append(RoleDef(name=ROLE_PENDING, color=hex_to_int_color("#E74C3C"), hoist=True))

    return out

def build_categories(cfg: dict) -> List[CategoryDef]:
    out: List[CategoryDef] = []

    # ENTRADA sempre existe
    entry_channels = [
        ChannelDef(
            name=ENTRY_CHANNEL_NAME,
            type="text",
            topic=f"Entrada do clã — libere o acesso ao servidor ({CLAN_NAME} / {GAME_NAME}).",
            slowmode=0,
            user_limit=0,
        )
    ]
    out.append(
        CategoryDef(
            name=f"{ENTRY_CATEGORY_EMOJI} {ENTRY_CATEGORY_NAME}".strip(),
            raw_name=ENTRY_CATEGORY_NAME,
            emoji=ENTRY_CATEGORY_EMOJI,
            channels=entry_channels,
        )
    )

    for c in (cfg.get("categories") or []):
        raw = str(c.get("name", "")).strip()
        if not raw:
            continue

        emoji = str(c.get("emoji", "📁")).strip()
        display = f"{emoji} {raw}".strip()

        channels: List[ChannelDef] = []
        for ch in (c.get("channels") or []):
            cname = str(ch.get("name", "")).strip()
            if not cname:
                continue

            ctype = str(ch.get("type", "text")).strip().lower()
            if ctype not in ("text", "voice"):
                ctype = "text"

            channels.append(
                ChannelDef(
                    name=cname,
                    type=ctype,
                    topic=ch.get("topic"),
                    slowmode=int(ch.get("slowmode", 0) or 0),
                    user_limit=int(ch.get("user_limit", 0) or 0),
                )
            )

        out.append(CategoryDef(name=display, raw_name=raw, emoji=emoji, channels=channels))

    # Garantir boas-vindas (se categoria existir)
    for c in out:
        if norm(c.raw_name) == norm(WELCOME_CATEGORY_RAW):
            if all(norm(ch.name) != norm(WELCOME_CHANNEL_NAME) for ch in c.channels):
                c.channels.append(
                    ChannelDef(
                        name=WELCOME_CHANNEL_NAME,
                        type="text",
                        topic="Boas-vindas e informações rápidas.",
                        slowmode=0,
                        user_limit=0,
                    )
                )
            break

    # Garantir logs STAFF
    exists_logs_cat = any(norm(c.raw_name) == norm(LOGS_CATEGORY_RAW) for c in out)
    if not exists_logs_cat:
        out.append(
            CategoryDef(
                name=f"{LOGS_CATEGORY_EMOJI} {LOGS_CATEGORY_RAW}".strip(),
                raw_name=LOGS_CATEGORY_RAW,
                emoji=LOGS_CATEGORY_EMOJI,
                channels=[
                    ChannelDef(
                        name=LOGS_CHANNEL_NAME,
                        type="text",
                        topic="Logs internos (setup/registro).",
                        slowmode=0,
                        user_limit=0,
                    )
                ],
            )
        )
    else:
        for c in out:
            if norm(c.raw_name) == norm(LOGS_CATEGORY_RAW):
                if all(norm(ch.name) != norm(LOGS_CHANNEL_NAME) for ch in c.channels):
                    c.channels.append(
                        ChannelDef(
                            name=LOGS_CHANNEL_NAME,
                            type="text",
                            topic="Logs internos (setup/registro).",
                            slowmode=0,
                            user_limit=0,
                        )
                    )
                break

    return out


# =========================
# Ensure helpers
# =========================

async def ensure_role(guild: discord.Guild, rdef: RoleDef) -> discord.Role:
    existing = discord.utils.get(guild.roles, name=rdef.name)
    color = discord.Color(rdef.color)

    if existing:
        try:
            changed = (
                existing.color != color
                or existing.hoist != rdef.hoist
                or existing.mentionable != rdef.mentionable
            )
            if changed:
                await existing.edit(color=color, hoist=rdef.hoist, mentionable=rdef.mentionable, reason="Sync role")
        except discord.Forbidden:
            pass
        return existing

    return await guild.create_role(
        name=rdef.name,
        color=color,
        hoist=rdef.hoist,
        mentionable=rdef.mentionable,
        reason="Create role",
    )

async def ensure_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=name)
    if cat:
        return cat
    return await guild.create_category(name, reason="Create category")

async def ensure_text_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    topic: Optional[str],
    slowmode: int,
) -> discord.TextChannel:
    ch = discord.utils.get(category.text_channels, name=name)
    desired_slowmode = slowmode_for_channel(name, slowmode)

    if ch:
        try:
            changed = (ch.topic != topic) or (ch.slowmode_delay != (desired_slowmode or 0))
            if changed:
                await ch.edit(topic=topic, slowmode_delay=desired_slowmode or 0, reason="Sync text")
        except discord.Forbidden:
            pass
        return ch

    return await guild.create_text_channel(
        name=name,
        category=category,
        topic=topic,
        slowmode_delay=desired_slowmode or 0,
        reason="Create text",
    )

async def ensure_voice_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    user_limit: int,
) -> discord.VoiceChannel:
    ch = discord.utils.get(category.voice_channels, name=name)
    if ch:
        try:
            if ch.user_limit != (user_limit or 0):
                await ch.edit(user_limit=user_limit or 0, reason="Sync voice")
        except discord.Forbidden:
            pass
        return ch

    return await guild.create_voice_channel(
        name=name,
        category=category,
        user_limit=user_limit or 0,
        reason="Create voice",
    )


# =========================
# Visibility / Write Policies
# =========================

def has_any_bypass_role(member: discord.Member) -> bool:
    if not BYPASS_ROLES:
        return False
    return any(r.name in BYPASS_ROLES for r in member.roles)

async def ensure_category_lockdown(guild: discord.Guild, category: discord.CategoryChannel) -> int:
    """
    Para TODAS as categorias (exceto ENTRADA e STAFF):
      - @everyone: não vê
      - ✅ Membro: vê
      - ⛔ Pendente: não vê
    """
    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    if not role_member or not role_pending:
        return 0

    ow = category.overwrites
    changed = False

    def get_ow(target):
        return ow.get(target, discord.PermissionOverwrite())

    o = get_ow(guild.default_role)
    if o.view_channel is not False:
        o.view_channel = False; changed = True
    ow[guild.default_role] = o

    o = get_ow(role_member)
    if o.view_channel is not True:
        o.view_channel = True; changed = True
    ow[role_member] = o

    o = get_ow(role_pending)
    if o.view_channel is not False:
        o.view_channel = False; changed = True
    ow[role_pending] = o

    if changed:
        try:
            await category.edit(overwrites=ow, reason="Visibility: lockdown categories")
            return 1
        except discord.Forbidden:
            return 0
    return 0

async def ensure_entry_channel_policy(guild: discord.Guild, entry_channel: discord.TextChannel) -> int:
    """
    POLÍTICA PROFISSIONAL:
      - #entrada não é chat. É só para botão e instruções.
      - @everyone: vê, não fala
      - ⛔ Pendente: vê, não fala
      - ✅ Membro: vê, não fala
      - Staff: vê e fala
    """
    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    staff_roles = get_staff_roles(guild)
    if not role_member or not role_pending:
        return 0

    ow = entry_channel.overwrites
    changed = False

    def get_ow(target):
        return ow.get(target, discord.PermissionOverwrite())

    # @everyone
    o = get_ow(guild.default_role)
    if o.view_channel is not True:
        o.view_channel = True; changed = True
    if o.send_messages is not False:
        o.send_messages = False; changed = True
    if o.read_message_history is not True:
        o.read_message_history = True; changed = True
    ow[guild.default_role] = o

    # pending
    o = get_ow(role_pending)
    if o.view_channel is not True:
        o.view_channel = True; changed = True
    if o.send_messages is not False:
        o.send_messages = False; changed = True
    if o.read_message_history is not True:
        o.read_message_history = True; changed = True
    ow[role_pending] = o

    # member (não fala no #entrada)
    o = get_ow(role_member)
    if o.view_channel is not True:
        o.view_channel = True; changed = True
    if o.send_messages is not False:
        o.send_messages = False; changed = True
    if o.read_message_history is not True:
        o.read_message_history = True; changed = True
    ow[role_member] = o

    # staff (pode falar)
    for sr in staff_roles:
        o = get_ow(sr)
        if o.view_channel is not True:
            o.view_channel = True; changed = True
        if o.send_messages is not True:
            o.send_messages = True; changed = True
        if o.read_message_history is not True:
            o.read_message_history = True; changed = True
        ow[sr] = o

    if changed:
        try:
            await entry_channel.edit(overwrites=ow, reason="Policy: entry channel read-only")
            return 1
        except discord.Forbidden:
            return 0
    return 0

async def ensure_pending_cannot_write_any_text(guild: discord.Guild) -> int:
    """
    Garante que ⛔ Pendente não escreve em nenhum canal de texto.
    """
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    if not role_pending:
        return 0

    changed_count = 0
    for ch in guild.text_channels:
        ow = ch.overwrites
        o = ow.get(role_pending, discord.PermissionOverwrite())
        if o.send_messages is not False:
            o.send_messages = False
            ow[role_pending] = o
            try:
                await ch.edit(overwrites=ow, reason="Lock: pending cannot write")
                changed_count += 1
            except discord.Forbidden:
                pass
    return changed_count

async def ensure_read_only_channels(guild: discord.Guild, cats: List[CategoryDef]) -> int:
    """
    Canais somente leitura (ex.: regras/avisos):
      - ✅ Membro: não escreve
      - Staff: escreve
      - ⛔ Pendente: não escreve
    """
    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    staff_roles = get_staff_roles(guild)
    if not role_member or not role_pending:
        return 0

    # achar display name da categoria alvo
    target_display = None
    for c in cats:
        if norm(c.raw_name) == norm(READ_ONLY_CATEGORY_RAW):
            target_display = c.name
            break
    if not target_display:
        return 0

    cat = discord.utils.get(guild.categories, name=target_display)
    if not cat:
        return 0

    changed = 0
    target_names = {norm(x) for x in READ_ONLY_CHANNELS}

    for ch in cat.text_channels:
        if norm(ch.name) not in target_names:
            continue

        ow = ch.overwrites

        o = ow.get(role_member, discord.PermissionOverwrite())
        if o.send_messages is not False:
            o.send_messages = False
            ow[role_member] = o

        o = ow.get(role_pending, discord.PermissionOverwrite())
        if o.send_messages is not False:
            o.send_messages = False
            ow[role_pending] = o

        for sr in staff_roles:
            o = ow.get(sr, discord.PermissionOverwrite())
            if o.send_messages is not True:
                o.send_messages = True
                ow[sr] = o

        try:
            await ch.edit(overwrites=ow, reason="Policy: read-only channel")
            changed += 1
        except discord.Forbidden:
            pass

    return changed


# =========================
# Premium UI (embed + pin + logs)
# =========================

PIN_MARKER = "[DZO_CLAN_PIN_INSTRUCOES]"

def build_entry_embed() -> discord.Embed:
    emb = discord.Embed(
        title=f"Entrada — {CLAN_NAME}",
        description="Libere o acesso para ver os canais do clã.",
    )
    emb.add_field(name="Como entrar", value="Clique em **Liberar acesso** e informe seu nome no jogo.", inline=False)
    emb.add_field(name="Depois do acesso", value="Vá no chat e no LFG para achar dupla.", inline=False)
    emb.set_footer(text=f"{GAME_NAME}")
    return emb

def build_entry_instructions_text() -> str:
    return (
        f"{PIN_MARKER}\n"
        "🧟 **CADASTRO OBRIGATÓRIO**\n\n"
        "✅ **Como liberar acesso:**\n"
        "1) Clique no botão **Liberar acesso**\n"
        "2) Digite seu **nome no jogo**\n"
        "3) Você recebe o cargo **✅ Membro** e os canais são liberados\n\n"
        "⛔ **Se você não se cadastrar:**\n"
        "- Você fica como **⛔ Pendente**\n"
        "- Não consegue escrever nos chats\n"
    )

async def find_logs_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    display = f"{LOGS_CATEGORY_EMOJI} {LOGS_CATEGORY_RAW}".strip()
    cat = discord.utils.get(guild.categories, name=display)
    if not cat:
        return None
    return discord.utils.get(cat.text_channels, name=LOGS_CHANNEL_NAME)

async def log_event(guild: discord.Guild, text: str):
    ch = await find_logs_channel(guild)
    if not ch:
        return
    try:
        await ch.send(text)
    except discord.Forbidden:
        pass

async def ensure_entry_instructions_pinned(entry_channel: discord.TextChannel) -> int:
    """
    - Se já existir um PIN com o marker, não duplica.
    - Se existir mensagem do bot com o marker, fixa.
    - Senão, envia e fixa.
    Retorna 1 se garantiu pin (novo ou ajustado), 0 se não conseguiu.
    """
    try:
        pins = await entry_channel.pins()
        for m in pins:
            if (m.author and m.author.id == entry_channel.guild.me.id) and (m.content and PIN_MARKER in m.content):
                return 1
    except discord.Forbidden:
        return 0
    except Exception:
        pass

    # procurar histórico recente
    try:
        async for m in entry_channel.history(limit=50):
            if (m.author and m.author.id == entry_channel.guild.me.id) and (m.content and PIN_MARKER in m.content):
                try:
                    await m.pin(reason="Pin: instruções de cadastro")
                    return 1
                except discord.Forbidden:
                    return 0
    except Exception:
        pass

    # enviar nova e fixar
    try:
        msg = await entry_channel.send(build_entry_instructions_text())
        try:
            await msg.pin(reason="Pin: instruções de cadastro")
        except discord.Forbidden:
            return 0
        return 1
    except discord.Forbidden:
        return 0

async def send_welcome(guild: discord.Guild, member: discord.Member):
    cats = build_categories(CONFIG)
    target_display = None
    for c in cats:
        if norm(c.raw_name) == norm(WELCOME_CATEGORY_RAW):
            target_display = c.name
            break
    if not target_display:
        return

    cat = discord.utils.get(guild.categories, name=target_display)
    if not cat:
        return

    ch = discord.utils.get(cat.text_channels, name=WELCOME_CHANNEL_NAME)
    if not ch:
        return

    try:
        await ch.send(f"✅ {member.mention} entrou no clã. Bem-vindo.")
    except discord.Forbidden:
        pass


# =========================
# Aggressive purge (not in config)
# =========================

def protected_channel_ids(guild: discord.Guild) -> Set[int]:
    prot = set()
    for ch in [guild.system_channel, guild.rules_channel, guild.public_updates_channel]:
        if ch:
            prot.add(ch.id)
    return prot

async def aggressive_purge_not_in_config(guild: discord.Guild, cats: List[CategoryDef]) -> Tuple[int, int]:
    deleted_channels = 0
    deleted_categories = 0

    protected = protected_channel_ids(guild)
    desired_cat_names = set(c.name for c in cats)
    desired_per_cat = {c.name: set(ch.name for ch in c.channels) for c in cats}
    desired_all_channels = set()
    for c in cats:
        for ch in c.channels:
            desired_all_channels.add(ch.name)

    for cat in list(guild.categories):
        if cat.name in PRESERVE_CATEGORIES:
            continue

        if cat.name in desired_cat_names:
            desired_names = desired_per_cat.get(cat.name, set())

            for ch in list(cat.text_channels):
                if ch.id in protected or ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Purge: text channel not in config")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

            for ch in list(cat.voice_channels):
                if ch.id in protected or ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Purge: voice channel not in config")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

    for ch in list(guild.channels):
        if getattr(ch, "id", None) in protected:
            continue
        if getattr(ch, "name", "") in PRESERVE_CHANNELS:
            continue

        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)) and ch.category is None:
            if ch.name not in desired_all_channels:
                try:
                    await ch.delete(reason="Purge: uncategorized channel not in config")
                    deleted_channels += 1
                except discord.Forbidden:
                    pass

    for cat in list(guild.categories):
        if cat.name in PRESERVE_CATEGORIES:
            continue
        if cat.name in desired_cat_names:
            continue

        for ch in list(cat.channels):
            if getattr(ch, "id", None) in protected:
                continue
            if getattr(ch, "name", "") in PRESERVE_CHANNELS:
                continue
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
                try:
                    await ch.delete(reason="Purge: channel in non-config category")
                    deleted_channels += 1
                except discord.Forbidden:
                    pass

        try:
            if len(cat.channels) == 0:
                await cat.delete(reason="Purge: category not in config")
                deleted_categories += 1
        except discord.Forbidden:
            pass

    return deleted_channels, deleted_categories


# =========================
# Aggressive roles
# =========================

def role_is_protected(guild: discord.Guild, role: discord.Role) -> bool:
    if role.is_default():
        return True
    if role.managed:
        return True
    if role == guild.me.top_role:
        return True
    if role.name in PRESERVE_ROLES:
        return True
    return False

async def sync_roles_aggressive(guild: discord.Guild, desired: List[RoleDef]) -> Tuple[int, int, int]:
    created_or_updated = 0
    desired_names = [r.name for r in desired]
    desired_set = set(desired_names)

    ensured: List[discord.Role] = []
    for rdef in desired:
        r = await ensure_role(guild, rdef)
        ensured.append(r)
        created_or_updated += 1

    try:
        bot_top = guild.me.top_role.position
        movable = [r for r in ensured if r.position < bot_top and not r.managed and not r.is_default()]
        name_to_role = {r.name: r for r in movable}
        ordered = [name_to_role[n] for n in desired_names if n in name_to_role]

        base = bot_top - len(ordered)
        if base < 1:
            base = 1

        positions = {role: base + i for i, role in enumerate(ordered)}
        needs = any(role.position != positions.get(role, role.position) for role in ordered)
        if needs:
            await guild.edit_role_positions(positions=positions)
    except Exception:
        pass

    deleted = 0
    skipped = 0

    if not AGGRESSIVE_ROLES:
        return created_or_updated, deleted, skipped

    bot_top = guild.me.top_role.position
    for role in list(guild.roles):
        if role_is_protected(guild, role):
            continue
        if role.name in desired_set:
            continue
        if role.position >= bot_top:
            skipped += 1
            continue
        try:
            await role.delete(reason="Purge: role not in config")
            deleted += 1
        except discord.Forbidden:
            skipped += 1

    return created_or_updated, deleted, skipped


# =========================
# Member enforcement
# =========================

async def enforce_membership(guild: discord.Guild) -> Tuple[int, int, int, int]:
    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    if not role_member or not role_pending:
        return 0, 0, 0, 0

    pending_added = 0
    pending_removed = 0
    without_member = 0
    bypass_count = 0

    for m in guild.members:
        if m.bot:
            continue
        if has_any_bypass_role(m):
            bypass_count += 1
            continue

        has_member = role_member in m.roles
        has_pending = role_pending in m.roles

        if not has_member:
            without_member += 1

        if has_member and has_pending:
            try:
                await m.remove_roles(role_pending, reason="Enforce: member cannot be pending")
                pending_removed += 1
            except discord.Forbidden:
                pass
            continue

        if REQUIRE_MEMBER_ROLE and (not has_member) and (not has_pending):
            try:
                await m.add_roles(role_pending, reason="Enforce: missing member role -> pending")
                pending_added += 1
            except discord.Forbidden:
                pass

    return pending_added, pending_removed, without_member, bypass_count


# =========================
# Registration UI
# =========================

class EntryModal(discord.ui.Modal, title="Liberar acesso"):
    game_name = discord.ui.TextInput(
        label="Seu nome no jogo",
        placeholder="Ex: SamCherri",
        min_length=3,
        max_length=32,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

        role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
        role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
        if not role_member or not role_pending:
            return await interaction.response.send_message("Cargos base não existem. Rode /setup.", ephemeral=True)

        ign = self.game_name.value.strip()
        new_nick = f"{NICK_PREFIX}{ign}".strip() if NICK_PREFIX else ign

        try:
            await member.edit(nick=new_nick, reason="Entry: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mudar o apelido. Dê ao bot **Gerenciar Apelidos** e coloque o cargo do bot acima dos outros.",
                ephemeral=True,
            )

        try:
            if role_pending in member.roles:
                await member.remove_roles(role_pending, reason="Entry: remove pending")
            if role_member not in member.roles:
                await member.add_roles(role_member, reason="Entry: add member")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos cargos. Dê ao bot **Gerenciar Cargos** e coloque o cargo do bot acima.",
                ephemeral=True,
            )

        await interaction.response.send_message("✅ Acesso liberado.", ephemeral=True)

        await send_welcome(guild, member)
        await log_event(guild, f"🧾 Registro: {member} -> nick '{new_nick}'")

class EntryView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Liberar acesso", style=discord.ButtonStyle.success, emoji="🧟", custom_id="entry_button")
    async def entry_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EntryModal())


# =========================
# Commands / Events
# =========================

@bot.event
async def on_ready():
    try:
        bot.add_view(EntryView())
    except Exception:
        pass

    try:
        synced = await bot.tree.sync()
        print(f"[OK] Online: {bot.user} | comandos: {len(synced)}", flush=True)
    except Exception as e:
        print(f"[WARN] sync falhou: {e}", flush=True)

    if not audit_members.is_running():
        audit_members.start()

@bot.event
async def on_member_join(member: discord.Member):
    if not FORCE_ON_JOIN:
        return
    if has_any_bypass_role(member):
        return

    guild = member.guild
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    if not role_pending:
        return

    try:
        if role_pending not in member.roles:
            await member.add_roles(role_pending, reason="Auto: pending on join")
    except discord.Forbidden:
        return

    if PING_ON_JOIN:
        ch = discord.utils.get(guild.text_channels, name=ENTRY_CHANNEL_NAME)
        if ch:
            try:
                await ch.send(f"{member.mention} clique em **Liberar acesso** para entrar.")
            except discord.Forbidden:
                pass


@bot.tree.command(name="recriar_painel", description="Recria o painel de entrada (embed + botão) e fixa as instruções.")
@app_commands.checks.has_permissions(administrator=True)
async def recriar_painel(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)

    entry_cat_name = f"{ENTRY_CATEGORY_EMOJI} {ENTRY_CATEGORY_NAME}".strip()
    cat = discord.utils.get(guild.categories, name=entry_cat_name)
    if not cat:
        return await interaction.response.send_message("Categoria de entrada não existe. Rode /setup.", ephemeral=True)

    ch = discord.utils.get(cat.text_channels, name=ENTRY_CHANNEL_NAME)
    if not ch:
        return await interaction.response.send_message("Canal de entrada não existe. Rode /setup.", ephemeral=True)

    await ensure_entry_channel_policy(guild, ch)
    pinned = await ensure_entry_instructions_pinned(ch)

    try:
        await ch.send(embed=build_entry_embed(), view=EntryView())
        await log_event(guild, f"🧾 Painel recriado por: {interaction.user} | pin={pinned}")
        return await interaction.response.send_message("✅ Painel recriado e instruções fixadas.", ephemeral=True)
    except discord.Forbidden:
        return await interaction.response.send_message("Sem permissão para enviar mensagem no canal de entrada.", ephemeral=True)


@bot.tree.command(name="setup", description="AGRESSIVO: aplica o config, apaga extras e corrige permissões (entrada + somente leitura + pendente).")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message(
        f"⏳ Setup agressivo: aplicando config + limpeza total — **{CLAN_NAME}** ({GAME_NAME})...",
        ephemeral=True
    )

    cats = build_categories(CONFIG)
    role_defs = build_role_defs(CONFIG)

    # 1) roles (agressivo)
    ru, rdel, rskip = await sync_roles_aggressive(guild, role_defs)

    # 2) estrutura (categorias/canais)
    entry_channel: Optional[discord.TextChannel] = None

    for cdef in cats:
        cat = await ensure_category(guild, cdef.name)

        # lockdown em tudo fora ENTRADA e fora STAFF/LOGS
        if norm(cdef.raw_name) not in {norm(ENTRY_CATEGORY_NAME), norm(LOGS_CATEGORY_RAW)}:
            await ensure_category_lockdown(guild, cat)

        for chdef in cdef.channels:
            if chdef.type == "voice":
                await ensure_voice_channel(guild, cat, chdef.name, chdef.user_limit or 0)
            else:
                tch = await ensure_text_channel(guild, cat, chdef.name, chdef.topic, chdef.slowmode or 0)
                if norm(tch.name) == norm(ENTRY_CHANNEL_NAME) and norm(cdef.raw_name) == norm(ENTRY_CATEGORY_NAME):
                    entry_channel = tch

    # 3) entrada: política (read-only) + pin + painel
    panel_sent = 0
    pin_ok = 0
    if entry_channel:
        await ensure_entry_channel_policy(guild, entry_channel)
        pin_ok = await ensure_entry_instructions_pinned(entry_channel)
        try:
            await entry_channel.send(embed=build_entry_embed(), view=EntryView())
            panel_sent = 1
        except discord.Forbidden:
            pass

    # 4) purge total (agressivo)
    del_ch = del_cat = 0
    if AGGRESSIVE_CHANNELS:
        del_ch, del_cat = await aggressive_purge_not_in_config(guild, cats)

    # 5) membros
    pending_added, pending_removed, without_member, bypass_count = await enforce_membership(guild)

    # 6) garantir pendente sem escrever em qualquer chat
    locked_text = await ensure_pending_cannot_write_any_text(guild)

    # 7) somente leitura (regras/avisos)
    ro_changed = await ensure_read_only_channels(guild, cats)

    await log_event(
        guild,
        f"🧾 Setup por: {interaction.user} | del_ch={del_ch} del_cat={del_cat} rdel={rdel} pin={pin_ok}"
    )

    await interaction.followup.send(
        "✅ Setup finalizado.\n"
        f"🧹 Limpeza: canais removidos **{del_ch}**, categorias removidas **{del_cat}**.\n"
        f"🎭 Cargos: sync **{ru}**, removidos **{rdel}**, ignorados **{rskip}**.\n"
        f"🧾 Registro: pendentes adicionados **{pending_added}**, pendentes removidos **{pending_removed}**.\n"
        f"📌 Sem '{ROLE_MEMBER}': **{without_member}** | staff bypass: **{bypass_count}**.\n"
        f"🔒 Pendente sem escrever: aplicado em **{locked_text}** canais.\n"
        f"📌 Somente leitura (regras/avisos): **{ro_changed}** canais ajustados.\n"
        f"📌 Instruções fixadas no #entrada: **{pin_ok}**.\n"
        f"🧩 Painel enviado: **{panel_sent}**.",
        ephemeral=True,
    )


@bot.tree.command(name="status_membros", description="Mostra quantos estão sem acesso e quantos estão pendentes.")
@app_commands.checks.has_permissions(administrator=True)
async def status_membros(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro de um servidor.", ephemeral=True)

    role_member = discord.utils.get(guild.roles, name=ROLE_MEMBER)
    role_pending = discord.utils.get(guild.roles, name=ROLE_PENDING)
    if not role_member or not role_pending:
        return await interaction.response.send_message("Cargos base não existem. Rode /setup.", ephemeral=True)

    without_member = 0
    pending = 0
    bypass = 0

    for m in guild.members:
        if m.bot:
            continue
        if has_any_bypass_role(m):
            bypass += 1
            continue
        if role_member not in m.roles:
            without_member += 1
        if role_pending in m.roles:
            pending += 1

    await interaction.response.send_message(
        f"📌 Sem '{ROLE_MEMBER}': **{without_member}**\n"
        f"⛔ Com '{ROLE_PENDING}': **{pending}**\n"
        f"🛡️ Staff bypass: **{bypass}**",
        ephemeral=True,
    )


@tasks.loop(minutes=AUDIT_INTERVAL_MIN)
async def audit_members():
    for guild in bot.guilds:
        try:
            await enforce_membership(guild)
            await ensure_pending_cannot_write_any_text(guild)

            # manter #entrada read-only + pin (se alguém despinou)
            entry_cat_name = f"{ENTRY_CATEGORY_EMOJI} {ENTRY_CATEGORY_NAME}".strip()
            cat = discord.utils.get(guild.categories, name=entry_cat_name)
            if cat:
                ch = discord.utils.get(cat.text_channels, name=ENTRY_CHANNEL_NAME)
                if ch:
                    await ensure_entry_channel_policy(guild, ch)
                    await ensure_entry_instructions_pinned(ch)

        except Exception:
            pass


def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()