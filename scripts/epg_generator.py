#!/usr/bin/env python3

"""
Gerador EPG XMLTV para canais IPTV

Fontes:
    1. mi.tv
    2. Guia de TV

Entradas:
    channels.json

Saídas:
    output/epg.xml
    output/epg.xml.gz
    output/playlist.m3u
    output/epg_aliases.json

Fuso padrão:
    America/Fortaleza
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURAÇÃO
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

CHANNELS_FILE = ROOT / "channels.json"

OUTPUT_DIR = ROOT / "output"

EPG_XML = OUTPUT_DIR / "epg.xml"
EPG_GZ = OUTPUT_DIR / "epg.xml.gz"
PLAYLIST = OUTPUT_DIR / "playlist.m3u"
ALIASES_FILE = OUTPUT_DIR / "epg_aliases.json"

DEFAULT_TIMEZONE = "America/Fortaleza"

MITV_BASE = "https://mi.tv/br"
MITV_PROGRAMACAO = f"{MITV_BASE}/programacao"

GUIA_BASE = "https://www.guiadetv.com"

REQUEST_TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

# intervalo mínimo entre requisições
REQUEST_DELAY = 0.8


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("EPG")


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str) -> Optional[str]:
    """
    Baixa uma página HTML.
    """

    try:
        log.info("GET %s", url)

        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        time.sleep(REQUEST_DELAY)

        return response.text

    except requests.RequestException as exc:
        log.warning(
            "Falha ao acessar %s: %s",
            url,
            exc,
        )

        return None


# ============================================================
# UTILITÁRIOS
# ============================================================

def normalize(text: str) -> str:
    """
    Normaliza nomes para comparação.

    Exemplo:

        TV CIDADE VERDE 5.1
        TV Cidade Verde
        TV CIDADE VERDE 5.1 FHD

    podem ser comparados de maneira consistente.
    """

    if not text:
        return ""

    text = unicodedata.normalize(
        "NFKD",
        text,
    )

    text = "".join(
        char
        for char in text
        if not unicodedata.combining(char)
    )

    text = text.upper()

    text = text.replace("&", " E ")

    text = re.sub(
        r"\b(FHD|H264|H265|HEVC|HD|SD|4K|UHD)\b",
        " ",
        text,
    )

    text = re.sub(
        r"\b\d+(\.\d+)?\b",
        " ",
        text,
    )

    text = re.sub(
        r"[^A-Z0-9]+",
        " ",
        text,
    )

    return " ".join(text.split())


def slugify(text: str) -> str:
    """
    Converte nome em slug.
    """

    value = normalize(text)

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9]+",
        "-",
        value,
    )

    return value.strip("-")


def xml_escape(text: str) -> str:
    """
    Escapa caracteres XML.
    """

    if text is None:
        return ""

    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def parse_time(text: str) -> Optional[tuple[int, int]]:
    """
    Extrai HH:MM.
    """

    if not text:
        return None

    match = re.search(
        r"\b(\d{1,2}):(\d{2})\b",
        text,
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if hour > 23 or minute > 59:
        return None

    return hour, minute


def now_local(tz_name: str) -> datetime:
    return datetime.now(
        ZoneInfo(tz_name)
    )


# ============================================================
# MODELOS
# ============================================================

@dataclass
class Program:
    channel_id: str
    title: str
    description: str
    start: datetime
    end: Optional[datetime] = None


@dataclass
class Channel:
    name: str
    tvg_id: str
    tvg_name: str
    group: str
    url: str
    logo: str
    aliases: list[str]


# ============================================================
# LEITURA DOS CANAIS
# ============================================================

def load_channels() -> tuple[dict, list[Channel]]:
    if not CHANNELS_FILE.exists():
        raise FileNotFoundError(
            f"Arquivo não encontrado: {CHANNELS_FILE}"
        )

    data = json.loads(
        CHANNELS_FILE.read_text(
            encoding="utf-8"
        )
    )

    channels = []

    for item in data.get("channels", []):
        channels.append(
            Channel(
                name=item.get("name", "").strip(),
                tvg_id=item.get("tvg_id", "").strip(),
                tvg_name=item.get("tvg_name", "").strip(),
                group=item.get("group", "Brasil").strip(),
                url=item.get("url", "").strip(),
                logo=item.get("logo", "").strip(),
                aliases=item.get("aliases", []),
            )
        )

    return data, channels


# ============================================================
# MAPEAMENTO CONHECIDO
# ============================================================

# Slugs conhecidos / preferenciais.
#
# O gerador tenta primeiro estes mapeamentos.
# Depois utiliza busca automática.

KNOWN_GUIA_SLUGS = {
    "TV CULTURA": "tv-cultura",
    "TV SENADO": "tv-senado",
    "TV BRASIL": "tv-brasil",
    "BAND SP": "band",
    "BAND RIO": "band-rj",
    "TV ASSEMBLEIA PIAUI": "tv-assembleia-piaui",
    "TV ASSEMBLEIA PI": "tv-assembleia-piaui",
    "CNN BRASIL": "cnn-brasil",
    "BANDSPORTS": "bandsports",
    "BANDNEWS TV": "bandnews",
}


KNOWN_MITV_SLUGS = {
    "TV CULTURA": "tv-cultura",
    "TV BRASIL": "tv-brasil",
    "CNN BRASIL": "cnn-brasil",
    "BAND SP": "band",
}


# ============================================================
# DESCOBERTA DE PÁGINA DO GUIA DE TV
# ============================================================

def guia_channel_url(channel_name: str) -> Optional[str]:
    """
    Tenta localizar uma página individual no Guia de TV.
    """

    normalized = normalize(channel_name)

    if normalized in KNOWN_GUIA_SLUGS:
        slug = KNOWN_GUIA_SLUGS[normalized]

        return f"{GUIA_BASE}/canal/{slug}"

    slug = slugify(channel_name)

    candidates = [
        f"{GUIA_BASE}/canal/{slug}",
        f"{GUIA_BASE}/canal/{slug}-br",
    ]

    for url in candidates:

        html = fetch(url)

        if html and len(html) > 500:
            soup = BeautifulSoup(
                html,
                "html.parser",
            )

            title = normalize(
                soup.title.get_text(" ", strip=True)
                if soup.title
                else ""
            )

            if normalized in title or title in normalized:
                return url

    return None


# ============================================================
# GUIA DE TV
# ============================================================

def scrape_guia_channel(
    channel: Channel,
    tz: ZoneInfo,
) -> list[Program]:

    url = guia_channel_url(channel.name)

    if not url:
        log.warning(
            "Guia de TV: página não encontrada para %s",
            channel.name,
        )
        return []

    log.info(
        "Guia de TV: %s -> %s",
        channel.name,
        url,
    )

    html = fetch(url)

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    programs = []

    current_date = datetime.now(tz).date()

    # --------------------------------------------------------
    # Procura blocos de programação.
    #
    # O site possui horários seguidos dos títulos.
    # A estrutura HTML pode mudar, portanto usamos heurísticas.
    # --------------------------------------------------------

    elements = soup.find_all(
        ["article", "div", "li"]
    )

    parsed = []

    for element in elements:

        text = element.get_text(
            " ",
            strip=True,
        )

        if not text:
            continue

        tm = parse_time(text)

        if not tm:
            continue

        hour, minute = tm

        # Remove o horário do texto
        title = re.sub(
            r"^\s*\d{1,2}:\d{2}\s*",
            "",
            text,
        ).strip()

        if not title:
            continue

        # Evita blocos gigantes
        if len(title) > 500:
            continue

        # Remove textos de navegação
        bad = [
            "PROGRAMACAO DA TV",
            "NOSSO APLICATIVO",
            "LINKS UTEIS",
            "CONFIGURACOES",
        ]

        if normalize(title) in bad:
            continue

        parsed.append(
            (
                datetime.combine(
                    current_date,
                    datetime.min.time(),
                ).replace(
                    hour=hour,
                    minute=minute,
                    tzinfo=tz,
                ),
                title,
            )
        )

    # Remove duplicados
    unique = {}

    for start, title in parsed:

        key = (
            start.isoformat(),
            normalize(title),
        )

        unique[key] = (
            start,
            title,
        )

    parsed = list(unique.values())

    parsed.sort(
        key=lambda item: item[0]
    )

    for index, (start, title) in enumerate(parsed):

        end = None

        if index + 1 < len(parsed):

            next_start = parsed[index + 1][0]

            if next_start > start:
                end = next_start

        programs.append(
            Program(
                channel_id=channel.tvg_id,
                title=title,
                description="",
                start=start,
                end=end,
            )
        )

    return programs


# ============================================================
# MI.TV
# ============================================================

def mitv_channel_url(channel_name: str) -> Optional[str]:

    normalized = normalize(channel_name)

    if normalized in KNOWN_MITV_SLUGS:

        slug = KNOWN_MITV_SLUGS[normalized]

        return f"{MITV_BASE}/canais/{slug}"

    slug = slugify(channel_name)

    candidates = [
        f"{MITV_BASE}/canais/{slug}",
    ]

    for url in candidates:

        html = fetch(url)

        if not html:
            continue

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        title = normalize(
            soup.title.get_text(" ", strip=True)
            if soup.title
            else ""
        )

        if normalized in title or title in normalized:
            return url

    return None


def scrape_mitv_channel(
    channel: Channel,
    tz: ZoneInfo,
) -> list[Program]:

    url = mitv_channel_url(
        channel.name
    )

    if not url:
        log.warning(
            "mi.tv: página não encontrada para %s",
            channel.name,
        )

        return []

    log.info(
        "mi.tv: %s -> %s",
        channel.name,
        url,
    )

    html = fetch(url)

    if not html:
        return []

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    programs = []

    current_date = datetime.now(tz).date()

    # --------------------------------------------------------
    # A estrutura do mi.tv pode variar.
    # Buscamos horários e títulos próximos no DOM.
    # --------------------------------------------------------

    text_nodes = []

    for element in soup.find_all(
        ["article", "div", "li", "section"]
    ):

        text = element.get_text(
            " ",
            strip=True,
        )

        if text:
            text_nodes.append(text)

    parsed = []

    for text in text_nodes:

        match = re.search(
            r"\b(\d{1,2}):(\d{2})\b",
            text,
        )

        if not match:
            continue

        hour = int(match.group(1))
        minute = int(match.group(2))

        if hour > 23 or minute > 59:
            continue

        title = re.sub(
            r"\b\d{1,2}:\d{2}\b",
            "",
            text,
            count=1,
        ).strip()

        if not title:
            continue

        if len(title) > 400:
            continue

        parsed.append(
            (
                datetime.combine(
                    current_date,
                    datetime.min.time(),
                ).replace(
                    hour=hour,
                    minute=minute,
                    tzinfo=tz,
                ),
                title,
            )
        )

    # Remove duplicados
    unique = {}

    for start, title in parsed:

        key = (
            start.isoformat(),
            normalize(title),
        )

        unique[key] = (
            start,
            title,
        )

    parsed = list(unique.values())

    parsed.sort(
        key=lambda item: item[0]
    )

    for index, (start, title) in enumerate(parsed):

        end = None

        if index + 1 < len(parsed):

            next_start = parsed[index + 1][0]

            if next_start > start:
                end = next_start

        programs.append(
            Program(
                channel_id=channel.tvg_id,
                title=title,
                description="",
                start=start,
                end=end,
            )
        )

    return programs


# ============================================================
# DESCOBERTA DE PROGRAMAS
# ============================================================

def collect_programs(
    channels: list[Channel],
    tz: ZoneInfo,
) -> list[Program]:

    all_programs = []

    for channel in channels:

        log.info(
            "=========================================="
        )

        log.info(
            "Processando: %s [%s]",
            channel.name,
            channel.tvg_id,
        )

        programs = scrape_mitv_channel(
            channel,
            tz,
        )

        source = "mi.tv"

        # Fallback
        if not programs:

            programs = scrape_guia_channel(
                channel,
                tz,
            )

            source = "Guia de TV"

        if programs:

            log.info(
                "%s: %d programas encontrados (%s)",
                channel.name,
                len(programs),
                source,
            )

            all_programs.extend(
                programs
            )

        else:

            log.warning(
                "%s: nenhum programa encontrado",
                channel.name,
            )

    return all_programs


# ============================================================
# AJUSTE DE HORÁRIOS
# ============================================================

def finalize_program_times(
    programs: list[Program],
    tz: ZoneInfo,
):

    by_channel = {}

    for program in programs:

        by_channel.setdefault(
            program.channel_id,
            [],
        ).append(program)

    for channel_id, items in by_channel.items():

        items.sort(
            key=lambda p: p.start
        )

        for index, program in enumerate(items):

            if index + 1 < len(items):

                next_program = items[index + 1]

                if (
                    program.end is None
                    or program.end <= program.start
                ):
                    program.end = next_program.start

            if program.end is None:

                program.end = (
                    program.start
                    + timedelta(hours=1)
                )

            # Segurança
            if program.end <= program.start:

                program.end = (
                    program.start
                    + timedelta(minutes=30)
                )


# ============================================================
# XMLTV
# ============================================================

def xmltv_time(dt: datetime) -> str:

    # XMLTV normalmente utiliza:
    # YYYYMMDDHHMMSS +ZZZZ

    offset = dt.strftime("%z")

    return dt.strftime(
        "%Y%m%d%H%M%S"
    ) + f" {offset}"


def build_xmltv(
    channels: list[Channel],
    programs: list[Program],
) -> str:

    lines = []

    lines.append(
        '<?xml version="1.0" encoding="UTF-8"?>'
    )

    lines.append(
        '<tv generator-info-name="IPTV EPG Generator" '
        'generator-info-url="https://github.com/">'
    )

    # --------------------------------------------------------
    # CHANNELS
    # --------------------------------------------------------

    for channel in channels:

        lines.append(
            f'  <channel id="{xml_escape(channel.tvg_id)}">'
        )

        lines.append(
            f'    <display-name lang="pt">'
            f'{xml_escape(channel.tvg_name or channel.name)}'
            f'</display-name>'
        )

        if channel.logo:

            lines.append(
                f'    <icon src="{xml_escape(channel.logo)}"/>'
            )

        lines.append(
            "  </channel>"
        )

    # --------------------------------------------------------
    # PROGRAMAS
    # --------------------------------------------------------

    for program in sorted(
        programs,
        key=lambda p: (
            p.channel_id,
            p.start,
        ),
    ):

        lines.append(
            f'  <programme '
            f'start="{xmltv_time(program.start)}" '
            f'stop="{xmltv_time(program.end)}" '
            f'channel="{xml_escape(program.channel_id)}">'
        )

        lines.append(
            f'    <title lang="pt">'
            f'{xml_escape(program.title)}'
            f'</title>'
        )

        if program.description:

            lines.append(
                f'    <desc lang="pt">'
                f'{xml_escape(program.description)}'
                f'</desc>'
            )

        lines.append(
            "  </programme>"
        )

    lines.append("</tv>")

    return "\n".join(lines) + "\n"


# ============================================================
# PLAYLIST M3U
# ============================================================

def build_playlist(
    channels: list[Channel],
) -> str:

    lines = [
        "#EXTM3U",
    ]

    for channel in channels:

        name = channel.name

        lines.append(
            f'#EXTINF:-1 '
            f'tvg-id="{channel.tvg_id}" '
            f'tvg-name="{channel.tvg_name}" '
            f'tvg-logo="{channel.logo}" '
            f'group-title="{channel.group}",'
            f'{name}'
        )

        lines.append(
            channel.url
        )

    return "\n".join(lines) + "\n"


# ============================================================
# ALIASES
# ============================================================

def build_aliases(
    channels: list[Channel],
) -> dict:

    result = {}

    for channel in channels:

        aliases = set()

        aliases.add(
            channel.name
        )

        aliases.add(
            channel.tvg_name
        )

        aliases.add(
            normalize(channel.name)
        )

        for alias in channel.aliases:

            aliases.add(alias)

            aliases.add(
                normalize(alias)
            )

        result[channel.tvg_id] = {
            "name": channel.name,
            "tvg_name": channel.tvg_name,
            "aliases": sorted(
                aliases
            ),
        }

    return result


# ============================================================
# VALIDAÇÃO
# ============================================================

def validate_channels(
    channels: list[Channel],
):

    seen_ids = set()

    for channel in channels:

        if not channel.tvg_id:

            log.warning(
                "Canal sem tvg_id: %s",
                channel.name,
            )

        if channel.tvg_id in seen_ids:

            log.info(
                "Múltiplos streams encontrados para %s",
                channel.tvg_id,
            )

        seen_ids.add(
            channel.tvg_id
        )


def validate_xml(
    xml: str,
):

    if not xml.strip().startswith(
        "<?xml"
    ):

        raise ValueError(
            "XMLTV inválido: declaração XML ausente."
        )

    if "<tv" not in xml:

        raise ValueError(
            "XMLTV inválido: elemento <tv> ausente."
        )

    if "</tv>" not in xml:

        raise ValueError(
            "XMLTV inválido: fechamento </tv> ausente."
        )


# ============================================================
# ESCRITA
# ============================================================

def write_outputs(
    channels: list[Channel],
    programs: list[Program],
):

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # XML
    xml = build_xmltv(
        channels,
        programs,
    )

    validate_xml(xml)

    EPG_XML.write_text(
        xml,
        encoding="utf-8",
    )

    # GZIP
    with gzip.open(
        EPG_GZ,
        "wt",
        encoding="utf-8",
        compresslevel=9,
    ) as gz:

        gz.write(xml)

    # Playlist
    playlist = build_playlist(
        channels
    )

    PLAYLIST.write_text(
        playlist,
        encoding="utf-8",
    )

    # Aliases
    aliases = build_aliases(
        channels
    )

    ALIASES_FILE.write_text(
        json.dumps(
            aliases,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log.info(
        "Arquivos gerados:"
    )

    log.info(
        " - %s",
        EPG_XML,
    )

    log.info(
        " - %s",
        EPG_GZ,
    )

    log.info(
        " - %s",
        PLAYLIST,
    )

    log.info(
        " - %s",
        ALIASES_FILE,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    log.info(
        "=========================================="
    )

    log.info(
        "GERADOR EPG IPTV"
    )

    log.info(
        "=========================================="
    )

    config, channels = load_channels()

    timezone_name = (
        config.get(
            "timezone"
        )
        or DEFAULT_TIMEZONE
    )

    tz = ZoneInfo(
        timezone_name
    )

    days = int(
        config.get(
            "days",
            1,
        )
    )

    log.info(
        "Timezone: %s",
        timezone_name,
    )

    log.info(
        "Dias configurados: %d",
        days,
    )

    log.info(
        "Canais: %d",
        len(channels),
    )

    validate_channels(
        channels
    )

    programs = collect_programs(
        channels,
        tz,
    )

    # Limita aos dias configurados.
    start_limit = (
        datetime.now(tz)
        .replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    )

    end_limit = (
        start_limit
        + timedelta(days=days + 1)
    )

    filtered = []

    for program in programs:

        if (
            program.start >= start_limit
            and program.start < end_limit
        ):
            filtered.append(
                program
            )

    finalize_program_times(
        filtered,
        tz,
    )

    log.info(
        "Total de programas: %d",
        len(filtered),
    )

    write_outputs(
        channels,
        filtered,
    )

    log.info(
        "=========================================="
    )

    log.info(
        "EPG FINALIZADO"
    )

    log.info(
        "=========================================="
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        log.error(
            "Execução interrompida."
        )

        sys.exit(130)

    except Exception as exc:

        log.exception(
            "Erro fatal: %s",
            exc,
        )

        sys.exit(1)
