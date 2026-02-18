import os
from dataclasses import dataclass
from typing import List, Optional, Set

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
    name: str
    emoji: str
    channels: List[ChannelDef]


# =========================
# Load config
# =========================

CONFIG = load_config("config.yaml")

REG_CFG = CONFIG.get("registration", {})
REGISTER_CHANNEL_NAME = str(REG_CFG.get("channel_name", "📋-registrar-se"))
FORCE_ON_JOIN = bool(REG_CFG.get("force_on_join", True))

# Opção A: nickname_prefix vazio -> nick fica exatamente igual ao nome do jogo
NICK_PREFIX = str(REG_CFG.get("nickname_prefix", "") or "")

ROLE_UNREG = str(REG_CFG.get("unregistered_role_name", "⛔ Não Registrado"))
ROLE_REG = str(REG_CFG.get("registered_role_name", "✅ Registrado"))
PING_ON_JOIN = bool(REG_CFG.get("ping_on_join_in_channel", False))
AUDIT_INTERVAL_MIN = int(REG_CFG.get("audit_interval_min", 10))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Secrets/Variables).")


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

async def ensure_role(guild: discord.Guild, rdef: RoleDef) -> discord.Role:
    existing = discord.utils.get(guild.roles, name=rdef.name)
    color = discord.Color(rdef.color)

    if existing:
        try:
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
    if ch:
        try:
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

def build_role_defs(cfg: dict) -> List[RoleDef]:
    out: List[RoleDef] = []
    for r in (cfg.get("roles") or []):
        out.append(
            RoleDef(
                name=str(r.get("name", "")).strip(),
                color=hex_to_int_color(r.get("color", "#95A5A6")),
                hoist=bool(r.get("hoist", False)),
                mentionable=bool(r.get("mentionable", False)),
            )
        )

    names = {x.name for x in out}
    if ROLE_REG not in names:
        out.append(RoleDef(name=ROLE_REG, color=hex_to_int_color("#2ECC71"), hoist=True))
    if ROLE_UNREG not in names:
        out.append(RoleDef(name=ROLE_UNREG, color=hex_to_int_color("#E74C3C"), hoist=True))

    return [r for r in out if r.name]

def build_categories(cfg: dict) -> List[CategoryDef]:
    out: List[CategoryDef] = []
    for c in (cfg.get("categories") or []):
        channels: List[ChannelDef] = []
        for ch in (c.get("channels") or []):
            channels.append(
                ChannelDef(
                    name=str(ch.get("name", "")).strip(),
                    type=str(ch.get("type", "text")).strip().lower(),
                    topic=ch.get("topic"),
                    slowmode=int(ch.get("slowmode", 0) or 0),
                    user_limit=int(ch.get("user_limit", 0) or 0),
                )
            )
        out.append(
            CategoryDef(
                name=f"{c.get('emoji', '📁')} {str(c.get('name', '')).strip()}".strip(),
                emoji=str(c.get("emoji", "📁")),
                channels=channels,
            )
        )
    return out


# =========================
# Enforcement: members
# =========================

def is_member_without_roles(member: discord.Member) -> bool:
    # member.roles sempre contém @everyone; sem cargos => len == 1
    return (not member.bot) and len(member.roles) <= 1

async def enforce_unregistered_for_no_role_members(guild: discord.Guild) -> tuple[int, int]:
    """
    - Adiciona 'Não Registrado' para membros sem cargo.
    - Remove 'Não Registrado' de quem já tem 'Registrado' (consistência).
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
# Enforcement: visibility
# =========================

def protected_channel_ids(guild: discord.Guild) -> Set[int]:
    prot = set()
    for ch in [guild.system_channel, guild.rules_channel, guild.public_updates_channel]:
        if ch:
            prot.add(ch.id)
    return prot

async def enforce_visibility_rules(guild: discord.Guild) -> tuple[int, int]:
    """
    Regra:
      - Em TODOS os canais/categorias: 'Não Registrado' não pode ver
      - EXCEÇÃO: canal de registro -> 'Não Registrado' pode ver, mas não falar
    """
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return 0, 0

    protected = protected_channel_ids(guild)
    changed_categories = 0
    changed_channels = 0

    # categorias: unreg não vê
    for cat in guild.categories:
        try:
            ow = cat.overwrites
            ow_unreg = ow.get(unreg_role, discord.PermissionOverwrite())
            if ow_unreg.view_channel is not False:
                ow_unreg.view_channel = False
                ow[unreg_role] = ow_unreg
                await cat.edit(overwrites=ow, reason="Enforce: hide unregistered on categories")
                changed_categories += 1
        except discord.Forbidden:
            pass

    # canais: unreg não vê, exceto registrar
    for ch in guild.channels:
        if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
            continue
        if ch.id in protected:
            continue

        is_register = (norm(ch.name) == norm(REGISTER_CHANNEL_NAME))

        try:
            ow = ch.overwrites

            if is_register:
                # unreg vê mas não fala
                ow_unreg = ow.get(unreg_role, discord.PermissionOverwrite())
                ow_unreg.view_channel = True
                if isinstance(ch, discord.TextChannel):
                    ow_unreg.send_messages = False
                    ow_unreg.read_message_history = True
                ow[unreg_role] = ow_unreg

                # everyone vê mas não fala
                ow_every = ow.get(guild.default_role, discord.PermissionOverwrite())
                ow_every.view_channel = True
                if isinstance(ch, discord.TextChannel):
                    ow_every.send_messages = False
                    ow_every.read_message_history = True
                ow[guild.default_role] = ow_every

                # registrado vê e fala (texto)
                ow_reg = ow.get(reg_role, discord.PermissionOverwrite())
                ow_reg.view_channel = True
                if isinstance(ch, discord.TextChannel):
                    ow_reg.send_messages = True
                    ow_reg.read_message_history = True
                ow[reg_role] = ow_reg
            else:
                ow_unreg = ow.get(unreg_role, discord.PermissionOverwrite())
                if ow_unreg.view_channel is not False:
                    ow_unreg.view_channel = False
                    ow[unreg_role] = ow_unreg

            await ch.edit(overwrites=ow, reason="Enforce: visibility rules")
            changed_channels += 1
        except discord.Forbidden:
            pass

    return changed_categories, changed_channels


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

        # NICK = exatamente o nome do jogo (prefixo vazio)
        new_nick = f"{NICK_PREFIX}{self.game_name.value}".strip()

        try:
            await member.edit(nick=new_nick, reason="Cadastro: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mudar o apelido. Dê ao bot 'Gerenciar Apelidos' e coloque o cargo do bot acima dos outros.",
                ephemeral=True,
            )

        try:
            if unreg_role in member.roles:
                await member.remove_roles(unreg_role, reason="Cadastro: remove unregistered")
            if reg_role not in member.roles:
                await member.add_roles(reg_role, reason="Cadastro: add registered")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos cargos. Dê ao bot 'Gerenciar Cargos' e coloque o cargo do bot acima.",
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

@bot.tree.command(name="setup", description="Cria/sincroniza estrutura e força cadastro (visibilidade + membros).")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message("⏳ Setup: criando estrutura + aplicando regras...", ephemeral=True)

    # 1) Roles do config
    for rdef in build_role_defs(CONFIG):
        await ensure_role(guild, rdef)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.followup.send("❌ Erro: cargos base não existem.", ephemeral=True)

    # 2) Estrutura do config
    reg_channel: Optional[discord.TextChannel] = None
    for cdef in build_categories(CONFIG):
        cat = await ensure_category(guild, cdef.name)
        for ch in cdef.channels:
            if ch.type == "voice":
                await ensure_voice_channel(guild, cat, ch.name, ch.user_limit or 0)
            else:
                tch = await ensure_text_channel(guild, cat, ch.name, ch.topic, ch.slowmode or 0)
                if norm(tch.name) == norm(REGISTER_CHANNEL_NAME):
                    reg_channel = tch

    # 3) Painel de cadastro
    if reg_channel:
        try:
            await reg_channel.send(
                "📋 **REGISTRO OBRIGATÓRIO**\n\nClique em **Cadastrar** para liberar o servidor.",
                view=RegisterView(),
            )
        except discord.Forbidden:
            pass

    # 4) Forçar visibilidade (não registrado só vê registrar)
    cats_changed, ch_changed = await enforce_visibility_rules(guild)

    # 5) Forçar membros sem cargo => não registrado
    added, fixed = await enforce_unregistered_for_no_role_members(guild)

    await interaction.followup.send(
        "✅ Setup finalizado.\n"
        f"🔒 Visibilidade aplicada: categorias alteradas **{cats_changed}**, canais ajustados **{ch_changed}**.\n"
        f"👥 Membros: **{added}** receberam '{ROLE_UNREG}' (sem cargo), **{fixed}** corrigidos (registrado+não registrado).",
        ephemeral=True,
    )

@bot.tree.command(name="verificar_registro", description="Mostra quantos membros ainda estão como Não Registrado e quantos estão sem cargo.")
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
            await enforce_visibility_rules(guild)
        except Exception:
            pass

def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()