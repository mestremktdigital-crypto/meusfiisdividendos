import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

# --------------------------------------------------------------------------
# Tenta usar curl_cffi (imita a "impressão digital" TLS de um browser real,
# necessário pra não tomar 403 do Cloudflare no Status Invest). Se não
# estiver instalado, cai pro urllib puro — funciona pro Investidor10 e pro
# Fundamentus, mas o Status Invest pode falhar sem ele (o código já trata
# isso e simplesmente pula a fonte, sem quebrar o resto do script).
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
BOLSAI_API_KEY = os.environ.get("BOLSAI_API_KEY", "").strip()  # opcional
# Permite desligar as fontes novas via secret/variável de ambiente, caso
# alguma delas comece a dar problema recorrente no GitHub Actions.
ENABLE_INVESTIDOR10 = os.environ.get("ENABLE_INVESTIDOR10", "true").strip().lower() != "false"
ENABLE_STATUSINVEST = os.environ.get("ENABLE_STATUSINVEST", "true").strip().lower() != "false"

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 1  # O plano atual da brapi só aceita 1 ticker por chamada (lotes de 10 dão 400)
SCRAPE_TIMEOUT = 15
SCRAPE_SLEEP = 0.4  # intervalo entre requisições pros sites raspados (educado com o servidor)

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


# --------------------------------------------------------------------------
# FONTE 1: brapi.dev -> preço em tempo (quase) real e nome do fundo
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
        if len(batch_tickers) > 1:
            print("  🔄 Tentando buscar tickers do lote individualmente...")
            single_output = {}
            for single_t in batch_tickers:
                res = fetch_brapi_batch([single_t])
                single_output.update(res)
                time.sleep(0.2)
            return single_output
        return {}


def fetch_brapi_all(tickers):
    brapi_data = {}
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 [brapi] Lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        batch_res = fetch_brapi_batch(batch)
        brapi_data.update(batch_res)
        print(f"  └─ {len(batch_res)} de {len(batch)} retornados.")
        time.sleep(0.3)
    return brapi_data


# --------------------------------------------------------------------------
# Helpers de parsing compartilhados pelo Investidor10 e pelo Status Invest
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
        # remove scripts/estilos pra não poluir o texto
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    else:
        text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_after(text, label_pattern, value_pattern, window=300):
    """Acha `label_pattern` no texto e procura `value_pattern` logo depois
    (dentro de uma janela de `window` caracteres). Retorna o primeiro grupo
    capturado ou None. É a base de toda a raspagem: resistente a mudança de
    HTML/CSS, mas exige que o TEXTO visível da página continue parecido.
    """
    m = re.search(label_pattern, text, re.IGNORECASE)
    if not m:
        return None
    trecho = text[m.end():m.end() + window]
    v = re.search(value_pattern, trecho, re.IGNORECASE)
    return v.group(1) if v else None


def _http_get(url, use_curl_cffi=True):
    """GET genérico: tenta curl_cffi (impersona Chrome, contorna Cloudflare)
    e cai pro urllib se ele não estiver disponível ou falhar."""
    if use_curl_cffi and HAS_CURL_CFFI:
        resp = cffi_requests.get(url, headers=HEADERS, timeout=SCRAPE_TIMEOUT, impersonate="chrome")
        resp.raise_for_status()
        return resp.text
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as response:
        raw = response.read()
    return raw.decode('utf-8', errors='ignore')


# --------------------------------------------------------------------------
# FONTE 2: Investidor10 (scraping) -> dados mais atualizados que o
# Fundamentus, cobre exatamente os campos que o plano free da brapi deixa
# em branco (P/VP, DY, VPA, vacância, segmento). Uma requisição por ticker.
# --------------------------------------------------------------------------
def fetch_investidor10_fii(ticker):
    url = f"https://investidor10.com.br/fiis/{ticker.lower()}/"
    try:
        html = _http_get(url)
    except Exception as e:
        print(f"    ❌ [investidor10] Erro em {ticker}: {e}")
        return {}

    text = _get_flat_text(html)

    # Preço da cota: rótulo "VALOR DA COTA" (mais específico que o
    # "{TICKER} Cotação" lá em cima, que aparece antes de o texto ficar
    # previsível o bastante pra raspar com segurança).
    preco_txt = _extract_after(text, r'VALOR DA COTA', r'R\$\s*([\d\.,]+)')
    dy_12m = _extract_after(text, r'DY\s*\(12M\)', r'([\d,]+)\s*%')
    p_vp = _extract_after(text, r'\bP\s*/\s*VP\b', r'([\d,]+)')
    vacancia = _extract_after(text, r'VAC[ÂA]NCIA\b', r'([\d,]+)\s*%')
    segmento = _extract_after(text, r'\bSEGMENTO\b', r'([A-Za-zÀ-ú/ ]+?)(?:\s+TIPO DE FUNDO|\s+PRAZO)')
    vpa_txt = _extract_after(text, r'VAL\.\s*PATRIMONIAL\s*P/\s*COTA', r'R\$\s*([\d\.,]+)')
    # o valor patrimonial do fundo vem como "R$ 7,57" + unidade "Bilhões"
    # separados (ex: "VALOR PATRIMONIAL R$ 7,57 Bilhões")
    vp_match = re.search(
        r'(?<!P/ )VALOR PATRIMONIAL\D{0,20}?R\$\s*([\d\.,]+)\s*(Bilh\w*|Milh\w*|Mil\b)?',
        text, re.IGNORECASE
    )
    valor_mercado = 0.0
    if vp_match:
        valor_mercado = _to_float_br_com_unidade(vp_match.group(1), vp_match.group(2))

    # nome do fundo: pega do <h1>/<h2> quando dá, sem depender do texto corrido
    nome = ""
    if HAS_BS4:
        try:
            soup = BeautifulSoup(html, "html.parser")
            h2 = soup.find("h2")
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
    if vacancia:
        resultado["vacancia_pct"] = _to_float_br(vacancia)
    if segmento:
        resultado["segmento"] = segmento.strip(" -")
    if vpa_txt:
        resultado["vpa"] = _to_float_br(vpa_txt)
    if valor_mercado:
        resultado["valor_mercado"] = valor_mercado

    return resultado


def fetch_investidor10_all(tickers):
    if not ENABLE_INVESTIDOR10:
        print("  ⏭️  ENABLE_INVESTIDOR10=false — pulando Investidor10.")
        return {}
    if not HAS_BS4:
        print("  ⚠️ beautifulsoup4 não instalado — pulando Investidor10 (adicione ao requirements.txt).")
        return {}

    print(f"  🔎 Consultando Investidor10 para {len(tickers)} tickers...")
    result = {}
    ok = 0
    for t in tickers:
        dados = fetch_investidor10_fii(t)
        if dados:
            result[t] = dados
            ok += 1
        time.sleep(SCRAPE_SLEEP)
    print(f"  ✅ {ok} de {len(tickers)} resolvidos via Investidor10.")
    return result


# --------------------------------------------------------------------------
# FONTE 3: Status Invest -> segunda camada de dados atualizados, cobre o que
# sobrar do Investidor10. Se curl_cffi não estiver instalado, essa fonte é
# pulada silenciosamente (o Status Invest costuma bloquear requisições
# "cruas" vindas de IPs de datacenter como os runners do GitHub Actions).
#
# Tenta primeiro o endpoint em lote usado pela página de busca avançada
# (fundos-imobiliarios/busca-avancada) — 1 requisição pra ~124 FIIs em vez
# de 1 por ticker. Honestidade sobre o que isso resolve: a URL e os nomes
# de campo (ticker/companyName/pvp/netWorth...) foram inferidos a partir do
# HTML da página de busca avançada, não confirmados contra uma resposta
# real do endpoint — não dá pra garantir 100% que batem. Por isso a função
# de lote é 100% best-effort: se o endpoint não existir, mudar de nome de
# campo, ou o site bloquear, ela retorna {} e o código cai automaticamente
# pro scraping por ticker abaixo (que já foi validado), sem quebrar nada.
# --------------------------------------------------------------------------
def fetch_statusinvest_batch():
    if not HAS_CURL_CFFI:
        print("  ⏭️  curl_cffi não instalado — pulando tentativa de lote do Status Invest.")
        return {}

    url = ("https://statusinvest.com.br/category/advancedsearchresultpaginated"
           '?search={"Sector":null,"SubSector":null,"Segment":null,"my_range":"-20;100"}'
           "&orderColumn=&isAsc=&page=0&take=1000&CategoryType=2")

    try:
        resp = cffi_requests.get(
            url,
            headers={
                **HEADERS,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://statusinvest.com.br/fundos-imobiliarios",
            },
            impersonate="chrome",
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"  ❌ [statusinvest-lote] HTTP {resp.status_code} (provável bloqueio anti-bot).")
            return {}
        data = resp.json()
    except Exception as e:
        print(f"  ❌ [statusinvest-lote] Erro: {e}")
        return {}

    items = data.get("list") if isinstance(data, dict) else data
    if not items:
        print("  ⚠️ [statusinvest-lote] Resposta sem lista de FIIs (endpoint/layout pode ter mudado).")
        return {}

    # Debug: imprime as chaves brutas do 1º item, igual já fazemos no
    # fetch_bolsai_fii. Serve pra confirmar (ou corrigir rápido, num log do
    # workflow_dispatch) os nomes de campo assumidos abaixo, sem precisar
    # adivinhar de novo caso o site mude alguma coisa.
    print(f"  🔎 [statusinvest-lote] campos brutos do 1º item: {list(items[0].keys())}")

    result = {}
    for item in items:
        papel = (item.get("ticker") or item.get("code") or "").upper()
        if not papel or not TICKER_RE.match(papel):
            continue
        result[papel] = {
            "nome": item.get("companyName") or item.get("name") or "",
            "segmento": item.get("segment") or item.get("sectorName") or "",
            "preco": float(item.get("price") or item.get("value") or 0.0),
            "dy_12m_pct": float(item.get("dy") or 0.0),
            "p_vp": float(item.get("pvp") or 0.0),
            "valor_mercado": float(item.get("netWorth") or item.get("marketCap") or 0.0),
            "vacancia_pct": float(item.get("vacancy") or 0.0),
        }

    print(f"  ✅ [statusinvest-lote] {len(result)} FIIs lidos em 1 única requisição.")
    return result


def fetch_statusinvest_fii(ticker):
    url = f"https://statusinvest.com.br/fundos-imobiliarios/{ticker.lower()}"
    try:
        html = _http_get(url, use_curl_cffi=True)
    except Exception as e:
        print(f"    ❌ [statusinvest] Erro em {ticker}: {e}")
        return {}

    text = _get_flat_text(html)

    preco = _extract_after(text, r'Valor atual', r'R\$\s*([\d\.,]+)')
    dy_12m = _extract_after(text, r'Dividend Yield', r'([\d,]+)\s*%')
    p_vp = _extract_after(text, r'\bP\s*/\s*VP\b', r'([\d,]+)')
    vpa_txt = _extract_after(text, r'Val\.\s*patrim\w*\.?\s*p/\s*cota', r'R\$\s*([\d\.,]+)')
    patrimonio_txt = _extract_after(text, r'Patrim[ôo]nio\b', r'R\$\s*([\d\.,]+)')

    resultado = {}
    if preco:
        resultado["preco"] = _to_float_br(preco)
    if dy_12m:
        resultado["dy_12m_pct"] = _to_float_br(dy_12m)
    if p_vp:
        resultado["p_vp"] = _to_float_br(p_vp)
    if vpa_txt:
        resultado["vpa"] = _to_float_br(vpa_txt)
    if patrimonio_txt:
        resultado["valor_mercado"] = _to_float_br(patrimonio_txt)

    return resultado


def fetch_statusinvest_all(tickers):
    if not ENABLE_STATUSINVEST:
        print("  ⏭️  ENABLE_STATUSINVEST=false — pulando Status Invest.")
        return {}
    if not HAS_CURL_CFFI:
        print("  ⚠️ curl_cffi não instalado — pulando Status Invest (adicione ao requirements.txt).")
        print("     Sem ele o Status Invest costuma bloquear com 403 (proteção Cloudflare).")
        return {}

    # Tentativa 1: endpoint em lote (1 requisição em vez de 1 por ticker).
    batch_data = fetch_statusinvest_batch()
    result = {t: batch_data[t] for t in tickers if t in batch_data}
    faltantes = [t for t in tickers if t not in result]

    if not faltantes:
        print(f"  ✅ {len(result)} de {len(tickers)} resolvidos via Status Invest (lote).")
        return result

    # Tentativa 2 (fallback): scraping por ticker só pra quem o lote não
    # cobriu — ou pra todo mundo, se o lote falhou por completo.
    if batch_data:
        print(f"  🔎 [statusinvest] Lote resolveu {len(result)}; "
              f"buscando os {len(faltantes)} restantes por ticker...")
    else:
        print(f"  🔎 [statusinvest] Lote indisponível; buscando {len(faltantes)} tickers individualmente...")

    ok_individual = 0
    for t in faltantes:
        dados = fetch_statusinvest_fii(t)
        if dados:
            result[t] = dados
            ok_individual += 1
        time.sleep(SCRAPE_SLEEP)
    print(f"  ✅ {len(result)} de {len(tickers)} resolvidos via Status Invest "
          f"(lote: {len(result) - ok_individual}, individual: {ok_individual}).")
    return result


# --------------------------------------------------------------------------
# FONTE 4: Fundamentus -> camada de segurança final antes do bolsai. Cobre o
# que as duas fontes de cima não trouxerem. Uma única requisição cobre todos
# os FIIs de uma vez (por isso continua valendo a pena manter, mesmo tendo
# atraso em relação ao Investidor10/Status Invest).
#
# StatusInvest via HTML foi mantido acima como fonte separada; o antigo
# comentário sobre "StatusInvest removido por bloquear 403" não vale mais
# porque agora usamos curl_cffi pra imitar o TLS de um browser real.
# --------------------------------------------------------------------------
def fetch_fundamentus_fiis():
    url = "https://www.fundamentus.com.br/fii_resultado.php"
    req = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
    except Exception as e:
        print(f"  ❌ Erro ao acessar Fundamentus: {e}")
        return {}

    # O site é antigo e serve em Latin-1, não UTF-8
    html = raw.decode('iso-8859-1', errors='ignore')

    # IMPORTANTE: não depender de haver uma tag <tbody> explícita no HTML —
    # o Fundamentus nem sempre fecha a tabela dessa forma, e isso fazia o
    # parser antigo não achar nada (0 FIIs, mesmo com o site no ar).
    # Pegamos a tabela inteira (cabeçalho + linhas) e filtramos linha por
    # linha pelo formato do ticker na primeira célula.
    table_match = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        print("  ⚠️ Não foi possível localizar a tabela do Fundamentus (layout pode ter mudado).")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL)
    result = {}

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 13:
            continue  # linha de cabeçalho (usa <th>, não <td>) ou lixo
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        papel = clean[0].upper()
        if not TICKER_RE.match(papel):
            continue  # não parece um ticker válido, ignora a linha

        result[papel] = {
            "segmento": clean[1].strip() or "",
            "preco": _to_float_br(clean[2]),  # cotação do próprio Fundamentus (fallback de preço)
            "dy_12m_pct": _to_float_br(clean[4]),
            "p_vp": _to_float_br(clean[5]),
            "valor_mercado": _to_float_br(clean[6]),
            "vacancia_pct": _to_float_br(clean[12]),
        }

    print(f"  ✅ {len(result)} FIIs lidos do Fundamentus.")
    return result


# --------------------------------------------------------------------------
# FONTE 5 (opcional): bolsai.com -> só chamada para os tickers que sobraram
# sem preço/fundamentos depois de TODAS as fontes anteriores. Cota free é
# 200 req/dia e é compartilhada com qualquer outro uso que você faça da
# bolsai, então NÃO varremos todos os tickers com ela — só o resto
# (tipicamente bem pouca coisa, dado que agora são 4 fontes antes dela).
# Só roda se o secret BOLSAI_API_KEY existir no repositório.
# --------------------------------------------------------------------------
def fetch_bolsai_fii(ticker):
    if not BOLSAI_API_KEY:
        return {}

    url = f"https://api.usebolsai.com/api/v1/fiis/{ticker}"
    req = urllib.request.Request(url, headers={**HEADERS, "X-API-Key": BOLSAI_API_KEY})

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            item = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"    ❌ [bolsai] Erro em {ticker}: {e}")
        return {}

    print(f"    🔎 [bolsai] campos brutos de {ticker}: {list(item.keys())}")

    return {
        "nome": item.get("name") or item.get("company_name") or "",
        "segmento": item.get("segment") or item.get("segmento") or "",
        "preco": float(item.get("close_price") or item.get("price") or 0.0),
        "p_vp": float(item.get("p_vp") or item.get("price_to_book") or 0.0),
        "dy_12m_pct": float(item.get("dividend_yield_ttm") or item.get("dy_12m") or 0.0),
        "vpa": float(item.get("nav_per_share") or item.get("book_value_per_share") or 0.0),
        "vacancia_pct": float(item.get("vacancy_rate") or item.get("vacancia") or 0.0),
    }


def fetch_bolsai_for_missing(pendentes):
    if not BOLSAI_API_KEY:
        print("  ⏭️  BOLSAI_API_KEY não configurado — pulando fallback bolsai.")
        return {}
    if not pendentes:
        return {}

    print(f"  🔁 Consultando bolsai para {len(pendentes)} tickers pendentes...")
    result = {}
    for t in pendentes:
        dados = fetch_bolsai_fii(t)
        if dados:
            result[t] = dados
        time.sleep(0.3)
    print(f"  ✅ {len(result)} de {len(pendentes)} resolvidos via bolsai.")
    return result


# --------------------------------------------------------------------------
# MERGE: combina as 5 fontes e deriva VPA / PL quando algo faltar.
# Ordem de prioridade (mais atual -> mais atrasado):
#   Preço:       brapi > investidor10 > statusinvest > fundamentus > bolsai
#   Fundamentos: investidor10 > statusinvest > fundamentus > bolsai
# Nunca inventa número: quando falta dado em todas as fontes, marca
# dados_completos=False.
# --------------------------------------------------------------------------
def merge_data(tickers, brapi_data, inv10_data, status_data, fundamentus_data, bolsai_data=None):
    bolsai_data = bolsai_data or {}
    merged = {}

    for symbol in tickers:
        b = brapi_data.get(symbol, {})
        i10 = inv10_data.get(symbol, {})
        si = status_data.get(symbol, {})
        f = fundamentus_data.get(symbol, {})
        k = bolsai_data.get(symbol, {})

        achou_inv10 = symbol in inv10_data
        achou_status = symbol in status_data
        achou_fundamentus = symbol in fundamentus_data
        achou_bolsai = symbol in bolsai_data

        # Preço: brapi (tempo quase real) > investidor10 > statusinvest > fundamentus > bolsai
        preco = b.get("preco") or i10.get("preco") or si.get("preco") or f.get("preco") or k.get("preco") or 0.0
        nome = b.get("nome") or i10.get("nome") or k.get("nome") or symbol

        # Fundamentos: investidor10 > statusinvest > fundamentus > bolsai
        p_vp = i10.get("p_vp") or si.get("p_vp") or f.get("p_vp") or k.get("p_vp") or 0.0

        if achou_inv10 and i10.get("dy_12m_pct") is not None:
            dy_12m = i10.get("dy_12m_pct") or 0.0
        elif achou_status and si.get("dy_12m_pct") is not None:
            dy_12m = si.get("dy_12m_pct") or 0.0
        elif achou_fundamentus:
            dy_12m = f.get("dy_12m_pct") or 0.0
        else:
            dy_12m = k.get("dy_12m_pct") or 0.0

        segmento = i10.get("segmento") or f.get("segmento") or b.get("segmento_brapi") or k.get("segmento") or "Fundo Imobiliário"
        vacancia = i10.get("vacancia_pct") or f.get("vacancia_pct") or k.get("vacancia_pct") or 0.0
        valor_mercado = i10.get("valor_mercado") or si.get("valor_mercado") or f.get("valor_mercado") or 0.0

        # VPA: usa o valor já pronto da fonte mais atual que tiver, senão
        # deriva de Preço ÷ P/VP (nunca inventa: só deriva se os dois dados
        # vierem da MESMA fonte, senão o VPA sai distorcido).
        if i10.get("vpa"):
            vpa = round(i10.get("vpa"), 2)
        elif si.get("vpa"):
            vpa = round(si.get("vpa"), 2)
        elif achou_fundamentus and p_vp > 0 and f.get("preco"):
            vpa = round(f.get("preco") / p_vp, 2)
        elif p_vp > 0 and k.get("vpa"):
            vpa = round(k.get("vpa"), 2)
        else:
            vpa = 0.0

        # valor_mercado já É o patrimônio quando vem do Investidor10/StatusInvest
        # (que reportam patrimônio do fundo, não capitalização de mercado);
        # quando só temos os dados do Fundamentus, valor_mercado ÷ p_vp
        # aproxima o patrimônio líquido, igual à lógica original.
        if valor_mercado:
            patrimonio_liquido = valor_mercado
        elif p_vp > 0 and f.get("valor_mercado"):
            patrimonio_liquido = round(f.get("valor_mercado") / p_vp, 2)
        else:
            patrimonio_liquido = 0.0

        tem_preco = preco > 0
        # "Completo" = encontramos o FII em pelo menos uma fonte de fundamentos.
        tem_fundamentos = achou_inv10 or achou_status or achou_fundamentus or achou_bolsai

        fontes = []
        if b:
            fontes.append("brapi.dev")
        if i10:
            fontes.append("investidor10.com.br")
        if si:
            fontes.append("statusinvest.com.br")
        if f:
            fontes.append("fundamentus.com.br")
        if k:
            fontes.append("bolsai")
        fonte = " + ".join(fontes) if fontes else "indisponivel"

        merged[symbol] = {
            "nome": nome,
            "segmento": segmento,
            "setor_atuacao": segmento,
            "preco": float(preco),
            "p_vp": float(p_vp),
            "valor_patrimonial_cota": vpa,
            "dy_12m": float(dy_12m or 0.0),
            # Nenhuma fonte dá DY mensal isolado; aproximação por 1/12 do DY 12m,
            # igual à convenção já usada antes no script.
            "dy_mensal": round((dy_12m or 0.0) / 12.0, 4) if dy_12m else 0.0,
            "patrimonio_liquido": float(patrimonio_liquido),
            "vacancia_fisica": float(vacancia),
            "dados_completos": bool(tem_preco and tem_fundamentos),
            "fonte_dados": fonte,
        }

    return merged


def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers únicos de {TICKERS_FILE}...")

    if not tickers:
        print("⚠️ Nenhum ticker encontrado. Encerrando.")
        return

    if not BRAPI_TOKEN:
        print("⚠️ BRAPI_TOKEN não detectado. As cotações podem vir limitadas.")
    else:
        print("✅ BRAPI_TOKEN carregado com sucesso.")

    print("\n--- Etapa 1/5: preços via brapi.dev ---")
    brapi_data = fetch_brapi_all(tickers)

    print("\n--- Etapa 2/5: fundamentos atualizados via investidor10.com.br ---")
    inv10_data = fetch_investidor10_all(tickers)

    print("\n--- Etapa 3/5: fundamentos atualizados via statusinvest.com.br ---")
    status_data = fetch_statusinvest_all(tickers)

    print("\n--- Etapa 4/5: fundamentos via fundamentus.com.br (fallback com atraso) ---")
    fundamentus_data = fetch_fundamentus_fiis()

    # Quem ficou sem preço OU sem qualquer fonte de fundamentos depois das
    # quatro primeiras fontes
    pendentes = [
        t for t in tickers
        if not (
            (brapi_data.get(t, {}).get("preco")
             or inv10_data.get(t, {}).get("preco")
             or status_data.get(t, {}).get("preco")
             or fundamentus_data.get(t, {}).get("preco"))
            and (t in inv10_data or t in status_data or t in fundamentus_data)
        )
    ]

    print(f"\n--- Etapa 5/5: fallback bolsai só para pendentes ({len(pendentes)} tickers) ---")
    bolsai_data = fetch_bolsai_for_missing(pendentes)

    fiis_data = merge_data(tickers, brapi_data, inv10_data, status_data, fundamentus_data, bolsai_data)

    completos = sum(1 for v in fiis_data.values() if v["dados_completos"])
    fontes_usadas = ["brapi.dev"]
    if ENABLE_INVESTIDOR10 and HAS_BS4:
        fontes_usadas.append("investidor10.com.br")
    if ENABLE_STATUSINVEST and HAS_CURL_CFFI:
        fontes_usadas.append("statusinvest.com.br")
    fontes_usadas.append("fundamentus.com.br")
    if BOLSAI_API_KEY:
        fontes_usadas.append("bolsai")
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "fonte": " + ".join(fontes_usadas),
        "total_fiis": len(fiis_data),
        "fiis_com_dados_completos": completos,
        "fiis": fiis_data,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 SUCESSO! {len(fiis_data)} FIIs salvos em {OUTPUT_FILE} "
          f"({completos} com fundamentos completos).")


if __name__ == "__main__":
    main()
