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
    name: str          # DISPLAY NAME (com emoji)
    raw_name: str      # NOME SEM EMOJI (pra matching/ordem)
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

REG_CFG = CONFIG.get("registration", {})
REGISTER_CHANNEL_NAME = str(REG_CFG.get("channel_name", "entrada")).strip()
REGISTER_CATEGORY_NAME = str(REG_CFG.get("category_name", "ENTRADA")).strip()
REGISTER_CATEGORY_EMOJI = str(REG_CFG.get("category_emoji", "🧟")).strip()

FORCE_ON_JOIN = bool(REG_CFG.get("force_on_join", True))
NICK_PREFIX = str(REG_CFG.get("nickname_prefix", "") or "").strip()
ROLE_UNREG = str(REG_CFG.get("unregistered_role_name", "⛔ Pendente")).strip()
ROLE_REG = str(REG_CFG.get("registered_role_name", "✅ Membro")).strip()
PING_ON_JOIN = bool(REG_CFG.get("ping_on_join_in_channel", False))
AUDIT_INTERVAL_MIN = int(REG_CFG.get("audit_interval_min", 10))

# NOVO: regra de “sem registro”
REQUIRE_REGISTERED = bool(REG_CFG.get("require_registered_role", True))
BYPASS_ROLES = set((REG_CFG.get("bypass_roles") or []))  # ex.: ["🛠️ Staff"]

CLAN_CFG = CONFIG.get("clan", {})
CLAN_NAME = str(CLAN_CFG.get("name", "ZombieClan")).strip()
GAME_NAME = str(CLAN_CFG.get("game", "Death Zone Online")).strip()
SERVER_TAG = str(CLAN_CFG.get("tag", "ZC")).strip()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Railway Variables/Secrets).")


# =========================
# Bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


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

    # garante cargos base do fluxo de entrada
    names = {x.name for x in out}
    if ROLE_REG not in names:
        out.append(RoleDef(name=ROLE_REG, color=hex_to_int_color("#2ECC71"), hoist=True))
    if ROLE_UNREG not in names:
        out.append(RoleDef(name=ROLE_UNREG, color=hex_to_int_color("#E74C3C"), hoist=True))

    return out

def build_categories(cfg: dict) -> List[CategoryDef]:
    out: List[CategoryDef] = []

    # categoria de entrada sempre existe (primeira)
    entry_channels = [
        ChannelDef(
            name=REGISTER_CHANNEL_NAME,
            type="text",
            topic=f"Entrada do clã — registre-se para liberar ({CLAN_NAME} / {GAME_NAME}).",
            slowmode=0,
            user_limit=0,
        )
    ]
    out.append(
        CategoryDef(
            name=f"{REGISTER_CATEGORY_EMOJI} {REGISTER_CATEGORY_NAME}".strip(),
            raw_name=REGISTER_CATEGORY_NAME,
            emoji=REGISTER_CATEGORY_EMOJI,
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

        out.append(
            CategoryDef(
                name=display,
                raw_name=raw,
                emoji=emoji,
                channels=channels,
            )
        )

    return out


# =========================
# Discord ensure helpers
# =========================

async def ensure_role(guild: discord.Guild, rdef: RoleDef) -> discord.Role:
    existing = discord.utils.get(guild.roles, name=rdef.name)
    color = discord.Color(rdef.color)

    if existing:
        try:
            changed = False
            if existing.color != color:
                changed = True
            if existing.hoist != rdef.hoist:
                changed = True
            if existing.mentionable != rdef.mentionable:
                changed = True

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

async def ensure_category(guild: discord.Guild, display_name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=display_name)
    if cat:
        return cat
    return await guild.create_category(display_name, reason="Create category")

async def ensure_text_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    topic: Optional[str],
    slowmode: int,
) -> discord.TextChannel:
    ch = discord.utils.get(category.text_channels, name=name)
    if ch:
        try:
            changed = False
            if ch.topic != topic:
                changed = True
            if ch.slowmode_delay != (slowmode or 0):
                changed = True
            if changed:
                await ch.edit(topic=topic, slowmode_delay=slowmode or 0, reason="Sync text")
        except discord.Forbidden:
            pass
        return ch

    return await guild.create_text_channel(
        name=name,
        category=category,
        topic=topic,
        slowmode_delay=slowmode or 0,
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
# Registration / Members enforcement
# =========================

def has_any_bypass_role(member: discord.Member) -> bool:
    if not BYPASS_ROLES:
        return False
    return any(r.name in BYPASS_ROLES for r in member.roles)

async def enforce_registration_state(guild: discord.Guild) -> Tuple[int, int, int]:
    """
    Correção de estado:
      - Se membro NÃO tem ROLE_REG e REQUIRE_REGISTERED=True => garantir ROLE_UNREG (pendente), exceto bypass_roles.
      - Se membro tem ROLE_REG => remover ROLE_UNREG.
    Retorna: (added_pending, removed_pending, members_without_registered)
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0, 0, 0

    added_pending = 0
    removed_pending = 0
    without_registered = 0

    for m in guild.members:
        if m.bot:
            continue
        if has_any_bypass_role(m):
            # bypass: não mexe
            continue

        has_reg = reg_role in m.roles
        has_unreg = unreg_role in m.roles

        if not has_reg:
            without_registered += 1

        # registrado não pode ficar pendente
        if has_reg and has_unreg:
            try:
                await m.remove_roles(unreg_role, reason="Enforce: registered cannot be pending")
                removed_pending += 1
            except discord.Forbidden:
                pass
            continue

        # se exigimos registro, todo mundo sem ROLE_REG fica pendente
        if REQUIRE_REGISTERED and (not has_reg) and (not has_unreg):
            try:
                await m.add_roles(unreg_role, reason="Enforce: missing registered role -> pending")
                added_pending += 1
            except discord.Forbidden:
                pass

    return added_pending, removed_pending, without_registered


# =========================
# Visibility enforcement
# =========================

async def ensure_entry_visibility(guild: discord.Guild, entry_channel: discord.TextChannel) -> int:
    """
    Canal de entrada:
      - @everyone: vê, mas não fala
      - Pendente: vê, mas não fala
      - Membro: vê e fala
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0

    ow = entry_channel.overwrites
    changed = False

    ow_every = ow.get(guild.default_role, discord.PermissionOverwrite())
    if ow_every.view_channel is not True:
        ow_every.view_channel = True
        changed = True
    if ow_every.send_messages is not False:
        ow_every.send_messages = False
        changed = True
    if ow_every.read_message_history is not True:
        ow_every.read_message_history = True
        changed = True
    ow[guild.default_role] = ow_every

    ow_pending = ow.get(unreg_role, discord.PermissionOverwrite())
    if ow_pending.view_channel is not True:
        ow_pending.view_channel = True
        changed = True
    if ow_pending.send_messages is not False:
        ow_pending.send_messages = False
        changed = True
    if ow_pending.read_message_history is not True:
        ow_pending.read_message_history = True
        changed = True
    ow[unreg_role] = ow_pending

    ow_member = ow.get(reg_role, discord.PermissionOverwrite())
    if ow_member.view_channel is not True:
        ow_member.view_channel = True
        changed = True
    if ow_member.send_messages is not True:
        ow_member.send_messages = True
        changed = True
    if ow_member.read_message_history is not True:
        ow_member.read_message_history = True
        changed = True
    ow[reg_role] = ow_member

    if changed:
        try:
            await entry_channel.edit(overwrites=ow, reason="Visibility: entry channel")
            return 1
        except discord.Forbidden:
            return 0
    return 0

async def ensure_category_lockdown(guild: discord.Guild, category: discord.CategoryChannel) -> int:
    """
    Para TODAS as categorias (exceto ENTRADA):
      - @everyone: não vê
      - Membro: vê
      - Pendente: não vê
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0

    ow = category.overwrites
    changed = False

    ow_every = ow.get(guild.default_role, discord.PermissionOverwrite())
    if ow_every.view_channel is not False:
        ow_every.view_channel = False
        changed = True
    ow[guild.default_role] = ow_every

    ow_member = ow.get(reg_role, discord.PermissionOverwrite())
    if ow_member.view_channel is not True:
        ow_member.view_channel = True
        changed = True
    ow[reg_role] = ow_member

    ow_pending = ow.get(unreg_role, discord.PermissionOverwrite())
    if ow_pending.view_channel is not False:
        ow_pending.view_channel = False
        changed = True
    ow[unreg_role] = ow_pending

    if changed:
        try:
            await category.edit(overwrites=ow, reason="Visibility: lockdown categories")
            return 1
        except discord.Forbidden:
            return 0
    return 0


# =========================
# Aggressive sync: channels/categories
# =========================

def protected_channel_ids(guild: discord.Guild) -> Set[int]:
    prot = set()
    for ch in [guild.system_channel, guild.rules_channel, guild.public_updates_channel]:
        if ch:
            prot.add(ch.id)
    return prot

def desired_structure(cfg: dict) -> Tuple[List[CategoryDef], Set[str], Dict[str, Set[str]]]:
    cats = build_categories(cfg)
    desired_cat_names = set([c.name for c in cats])
    per_cat: Dict[str, Set[str]] = {}
    for c in cats:
        per_cat[c.name] = set([ch.name for ch in c.channels])
    return cats, desired_cat_names, per_cat

async def aggressive_purge_not_in_config(guild: discord.Guild, cats: List[CategoryDef]) -> Tuple[int, int]:
    """
    AGRESSIVO REAL:
      - apaga TODOS os canais/categorias fora do config (salvo preserve_* e canais protegidos do Discord)
      - apaga canais extras dentro de categorias do config
    Retorna: (deleted_channels, deleted_categories)
    """
    deleted_channels = 0
    deleted_categories = 0

    protected = protected_channel_ids(guild)
    desired_cat_display = set([c.name for c in cats])
    desired_per_cat = {c.name: set(ch.name for ch in c.channels) for c in cats}

    # 1) Limpa canais dentro das categorias que existem no config (remove extras)
    for cat in list(guild.categories):
        if cat.name in PRESERVE_CATEGORIES:
            continue

        if cat.name in desired_cat_display:
            desired_names = desired_per_cat.get(cat.name, set())

            for ch in list(cat.text_channels):
                if ch.id in protected or ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Aggressive purge: extra text channel not in config")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

            for ch in list(cat.voice_channels):
                if ch.id in protected or ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Aggressive purge: extra voice channel not in config")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

    # 2) Apaga canais soltos (sem categoria) que não estão no config
    desired_all_channel_names = set()
    for c in cats:
        for ch in c.channels:
            desired_all_channel_names.add(ch.name)

    for ch in list(guild.channels):
        if getattr(ch, "id", None) in protected:
            continue
        if getattr(ch, "name", "") in PRESERVE_CHANNELS:
            continue

        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)) and ch.category is None:
            if ch.name not in desired_all_channel_names:
                try:
                    await ch.delete(reason="Aggressive purge: uncategorized channel not in config")
                    deleted_channels += 1
                except discord.Forbidden:
                    pass

    # 3) Apaga categorias fora do config (e todos os canais dentro), salvo preserve
    for cat in list(guild.categories):
        if cat.name in PRESERVE_CATEGORIES:
            continue
        if cat.name in desired_cat_display:
            continue

        # apaga canais dentro (exceto protegidos/preserve)
        for ch in list(cat.channels):
            if getattr(ch, "id", None) in protected:
                continue
            if getattr(ch, "name", "") in PRESERVE_CHANNELS:
                continue
            if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
                try:
                    await ch.delete(reason="Aggressive purge: channel in non-config category")
                    deleted_channels += 1
                except discord.Forbidden:
                    pass

        # tenta deletar categoria (vai falhar se sobrar canal protegido)
        try:
            if len(cat.channels) == 0:
                await cat.delete(reason="Aggressive purge: category not in config")
                deleted_categories += 1
        except discord.Forbidden:
            pass

    return deleted_channels, deleted_categories

async def reorder_categories_and_channels(guild: discord.Guild, cats: List[CategoryDef]) -> Tuple[int, int]:
    moved_categories = 0
    moved_channels = 0

    desired_order = [c.name for c in cats]
    existing = [discord.utils.get(guild.categories, name=n) for n in desired_order]
    existing = [c for c in existing if c is not None]

    try:
        mapping = {cat.id: i for i, cat in enumerate(existing)}
        needs = any(cat.position != i for i, cat in enumerate(existing))
        if needs:
            await guild.edit_channel_positions(positions={cat: mapping[cat.id] for cat in existing})
            moved_categories = 1
    except Exception:
        pass

    for c in cats:
        cat = discord.utils.get(guild.categories, name=c.name)
        if not cat:
            continue

        desired_names = [ch.name for ch in c.channels]
        current_channels = [x for x in cat.channels if isinstance(x, (discord.TextChannel, discord.VoiceChannel))]
        by_name = {x.name: x for x in current_channels}
        ordered = [by_name.get(n) for n in desired_names if n in by_name]

        needs = False
        for idx, ch in enumerate(ordered):
            if ch is None:
                continue
            if ch.position != idx:
                needs = True
                break

        if needs:
            try:
                await guild.edit_channel_positions(positions={ch: i for i, ch in enumerate(ordered) if ch is not None})
                moved_channels += 1
            except Exception:
                pass

    return moved_categories, moved_channels


# =========================
# Aggressive sync: roles
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

    # reorder roles (bloco abaixo do cargo do bot)
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
            await role.delete(reason="Aggressive sync: role not in config")
            deleted += 1
        except discord.Forbidden:
            skipped += 1

    return created_or_updated, deleted, skipped


# =========================
# Registration UI
# =========================

class RegisterModal(discord.ui.Modal, title="Entrada — Clã de Zumbi"):
    game_name = discord.ui.TextInput(
        label="Seu nome no jogo",
        placeholder="Ex: SamCherri",
        min_length=3,
        max_length=32,
        required=True,
    )

    platform = discord.ui.TextInput(
        label="Plataforma (PC/Console/Mobile)",
        placeholder="Ex: PC",
        min_length=2,
        max_length=16,
        required=False,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

        reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
        unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
        if not reg_role or not unreg_role:
            return await interaction.response.send_message("Cargos base não existem. Rode /setup.", ephemeral=True)

        ign = self.game_name.value.strip()
        plat = (self.platform.value or "").strip()
        suffix = f" [{plat}]" if plat else ""

        # Nick padrão: "[TAG] Nome"
        if NICK_PREFIX:
            new_nick = f"{NICK_PREFIX}{ign}{suffix}".strip()
        else:
            new_nick = f"[{SERVER_TAG}] {ign}{suffix}".strip()

        try:
            await member.edit(nick=new_nick, reason="Entrada: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mudar o apelido. Dê ao bot **Gerenciar Apelidos** e coloque o cargo do bot acima dos outros.",
                ephemeral=True,
            )

        try:
            if unreg_role in member.roles:
                await member.remove_roles(unreg_role, reason="Entrada: remove pending")
            if reg_role not in member.roles:
                await member.add_roles(reg_role, reason="Entrada: add member")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos cargos. Dê ao bot **Gerenciar Cargos** e coloque o cargo do bot acima.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Liberado.\nSeu nick agora é **{new_nick}**.",
            ephemeral=True
        )

class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Entrar no clã", style=discord.ButtonStyle.success, emoji="🧟", custom_id="register_button")
    async def register_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterModal())


# =========================
# Commands / Events
# =========================

@bot.event
async def on_ready():
    try:
        bot.add_view(RegisterView())
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
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not unreg_role:
        return

    try:
        await member.add_roles(unreg_role, reason="Auto: pending on join")
    except discord.Forbidden:
        return

    if PING_ON_JOIN:
        ch = discord.utils.get(guild.text_channels, name=REGISTER_CHANNEL_NAME)
        if ch:
            try:
                await ch.send(f"{member.mention} clique em **Entrar no clã** para liberar o servidor.")
            except discord.Forbidden:
                pass


@bot.tree.command(name="setup", description="AGRESSIVO: sincroniza TUDO do config e apaga o resto. Também verifica registro dos membros.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message(
        f"⏳ Setup agressivo: aplicando config + limpando o servidor — **{CLAN_NAME}** ({GAME_NAME})...",
        ephemeral=True
    )

    cats, _, _ = desired_structure(CONFIG)
    role_defs = build_role_defs(CONFIG)

    # 1) Roles
    ru, rdel, rskip = await sync_roles_aggressive(guild, role_defs)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.followup.send("❌ Erro: cargos base não existem.", ephemeral=True)

    # 2) Criar/ajustar categorias/canais conforme config
    entry_channel: Optional[discord.TextChannel] = None

    for cdef in cats:
        cat = await ensure_category(guild, cdef.name)

        # trava categorias exceto ENTRADA
        if norm(cdef.raw_name) != norm(REGISTER_CATEGORY_NAME):
            await ensure_category_lockdown(guild, cat)

        for ch in cdef.channels:
            if ch.type == "voice":
                await ensure_voice_channel(guild, cat, ch.name, ch.user_limit or 0)
            else:
                tch = await ensure_text_channel(guild, cat, ch.name, ch.topic, ch.slowmode or 0)
                if norm(tch.name) == norm(REGISTER_CHANNEL_NAME) and norm(cdef.raw_name) == norm(REGISTER_CATEGORY_NAME):
                    entry_channel = tch

    # 3) Canal de entrada (permissões + painel)
    panel_sent = 0
    if entry_channel:
        await ensure_entry_visibility(guild, entry_channel)
        try:
            await entry_channel.send(
                f"🧟 **ENTRADA OBRIGATÓRIA — {CLAN_NAME}**\n\n"
                f"- Jogo: **{GAME_NAME}**\n"
                f"- Tag: **[{SERVER_TAG}]**\n\n"
                f"Clique em **Entrar no clã** para liberar as áreas.",
                view=RegisterView(),
            )
            panel_sent = 1
        except discord.Forbidden:
            pass

    # 4) PURGE TOTAL: apaga tudo fora do config
    del_ch = del_cat = 0
    if AGGRESSIVE_CHANNELS:
        del_ch, del_cat = await aggressive_purge_not_in_config(guild, cats)

    # 5) Ordenar conforme config
    moved_cat, moved_ch = await reorder_categories_and_channels(guild, cats)

    # 6) Verificar/corrigir registro dos membros
    added_pending, removed_pending, without_registered = await enforce_registration_state(guild)

    await interaction.followup.send(
        "✅ Setup agressivo finalizado.\n"
        f"🧹 Purge: canais removidos **{del_ch}**, categorias removidas **{del_cat}**.\n"
        f"🧩 Ordem: categorias movidas **{moved_cat}**, canais reordenados **{moved_ch}**.\n"
        f"🎭 Cargos: sync **{ru}**, removidos **{rdel}**, ignorados **{rskip}**.\n"
        f"🧾 Registro: pendentes adicionados **{added_pending}**, pendentes removidos (já membros) **{removed_pending}**.\n"
        f"📌 Sem '{ROLE_REG}' (sem registro): **{without_registered}**.",
        ephemeral=True,
    )


@bot.tree.command(name="verificar_registro", description="Mostra quantos estão sem registro e quantos estão pendentes.")
@app_commands.checks.has_permissions(administrator=True)
async def verificar_registro(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.response.send_message("Cargos base não existem. Rode /setup.", ephemeral=True)

    pending_count = 0
    without_registered = 0
    bypass_count = 0

    for m in guild.members:
        if m.bot:
            continue
        if has_any_bypass_role(m):
            bypass_count += 1
            continue
        if reg_role not in m.roles:
            without_registered += 1
        if unreg_role in m.roles:
            pending_count += 1

    await interaction.response.send_message(
        f"📌 Sem '{ROLE_REG}' (sem registro): **{without_registered}**\n"
        f"⛔ Com '{ROLE_UNREG}' (pendente): **{pending_count}**\n"
        f"🛡️ Ignorados por bypass_roles: **{bypass_count}**",
        ephemeral=True,
    )


@tasks.loop(minutes=AUDIT_INTERVAL_MIN)
async def audit_members():
    for guild in bot.guilds:
        try:
            await enforce_registration_state(guild)
        except Exception:
            pass


def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()