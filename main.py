import os
import re
import asyncio
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands
import yaml


# =========================
# Config
# =========================

def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError("Não achei config.yaml na raiz do projeto.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml inválido (esperado um dict).")
    return cfg


def hex_to_color_int(hex_str: str) -> int:
    s = (hex_str or "").strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) != 6:
        raise ValueError(f"Cor inválida: {hex_str} (use #RRGGBB)")
    return int(s, 16)


def norm(s: str) -> str:
    return (s or "").strip().lower()


CONFIG = load_config("config.yaml")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")  # Railway Variables/Secrets
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN não configurado (Secrets/Variables).")

GATE_ROLE_NAME = CONFIG.get("gate_role", {}).get("name", "📝 Não Registrado")
GATE_ROLE_COLOR = hex_to_color_int(CONFIG.get("gate_role", {}).get("color", "#444444"))

DEFAULT_ROLE_NAME = CONFIG.get("default_role", "🪖 Recruta")
DEFAULT_ROLE_COLOR = hex_to_color_int(CONFIG.get("default_role_color", "#7A7A7A"))

REG_CATEGORY_NAME = CONFIG.get("registration", {}).get("category_name", "📝 CADASTRO")
REG_CHANNEL_NAME = CONFIG.get("registration", {}).get("channel_name", "registrar")

NICK_PREFIX = CONFIG.get("registration", {}).get("nick_prefix", "Rec")  # "Rec"
NICK_MAX_GAME_NAME = int(CONFIG.get("registration", {}).get("max_game_name", 24))

STRUCTURE = CONFIG.get("structure", {})

# =========================
# Bot
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# Find helpers
# =========================

def find_role(guild: discord.Guild, name: str) -> Optional[discord.Role]:
    n = norm(name)
    for r in guild.roles:
        if norm(r.name) == n:
            return r
    return None


def find_category(guild: discord.Guild, name: str) -> Optional[discord.CategoryChannel]:
    n = norm(name)
    for c in guild.categories:
        if norm(c.name) == n:
            return c
    return None


def find_text_channel(guild: discord.Guild, name: str) -> Optional[discord.TextChannel]:
    n = norm(name)
    for ch in guild.text_channels:
        if norm(ch.name) == n:
            return ch
    return None


def is_admin(member: discord.Member) -> bool:
    return bool(member.guild_permissions.administrator)


# =========================
# Core: Setup
# =========================

async def ensure_roles(guild: discord.Guild) -> Dict[str, discord.Role]:
    # Gate role
    gate = find_role(guild, GATE_ROLE_NAME)
    if gate is None:
        gate = await guild.create_role(
            name=GATE_ROLE_NAME,
            colour=discord.Color(GATE_ROLE_COLOR),
            hoist=True,
            mentionable=False,
            reason="Setup: gate role"
        )
    else:
        try:
            await gate.edit(colour=discord.Color(GATE_ROLE_COLOR), hoist=True, reason="Setup: gate role update")
        except discord.Forbidden:
            pass

    # Default role
    default = find_role(guild, DEFAULT_ROLE_NAME)
    if default is None:
        default = await guild.create_role(
            name=DEFAULT_ROLE_NAME,
            colour=discord.Color(DEFAULT_ROLE_COLOR),
            hoist=True,
            mentionable=False,
            reason="Setup: default role"
        )
    else:
        try:
            await default.edit(colour=discord.Color(DEFAULT_ROLE_COLOR), hoist=True, reason="Setup: default role update")
        except discord.Forbidden:
            pass

    return {"gate": gate, "default": default}


def reg_channel_overwrites(guild: discord.Guild, gate_role: discord.Role) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    # Canal de cadastro: todo mundo vê, mas só não-registrado fala.
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False, read_message_history=True),
        gate_role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    return ow


def locked_category_overwrites(guild: discord.Guild, gate_role: discord.Role) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    # Todas as outras categorias: gate NÃO vê. Registrados veem normalmente (@everyone).
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True),
        gate_role: discord.PermissionOverwrite(view_channel=False),
    }
    return ow


async def ensure_registration_area(guild: discord.Guild, gate_role: discord.Role) -> discord.TextChannel:
    cat = find_category(guild, REG_CATEGORY_NAME)
    if cat is None:
        cat = await guild.create_category(REG_CATEGORY_NAME, reason="Setup: registration category")

    # categoria de cadastro: todo mundo vê (para achar), mas canal controla fala
    try:
        await cat.edit(overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True)}, reason="Setup: reg cat perms")
    except discord.Forbidden:
        pass

    ch = None
    for t in cat.text_channels:
        if norm(t.name) == norm(REG_CHANNEL_NAME):
            ch = t
            break

    if ch is None:
        ch = await guild.create_text_channel(
            REG_CHANNEL_NAME,
            category=cat,
            overwrites=reg_channel_overwrites(guild, gate_role),
            topic="Faça seu cadastro com /registrar para liberar o servidor.",
            reason="Setup: registration channel"
        )
    else:
        try:
            await ch.edit(
                overwrites=reg_channel_overwrites(guild, gate_role),
                topic="Faça seu cadastro com /registrar para liberar o servidor.",
                reason="Setup: registration channel update"
            )
        except discord.Forbidden:
            pass

    return ch


async def ensure_basic_structure(guild: discord.Guild, gate_role: discord.Role) -> None:
    """
    Cria canais básicos do Death Zone Online e aplica permissão:
    - Não registrado NÃO vê nada fora do cadastro.
    """
    categories: List[dict] = STRUCTURE.get("categories", [])
    if not categories:
        return

    for cat_def in categories:
        cat_name = str(cat_def.get("name", "")).strip()
        if not cat_name:
            continue

        cat = find_category(guild, cat_name)
        if cat is None:
            cat = await guild.create_category(cat_name, reason="Setup: create category")

        # bloqueia gate
        try:
            await cat.edit(overwrites=locked_category_overwrites(guild, gate_role), reason="Setup: lock gate role")
        except discord.Forbidden:
            pass

        # channels
        for ch_def in (cat_def.get("channels", []) or []):
            ch_name = str(ch_def.get("name", "")).strip()
            ch_type = str(ch_def.get("type", "text")).strip().lower()
            if not ch_name:
                continue

            if ch_type == "text":
                # cria se não existir
                existing = None
                for t in cat.text_channels:
                    if norm(t.name) == norm(ch_name):
                        existing = t
                        break
                if existing is None:
                    await guild.create_text_channel(
                        ch_name,
                        category=cat,
                        topic=ch_def.get("topic"),
                        slowmode_delay=int(ch_def.get("slowmode", 0) or 0),
                        reason="Setup: create text channel"
                    )
                else:
                    try:
                        await existing.edit(
                            topic=ch_def.get("topic"),
                            slowmode_delay=int(ch_def.get("slowmode", 0) or 0),
                            reason="Setup: update text channel"
                        )
                    except discord.Forbidden:
                        pass

            elif ch_type == "voice":
                existing = None
                for v in cat.voice_channels:
                    if norm(v.name) == norm(ch_name):
                        existing = v
                        break
                user_limit = ch_def.get("user_limit")
                user_limit = int(user_limit) if user_limit is not None else None

                if existing is None:
                    await guild.create_voice_channel(
                        ch_name,
                        category=cat,
                        user_limit=user_limit,
                        reason="Setup: create voice channel"
                    )
                else:
                    try:
                        await existing.edit(user_limit=user_limit, reason="Setup: update voice channel")
                    except discord.Forbidden:
                        pass


# =========================
# Registration / nickname
# =========================

def sanitize_game_name(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip())
    s = s[:NICK_MAX_GAME_NAME]
    return s


def build_nickname(game_name: str) -> str:
    gn = sanitize_game_name(game_name)
    nick = f"{NICK_PREFIX} {gn}".strip()
    return nick[:32]


class RegistrationModal(discord.ui.Modal, title="Cadastro — Death Zone Online"):
    game_name = discord.ui.TextInput(
        label="Nome no jogo",
        placeholder="Ex: Sam Cherri",
        required=True,
        max_length=32
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)

        guild = interaction.guild
        member: discord.Member = interaction.user

        gate = find_role(guild, GATE_ROLE_NAME)
        default = find_role(guild, DEFAULT_ROLE_NAME)

        if not default:
            return await interaction.response.send_message(f"Cargo base '{DEFAULT_ROLE_NAME}' não existe. Rode /setup.", ephemeral=True)

        # dá cargo base (se não tiver)
        if default not in member.roles:
            try:
                await member.add_roles(default, reason="Registration: assign default role")
            except discord.Forbidden:
                return await interaction.response.send_message("Sem permissão para dar cargo base (Manage Roles).", ephemeral=True)

        # remove gate
        if gate and gate in member.roles:
            try:
                await member.remove_roles(gate, reason="Registration: remove gate role")
            except discord.Forbidden:
                pass

        # muda nick
        nick = build_nickname(str(self.game_name.value))
        try:
            await member.edit(nick=nick, reason="Registration: set nickname")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Cadastro ok, mas não consegui mudar seu apelido. Dê ao bot 'Manage Nicknames'.",
                ephemeral=True
            )

        await interaction.response.send_message(f"✅ Registrado. Seu apelido agora é **{nick}**.", ephemeral=True)


# =========================
# Events / Commands
# =========================

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"[OK] Online: {bot.user} | comandos: {len(synced)}", flush=True)
    except Exception as e:
        print(f"[WARN] Falha ao sync comandos: {e}", flush=True)


@bot.event
async def on_member_join(member: discord.Member):
    # Força cadastro
    guild = member.guild
    gate = find_role(guild, GATE_ROLE_NAME)
    if not gate:
        return

    try:
        await member.add_roles(gate, reason="Gate: must register")
    except discord.Forbidden:
        return

    # tenta DM
    try:
        await member.send(
            "Bem-vindo ao servidor.\n"
            "Para liberar acesso, faça o cadastro:\n"
            f"1) Vá no canal **#{REG_CHANNEL_NAME}**\n"
            "2) Use **/registrar** e informe seu nome no jogo.\n"
        )
    except Exception:
        pass


@bot.tree.command(name="setup", description="Cria/ajusta estrutura básica do Death Zone Online e força cadastro.")
async def setup_cmd(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
    if not is_admin(interaction.user):
        return await interaction.response.send_message("Apenas Administrador pode usar /setup.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        roles = await ensure_roles(interaction.guild)
        await ensure_registration_area(interaction.guild, roles["gate"])
        await ensure_basic_structure(interaction.guild, roles["gate"])
    except discord.Forbidden:
        return await interaction.followup.send("❌ Sem permissões (Manage Roles / Manage Channels / Manage Nicknames).", ephemeral=True)
    except Exception as e:
        return await interaction.followup.send(f"❌ Erro no setup: {e}", ephemeral=True)

    await interaction.followup.send("✅ Setup concluído. Cadastro obrigatório ativo.", ephemeral=True)


@bot.tree.command(name="registrar", description="Cadastro obrigatório: define seu nome no jogo e libera o servidor.")
async def registrar_cmd(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("Use isso dentro do servidor.", ephemeral=True)
    await interaction.response.send_modal(RegistrationModal())


def main():
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
