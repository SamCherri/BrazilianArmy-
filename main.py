import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord import app_commands
from discord.ext import commands
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
REGISTER_CHANNEL_NAME = REG_CFG.get("channel_name", "📋-registrar-se")
FORCE_ON_JOIN = bool(REG_CFG.get("force_on_join", True))
NICK_PREFIX = str(REG_CFG.get("nickname_prefix", "") or "")
ROLE_UNREG = str(REG_CFG.get("unregistered_role_name", "⛔ Não Registrado"))
ROLE_REG = str(REG_CFG.get("registered_role_name", "✅ Registrado"))
PING_ON_JOIN = bool(REG_CFG.get("ping_on_join_in_channel", False))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Secrets/Variables).")


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
    overwrites: Optional[dict] = None,
) -> discord.TextChannel:
    ch = discord.utils.get(category.text_channels, name=name)
    if ch:
        try:
            await ch.edit(topic=topic, slowmode_delay=slowmode or 0, overwrites=overwrites, reason="Sync text")
        except discord.Forbidden:
            pass
        return ch
    return await guild.create_text_channel(
        name=name,
        category=category,
        topic=topic,
        slowmode_delay=slowmode or 0,
        overwrites=overwrites,
        reason="Create text",
    )

async def ensure_voice_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    user_limit: int,
    overwrites: Optional[dict] = None,
) -> discord.VoiceChannel:
    ch = discord.utils.get(category.voice_channels, name=name)
    if ch:
        try:
            await ch.edit(user_limit=user_limit or 0, overwrites=overwrites, reason="Sync voice")
        except discord.Forbidden:
            pass
        return ch
    return await guild.create_voice_channel(
        name=name,
        category=category,
        user_limit=user_limit or 0,
        overwrites=overwrites,
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

    # garante cargos base mesmo se esquecer
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

def overwrites_for_registration(
    guild: discord.Guild,
    reg_role: discord.Role,
    unreg_role: discord.Role,
    is_registration_channel: bool,
) -> dict:
    everyone = guild.default_role

    # padrão: só Registrado vê
    ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        unreg_role: discord.PermissionOverwrite(view_channel=False),
        reg_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    # canal de registro: todo mundo vê, não registrado vê, mas não fala (botões funcionam)
    if is_registration_channel:
        ow[everyone] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
        ow[unreg_role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)

    return ow

def protected_channel_ids(guild: discord.Guild) -> Set[int]:
    prot = set()
    for ch in [guild.system_channel, guild.rules_channel, guild.public_updates_channel]:
        if ch:
            prot.add(ch.id)
    return prot

def desired_from_config(cfg: dict) -> Tuple[Set[str], Set[Tuple[str, str, str]]]:
    """
    Returns:
      desired_categories: set(category_name)
      desired_channels: set( (category_name, channel_type, channel_name) )
    """
    desired_categories: Set[str] = set()
    desired_channels: Set[Tuple[str, str, str]] = set()

    cats = build_categories(cfg)
    for c in cats:
        desired_categories.add(c.name)
        for ch in c.channels:
            desired_channels.add((c.name, ch.type, ch.name))
    return desired_categories, desired_channels

def desired_roles_from_config(cfg: dict) -> Set[str]:
    desired = set()
    for r in (cfg.get("roles") or []):
        name = str(r.get("name", "")).strip()
        if name:
            desired.add(name)

    # sempre inclui cargos base
    desired.add(ROLE_REG)
    desired.add(ROLE_UNREG)
    return desired


# =========================
# Registration UI
# =========================

class RegisterModal(discord.ui.Modal, title="Cadastro Death Zone Online"):
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
            return await interaction.response.send_message("Erro: guild/member inválido.", ephemeral=True)

        reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
        unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
        if not reg_role or not unreg_role:
            return await interaction.response.send_message("Erro: cargos base não existem. Rode /setup.", ephemeral=True)

        new_nick = f"{NICK_PREFIX}{self.game_name.value}".strip()

        try:
            await member.edit(nick=new_nick, reason="Cadastro: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mudar seu apelido. O bot precisa de 'Gerenciar Apelidos' e estar acima dos cargos.",
                ephemeral=True,
            )

        try:
            if unreg_role in member.roles:
                await member.remove_roles(unreg_role, reason="Cadastro: remove unregistered")
            if reg_role not in member.roles:
                await member.add_roles(reg_role, reason="Cadastro: add registered")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos cargos. O bot precisa de 'Gerenciar Cargos' e estar acima dos cargos.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            f"✅ Registrado com sucesso. Seu nick agora é **{new_nick}**.",
            ephemeral=True,
        )

class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cadastrar", style=discord.ButtonStyle.success, emoji="✅", custom_id="register_button")
    async def register_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterModal())


# =========================
# AGGRESSIVE CLEANUP - Channels/Categories
# =========================

async def aggressive_cleanup_channels(guild: discord.Guild, desired_categories: Set[str], desired_channels: Set[Tuple[str, str, str]]) -> Tuple[int, int, int]:
    protected_ids = protected_channel_ids(guild)

    deleted_text = 0
    deleted_voice = 0
    deleted_cats = 0

    # 1) Canais em categorias: extras + duplicados
    for cat in list(guild.categories):
        seen_text = set()
        seen_voice = set()

        for ch in list(cat.text_channels):
            if ch.id in protected_ids:
                continue
            key = (cat.name, "text", ch.name)
            if key not in desired_channels:
                try:
                    await ch.delete(reason="Aggressive cleanup: text not in config")
                    deleted_text += 1
                except discord.Forbidden:
                    pass
                continue

            dup_key = ("text", norm(ch.name))
            if dup_key in seen_text:
                try:
                    await ch.delete(reason="Aggressive cleanup: duplicate text")
                    deleted_text += 1
                except discord.Forbidden:
                    pass
            else:
                seen_text.add(dup_key)

        for ch in list(cat.voice_channels):
            if ch.id in protected_ids:
                continue
            key = (cat.name, "voice", ch.name)
            if key not in desired_channels:
                try:
                    await ch.delete(reason="Aggressive cleanup: voice not in config")
                    deleted_voice += 1
                except discord.Forbidden:
                    pass
                continue

            dup_key = ("voice", norm(ch.name))
            if dup_key in seen_voice:
                try:
                    await ch.delete(reason="Aggressive cleanup: duplicate voice")
                    deleted_voice += 1
                except discord.Forbidden:
                    pass
            else:
                seen_voice.add(dup_key)

    # 2) Canais soltos (sem categoria) -> apaga (config sempre cria em categoria)
    for ch in list(guild.text_channels):
        if ch.category is not None:
            continue
        if ch.id in protected_ids:
            continue
        try:
            await ch.delete(reason="Aggressive cleanup: root text channel")
            deleted_text += 1
        except discord.Forbidden:
            pass

    for ch in list(guild.voice_channels):
        if ch.category is not None:
            continue
        if ch.id in protected_ids:
            continue
        try:
            await ch.delete(reason="Aggressive cleanup: root voice channel")
            deleted_voice += 1
        except discord.Forbidden:
            pass

    # 3) Categorias fora do config -> apaga (se não tiver canal protegido)
    for cat in list(guild.categories):
        if cat.name in desired_categories:
            continue
        if any(ch.id in protected_ids for ch in cat.channels):
            continue
        try:
            # garantia: apaga canais remanescentes
            for ch in list(cat.channels):
                if ch.id in protected_ids:
                    continue
                try:
                    await ch.delete(reason="Aggressive cleanup: purge remaining")
                except discord.Forbidden:
                    pass
            await cat.delete(reason="Aggressive cleanup: category not in config")
            deleted_cats += 1
        except discord.Forbidden:
            pass

    return deleted_text, deleted_voice, deleted_cats


# =========================
# AGGRESSIVE CLEANUP - Roles
# =========================

async def aggressive_cleanup_roles(guild: discord.Guild, desired_role_names: Set[str]) -> Tuple[int, int]:
    """
    Apaga:
      - cargos não desejados
      - duplicados pelo mesmo nome (mantém 1)
    Protege:
      - @everyone
      - cargos managed (integrações/bots)
      - cargo do bot
      - cargos acima/igual ao cargo do bot (não dá)
    """
    bot_member = guild.me
    if bot_member is None:
        return 0, 0

    bot_top = bot_member.top_role

    deleted_roles = 0
    deleted_dupes = 0

    # 1) Resolver duplicados por nome: manter 1 (o mais alto)
    # map normalized name -> list of roles
    by_name: Dict[str, List[discord.Role]] = {}
    for r in guild.roles:
        if r.is_default() or r.managed:
            continue
        if r.id == bot_top.id:
            continue
        by_name.setdefault(norm(r.name), []).append(r)

    for name_norm, roles in by_name.items():
        if len(roles) <= 1:
            continue

        # ordena por posição desc e mantém a primeira
        roles.sort(key=lambda x: x.position, reverse=True)
        keep = roles[0]
        for r in roles[1:]:
            # só se bot conseguir (role abaixo do bot)
            if r >= bot_top:
                continue
            try:
                await r.delete(reason="Aggressive cleanup: duplicate role")
                deleted_dupes += 1
            except discord.Forbidden:
                pass

    # 2) Apagar cargos fora do config
    desired_norm = {norm(x) for x in desired_role_names}

    for r in list(guild.roles):
        if r.is_default() or r.managed:
            continue
        if r.id == bot_top.id:
            continue

        # bot só apaga se o cargo estiver abaixo dele
        if r >= bot_top:
            continue

        if norm(r.name) not in desired_norm:
            try:
                await r.delete(reason="Aggressive cleanup: role not in config")
                deleted_roles += 1
            except discord.Forbidden:
                pass

    return deleted_roles, deleted_dupes


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


@bot.tree.command(name="setup", description="AGRESSIVO: sincroniza e apaga tudo fora do config (canais/categorias/cargos).")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message("⏳ Setup AGRESSIVO em execução...", ephemeral=True)

    # 1) Criar/atualizar cargos do config
    role_defs = build_role_defs(CONFIG)
    for rdef in role_defs:
        await ensure_role(guild, rdef)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.followup.send("❌ Erro: cargos base não existem.", ephemeral=True)

    # 2) Criar/atualizar estrutura do config
    categories = build_categories(CONFIG)
    reg_channel: Optional[discord.TextChannel] = None

    for cdef in categories:
        cat = await ensure_category(guild, cdef.name)
        for ch in cdef.channels:
            is_reg = (norm(ch.name) == norm(REGISTER_CHANNEL_NAME))
            ow = overwrites_for_registration(guild, reg_role, unreg_role, is_registration_channel=is_reg)

            if ch.type == "voice":
                await ensure_voice_channel(guild, cat, ch.name, ch.user_limit or 0, ow)
            else:
                tch = await ensure_text_channel(guild, cat, ch.name, ch.topic, ch.slowmode or 0, ow)
                if is_reg:
                    reg_channel = tch

    # 3) Posta o painel (sempre)
    if reg_channel:
        try:
            await reg_channel.send(
                "📋 **REGISTRO OBRIGATÓRIO**\n\nClique em **Cadastrar** para liberar o servidor.",
                view=RegisterView(),
            )
        except discord.Forbidden:
            pass

    # 4) Limpeza AGRESSIVA de canais/categorias
    desired_categories, desired_channels = desired_from_config(CONFIG)
    dt, dv, dc = await aggressive_cleanup_channels(guild, desired_categories, desired_channels)

    # 5) Limpeza AGRESSIVA de cargos
    desired_roles = desired_roles_from_config(CONFIG)
    dr, dd = await aggressive_cleanup_roles(guild, desired_roles)

    await interaction.followup.send(
        f"✅ Setup AGRESSIVO concluído.\n"
        f"🧹 Canais: apagados **{dt}** texto, **{dv}** voz, **{dc}** categorias.\n"
        f"🧹 Cargos: apagados **{dr}** fora do config, **{dd}** duplicados.\n\n"
        f"Se algum cargo não foi apagado, é porque está acima do bot ou é managed (Discord não permite).",
        ephemeral=True
    )

@setup_cmd.error
async def setup_error(interaction: discord.Interaction, error: Exception):
    try:
        await interaction.response.send_message(f"❌ Erro no setup: {error}", ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(f"❌ Erro no setup: {error}", ephemeral=True)


# Force registration on join
@bot.event
async def on_member_join(member: discord.Member):
    if not FORCE_ON_JOIN:
        return

    guild = member.guild
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    if not unreg_role or not reg_role:
        return

    if reg_role in member.roles:
        return

    try:
        if unreg_role not in member.roles:
            await member.add_roles(unreg_role, reason="Auto: force registration")
    except discord.Forbidden:
        return

    if PING_ON_JOIN:
        ch = discord.utils.get(guild.text_channels, name=REGISTER_CHANNEL_NAME)
        if ch:
            try:
                await ch.send(f"{member.mention} faça seu cadastro clicando em **Cadastrar**.")
            except discord.Forbidden:
                pass


# Admin: verify unregistered
@bot.tree.command(name="verificar_registro", description="Mostra quantos membros ainda estão como Não Registrado.")
@app_commands.checks.has_permissions(administrator=True)
async def verificar_registro(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not unreg_role:
        return await interaction.response.send_message("Cargo 'Não Registrado' não existe.", ephemeral=True)

    await interaction.response.send_message(f"⛔ Não Registrados: **{len(unreg_role.members)}**", ephemeral=True)


# Run
def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()