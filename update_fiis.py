import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
BOLSAI_API_KEY = os.environ.get("BOLSAI_API_KEY", "").strip()  # opcional
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 10  # Processa 10 tickers por requisição na brapi

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'pt-BR,pt;q=0.9'
}


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
        with urllib.request.urlopen(req, timeout=15) as response:
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
                    # Alguns tickers no plano pago já trazem isso; no free normalmente vem 0
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
# FONTE 2: StatusInvest -> P/VP, DY, patrimônio, segmento e cotação num único
# request JSON (mais fácil de parsear que HTML e costuma atualizar durante o
# pregão, diferente do Fundamentus). NÃO VALIDADO AO VIVO por mim — a sandbox
# em que rodo não tem acesso de rede a statusinvest.com.br. Na primeira
# execução real, confira no log "[statusinvest] campos brutos" se os nomes
# de campo abaixo batem; se não baterem, me manda o log que eu ajusto.
# --------------------------------------------------------------------------
def fetch_statusinvest_fiis():
    url = ("https://statusinvest.com.br/category/advancedsearchresultpaginated"
           '?search={"Sector":null,"SubSector":null,"Segment":null,"my_range":"-20;100"}'
           "&orderColumn=&isAsc=&page=0&take=1000&CategoryType=2")
    req = urllib.request.Request(url, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"})

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"  ❌ Erro ao acessar StatusInvest: {e}")
        return {}

    items = data.get("list") if isinstance(data, dict) else data
    if not items:
        print("  ⚠️ Resposta do StatusInvest sem lista de FIIs (layout/endpoint pode ter mudado).")
        return {}

    print(f"  🔎 [statusinvest] campos brutos do 1º item: {list(items[0].keys())}")

    result = {}
    for item in items:
        papel = (item.get("ticker") or item.get("code") or "").upper()
        if not papel:
            continue
        result[papel] = {
            "segmento": item.get("segment") or item.get("sectorName") or "",
            "preco": float(item.get("price") or item.get("value") or 0.0),
            "dy_12m_pct": float(item.get("dy") or 0.0),
            "p_vp": float(item.get("pvp") or 0.0),
            "valor_mercado": float(item.get("netWorth") or item.get("marketCap") or 0.0),
            "vacancia_pct": float(item.get("vacancy") or 0.0),
        }

    print(f"  ✅ {len(result)} FIIs lidos do StatusInvest.")
    return result


# --------------------------------------------------------------------------
# FONTE 3: Fundamentus -> segunda camada de fundamentos reais (cobre o que o
# StatusInvest não trouxer). Uma única requisição cobre todos os FIIs.
# --------------------------------------------------------------------------
def _to_float_br(txt):
    """Converte '1.234,56' ou '8,50%' (formato BR) para float."""
    if not txt:
        return 0.0
    txt = txt.replace('.', '').replace(',', '.').replace('%', '').strip()
    try:
        return float(txt)
    except ValueError:
        return 0.0


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

    body_match = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
    if not body_match:
        print("  ⚠️ Não foi possível localizar a tabela do Fundamentus (layout pode ter mudado).")
        return {}

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', body_match.group(1), re.DOTALL)
    result = {}

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 13:
            continue
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        papel = clean[0].upper()
        if not papel:
            continue

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
# FONTE 4 (opcional): bolsai.com -> só chamada para os tickers que sobraram
# sem preço/fundamentos depois de StatusInvest + Fundamentus. Cota free é 200
# req/dia e é compartilhada com qualquer outro uso que você faça da bolsai,
# então NÃO varremos os 127 tickers com ela — só o resto (tipicamente < 20).
# Só roda se o secret BOLSAI_API_KEY existir no repositório.
# ATENÇÃO: os nomes de campo abaixo são uma melhor tentativa a partir da doc
# pública da bolsai — rode um teste manual (workflow_dispatch) e confira o
# log "[bolsai] campos brutos" na primeira execução pra validar/ajustar.
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
# MERGE: combina as fontes e deriva VPA / PL a partir de dados reais
# (P/VP = Preço ÷ VPA  =>  VPA = Preço ÷ P/VP; PL ≈ Valor de Mercado ÷ P/VP)
# Nunca inventa número: quando falta dado nas duas fontes, marca dados_completos=False
# --------------------------------------------------------------------------
def merge_data(tickers, brapi_data, statusinvest_data, fundamentus_data, bolsai_data=None):
    statusinvest_data = statusinvest_data or {}
    bolsai_data = bolsai_data or {}
    merged = {}

    for symbol in tickers:
        b = brapi_data.get(symbol, {})
        s = statusinvest_data.get(symbol, {})
        f = fundamentus_data.get(symbol, {})
        k = bolsai_data.get(symbol, {})

        achou_statusinvest = symbol in statusinvest_data
        achou_fundamentus = symbol in fundamentus_data
        achou_bolsai = symbol in bolsai_data

        # Preço: brapi (tempo quase real) > StatusInvest > Fundamentus > bolsai
        preco = b.get("preco") or s.get("preco") or f.get("preco") or k.get("preco") or 0.0
        nome = b.get("nome") or k.get("nome") or symbol

        # Fundamentos: prioriza StatusInvest (mais fresco), cai pro Fundamentus, depois bolsai
        p_vp = s.get("p_vp") or f.get("p_vp") or k.get("p_vp") or 0.0
        if achou_statusinvest and s.get("dy_12m_pct"):
            dy_12m = s.get("dy_12m_pct")
        elif achou_fundamentus:
            dy_12m = f.get("dy_12m_pct") or 0.0
        else:
            dy_12m = k.get("dy_12m_pct") or 0.0
        segmento = s.get("segmento") or f.get("segmento") or b.get("segmento_brapi") or k.get("segmento") or "Fundo Imobiliário"
        vacancia = s.get("vacancia_pct") or f.get("vacancia_pct") or k.get("vacancia_pct") or 0.0
        valor_mercado = s.get("valor_mercado") or f.get("valor_mercado") or 0.0

        # VPA e PL derivados de dados reais (nunca chutados):
        # P/VP = Preço ÷ VPA  =>  VPA = Preço ÷ P/VP
        # IMPORTANTE: usar o preço da MESMA fonte que calculou esse P/VP,
        # nunca um preço de outra fonte — senão o VPA sai distorcido
        # (VPA é o patrimônio por cota, não muda com a cotação do dia).
        if achou_statusinvest and p_vp > 0 and s.get("preco"):
            vpa = round(s.get("preco") / p_vp, 2)
        elif achou_fundamentus and p_vp > 0 and f.get("preco"):
            vpa = round(f.get("preco") / p_vp, 2)
        elif p_vp > 0 and k.get("vpa"):
            vpa = round(k.get("vpa"), 2)  # bolsai já manda VPA pronto, se disponível
        else:
            vpa = 0.0

        patrimonio_liquido = round(valor_mercado / p_vp, 2) if (p_vp > 0 and valor_mercado > 0) else valor_mercado

        tem_preco = preco > 0
        # "Completo" = encontramos o FII em pelo menos uma fonte de fundamentos.
        # Um valor 0 real (ex: fundo que não distribuiu no período) não é "faltando".
        tem_fundamentos = achou_statusinvest or achou_fundamentus or achou_bolsai

        fontes = []
        if b:
            fontes.append("brapi.dev")
        if s:
            fontes.append("statusinvest")
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

    print("\n--- Etapa 1/4: preços via brapi.dev ---")
    brapi_data = fetch_brapi_all(tickers)

    print("\n--- Etapa 2/4: fundamentos via statusinvest.com.br ---")
    statusinvest_data = fetch_statusinvest_fiis()

    print("\n--- Etapa 3/4: fundamentos via fundamentus.com.br (segunda camada) ---")
    fundamentus_data = fetch_fundamentus_fiis()

    # Quem ficou sem preço OU sem fundamentos depois das três primeiras fontes
    pendentes = [
        t for t in tickers
        if not (
            (brapi_data.get(t, {}).get("preco")
             or statusinvest_data.get(t, {}).get("preco")
             or fundamentus_data.get(t, {}).get("preco"))
            and (t in statusinvest_data or t in fundamentus_data)
        )
    ]

    print(f"\n--- Etapa 4/4: fallback bolsai só para pendentes ({len(pendentes)} tickers) ---")
    bolsai_data = fetch_bolsai_for_missing(pendentes)

    fiis_data = merge_data(tickers, brapi_data, statusinvest_data, fundamentus_data, bolsai_data)

    completos = sum(1 for v in fiis_data.values() if v["dados_completos"])
    fontes_usadas = ["brapi.dev", "statusinvest.com.br", "fundamentus.com.br"]
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
