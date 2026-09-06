#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import gzip
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import re
import pytz
import sys
import os
from urllib.parse import urljoin

# Configuração
DAYS = 2  # Quantos dias de EPG gerar (será sobrescrito pelo JSON)
TIMEZONE = pytz.timezone('America/Fortaleza')
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

# Cabeçalhos para requisições
HEADERS = {'User-Agent': USER_AGENT}

# Mapeamento de canais para slugs manuais (caso a heurística falhe)
SLUG_OVERRIDES = {
    'TV CIDADE VERDE 5.1': 'tv-cidade-verde',
    'TV CULTURA': 'tv-cultura',
    # Adicione mais conforme necessário
}

def carregar_canais(arquivo='channels.json'):
    """Carrega o JSON de canais."""
    with open(arquivo, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data['channels']

def slugify(nome):
    """Converte nome do canal para slug estilo mi.tv."""
    slug = nome.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
    # Substitui acentos
    import unicodedata
    slug = unicodedata.normalize('NFKD', slug).encode('ascii', 'ignore').decode('ascii')
    return slug

def obter_slug_canal(canal):
    """Obtém slug para o canal, tentando sobrescrita e heurística."""
    nome = canal['name']
    if nome in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[nome]
    # Tenta usar tvg_id se existir e for um slug válido
    tvg_id = canal.get('tvg_id', '')
    if tvg_id and re.match(r'^[a-z0-9\-]+$', tvg_id):
        return tvg_id
    return slugify(nome)

def extrair_programas_mi_tv(slug, data_inicio, dias):
    """Extrai programação do mi.tv para um slug, a partir de data_inicio, por 'dias' dias."""
    programas = []
    url_base = 'https://mi.tv/br/programacao/'
    # Construir URL com parâmetro de data (formato YYYY-MM-DD)
    for dia_offset in range(dias):
        data_atual = data_inicio + timedelta(days=dia_offset)
        data_str = data_atual.strftime('%Y-%m-%d')
        url = f"{url_base}{slug}?date={data_str}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"  Erro ao acessar mi.tv para {slug} em {data_str}: {e}")
            continue

        soup = BeautifulSoup(resp.text, 'html.parser')
        # Exemplo de estrutura: tabela com classe 'schedule-table' ou similar
        # Adaptar conforme inspeção real
        # Vamos procurar divs com horários e programas
        # <div class="program-row"> ou <tr>
        # Vamos usar um seletor genérico: encontrar todos os itens que contenham horário e título
        items = soup.select('.program-item, .program-row, .schedule-item')
        if not items:
            # Tentar outro seletor
            items = soup.select('table.schedule tr:not(:first-child)')
        for item in items:
            # Extrair hora de início (ex: "14:30")
            time_elem = item.select_one('.time, .hour, .schedule-time, td:first-child')
            if not time_elem:
                continue
            hora_str = time_elem.get_text(strip=True)
            # Extrair título
            title_elem = item.select_one('.title, .program-name, .schedule-title, td:nth-child(2)')
            if not title_elem:
                continue
            titulo = title_elem.get_text(strip=True)
            # Extrair descrição (opcional)
            desc_elem = item.select_one('.description, .desc, .program-desc, td:nth-child(3)')
            descricao = desc_elem.get_text(strip=True) if desc_elem else ''

            # Converter hora para datetime
            try:
                hora_obj = datetime.strptime(hora_str, '%H:%M').time()
                inicio = TIMEZONE.localize(datetime.combine(data_atual, hora_obj))
            except:
                continue

            # Duração padrão: 30 minutos (se não tiver fim)
            # Vamos tentar encontrar o próximo programa para calcular fim
            # Simplificando: assumir duração de 30 min
            fim = inicio + timedelta(minutes=30)

            programas.append({
                'start': inicio.astimezone(pytz.UTC),
                'stop': fim.astimezone(pytz.UTC),
                'title': titulo,
                'description': descricao,
                'channel': slug  # ou tvg_id
            })
        time.sleep(1)  # delay entre requisições
    return programas

def extrair_programas_guiatv(slug, data_inicio, dias):
    """Extrai programação do guiadetv.com."""
    # Estrutura semelhante, adaptar URL e seletores
    # Exemplo: https://www.guiadetv.com/programacao/globo-rio
    # Não implementado completamente para brevidade, mas pode ser análogo ao mi.tv
    # Retorna lista vazia para não quebrar
    return []

def obter_programas_canal(canal, dias):
    """Tenta obter programação do canal a partir de múltiplas fontes."""
    nome = canal['name']
    slug = obter_slug_canal(canal)
    programas = []
    # Tenta mi.tv
    print(f"Buscando EPG para {nome} (slug: {slug}) via mi.tv...")
    programas_mi = extrair_programas_mi_tv(slug, datetime.now(TIMEZONE), dias)
    if programas_mi:
        programas = programas_mi
    else:
        print(f"  mi.tv sem dados para {nome}, tentando guiadetv...")
        programas_guiatv = extrair_programas_guiatv(slug, datetime.now(TIMEZONE), dias)
        if programas_guiatv:
            programas = programas_guiatv
        else:
            print(f"  Nenhum dado encontrado para {nome}.")
    return programas

def gerar_xmltv(programas_por_canal, canais, arquivo_saida):
    """Gera arquivo XMLTV."""
    from xml.etree import ElementTree as ET
    tv = ET.Element('tv')
    # Adicionar informações dos canais (apenas os que têm programas)
    canais_com_programas = {c['name']: c for c in canais if c['name'] in programas_por_canal}
    for nome, canal in canais_com_programas.items():
        channel_elem = ET.SubElement(tv, 'channel', id=canal.get('tvg_id', nome))
        display_name = ET.SubElement(channel_elem, 'display-name')
        display_name.text = canal['name']
        if 'logo' in canal:
            icon = ET.SubElement(channel_elem, 'icon', src=canal['logo'])
        # Adicionar outros dados se desejar

    # Adicionar programas
    for nome, programas in programas_por_canal.items():
        if not programas:
            continue
        canal_id = next((c['tvg_id'] for c in canais if c['name'] == nome), nome)
        for prog in programas:
            programme = ET.SubElement(tv, 'programme',
                                      start=prog['start'].strftime('%Y%m%d%H%M%S %z'),
                                      stop=prog['stop'].strftime('%Y%m%d%H%M%S %z'),
                                      channel=canal_id)
            title = ET.SubElement(programme, 'title')
            title.text = prog['title']
            if prog.get('description'):
                desc = ET.SubElement(programme, 'desc')
                desc.text = prog['description']
            # Pode adicionar mais campos (category, etc.)

    # Escrever XML com declaração
    tree = ET.ElementTree(tv)
    tree.write(arquivo_saida, encoding='utf-8', xml_declaration=True)

def gerar_playlist(canais, arquivo_saida):
    """Gera arquivo M3U com os canais e atributos EPG."""
    with open(arquivo_saida, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        for canal in canais:
            tvg_id = canal.get('tvg_id', '')
            tvg_name = canal.get('tvg_name', canal['name'])
            group = canal.get('group', 'Brasil')
            logo = canal.get('logo', '')
            url = canal.get('url', '')
            # Escolher a URL (pode haver várias, usar a primeira)
            # Na estrutura, cada canal tem uma URL, então ok
            f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-name="{tvg_name}" tvg-logo="{logo}" group-title="{group}",{canal["name"]}\n')
            f.write(f'{url}\n')

def gerar_aliases(canais, arquivo_saida):
    """Gera mapeamento de aliases para EPG."""
    aliases = {}
    for canal in canais:
        nome = canal['name']
        tvg_id = canal.get('tvg_id', nome)
        # Incluir aliases fornecidos
        canal_aliases = canal.get('aliases', [])
        aliases[nome] = {
            'tvg_id': tvg_id,
            'aliases': canal_aliases,
            'slug': obter_slug_canal(canal)
        }
    with open(arquivo_saida, 'w', encoding='utf-8') as f:
        json.dump(aliases, f, ensure_ascii=False, indent=2)

def main():
    # Carregar canais
    canais = carregar_canais('channels.json')
    # Obter dias do JSON (se presente)
    global DAYS
    with open('channels.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        DAYS = data.get('days', DAYS)

    # Coletar programas de cada canal
    programas_por_canal = {}
    for canal in canais:
        nome = canal['name']
        programas = obter_programas_canal(canal, DAYS)
        if programas:
            programas_por_canal[nome] = programas
        else:
            print(f"AVISO: Nenhum programa encontrado para {nome}")

    # Garantir diretório output
    os.makedirs('output', exist_ok=True)

    # Gerar arquivos
    print("Gerando epg.xml...")
    gerar_xmltv(programas_por_canal, canais, 'output/epg.xml')
    print("Comprimindo epg.xml.gz...")
    with open('output/epg.xml', 'rb') as f_in:
        with gzip.open('output/epg.xml.gz', 'wb') as f_out:
            f_out.writelines(f_in)
    print("Gerando playlist.m3u...")
    gerar_playlist(canais, 'output/playlist.m3u')
    print("Gerando epg_aliases.json...")
    gerar_aliases(canais, 'output/epg_aliases.json')
    print("Concluído!")

if __name__ == '__main__':
    main()
