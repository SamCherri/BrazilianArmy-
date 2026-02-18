import os
from dataclasses import dataclass
from typing import Dict, List, Optional

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
# Helpers: roles/channels
# =========================

async def ensure_role(guild: discord.Guild, rdef: RoleDef) -> discord.Role:
    existing = discord.utils.get(guild.roles, name=rdef.name)
    color = discord.Color(rdef.color)
    if existing:
        # Atualiza se necessário
        changed = False
        if existing.color.value != color.value:
            await existing.edit(color=color, reason="Sync config (color)")
            changed = True
        if existing.hoist != rdef.hoist:
            await existing.edit(hoist=rdef.hoist, reason="Sync config (hoist)")
            changed = True
        if existing.mentionable != rdef.mentionable:
            await existing.edit(mentionable=rdef.mentionable, reason="Sync config (mentionable)")
            changed = True
        return existing

    return await guild.create_role(
        name=rdef.name,
        color=color,
        hoist=rdef.hoist,
        mentionable=rdef.mentionable,
        reason="Setup (create role)",
    )

async def ensure_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=name)
    if cat:
        return cat
    return await guild.create_category(name, reason="Setup (create category)")

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
        # Sync
        updates = {}
        if topic is not None and ch.topic != topic:
            updates["topic"] = topic
        if slowmode and ch.slowmode_delay != slowmode:
            updates["slowmode_delay"] = slowmode
        if overwrites is not None:
            # substitui as permissões do canal (garante travas)
            await ch.edit(overwrites=overwrites, reason="Sync config (overwrites)")
        if updates:
            await ch.edit(**updates, reason="Sync config (text channel)")
        return ch

    return await guild.create_text_channel(
        name=name,
        category=category,
        topic=topic,
        slowmode_delay=slowmode,
        overwrites=overwrites,
        reason="Setup (create text channel)",
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
        updates = {}
        if user_limit is not None and ch.user_limit != user_limit:
            updates["user_limit"] = user_limit
        if overwrites is not None:
            await ch.edit(overwrites=overwrites, reason="Sync config (overwrites)")
        if updates:
            await ch.edit(**updates, reason="Sync config (voice channel)")
        return ch

    return await guild.create_voice_channel(
        name=name,
        category=category,
        user_limit=user_limit or 0,
        overwrites=overwrites,
        reason="Setup (create voice channel)",
    )

def build_role_defs(cfg: dict) -> List[RoleDef]:
    out: List[RoleDef] = []
    for r in (cfg.get("roles") or []):
        out.append(
            RoleDef(
                name=r.get("name", "").strip(),
                color=hex_to_int_color(r.get("color", "#95A5A6")),
                hoist=bool(r.get("hoist", False)),
                mentionable=bool(r.get("mentionable", False)),
            )
        )
    # garante que os cargos base existam mesmo se esquecerem no YAML
    names = {x.name for x in out}
    if ROLE_REG not in names:
        out.append(RoleDef(name=ROLE_REG, color=hex_to_int_color("#2ECC71"), hoist=True))
    if ROLE_UNREG not in names:
        out.append(RoleDef(name=ROLE_UNREG, color=hex_to_int_color("#E74C3C"), hoist=True))
    return out

def build_categories(cfg: dict) -> List[CategoryDef]:
    out: List[CategoryDef] = []
    for c in (cfg.get("categories") or []):
        channels: List[ChannelDef] = []
        for ch in (c.get("channels") or []):
            channels.append(
                ChannelDef(
                    name=ch.get("name", "").strip(),
                    type=ch.get("type", "text").strip().lower(),
                    topic=ch.get("topic"),
                    slowmode=int(ch.get("slowmode", 0) or 0),
                    user_limit=int(ch.get("user_limit", 0) or 0),
                )
            )
        out.append(
            CategoryDef(
                name=f"{c.get('emoji', '📁')} {c.get('name', '').strip()}".strip(),
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

    # base: travado para não registrados
    ow = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        unreg_role: discord.PermissionOverwrite(view_channel=False),
        reg_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }

    # Canal de registro: não-registrado pode VER e CLICAR no botão (enviar msg é opcional)
    if is_registration_channel:
        ow[everyone] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
        ow[unreg_role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)

    return ow

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

        # Troca apelido
        new_nick = f"{NICK_PREFIX}{self.game_name.value}".strip()
        try:
            await member.edit(nick=new_nick, reason="Cadastro: set nickname")
        except discord.Forbidden:
            # Sem permissão para alterar nick
            return await interaction.response.send_message(
                "Não consegui mudar seu apelido. Coloque o bot acima dos cargos e dê permissão de 'Gerenciar Apelidos'.",
                ephemeral=True,
            )

        # Ajusta cargos
        try:
            if unreg_role in member.roles:
                await member.remove_roles(unreg_role, reason="Cadastro: remove unregistered")
            if reg_role not in member.roles:
                await member.add_roles(reg_role, reason="Cadastro: add registered")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Não consegui mexer nos seus cargos. O bot precisa de 'Gerenciar Cargos' e estar acima dos cargos.",
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
# Commands
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

@bot.tree.command(name="setup", description="Cria/sincroniza estrutura de canais/cargos e posta o painel de cadastro.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use isso dentro de um servidor.", ephemeral=True)

    await interaction.response.send_message("⏳ Criando/sincronizando estrutura...", ephemeral=True)

    # 1) Roles
    role_defs = build_role_defs(CONFIG)
    for rdef in role_defs:
        if not rdef.name:
            continue
        await ensure_role(guild, rdef)

    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not reg_role or not unreg_role:
        return await interaction.followup.send("Erro: não consegui criar cargos base.", ephemeral=True)

    # 2) Categories/Channels + permissions
    categories = build_categories(CONFIG)
    created_reg_channel: Optional[discord.TextChannel] = None

    for cdef in categories:
        cat = await ensure_category(guild, cdef.name)

        for ch in cdef.channels:
            is_reg = (norm(ch.name) == norm(REGISTER_CHANNEL_NAME))
            overwrites = overwrites_for_registration(guild, reg_role, unreg_role, is_registration_channel=is_reg)

            if ch.type == "voice":
                vch = await ensure_voice_channel(
                    guild=guild,
                    category=cat,
                    name=ch.name,
                    user_limit=ch.user_limit or 0,
                    overwrites=overwrites,
                )
            else:
                tch = await ensure_text_channel(
                    guild=guild,
                    category=cat,
                    name=ch.name,
                    topic=ch.topic,
                    slowmode=ch.slowmode or 0,
                    overwrites=overwrites,
                )
                if is_reg:
                    created_reg_channel = tch

    # 3) Post registration panel
    if created_reg_channel:
        try:
            await created_reg_channel.send(
                "📋 **REGISTRO OBRIGATÓRIO**\n\nClique no botão abaixo para se cadastrar e liberar o servidor.",
                view=RegisterView(),
            )
        except discord.Forbidden:
            pass

    await interaction.followup.send("✅ Setup concluído.", ephemeral=True)

@setup_cmd.error
async def setup_error(interaction: discord.Interaction, error: Exception):
    await interaction.response.send_message(f"❌ Erro no setup: {error}", ephemeral=True)

# =========================
# Force registration on join
# =========================

@bot.event
async def on_member_join(member: discord.Member):
    if not FORCE_ON_JOIN:
        return

    guild = member.guild
    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    reg_role = discord.utils.get(guild.roles, name=ROLE_REG)

    if not unreg_role or not reg_role:
        return

    # Se já tem registrado, não mexe
    if reg_role in member.roles:
        return

    # Aplica Não Registrado
    try:
        if unreg_role not in member.roles:
            await member.add_roles(unreg_role, reason="Auto: force registration")
    except discord.Forbidden:
        return

    # Opcional: avisar no canal de registro
    if PING_ON_JOIN:
        reg_channel = discord.utils.get(guild.text_channels, name=REGISTER_CHANNEL_NAME)
        if reg_channel:
            try:
                await reg_channel.send(f"{member.mention} faça seu cadastro clicando em **Cadastrar**.")
            except discord.Forbidden:
                pass

# =========================
# Anti-bypass: verifica quem não cadastrou (comando admin)
# =========================

@bot.tree.command(name="verificar_registro", description="Lista quantos membros ainda estão como Não Registrado.")
@app_commands.checks.has_permissions(administrator=True)
async def verificar_registro(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("Use dentro do servidor.", ephemeral=True)

    unreg_role = discord.utils.get(guild.roles, name=ROLE_UNREG)
    if not unreg_role:
        return await interaction.response.send_message("Cargo 'Não Registrado' não existe.", ephemeral=True)

    count = len(unreg_role.members)
    await interaction.response.send_message(f"⛔ Não Registrados: **{count}**", ephemeral=True)

# =========================
# Run
# =========================

def main():
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()