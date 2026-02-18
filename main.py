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
# IMPORTANTE: nome do canal SEM emoji. Emoji fica na categoria.
REGISTER_CHANNEL_NAME = str(REG_CFG.get("channel_name", "registrar-se")).strip()
REGISTER_CATEGORY_NAME = str(REG_CFG.get("category_name", "REGISTRO")).strip()
REGISTER_CATEGORY_EMOJI = str(REG_CFG.get("category_emoji", "📋")).strip()

FORCE_ON_JOIN = bool(REG_CFG.get("force_on_join", True))
NICK_PREFIX = str(REG_CFG.get("nickname_prefix", "") or "").strip()  # opcional
ROLE_UNREG = str(REG_CFG.get("unregistered_role_name", "⛔ Não Registrado")).strip()
ROLE_REG = str(REG_CFG.get("registered_role_name", "✅ Registrado")).strip()
PING_ON_JOIN = bool(REG_CFG.get("ping_on_join_in_channel", False))
AUDIT_INTERVAL_MIN = int(REG_CFG.get("audit_interval_min", 10))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Railway Variables/Secrets).")


# =========================
# Bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # necessário para varrer membros

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

    # garante cargos base
    names = {x.name for x in out}
    if ROLE_REG not in names:
        out.append(RoleDef(name=ROLE_REG, color=hex_to_int_color("#2ECC71"), hoist=True))
    if ROLE_UNREG not in names:
        out.append(RoleDef(name=ROLE_UNREG, color=hex_to_int_color("#E74C3C"), hoist=True))

    return out

def build_categories(cfg: dict) -> List[CategoryDef]:
    out: List[CategoryDef] = []

    # categoria de registro sempre existe (primeira)
    reg_channels = [
        ChannelDef(
            name=REGISTER_CHANNEL_NAME,
            type="text",
            topic="Cadastro obrigatório para liberar o servidor.",
            slowmode=0,
            user_limit=0,
        )
    ]
    out.append(
        CategoryDef(
            name=f"{REGISTER_CATEGORY_EMOJI} {REGISTER_CATEGORY_NAME}".strip(),
            raw_name=REGISTER_CATEGORY_NAME,
            emoji=REGISTER_CATEGORY_EMOJI,
            channels=reg_channels,
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
# Members enforcement
# =========================

def is_member_without_roles(member: discord.Member) -> bool:
    return (not member.bot) and len(member.roles) <= 1  # só @everyone

async def enforce_unregistered_for_no_role_members(guild: discord.Guild) -> Tuple[int, int]:
    """
    - Adiciona 'Não Registrado' para membros sem cargo.
    - Remove 'Não Registrado' de quem já tem 'Registrado'.
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0, 0

    added = 0
    fixed = 0

    for m in guild.members:
        if m.bot:
            continue

        if reg_role in m.roles and unreg_role in m.roles:
            try:
                await m.remove_roles(unreg_role, reason="Enforce: registered cannot be unregistered")
                fixed += 1
            except discord.Forbidden:
                pass
            continue

        if is_member_without_roles(m) and (reg_role not in m.roles) and (unreg_role not in m.roles):
            try:
                await m.add_roles(unreg_role, reason="Enforce: no roles -> unregistered")
                added += 1
            except discord.Forbidden:
                pass

    return added, fixed


# =========================
# Visibility enforcement (leve, sem “edit em tudo”)
# =========================

async def ensure_registration_visibility(guild: discord.Guild, reg_channel: discord.TextChannel) -> int:
    """
    Canal registrar-se:
      - @everyone: vê, mas não fala
      - Não Registrado: vê, mas não fala
      - Registrado: vê e fala
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0

    ow = reg_channel.overwrites

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

    ow_unreg = ow.get(unreg_role, discord.PermissionOverwrite())
    if ow_unreg.view_channel is not True:
        ow_unreg.view_channel = True
        changed = True
    if ow_unreg.send_messages is not False:
        ow_unreg.send_messages = False
        changed = True
    if ow_unreg.read_message_history is not True:
        ow_unreg.read_message_history = True
        changed = True
    ow[unreg_role] = ow_unreg

    ow_reg = ow.get(reg_role, discord.PermissionOverwrite())
    if ow_reg.view_channel is not True:
        ow_reg.view_channel = True
        changed = True
    if ow_reg.send_messages is not True:
        ow_reg.send_messages = True
        changed = True
    if ow_reg.read_message_history is not True:
        ow_reg.read_message_history = True
        changed = True
    ow[reg_role] = ow_reg

    if changed:
        try:
            await reg_channel.edit(overwrites=ow, reason="Visibility: registration channel")
            return 1
        except discord.Forbidden:
            return 0
    return 0

async def ensure_category_lockdown(guild: discord.Guild, category: discord.CategoryChannel) -> int:
    """
    Para TODAS as categorias (exceto REGISTRO):
      - @everyone: não vê
      - Registrado: vê
      - Não Registrado: não vê
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

    ow_reg = ow.get(reg_role, discord.PermissionOverwrite())
    if ow_reg.view_channel is not True:
        ow_reg.view_channel = True
        changed = True
    ow[reg_role] = ow_reg

    ow_unreg = ow.get(unreg_role, discord.PermissionOverwrite())
    if ow_unreg.view_channel is not False:
        ow_unreg.view_channel = False
        changed = True
    ow[unreg_role] = ow_unreg

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
    """
    Returns:
      - categories list (ordered)
      - desired category names (display)
      - desired channels per category (name set)
    """
    cats = build_categories(cfg)
    desired_cat_names = set([c.name for c in cats])
    per_cat: Dict[str, Set[str]] = {}
    for c in cats:
        per_cat[c.name] = set([ch.name for ch in c.channels])
    return cats, desired_cat_names, per_cat

async def delete_extra_channels_and_categories(guild: discord.Guild, cats: List[CategoryDef]) -> Tuple[int, int]:
    """
    AGRESSIVO:
      - remove canais duplicados/fora do config
      - remove categorias fora do config (se vazias depois da limpeza)
    """
    deleted_channels = 0
    deleted_categories = 0

    protected = protected_channel_ids(guild)

    desired_cat_display = set([c.name for c in cats])
    desired_per_cat = {c.name: set(ch.name for ch in c.channels) for c in cats}

    # 1) Limpar canais dentro de categorias desejadas (remove extras/duplicados)
    for cat in guild.categories:
        if cat.name in PRESERVE_CATEGORIES:
            continue

        if cat.name in desired_cat_display:
            desired_names = desired_per_cat.get(cat.name, set())

            # text
            for ch in list(cat.text_channels):
                if ch.id in protected:
                    continue
                if ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Aggressive sync: remove extra text channel")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

            # voice
            for ch in list(cat.voice_channels):
                if ch.id in protected:
                    continue
                if ch.name in PRESERVE_CHANNELS:
                    continue
                if ch.name not in desired_names:
                    try:
                        await ch.delete(reason="Aggressive sync: remove extra voice channel")
                        deleted_channels += 1
                    except discord.Forbidden:
                        pass

    # 2) Canais “soltos” fora de categoria (ex: #geral)
    for ch in list(guild.channels):
        if ch.id in protected:
            continue
        if ch.name in PRESERVE_CHANNELS:
            continue
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel)) and ch.category is None:
            # só mantém se estiver no config em alguma categoria (não deveria)
            should_keep = False
            for c in cats:
                if ch.name in set(x.name for x in c.channels):
                    should_keep = True
                    break
            if not should_keep:
                try:
                    await ch.delete(reason="Aggressive sync: remove uncategorized channel")
                    deleted_channels += 1
                except discord.Forbidden:
                    pass

    # 3) Categorias fora do config: delete (se não preservada)
    for cat in list(guild.categories):
        if cat.name in PRESERVE_CATEGORIES:
            continue
        if cat.name not in desired_cat_display:
            # tenta deletar (vai falhar se tiver canais que não deu pra apagar por permissão)
            try:
                # só deleta se vazia
                if len(cat.channels) == 0:
                    await cat.delete(reason="Aggressive sync: remove extra category")
                    deleted_categories += 1
            except discord.Forbidden:
                pass

    return deleted_channels, deleted_categories

async def reorder_categories_and_channels(guild: discord.Guild, cats: List[CategoryDef]) -> Tuple[int, int]:
    """
    Ordena categorias e canais conforme config.
    """
    moved_categories = 0
    moved_channels = 0

    # categorias na ordem do config
    desired_order = [c.name for c in cats]
    existing = [discord.utils.get(guild.categories, name=n) for n in desired_order]
    existing = [c for c in existing if c is not None]

    try:
        # edit_channel_positions aceita lista de (channel, position)
        mapping = {cat.id: i for i, cat in enumerate(existing)}
        # só aplica se realmente diferente
        needs = False
        for i, cat in enumerate(existing):
            if cat.position != i:
                needs = True
                break
        if needs:
            await guild.edit_channel_positions(positions={cat: mapping[cat.id] for cat in existing})
            moved_categories = 1
    except Exception:
        pass

    # canais dentro de cada categoria
    for c in cats:
        cat = discord.utils.get(guild.categories, name=c.name)
        if not cat:
            continue

        # desired order inside category: text first then voice, in config order (exatamente como no YAML)
        desired_names = [ch.name for ch in c.channels]
        current_channels = [x for x in cat.channels if isinstance(x, (discord.TextChannel, discord.VoiceChannel))]
        # map by name (se duplicado, vai pegar um; o resto deve ter sido removido no agressivo)
        by_name = {x.name: x for x in current_channels}

        ordered = [by_name.get(n) for n in desired_names if n in by_name]
        # atual ordem
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
# Aggressive sync: roles (create/update/order/delete)
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
    """
    - cria/atualiza roles do config
    - ordena conforme config (último do config fica mais alto dentro do nosso bloco)
    - apaga roles não desejados (somente se bot tem permissão/posição)
    """
    created_or_updated = 0

    desired_names = [r.name for r in desired]
    desired_set = set(desired_names)

    # 1) ensure roles
    ensured: List[discord.Role] = []
    for rdef in desired:
        r = await ensure_role(guild, rdef)
        ensured.append(r)
        created_or_updated += 1

    # 2) reorder roles: precisamos posicionar abaixo do cargo do bot (top_role)
    # Discord: posição maior = mais alto
    # vamos colocar o “mais alto” como o último no YAML.
    try:
        bot_top = guild.me.top_role.position
        movable = [r for r in ensured if r.position < bot_top and not r.managed and not r.is_default()]

        # ordenar conforme config
        name_to_role = {r.name: r for r in movable}
        ordered = [name_to_role[n] for n in desired_names if n in name_to_role]

        # posições alvo: vamos colocar um bloco logo abaixo do bot_top
        base = bot_top - len(ordered)
        if base < 1:
            base = 1

        positions = {}
        for i, role in enumerate(ordered):
            positions[role] = base + i

        # só aplica se mudou
        needs = any(role.position != positions.get(role, role.position) for role in ordered)
        if needs:
            await guild.edit_role_positions(positions=positions)
    except Exception:
        pass

    deleted = 0
    skipped = 0

    if not AGGRESSIVE_ROLES:
        return created_or_updated, deleted, skipped

    # 3) delete roles not in config (agressivo)
    bot_top = guild.me.top_role.position
    for role in list(guild.roles):
        if role_is_protected(guild, role):
            continue
        if role.name in desired_set:
            continue
        # só tenta se bot consegue
        if role.position >= bot_top:
            skipped += 1
            continue
        try:
            await role.delete(reason="Aggressive sync: remove extra role (not in config)")
            deleted += 1
        except discord.Forbidden:
            skipped += 1

    return created_or_updated, deleted, skipped


# =========================
# Registration UI
# =========================

class RegisterModal(discord.ui.Modal, title="Cadastro — Death Zone Online"):
    game_name = discord.ui.TextInput(
        label="Seu nome no jogo",
        placeholder="Ex: Sam Cherri",
        min_length=3,
        max_length=32,
        required=True,
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

        new_nick = f"{NICK_PREFIX}{self.game_name.value}".strip()

        try:
            await member.edit(nick=new_nick, reason="Cadastro: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mudar o apelido. Dê ao bot **Gerenciar Apelidos** e coloque o cargo do bot acima dos outros.",
                ephemeral=True,
            )

        try:
            if unreg_role in member.roles:
                await member.remove_roles(unreg_role, reason="Cadastro: remove unregistered")
            if reg_role not in member.roles:
                await member.add_roles(reg_role, reason="Cadastro: add registered")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos cargos. Dê ao bot **Gerenciar Cargos** e coloque o cargo do bot acima.",
                ephemeral=True,
            )

        await interaction.response.send_message(f"✅ Registrado. Seu nick agora é **{new_nick}**.", ephemeral=True)

class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cadastrar", style=discord.ButtonStyle.success, emoji="✅", custom_id="register_button")
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

    guild = member.guild
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not unreg_role:
        return

    try:
        await member.add_roles(unreg_role, reason="Auto: force registration on join")
    except discord.Forbidden:
        return

    if PING_ON_JOIN:
        ch = discord.utils.get(guild.text_channels, name=REGISTER_CHANNEL_NAME)
        if ch:
            try:
                await ch.send(f"{member.mention} faça seu cadastro clicando em **Cadastrar**.")
            except discord.Forbidden:
                pass


@bot.tree.command(name="setup", description="Sincroniza estrutura (agressivo) + força cadastro (visibilidade + membros).")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message("⏳ Setup: sincronizando (agressivo) + aplicando cadastro...", ephemeral=True)

    cats, _, _ = desired_structure(CONFIG)
    role_defs = build_role_defs(CONFIG)

    # 1) Roles (agressivo + ordenação)
    ru, rdel, rskip = await sync_roles_aggressive(guild, role_defs)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.followup.send("❌ Erro: cargos base não existem.", ephemeral=True)

    # 2) Estrutura do config (categorias/canais)
    reg_channel: Optional[discord.TextChannel] = None

    for cdef in cats:
        cat = await ensure_category(guild, cdef.name)

        # lock nas categorias (exceto registro, que é tratado pelo canal)
        if norm(cdef.raw_name) != norm(REGISTER_CATEGORY_NAME):
            await ensure_category_lockdown(guild, cat)

        for ch in cdef.channels:
            if ch.type == "voice":
                await ensure_voice_channel(guild, cat, ch.name, ch.user_limit or 0)
            else:
                tch = await ensure_text_channel(guild, cat, ch.name, ch.topic, ch.slowmode or 0)
                if norm(tch.name) == norm(REGISTER_CHANNEL_NAME) and norm(cdef.raw_name) == norm(REGISTER_CATEGORY_NAME):
                    reg_channel = tch

    # 3) Canal de registro (visibilidade + painel)
    panel_sent = 0
    if reg_channel:
        await ensure_registration_visibility(guild, reg_channel)
        try:
            await reg_channel.send(
                "📋 **REGISTRO OBRIGATÓRIO**\n\nClique em **Cadastrar** para liberar o servidor.",
                view=RegisterView(),
            )
            panel_sent = 1
        except discord.Forbidden:
            pass

    # 4) AGRESSIVO: deletar extras (canais/categorias fora do config)
    del_ch = del_cat = 0
    if AGGRESSIVE_CHANNELS:
        del_ch, del_cat = await delete_extra_channels_and_categories(guild, cats)

    # 5) Ordenar categorias/canais conforme config
    moved_cat, moved_ch = await reorder_categories_and_channels(guild, cats)

    # 6) Forçar membros sem cargo => não registrado
    added, fixed = await enforce_unregistered_for_no_role_members(guild)

    await interaction.followup.send(
        "✅ Setup finalizado.\n"
        f"👥 Membros: **{added}** receberam '{ROLE_UNREG}' (sem cargo), **{fixed}** corrigidos.\n"
        f"🧩 Estrutura: painel enviado **{panel_sent}**, categorias movidas **{moved_cat}**, canais reordenados **{moved_ch}**.\n"
        f"🧹 Agressivo: canais removidos **{del_ch}**, categorias removidas **{del_cat}**.\n"
        f"🎖️ Cargos: sync **{ru}**, removidos **{rdel}**, ignorados (sem permissão/posição) **{rskip}**.",
        ephemeral=True,
    )


@bot.tree.command(name="verificar_registro", description="Mostra quantos ainda estão como Não Registrado e quantos estão sem cargo.")
@app_commands.checks.has_permissions(administrator=True)
async def verificar_registro(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not unreg_role:
        return await interaction.response.send_message("Cargo 'Não Registrado' não existe.", ephemeral=True)

    total_unreg = len(unreg_role.members)
    sem_cargo = sum(1 for m in guild.members if is_member_without_roles(m))

    await interaction.response.send_message(
        f"⛔ Não Registrados (cargo): **{total_unreg}**\n"
        f"📌 Membros sem cargo (só @everyone): **{sem_cargo}**",
        ephemeral=True,
    )


@tasks.loop(minutes=AUDIT_INTERVAL_MIN)
async def audit_members():
    for guild in bot.guilds:
        try:
            await enforce_unregistered_for_no_role_members(guild)
            # não roda agressivo aqui pra não deletar coisas sem você pedir
        except Exception:
            pass


def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()