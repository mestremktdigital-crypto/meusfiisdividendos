import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

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
BATCH_SIZE = 1  # O plano atual da brapi só aceita 1 ticker por chamada (lotes de 10 dão 400)
SCRAPE_TIMEOUT = 15
SCRAPE_SLEEP = 0.4  # intervalo entre requisições pro Investidor10 (educado com o servidor)

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
# FONTE 2: Investidor10 (scraping) -> nossa fonte principal de fundamentos
# em tempo (quase) real. Cobre FIIs (/fiis/<ticker>/) E ações (/acoes/<ticker>/)
# — tenta a URL de FII primeiro (maioria dos tickers da carteira), e só cai
# pra URL de ação se a de FII não trouxer nada reconhecível (ticker não
# existe nessa categoria, ou é mesmo uma ação como PETR4/VALE3).
# Uma requisição por ticker (duas só no caso de ações, que erram a primeira
# tentativa por definição).
# --------------------------------------------------------------------------
def _parse_investidor10_html(html):
    """Extrai os campos comuns às páginas de FII e de ação do Investidor10.
    Alguns rótulos (VACÂNCIA, VAL. PATRIMONIAL P/COTA) só existem na página
    de FII e simplesmente não aparecem em ações — tudo bem, ficam de fora do
    dict sem inventar nada.
    """
    text = _get_flat_text(html)

    # Preço da cota/ação: rótulo "VALOR DA COTA" no FII; ações usam outro
    # rótulo perto do topo — tentamos os dois.
    preco_txt = (
        _extract_after(text, r'VALOR DA COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'\bCOTAÇÃO\b', r'R\$\s*([\d\.,]+)')
    )
    # DY (12M) é o rótulo confirmado na página de FII. Ações costumam expor
    # só "DY" (sem o "(12M)") — NÃO confirmei esse rótulo ao vivo (não tenho
    # acesso de rede ao investidor10.com.br neste ambiente), é best-effort;
    # se não bater, o campo simplesmente fica de fora, sem inventar número.
    dy_12m = (
        _extract_after(text, r'DY\s*\(12M\)', r'([\d,]+)\s*%')
        or _extract_after(text, r'\bDY\b', r'([\d,]+)\s*%')
    )
    p_vp = _extract_after(text, r'\bP\s*/\s*VP\b', r'([\d,]+)')
    vacancia = _extract_after(text, r'VAC[ÂA]NCIA\b', r'([\d,]+)\s*%')
    # "SEGMENTO" é o rótulo de FII; ações usam "SETOR DE ATUAÇÃO" — também
    # não confirmado ao vivo, mesma ressalva do DY acima.
    segmento = (
        _extract_after(text, r'\bSEGMENTO\b', r'([A-Za-zÀ-ú/ ]+?)(?:\s+TIPO DE FUNDO|\s+PRAZO)')
        or _extract_after(text, r'SETOR DE ATUA[ÇC][ÃA]O', r'([A-Za-zÀ-ú/, ]+?)(?:\s+SUBSETOR|\s+SEGMENTO|\s+ATIVIDADE)')
    )
    vpa_txt = (
        _extract_after(text, r'VAL\.\s*PATRIMONIAL\s*P/\s*COTA', r'R\$\s*([\d\.,]+)')
        or _extract_after(text, r'\bVPA\b', r'R\$?\s*([\d\.,]+)')
    )
    # o valor patrimonial do fundo vem como "R$ 7,57" + unidade "Bilhões"
    # separados (ex: "VALOR PATRIMONIAL R$ 7,57 Bilhões"). Pra ações, o
    # rótulo equivalente costuma ser "PATRIMÔNIO LÍQUIDO" — tentativa extra,
    # também não confirmada ao vivo.
    vp_match = re.search(
        r'(?<!P/ )VALOR PATRIMONIAL\D{0,20}?R\$\s*([\d\.,]+)\s*(Bilh\w*|Milh\w*|Mil\b)?',
        text, re.IGNORECASE
    ) or re.search(
        r'PATRIM[ÔO]NIO\s*L[ÍI]QUIDO\D{0,20}?R\$\s*([\d\.,]+)\s*(Bilh\w*|Milh\w*|Mil\b)?',
        text, re.IGNORECASE
    )
    valor_mercado = 0.0
    if vp_match:
        valor_mercado = _to_float_br_com_unidade(vp_match.group(1), vp_match.group(2))

    # nome do ativo: pega do <h1>/<h2> quando dá, sem depender do texto corrido
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
            continue  # tenta a próxima categoria (fii -> ação)

        resultado = _parse_investidor10_html(html)
        if resultado:
            return resultado
        # página respondeu mas não achou nada reconhecível: também tenta a
        # próxima categoria antes de desistir.

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
# FONTE 3 (último recurso): Fundamentus -> dados atrasados em relação ao
# Investidor10, então só entra pra preencher o que sobrar. Uma única
# requisição cobre todos os FIIs de uma vez (por isso ainda vale manter,
# mesmo com o atraso: é praticamente de graça).
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
# MERGE: combina brapi + investidor10 + fundamentus e deriva VPA / PL quando
# algo faltar.
# Ordem de prioridade (mais atual -> mais atrasado):
#   Preço:       brapi > investidor10 > fundamentus
#   Fundamentos: investidor10 > fundamentus (fundamentus é ÚLTIMO CASO)
# Nunca inventa número: quando falta dado em todas as fontes, marca
# dados_completos=False.
# --------------------------------------------------------------------------
def merge_data(tickers, brapi_data, inv10_data, fundamentus_data):
    merged = {}

    for symbol in tickers:
        b = brapi_data.get(symbol, {})
        i10 = inv10_data.get(symbol, {})
        f = fundamentus_data.get(symbol, {})

        achou_inv10 = symbol in inv10_data
        achou_fundamentus = symbol in fundamentus_data

        # Preço: brapi (tempo quase real) > investidor10 > fundamentus
        preco = b.get("preco") or i10.get("preco") or f.get("preco") or 0.0
        nome = b.get("nome") or i10.get("nome") or symbol

        # Fundamentos: investidor10 primeiro sempre; fundamentus só entra
        # quando o investidor10 não trouxe o campo específico (não é
        # "tudo ou nada" por ticker, é campo a campo, pra aproveitar ao
        # máximo o que o investidor10 já trouxe de mais atual).
        p_vp = i10.get("p_vp") or f.get("p_vp") or 0.0

        if achou_inv10 and i10.get("dy_12m_pct") is not None:
            dy_12m = i10.get("dy_12m_pct") or 0.0
        elif achou_fundamentus:
            dy_12m = f.get("dy_12m_pct") or 0.0
        else:
            dy_12m = 0.0

        segmento = i10.get("segmento") or f.get("segmento") or b.get("segmento_brapi") or ""
        vacancia = i10.get("vacancia_pct") or f.get("vacancia_pct") or 0.0
        valor_mercado = i10.get("valor_mercado") or f.get("valor_mercado") or 0.0

        # VPA: usa o valor já pronto do investidor10 quando tiver, senão
        # deriva de Preço ÷ P/VP do Fundamentus (nunca mistura fonte de
        # preço com fonte de P/VP diferentes, pra não distorcer o número).
        if i10.get("vpa"):
            vpa = round(i10.get("vpa"), 2)
        elif achou_fundamentus and p_vp > 0 and f.get("preco"):
            vpa = round(f.get("preco") / p_vp, 2)
        else:
            vpa = 0.0

        # valor_mercado já É o patrimônio quando vem do Investidor10 (que
        # reporta patrimônio do fundo, não capitalização de mercado);
        # quando só temos o dado do Fundamentus, valor_mercado ÷ p_vp
        # aproxima o patrimônio líquido.
        if valor_mercado:
            patrimonio_liquido = valor_mercado
        elif p_vp > 0 and f.get("valor_mercado"):
            patrimonio_liquido = round(f.get("valor_mercado") / p_vp, 2)
        else:
            patrimonio_liquido = 0.0

        tem_preco = preco > 0
        # "Completo" = encontramos o ticker em pelo menos uma fonte de fundamentos.
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
