import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime
from collections import OrderedDict

# --------------------------------------------------------------------------
# Tenta usar curl_cffi (imita a "impressão digital" TLS de um browser real).
# Ajuda o Investidor10 a não cair em bloqueio anti-bot vindo de IP de
# datacenter (GitHub Actions). Se não estiver instalado, cai pro urllib puro
# — o Investidor10 costuma funcionar assim mesmo, só com um pouco mais de
# chance de bloqueio ocasional.
# --------------------------------------------------------------------------
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
# Permite desligar o Investidor10 via secret/variável de ambiente, caso
# comece a dar problema recorrente no GitHub Actions.
ENABLE_INVESTIDOR10 = os.environ.get("ENABLE_INVESTIDOR10", "true").strip().lower() != "false"

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
SCRAPE_TIMEOUT = 15
SCRAPE_SLEEP = 0.4  # intervalo entre requisições pro Investidor10 (educado com o servidor)

# --------------------------------------------------------------------------
# Janela de checagem do Investidor10: como só ele fornece nome do ativo,
# VPA e o histórico mês a mês de proventos, e como esses dados não mudam
# todo dia (proventos são anunciados historicamente sempre perto do mesmo
# dia do mês), só raspamos um ticker por vez quando:
#   (a) nunca tivemos dado dele;
#   (b) já faz tempo demais que não checamos (rede de segurança, cobre
#       fundos com calendário irregular); ou
#   (c) hoje cai dentro da janela em volta do dia em que ele historicamente
#       anuncia provento.
# Isso reduz o volume diário de ~1200 tickers pra uma fração disso.
# --------------------------------------------------------------------------
JANELA_DIAS_PROVENTO = int(os.environ.get("JANELA_DIAS_PROVENTO", "5"))
CHECAGEM_SEGURANCA_DIAS = int(os.environ.get("CHECAGEM_SEGURANCA_DIAS", "10"))

# --------------------------------------------------------------------------
# Disjuntor de cota da brapi: se o Fundamentus falhar amplamente (fora do ar,
# timeout, bloqueio — como aconteceu em 18/08/2026, ver log), a etapa de
# fallback não deve tentar resolver TODOS os tickers via brapi de uma vez.
# Plano Gratuito da brapi: 15.000 requisições/mês, 1 ticker por chamada
# (confirmado em https://brapi.dev/faq/como-a-api-e-calculada). Jogar os
# ~1140 tickers da tickers.txt pra brapi numa única execução já consome
# quase 8% da cota MENSAL de uma vez só.
# --------------------------------------------------------------------------
LIMIAR_FALHA_AMPLA_PCT = float(os.environ.get("LIMIAR_FALHA_AMPLA_PCT", "0.5"))
BRAPI_FALLBACK_MAX_TICKERS = int(os.environ.get("BRAPI_FALLBACK_MAX_TICKERS", "300"))

# --------------------------------------------------------------------------
# Identificadores internos de fonte usados no fiis.json em vez do nome do
# domínio por extenso. É só um rótulo interno — não impede alguém que leia
# este arquivo .py de descobrir a origem, só evita que o nome do site
# apareça de forma óbvia pra quem só olha o JSON de saída público.
# --------------------------------------------------------------------------
FONTE_FUNDAMENTUS = "f4"
FONTE_INVESTIDOR10 = "i7"
FONTE_BRAPI = "b2"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'pt-BR,pt;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
}

# Formato de ticker B3: 4 letras + 1-2 dígitos + opcional 1 letra (ex: MXRF11, PETR4, DOVL11B)
TICKER_RE = re.compile(r'^[A-Z]{4}\d{1,2}[A-Z]?$')


def read_tickers():
    if not os.path.exists(TICKERS_FILE):
        print(f"⚠️ Arquivo {TICKERS_FILE} não encontrado.")
        return []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        tickers = []
        seen = set()
        for line in f:
            t = line.strip().upper()
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
        return tickers


def read_estado_anterior():
    """Carrega o fiis.json da execução anterior (se existir), pra recuperar
    campos que só o Investidor10 fornece (nome, VPA, proventos, checagem)
    nos dias em que um ticker não está na janela de raspagem. Sem isso,
    esses campos sumiriam do JSON toda vez que o ticker não fosse checado."""
    if not os.path.exists(OUTPUT_FILE):
        return {}
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data.get("fiis", {})
    except Exception as e:
        print(f"⚠️ Não foi possível ler o estado anterior de {OUTPUT_FILE}: {e}")
        return {}


def _first_not_none(*valores):
    """Retorna o primeiro valor da lista que não é None, na ordem de
    prioridade dada. Diferente de um encadeamento de 'or', preserva um
    0.0 legítimo (ex: DY de um fundo parado) em vez de tratá-lo como
    'ausente' e cair pra próxima fonte por engano."""
    for v in valores:
        if v is not None:
            return v
    return None


# --------------------------------------------------------------------------
# FONTE 1 (fallback): brapi.dev — só é chamada pros tickers que o
# Fundamentus não retornou (ex: ticker recém-listado, BDR, ou algo fora das
# tabelas dele). Deixou de ser a fonte primária de preço.
# --------------------------------------------------------------------------
def fetch_brapi_batch(batch_tickers):
    if not batch_tickers:
        return {}

    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true{token_param}"

    req = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            output = {}
            for item in results:
                symbol = item.get("symbol", "").upper()
                if not symbol:
                    continue
                output[symbol] = {
                    "nome": item.get("longName") or item.get("shortName") or symbol,
                    "preco": float(item.get("regularMarketPrice") or 0.0),
                    "segmento_brapi": item.get("sector") or "",
                }
            return output
    except Exception as e:
        print(f"  ❌ Erro no lote brapi [{tickers_str}]: {e}")
        return {}


def fetch_brapi_fallback(tickers):
    """Só roda pros tickers que o Fundamentus não cobriu. Mantém a brapi
    como rede de segurança, não mais como fonte do dia a dia.

    IMPORTANTE: pede UM ticker por chamada de propósito. Confirmado na
    documentação oficial da brapi (https://brapi.dev/faq/como-a-api-e-calculada):
    cada chamada HTTP conta como 1 requisição de cota INDEPENDENTE de quantos
    tickers vêm juntos — e o plano Gratuito só aceita 1 ticker por chamada
    mesmo (múltiplos tickers por chamada é recurso dos planos Startup/Pro).
    Ou seja, agrupar tickers aqui não economizaria cota nenhuma no plano
    gratuito, e ainda arriscaria a chamada falhar/ignorar o excesso. Não mexer
    nisso sem antes confirmar o plano contratado."""
    if not tickers:
        return {}
    print(f"  🔁 {len(tickers)} tickers ausentes no Fundamentus — tentando brapi como fallback...")
    brapi_data = {}
    for t in tickers:
        res = fetch_brapi_batch([t])
        brapi_data.update(res)
        time.sleep(0.3)
    print(f"  └─ {len(brapi_data)} de {len(tickers)} resolvidos via brapi (fallback).")
    return brapi_data


# --------------------------------------------------------------------------
# Helpers de parsing compartilhados
# --------------------------------------------------------------------------
def _to_float_br(txt):
    """Converte '1.234,56' ou '8,50%' (formato BR) para float. '-' vira 0.0."""
    if not txt:
        return 0.0
    txt = txt.replace('.', '').replace(',', '.').replace('%', '').strip()
    try:
        return float(txt)
    except ValueError:
        return 0.0


def _to_float_br_com_unidade(valor_txt, unidade_txt):
    """'7,57' + 'Bilhões' -> 7570000000.0 ; '45,60' + 'Milhões' -> 45600000.0."""
    base = _to_float_br(valor_txt)
    if not unidade_txt:
        return base
    u = unidade_txt.strip().lower()
    if u.startswith("bilh"):
        return base * 1_000_000_000
    if u.startswith("milh"):
        return base * 1_000_000
    if u.startswith("mil"):
        return base * 1_000
    return base


def _get_flat_text(html):
    """Baixa a estrutura HTML -> texto corrido com espaços simples.
    Deixa a extração por regex imune a mudança de classes/CSS do site,
    já que trabalha só em cima do texto visível, na ordem em que aparece.
    """
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    else:
        text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_after(text, label_pattern, value_pattern, window=300):
    """Acha `label_pattern` no texto e procura `value_pattern` logo depois
    (dentro de uma janela de `window` caracteres). Retorna o primeiro grupo
    capturado ou None."""
    m = re.search(label_pattern, text, re.IGNORECASE)
    if not m:
        return None
    trecho = text[m.end():m.end() + window]
    v = re.search(value_pattern, trecho, re.IGNORECASE)
    return v.group(1) if v else None


def _http_get(url, use_curl_cffi=True):
    """GET genérico: tenta curl_cffi (impersona Chrome) e cai pro urllib se
    ele não estiver disponível ou falhar."""
    if use_curl_cffi and HAS_CURL_CFFI:
        resp = cffi_requests.get(url, headers=HEADERS, timeout=SCRAPE_TIMEOUT, impersonate="chrome")
        resp.raise_for_status()
        return resp.text
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as response:
        raw = response.read()
    return raw.decode('utf-8', errors='ignore')


# --------------------------------------------------------------------------
# FONTE 2: Investidor10 (scraping) -> agora só fornece o que mais ninguém
# dá em lote: nome bonito do ativo, VPA e o histórico de proventos. Só é
# consultado por ticker quando ele está na janela (ver
# precisa_checar_investidor10, mais abaixo).
# --------------------------------------------------------------------------
PROVENTO_LINHA_RE = re.compile(
    r'([A-ZÀ-Ú][A-Za-zÀ-ú\.]*(?:\s+[A-ZÀ-Ú][A-Za-zÀ-ú\.]*){0,2})\s+'
    r'(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d+,\d+)'
)

TIPOS_NAO_MONETARIOS = {
    "bonificacao", "bonificação",
    "desdobramento", "desdobro",
    "grupamento",
    "direito de subscricao", "direito de subscrição",
    "subscricao", "subscrição",
}


def _is_provento_monetario(tipo_txt):
    tipo_norm = tipo_txt.strip().lower()
    tipo_norm = (
        tipo_norm
        .replace('á', 'a').replace('ã', 'a').replace('â', 'a')
        .replace('é', 'e').replace('ê', 'e')
        .replace('í', 'i')
        .replace('ó', 'o').replace('õ', 'o').replace('ô', 'o')
        .replace('ú', 'u').replace('ç', 'c')
    )
    for termo in TIPOS_NAO_MONETARIOS:
        termo_norm = (
            termo
            .replace('á', 'a').replace('ã', 'a').replace('â', 'a')
            .replace('é', 'e').replace('ê', 'e')
            .replace('í', 'i')
            .replace('ó', 'o').replace('õ', 'o').replace('ô', 'o')
            .replace('ú', 'u').replace('ç', 'c')
        )
        if termo_norm in tipo_norm:
            return False
    return True


def _parse_investidor10_proventos(text, max_meses=15):
    grupos = OrderedDict()
    for tipo, dcom_txt, dpag_txt, valor_txt in PROVENTO_LINHA_RE.findall(text):
        if not _is_provento_monetario(tipo):
            continue
        try:
            dcom = datetime.strptime(dcom_txt, '%d/%m/%Y')
            dpag = datetime.strptime(dpag_txt, '%d/%m/%Y')
        except ValueError:
            continue
        valor = _to_float_br(valor_txt)
        if valor <= 0:
            continue
        chave_mes = dcom.strftime('%Y-%m')
        g = grupos.setdefault(chave_mes, {"valor": 0.0, "data_com": dcom, "data_pagamento": dpag})
        g["valor"] += valor
        if dcom < g["data_com"]:
            g["data_com"] = dcom
        if dpag > g["data_pagamento"]:
            g["data_pagamento"] = dpag

    if not grupos:
        return []

    ordenado = sorted(grupos.values(), key=lambda g: g["data_com"], reverse=True)
    return [
        {
            "valor_por_cota": round(g["valor"], 6),
            "data_com": g["data_com"].strftime('%Y-%m-%d'),
            "data_pagamento": g["data_pagamento"].strftime('%Y-%m-%d'),
        }
        for g in ordenado[:max_meses]
    ]


def _parse_investidor10_html(html):
    text = _get_flat_text(html)

    preco_txt = (
        _extract_after(text, r'VALOR DA COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'Cota[çc][ãa]o\s+R\$', r'([\d\.,]+)', window=20)
        or _extract_after(text, r'\bCOTAÇÃO\b', r'R\$\s*([\d\.,]+)')
    )
    dy_12m = (
        _extract_after(text, r'DY\s*\(12M\)', r'([\d,]+)\s*%')
        or _extract_after(text, r'DY\s+atual\s*:', r'([\d,]+)\s*%')
    )
    p_vp = _extract_after(text, r'\bP\s*/\s*VP\b', r'([\d,]+)')
    vacancia = _extract_after(text, r'VAC[ÂA]NCIA\b', r'([\d,]+)\s*%')
    p_l = _extract_after(text, r'\bP\s*/\s*L\b', r'([\d,]+)', window=20)

    segmento = _extract_after(text, r'\bSEGMENTO\b', r'([A-Za-zÀ-ú/ ]+?)(?:\s+TIPO DE FUNDO|\s+PRAZO)')
    setor_atuacao_acao = None
    if not segmento:
        m_setor = re.search(
            r'\bSetor\s+([A-Za-zÀ-ú,\-/ ]+?)\s+Segmento\s+([A-Za-zÀ-ú,\-/ ]+?)'
            r'(?:\s+Regi[oõ]es|\s+PRODUÇÃO|\s+negócios|\s{2,}|\.)',
            text
        )
        if m_setor:
            segmento = m_setor.group(1)
            setor_atuacao_acao = m_setor.group(2).strip()

    vpa_txt = (
        _extract_after(text, r'VAL\.\s*PATRIMONIAL\s*P/\s*COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'\bVPA\b', r'([\d,]+)', window=20)
    )

    vp_match = re.search(
        r'(?<!P/ )VALOR PATRIMONIAL\D{0,20}?R\$\s*([\d\.,]+)\s*(Bilh\w*|Milh\w*|Mil\b)?',
        text, re.IGNORECASE
    ) or re.search(
        r'Patrim[oô]nio\s+L[ií]quido\D{0,20}?R\$\s*([\d\.,]+)\s*(Bilh\w*|Milh\w*|Mil\b)?',
        text, re.IGNORECASE
    )
    valor_mercado = 0.0
    if vp_match:
        valor_mercado = _to_float_br_com_unidade(vp_match.group(1), vp_match.group(2))

    nome = ""
    if HAS_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            h2 = soup.find("h2", class_="name-company") or soup.find("h2")
            if h2 and h2.get_text(strip=True):
                nome = h2.get_text(strip=True)
        except Exception:
            pass

    resultado = {}
    if nome:
        resultado["nome"] = nome
    if preco_txt:
        resultado["preco"] = _to_float_br(preco_txt)
    if dy_12m:
        resultado["dy_12m_pct"] = _to_float_br(dy_12m)
    if p_vp:
        resultado["p_vp"] = _to_float_br(p_vp)
    if p_l:
        resultado["p_l"] = _to_float_br(p_l)
    if vacancia:
        resultado["vacancia_pct"] = _to_float_br(vacancia)
    if segmento:
        resultado["segmento"] = segmento.strip(" -")
    if setor_atuacao_acao:
        resultado["setor_atuacao"] = setor_atuacao_acao
    if vpa_txt:
        resultado["vpa"] = _to_float_br(vpa_txt)
    if valor_mercado:
        resultado["valor_mercado"] = valor_mercado

    proventos = _parse_investidor10_proventos(text)
    if proventos:
        resultado["proventos_12m"] = proventos
        resultado["ultimo_provento"] = proventos[0]

    return resultado


def fetch_investidor10_fii(ticker):
    urls = (
        ("fii", f"https://investidor10.com.br/fiis/{ticker.lower()}/"),
        ("acao", f"https://investidor10.com.br/acoes/{ticker.lower()}/"),
    )
    ultimo_erro = None
    for categoria, url in urls:
        try:
            html = _http_get(url)
        except Exception as e:
            ultimo_erro = e
            continue

        resultado = _parse_investidor10_html(html)
        if resultado:
            return resultado

    if ultimo_erro:
        print(f"    ❌ [i10] {ticker} não encontrado (fii/ação): {ultimo_erro}")
    else:
        print(f"    ⚠️ [i10] {ticker} respondeu mas sem dados reconhecíveis.")
    return {}


def _dia_tipico_do_mes(ultimo_provento):
    """Dia do mês (1-31) da data-com do último provento salvo — usado como
    estimativa de quando o próximo deve ser anunciado."""
    if not ultimo_provento or not ultimo_provento.get("data_com"):
        return None
    try:
        return int(ultimo_provento["data_com"].split("-")[2])
    except (ValueError, IndexError, TypeError):
        return None


def _distancia_dias_no_mes(dia_a, dia_b):
    """Distância circular aproximada entre dois dias do mês (1-31),
    considerando o "wrap" de fim pra início do mês. É uma aproximação —
    não trata meses de tamanhos diferentes com precisão perfeita, o que é
    aceitável aqui (a rede de segurança de CHECAGEM_SEGURANCA_DIAS cobre
    qualquer imprecisão de borda)."""
    diff = abs(dia_a - dia_b)
    return min(diff, 31 - diff)


def precisa_checar_investidor10(ticker, estado_anterior, hoje):
    """Decide se hoje é dia de raspar este ticker no Investidor10."""
    prev = estado_anterior.get(ticker)
    if not prev:
        return True  # ticker novo — sempre checa pra semear o dado

    ultima_checagem_str = prev.get(f"_chk_{FONTE_INVESTIDOR10}")
    if not ultima_checagem_str:
        return True

    try:
        ultima_checagem = datetime.strptime(ultima_checagem_str, "%Y-%m-%d")
    except ValueError:
        return True

    if (hoje - ultima_checagem).days >= CHECAGEM_SEGURANCA_DIAS:
        return True  # rede de segurança

    dia_tipico = _dia_tipico_do_mes(prev.get("ultimo_provento"))
    if dia_tipico is not None and _distancia_dias_no_mes(hoje.day, dia_tipico) <= JANELA_DIAS_PROVENTO:
        return True  # dentro da janela de anúncio esperado

    return False


def fetch_investidor10_all(tickers, estado_anterior, hoje):
    """Retorna (dados_coletados, conjunto_de_tickers_checados_hoje).
    O segundo valor é necessário pro merge saber quem marcar com a data de
    checagem de hoje, mesmo quando a raspagem falha (pra não martelar o
    mesmo ticker todo dia só porque ele deu erro uma vez)."""
    if not ENABLE_INVESTIDOR10:
        print("  ⏭️  ENABLE_INVESTIDOR10=false — pulando Investidor10.")
        return {}, set()
    if not HAS_BS4:
        print("  ⚠️ beautifulsoup4 não instalado — pulando Investidor10 (adicione ao requirements.txt).")
        return {}, set()

    selecionados = [t for t in tickers if precisa_checar_investidor10(t, estado_anterior, hoje)]
    print(f"  🔎 {len(selecionados)} de {len(tickers)} tickers estão na janela de checagem hoje...")

    result = {}
    checados = set()
    ok = 0
    for t in selecionados:
        dados = fetch_investidor10_fii(t)
        checados.add(t)  # marca como checado mesmo em caso de falha
        if dados:
            result[t] = dados
            ok += 1
        time.sleep(SCRAPE_SLEEP)
    print(f"  ✅ {ok} de {len(selecionados)} resolvidos via Investidor10.")
    return result, checados


# --------------------------------------------------------------------------
# FONTE 3 (agora primária): fundamentus.com.br em lote -> preço e
# fundamentos numéricos de TODOS os tickers em só 2 requisições (uma pra
# FIIs, uma pra ações). Sem cota, sem token, sem limite de requisições por
# dia. Como o app atualiza depois das 18h (pregão já fechado), o preço de
# fechamento daqui é equivalente ao "tempo real" que a brapi traria nesse
# horário — por isso deixa de fazer sentido chamar a brapi todo dia.
# --------------------------------------------------------------------------
def _extrair_tabela_resultado(html):
    """Localiza especificamente a tabela com id="resultado" — a tabela de
    dados real, tanto em resultado.php quanto em fii_resultado.php.

    O bug original usava `re.search(r'<table[^>]*>(.*?)</table>', ...)`, que
    pega a PRIMEIRA <table> do HTML (menu, busca "Procurar por ação/fii",
    "Fundamentus Mobile" etc. também são montados com <table> nesse site
    legado) — não a tabela de resultados. Isso fazia a extração perder
    quase todas as linhas silenciosamente, sem lançar erro, e todo o
    restante caía no fallback da brapi (consumindo cota à toa).

    Tenta achar por id="resultado" primeiro (com BS4, se disponível, que é
    mais robusto a variações de aspas/atributos); cai pra regex ancorada no
    id como segunda opção; só usa "primeira tabela genérica" como último
    recurso, e nesse caso o alerta de sanidade abaixo deve pegar o problema.
    """
    if HAS_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            tabela = soup.find("table", {"id": "resultado"})
            if tabela is not None:
                return str(tabela)
        except Exception:
            pass

    m = re.search(r'<table[^>]*\bid=["\']resultado["\'][^>]*>(.*?)</table>', html, re.DOTALL)
    if m:
        return m.group(1)

    # Último recurso (mantém compatibilidade se o id mudar de nome) — o
    # alerta de sanidade em _alerta_se_poucos_resultados avisa se isso
    # capturou a coisa errada.
    m = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    return m.group(1) if m else None


def _alerta_se_poucos_resultados(rotulo, quantidade, minimo_esperado):
    """Log bem visível quando a extração retorna muito menos linhas do que
    o esperado. Sem isso, uma falha de parsing (mudança de layout, bloqueio
    parcial etc.) é silenciosa: o script simplesmente empurra tudo pro
    fallback da brapi e consome cota sem ninguém perceber até o extrato de
    uso da API estourar."""
    if quantidade < minimo_esperado:
        print(f"  🚨 ALERTA: só {quantidade} {rotulo} extraídos do Fundamentus "
              f"(esperado bem mais que {minimo_esperado}). A extração da tabela pode "
              f"estar quebrada — isso vai jogar a maioria dos tickers pro fallback da "
              f"brapi e consumir cota. Verifique se o layout do site mudou.")


FUNDAMENTUS_TIMEOUT = int(os.environ.get("FUNDAMENTUS_TIMEOUT", "25"))
FUNDAMENTUS_TENTATIVAS = int(os.environ.get("FUNDAMENTUS_TENTATIVAS", "3"))


def _fetch_fundamentus_html(url):
    """GET com retry/backoff pro Fundamentus, priorizando curl_cffi
    (impersona Chrome/TLS de navegador real) desde a PRIMEIRA tentativa —
    igual já era feito pro Investidor10 em _http_get() (que resolvia bem as
    requisições dele). Isso nunca tinha sido aplicado ao Fundamentus em
    nenhuma versão anterior do script; ele sempre usou urllib puro, que se
    anuncia com um User-Agent de navegador mas não replica a "impressão
    digital" TLS de um Chrome de verdade — algo que sites com proteção
    anti-bot mais chata podem usar pra identificar e enfileirar/limitar
    tráfego de datacenter (o que apareceria pro nosso lado como timeout,
    igual o log de 18/08, e não como um 403 explícito).

    Não dá pra garantir 100% que o Fundamentus vai responder (é site de
    terceiro, fora do nosso controle) — mas com curl_cffi como método
    principal + retry/backoff, a chance de um timeout pontual conseguir ser
    contornado sobe bastante. Se curl_cffi não estiver instalado, cai pro
    urllib puro em todas as tentativas (comportamento antigo)."""
    ultimo_erro = None
    for tentativa in range(1, FUNDAMENTUS_TENTATIVAS + 1):
        usar_cffi = HAS_CURL_CFFI
        try:
            if usar_cffi:
                resp = cffi_requests.get(url, headers=HEADERS, timeout=FUNDAMENTUS_TIMEOUT,
                                          impersonate="chrome")
                resp.raise_for_status()
                return resp.content
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=FUNDAMENTUS_TIMEOUT) as response:
                return response.read()
        except Exception as e:
            ultimo_erro = e
            metodo = "curl_cffi" if usar_cffi else "urllib"
            print(f"    ⏳ Tentativa {tentativa}/{FUNDAMENTUS_TENTATIVAS} ({metodo}) falhou: {e}")
            if tentativa < FUNDAMENTUS_TENTATIVAS:
                time.sleep(2 * tentativa)  # backoff: 2s, 4s, ...
    raise ultimo_erro


def fetch_fundamentus_fiis():
    url = "https://www.fundamentus.com.br/fii_resultado.php"

    try:
        raw = _fetch_fundamentus_html(url)
    except Exception as e:
        print(f"  ❌ Erro ao acessar Fundamentus (FIIs) após {FUNDAMENTUS_TENTATIVAS} tentativas: {e}")
        return {}

    html = raw.decode('iso-8859-1', errors='ignore')

    table_html = _extrair_tabela_resultado(html)
    if table_html is None:
        print("  ⚠️ Não foi possível localizar a tabela #resultado de FIIs do Fundamentus "
              "(layout pode ter mudado).")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
    result = {}

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 13:
            continue
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        papel = clean[0].upper()
        if not TICKER_RE.match(papel):
            continue

        result[papel] = {
            "segmento": clean[1].strip() or "",
            "preco": _to_float_br(clean[2]),
            "dy_12m_pct": _to_float_br(clean[4]),
            "p_vp": _to_float_br(clean[5]),
            "valor_mercado": _to_float_br(clean[6]),
            "vacancia_pct": _to_float_br(clean[12]),
        }

    print(f"  ✅ {len(result)} FIIs lidos do Fundamentus.")
    _alerta_se_poucos_resultados("FIIs", len(result), minimo_esperado=300)
    return result


def fetch_fundamentus_acoes():
    """Espelha fetch_fundamentus_fiis(), mas pra ações (resultado.php).
    Colunas na ordem em que a tabela do Fundamentus expõe:
    0 papel, 1 cotação, 2 P/L, 3 P/VP, 4 PSR, 5 Div.Yield, 6 P/Ativo,
    7 P/Cap.Giro, 8 P/EBIT, 9 P/ACL, 10 EV/EBIT, 11 EV/EBITDA, 12 Mrg Ebit,
    13 Mrg. Líq., 14 ROIC, 15 ROE, 16 Liq. Corr., 17 Liq. 2 meses,
    18 Patrim. Líq., 19 Dív.Brut/Patrim., 20 Cresc. Rec. 5a.
    Usamos só o que o app precisa: cotação, P/L, P/VP, DY e patrimônio.
    """
    url = "https://www.fundamentus.com.br/resultado.php"

    try:
        raw = _fetch_fundamentus_html(url)
    except Exception as e:
        print(f"  ❌ Erro ao acessar Fundamentus (ações) após {FUNDAMENTUS_TENTATIVAS} tentativas: {e}")
        return {}

    html = raw.decode('iso-8859-1', errors='ignore')

    table_html = _extrair_tabela_resultado(html)
    if table_html is None:
        print("  ⚠️ Não foi possível localizar a tabela #resultado de ações do Fundamentus "
              "(layout pode ter mudado).")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
    result = {}

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 19:
            continue  # linha de cabeçalho ou lixo — tabela de ações tem mais colunas que a de FII
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        papel = clean[0].upper()
        if not TICKER_RE.match(papel):
            continue

        result[papel] = {
            "preco": _to_float_br(clean[1]),
            "p_l": _to_float_br(clean[2]),
            "p_vp": _to_float_br(clean[3]),
            "dy_12m_pct": _to_float_br(clean[5]),
            "valor_mercado": _to_float_br(clean[18]),  # patrimônio líquido
        }

    print(f"  ✅ {len(result)} ações lidas do Fundamentus.")
    _alerta_se_poucos_resultados("ações", len(result), minimo_esperado=300)
    return result


def fetch_fundamentus_all():
    """Roda as duas buscas em lote (FIIs + ações) — 2 requisições no total
    pra cobrir toda a tickers.txt, contra as ~1200 que seriam necessárias
    ticker a ticker. Esta é agora a fonte primária de preço e fundamentos."""
    print("  🔎 Buscando FIIs em lote...")
    fiis = fetch_fundamentus_fiis()
    time.sleep(0.3)
    print("  🔎 Buscando ações em lote...")
    acoes = fetch_fundamentus_acoes()

    combined = dict(acoes)
    combined.update(fiis)  # em caso de colisão de ticker (raríssimo), FII prevalece
    return combined


# --------------------------------------------------------------------------
# MERGE: combina fundamentus (primária) + investidor10 (nome/VPA/proventos,
# quando checado hoje) + brapi (fallback) + estado anterior (carrega campos
# que não foram atualizados hoje, pra não sumirem do JSON).
# Nunca inventa número: quando falta dado em todas as fontes, marca
# dados_completos=False.
# --------------------------------------------------------------------------
def merge_data(tickers, fundamentus_data, brapi_data, inv10_data, checados_i10, estado_anterior, hoje_str):
    merged = {}

    for symbol in tickers:
        f = fundamentus_data.get(symbol, {})
        b = brapi_data.get(symbol, {})
        i10 = inv10_data.get(symbol, {})
        prev = estado_anterior.get(symbol, {})

        preco = _first_not_none(f.get("preco"), b.get("preco"), i10.get("preco"), prev.get("preco")) or 0.0
        nome = i10.get("nome") or b.get("nome") or prev.get("nome") or symbol

        p_vp = _first_not_none(f.get("p_vp"), i10.get("p_vp"), prev.get("p_vp")) or 0.0
        p_l = _first_not_none(f.get("p_l"), i10.get("p_l"), prev.get("p_l")) or 0.0
        dy_12m = _first_not_none(f.get("dy_12m_pct"), i10.get("dy_12m_pct"), prev.get("dy_12m")) or 0.0
        vacancia = _first_not_none(f.get("vacancia_pct"), i10.get("vacancia_pct"), prev.get("vacancia_fisica")) or 0.0
        patrimonio = _first_not_none(f.get("valor_mercado"), i10.get("valor_mercado"), prev.get("patrimonio_liquido")) or 0.0

        segmento = f.get("segmento") or i10.get("segmento") or b.get("segmento_brapi") or prev.get("segmento") or ""
        setor_atuacao = i10.get("setor_atuacao") or prev.get("setor_atuacao") or segmento

        if i10.get("vpa"):
            vpa = round(i10.get("vpa"), 2)
        elif prev.get("valor_patrimonial_cota"):
            vpa = prev.get("valor_patrimonial_cota")
        elif p_vp > 0 and preco:
            vpa = round(preco / p_vp, 2)
        else:
            vpa = 0.0

        tem_preco = preco > 0
        tem_fundamentos = bool(f) or bool(i10) or bool(prev)

        fontes = []
        if f:
            fontes.append(FONTE_FUNDAMENTUS)
        if i10:
            fontes.append(FONTE_INVESTIDOR10)
        if b:
            fontes.append(FONTE_BRAPI)
        fonte = "+".join(fontes) if fontes else prev.get("fonte_dados", "indisponivel")

        registro = {
            "nome": nome,
            "segmento": segmento,
            "setor_atuacao": setor_atuacao,
            "preco": float(preco),
            "p_vp": float(p_vp),
            "p_l": float(p_l),
            "valor_patrimonial_cota": vpa,
            "dy_12m": float(dy_12m),
            "dy_mensal": round(dy_12m / 12.0, 4) if dy_12m else 0.0,
            "patrimonio_liquido": float(patrimonio),
            "vacancia_fisica": float(vacancia),
            "dados_completos": bool(tem_preco and tem_fundamentos),
            "fonte_dados": fonte,
        }

        proventos_12m = i10.get("proventos_12m") or prev.get("proventos_12m")
        if proventos_12m:
            registro["proventos_12m"] = proventos_12m
            registro["ultimo_provento"] = i10.get("ultimo_provento") or prev.get("ultimo_provento")

        if symbol in checados_i10:
            registro[f"_chk_{FONTE_INVESTIDOR10}"] = hoje_str
        elif prev.get(f"_chk_{FONTE_INVESTIDOR10}"):
            registro[f"_chk_{FONTE_INVESTIDOR10}"] = prev[f"_chk_{FONTE_INVESTIDOR10}"]

        merged[symbol] = registro

    return merged


def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers únicos de {TICKERS_FILE}...")

    if not tickers:
        print("⚠️ Nenhum ticker encontrado. Encerrando.")
        return

    hoje_com_hora = datetime.utcnow()
    hoje = datetime(hoje_com_hora.year, hoje_com_hora.month, hoje_com_hora.day)
    hoje_str = hoje.strftime("%Y-%m-%d")

    estado_anterior = read_estado_anterior()
    print(f"📦 Estado anterior carregado: {len(estado_anterior)} tickers.")

    print("\n--- Etapa 1/3: preço e fundamentos via Fundamentus (fonte primária, 2 requisições no total) ---")
    fundamentus_data = fetch_fundamentus_all()

    faltantes = [t for t in tickers if t not in fundamentus_data]
    print(f"\n--- Etapa 2/3: brapi como fallback ({len(faltantes)} tickers ausentes no Fundamentus) ---")

    faltantes_para_brapi = faltantes
    if tickers and len(faltantes) / len(tickers) > LIMIAR_FALHA_AMPLA_PCT:
        # Não são "alguns tickers que o Fundamentus não cobre" (BDR, recém-listado
        # etc.) — é uma fração grande demais pra ser normal, provavelmente o
        # Fundamentus caiu/bloqueou/deu timeout por completo (como em 18/08).
        # Jogar TODOS esses tickers pra brapi de uma vez queima a cota mensal
        # inteira (plano Gratuito: 15.000 req/mês, 1 ticker por chamada — ver
        # https://brapi.dev/faq/como-a-api-e-calculada) em pouquíssimas execuções
        # ruins. Por isso, nesse cenário, limitamos quantos tickers vão pra brapi
        # nesta execução e priorizamos os que não têm NENHUM dado em cache do dia
        # anterior (os demais mantêm o último preço/fundamento bom salvo).
        print(f"  🚨 ALERTA: {len(faltantes)} de {len(tickers)} tickers "
              f"({len(faltantes) / len(tickers):.0%}) ficaram sem dado do Fundamentus — isso indica "
              f"falha ampla da fonte (fora do ar, timeout, bloqueio), não apenas tickers legitimamente "
              f"ausentes das tabelas dele. Pra proteger a cota mensal da brapi, o fallback desta "
              f"execução fica limitado a {BRAPI_FALLBACK_MAX_TICKERS} tickers, priorizando quem não "
              f"tem nenhum dado salvo de execuções anteriores. O restante mantém o último preço/"
              f"fundamentos bons conhecidos (fonte_dados vai refletir isso).")
        faltantes_para_brapi = sorted(faltantes, key=lambda t: t in estado_anterior)[:BRAPI_FALLBACK_MAX_TICKERS]

    if faltantes_para_brapi and not BRAPI_TOKEN:
        print("⚠️ BRAPI_TOKEN não detectado — fallback pode vir limitado.")
    brapi_data = fetch_brapi_fallback(faltantes_para_brapi)

    print("\n--- Etapa 3/3: Investidor10 (nome, VPA, proventos — só tickers na janela de checagem) ---")
    inv10_data, checados_i10 = fetch_investidor10_all(tickers, estado_anterior, hoje)

    fiis_data = merge_data(tickers, fundamentus_data, brapi_data, inv10_data, checados_i10, estado_anterior, hoje_str)

    completos = sum(1 for v in fiis_data.values() if v["dados_completos"])

    fontes_usadas = [FONTE_FUNDAMENTUS]
    if brapi_data:
        fontes_usadas.append(FONTE_BRAPI)
    if inv10_data:
        fontes_usadas.append(FONTE_INVESTIDOR10)

    result = {
        "gerado_em": hoje_com_hora.isoformat() + "Z",
        "fonte": "+".join(fontes_usadas),
        "total_fiis": len(fiis_data),
        "fiis_com_dados_completos": completos,
        "fiis": fiis_data,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 SUCESSO! {len(fiis_data)} ativos salvos em {OUTPUT_FILE} "
          f"({completos} com dados completos, {len(checados_i10)} checados no Investidor10 hoje).")


if __name__ == "__main__":
    main()
