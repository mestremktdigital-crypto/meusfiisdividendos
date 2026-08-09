import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
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
# FONTE 2: Fundamentus -> P/VP, Dividend Yield, Segmento e Vacância REAIS
# Uma única requisição cobre TODOS os FIIs listados na B3 (não gasta cota da brapi)
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
            "dy_12m_pct": _to_float_br(clean[4]),
            "p_vp": _to_float_br(clean[5]),
            "valor_mercado": _to_float_br(clean[6]),
            "vacancia_pct": _to_float_br(clean[12]),
        }

    print(f"  ✅ {len(result)} FIIs lidos do Fundamentus.")
    return result


# --------------------------------------------------------------------------
# MERGE: combina as duas fontes e deriva VPA / PL a partir de dados reais
# (P/VP = Preço ÷ VPA  =>  VPA = Preço ÷ P/VP; PL ≈ Valor de Mercado ÷ P/VP)
# Nunca inventa número: quando falta dado nas duas fontes, marca dados_completos=False
# --------------------------------------------------------------------------
def merge_data(tickers, brapi_data, fundamentus_data):
    merged = {}

    for symbol in tickers:
        b = brapi_data.get(symbol, {})
        f = fundamentus_data.get(symbol, {})

        preco = b.get("preco") or 0.0
        nome = b.get("nome") or symbol

        p_vp = f.get("p_vp") or 0.0
        dy_12m = f.get("dy_12m_pct") or 0.0
        segmento = f.get("segmento") or b.get("segmento_brapi") or "Fundo Imobiliário"
        vacancia = f.get("vacancia_pct") or 0.0
        valor_mercado = f.get("valor_mercado") or 0.0

        vpa = round(preco / p_vp, 2) if (p_vp > 0 and preco > 0) else 0.0
        # PL contábil aproximado a partir do valor de mercado reportado (dado real, não chute)
        patrimonio_liquido = round(valor_mercado / p_vp, 2) if (p_vp > 0 and valor_mercado > 0) else valor_mercado

        tem_preco = preco > 0
        tem_fundamentos = p_vp > 0 and dy_12m > 0

        if not b and not f:
            fonte = "indisponivel"
        elif b and f:
            fonte = "brapi.dev + fundamentus.com.br"
        elif f:
            fonte = "fundamentus.com.br"
        else:
            fonte = "brapi.dev"

        merged[symbol] = {
            "nome": nome,
            "segmento": segmento,
            "setor_atuacao": segmento,
            "preco": float(preco),
            "p_vp": float(p_vp),
            "valor_patrimonial_cota": vpa,
            "dy_12m": float(dy_12m),
            # Fundamentus não fornece DY mensal isolado; aproximação por 1/12 do DY 12m,
            # igual à convenção já usada antes no script.
            "dy_mensal": round(dy_12m / 12.0, 4) if dy_12m else 0.0,
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

    print("\n--- Etapa 1/2: preços via brapi.dev ---")
    brapi_data = fetch_brapi_all(tickers)

    print("\n--- Etapa 2/2: fundamentos reais via fundamentus.com.br ---")
    fundamentus_data = fetch_fundamentus_fiis()

    fiis_data = merge_data(tickers, brapi_data, fundamentus_data)

    completos = sum(1 for v in fiis_data.values() if v["dados_completos"])
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "fonte": "brapi.dev + fundamentus.com.br",
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
