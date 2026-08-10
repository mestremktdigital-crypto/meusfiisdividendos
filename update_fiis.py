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
# — o Investidor10 costuma funcionar assim mesmo.
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
ENABLE_INVESTIDOR10 = os.environ.get("ENABLE_INVESTIDOR10", "true").strip().lower() != "false"

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 1  # O plano atual da brapi só aceita 1 ticker por chamada
SCRAPE_TIMEOUT = 15
SCRAPE_SLEEP = 0.4  # intervalo entre requisições pro Investidor10

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
# FONTE 1: brapi.dev -> preço em tempo (quase) real e nome do ativo
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
    """Baixa a estrutura HTML -> texto corrido com espaços simples."""
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
    else:
        text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_after(text, label_pattern, value_pattern, window=300):
    """Acha `label_pattern` no texto e procura `value_pattern` logo depois."""
    m = re.search(label_pattern, text, re.IGNORECASE)
    if not m:
        return None
    trecho = text[m.end():m.end() + window]
    v = re.search(value_pattern, trecho, re.IGNORECASE)
    return v.group(1) if v else None


def _http_get(url, use_curl_cffi=True):
    """GET genérico: tenta curl_cffi (impersona Chrome) e cai pro urllib se falhar."""
    if use_curl_cffi and HAS_CURL_CFFI:
        resp = cffi_requests.get(url, headers=HEADERS, timeout=SCRAPE_TIMEOUT, impersonate="chrome")
        resp.raise_for_status()
        return resp.text
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=SCRAPE_TIMEOUT) as response:
        raw = response.read()
    return raw.decode('utf-8', errors='ignore')


# --------------------------------------------------------------------------
# FONTE 2: Investidor10 (scraping)
# --------------------------------------------------------------------------
PROVENTO_LINHA_RE = re.compile(
    r'([A-ZÀ-Ú][A-Za-zÀ-ú\.]*(?:\s+[A-ZÀ-Ú][A-Za-zÀ-ú\.]*){0,2})\s+'
    r'(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+(\d+,\d+)'
)


def _parse_investidor10_proventos(text, max_meses=15):
    """Extrai o histórico de pagamentos e agrupa por mês da data-com."""
    grupos = OrderedDict()
    for tipo, dcom_txt, dpag_txt, valor_txt in PROVENTO_LINHA_RE.findall(text):
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
    """Extrai os campos comuns às páginas de FII e de ação do Investidor10."""
    text = _get_flat_text(html)

    # Preço da cota/ação
    preco_txt = (
        _extract_after(text, r'VALOR DA COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'Cota[çc][ãa]o\s+R\$', r'([\d\.,]+)', window=20)
        or _extract_after(text, r'\bCOTAÇÃO\b', r'R\$\s*([\d\.,]+)')
    )
    # DY 12 meses
    dy_12m = (
        _extract_after(text, r'DY\s*\(12M\)', r'([\d,]+)\s*%')
        or _extract_after(text, r'DY\s+atual\s*:', r'([\d,]+)\s*%')
    )
    p_vp = _extract_after(text, r'\bP\s*/\s*VP\b', r'([\d,]+)')
    vacancia = _extract_after(text, r'VAC[ÂA]NCIA\b', r'([\d,]+)\s*%')

    # P/L (Preço / Lucro) para Ações:
    p_l = _extract_after(text, r'\bP\s*/\s*L\b', r'([\d,]+)', window=20)

    # Segmento/setor
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

    # VPA (valor patrimonial por cota/ação)
    vpa_txt = (
        _extract_after(text, r'VAL\.\s*PATRIMONIAL\s*P/\s*COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'\bVPA\b', r'([\d,]+)', window=20)
    )

    # Patrimônio
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
        print(f"    ❌ [investidor10] {ticker} não encontrado (fii/ação): {ultimo_erro}")
    else:
        print(f"    ⚠️ [investidor10] {ticker} respondeu mas sem dados reconhecíveis.")
    return {}


def fetch_investidor10_all(tickers):
    if not ENABLE_INVESTIDOR10:
        print("  ⏭️  ENABLE_INVESTIDOR10=false — pulando Investidor10.")
        return {}
    if not HAS_BS4:
        print("  ⚠️ beautifulsoup4 não instalado — pulando Investidor10.")
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


def fetch_fundamentus_fiis():
    url = "https://www.fundamentus.com.br/fii_resultado.php"
    req = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
    except Exception as e:
        print(f"  ❌ Erro ao acessar Fundamentus: {e}")
        return {}

    html = raw.decode('iso-8859-1', errors='ignore')

    table_match = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        print("  ⚠️ Não foi possível localizar a tabela do Fundamentus.")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL)
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
    return result


def merge_data(tickers, brapi_data, inv10_data, fundamentus_data):
    merged = {}

    for symbol in tickers:
        b = brapi_data.get(symbol, {})
        i10 = inv10_data.get(symbol, {})
        f = fundamentus_data.get(symbol, {})

        achou_inv10 = symbol in inv10_data
        achou_fundamentus = symbol in fundamentus_data

        preco = b.get("preco") or i10.get("preco") or f.get("preco") or 0.0
        nome = b.get("nome") or i10.get("nome") or symbol

        p_vp = i10.get("p_vp") or f.get("p_vp") or 0.0
        p_l = i10.get("p_l") or 0.0

        if achou_inv10 and i10.get("dy_12m_pct") is not None:
            dy_12m = i10.get("dy_12m_pct") or 0.0
        elif achou_fundamentus:
            dy_12m = f.get("dy_12m_pct") or 0.0
        else:
            dy_12m = 0.0

        segmento = i10.get("segmento") or f.get("segmento") or b.get("segmento_brapi") or (
            "Fundo Imobiliário" if achou_fundamentus else ""
        )
        setor_atuacao = i10.get("setor_atuacao") or segmento
        vacancia = i10.get("vacancia_pct") or f.get("vacancia_pct") or 0.0
        valor_mercado = i10.get("valor_mercado") or f.get("valor_mercado") or 0.0

        if i10.get("vpa"):
            vpa = round(i10.get("vpa"), 2)
        elif achou_fundamentus and p_vp > 0 and f.get("preco"):
            vpa = round(f.get("preco") / p_vp, 2)
        else:
            vpa = 0.0

        if valor_mercado:
            patrimonio_liquido = valor_mercado
        elif p_vp > 0 and f.get("valor_mercado"):
            patrimonio_liquido = round(f.get("valor_mercado") / p_vp, 2)
        else:
            patrimonio_liquido = 0.0

        tem_preco = preco > 0
        tem_fundamentos = achou_inv10 or achou_fundamentus

        fontes = []
        if b:
            fontes.append("brapi.dev")
        if i10:
            fontes.append("investidor10.com.br")
        if f:
            fontes.append("fundamentus.com.br")
        fonte = " + ".join(fontes) if fontes else "indisponivel"

        merged[symbol] = {
            "nome": nome,
            "segmento": segmento,
            "setor_atuacao": setor_atuacao,
            "preco": float(preco),
            "p_vp": float(p_vp),
            "p_l": float(p_l),
            "valor_patrimonial_cota": vpa,
            "dy_12m": float(dy_12m or 0.0),
            "dy_mensal": round((dy_12m or 0.0) / 12.0, 4) if dy_12m else 0.0,
            "patrimonio_liquido": float(patrimonio_liquido),
            "vacancia_fisica": float(vacancia),
            "dados_completos": bool(tem_preco and tem_fundamentos),
            "fonte_dados": fonte,
        }

        proventos_12m = i10.get("proventos_12m")
        if proventos_12m:
            merged[symbol]["proventos_12m"] = proventos_12m
            merged[symbol]["ultimo_provento"] = i10.get("ultimo_provento")

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

    print("\n--- Etapa 1/3: preços via brapi.dev ---")
    brapi_data = fetch_brapi_all(tickers)

    print("\n--- Etapa 2/3: fundamentos em tempo real via investidor10.com.br ---")
    inv10_data = fetch_investidor10_all(tickers)

    print("\n--- Etapa 3/3: fundamentus.com.br (ÚLTIMO CASO, só preenche o que sobrar) ---")
    fundamentus_data = fetch_fundamentus_fiis()

    fiis_data = merge_data(tickers, brapi_data, inv10_data, fundamentus_data)

    completos = sum(1 for v in fiis_data.values() if v["dados_completos"])
    fontes_usadas = ["brapi.dev"]
    if ENABLE_INVESTIDOR10 and HAS_BS4:
        fontes_usadas.append("investidor10.com.br")
    fontes_usadas.append("fundamentus.com.br")
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
