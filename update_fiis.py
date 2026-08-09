import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 10  # Processa 10 tickers por requisição


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


def load_existing_data():
    """Carrega dados existentes para não perder FIIs ou dados históricos."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "fiis" in data:
                    print(f"📂 Dados existentes carregados: {len(data['fiis'])} FIIs em {OUTPUT_FILE}.")
                    return data["fiis"]
        except Exception as e:
            print(f"⚠️ Não foi possível ler {OUTPUT_FILE} anterior: {e}")
    return {}


def extract_field(item, *keys, default=0.0):
    """
    Busca por chaves na raiz do item e dentro de sub-dicionários da BRAPI:
    defaultKeyStatistics, summaryDetail, financialData, summaryProfile.
    """
    if not isinstance(item, dict):
        return default

    sub_dicts = [
        item,
        item.get("defaultKeyStatistics") if isinstance(item.get("defaultKeyStatistics"), dict) else {},
        item.get("summaryDetail") if isinstance(item.get("summaryDetail"), dict) else {},
        item.get("financialData") if isinstance(item.get("financialData"), dict) else {},
        item.get("summaryProfile") if isinstance(item.get("summaryProfile"), dict) else {}
    ]

    for sub in sub_dicts:
        for k in keys:
            if k in sub and sub[k] is not None:
                val = sub[k]
                try:
                    return float(val)
                except (ValueError, TypeError):
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return default


def parse_fii_item(item, existing_fii=None):
    if existing_fii is None:
        existing_fii = {}

    symbol = item.get("symbol", "").upper()
    if not symbol:
        return None, None

    # Nome
    nome = (
        item.get("longName")
        or item.get("shortName")
        or existing_fii.get("nome")
        or symbol
    )

    # Segmento / Setor
    summary_profile = item.get("summaryProfile") if isinstance(item.get("summaryProfile"), dict) else {}
    segmento = (
        item.get("sector")
        or summary_profile.get("sector")
        or existing_fii.get("segmento")
        or "Fundo Imobiliário"
    )
    setor_atuacao = (
        item.get("industry")
        or summary_profile.get("industry")
        or existing_fii.get("setor_atuacao")
        or segmento
    )

    # Preço
    preco = extract_field(item, "regularMarketPrice", "price", default=0.0)
    if preco == 0.0:
        preco = existing_fii.get("preco", 0.0)

    # Valor Patrimonial por Cota (bookValue / valor_patrimonial_cota)
    valor_patrimonial_cota = extract_field(item, "bookValue", "valor_patrimonial_cota", "navPerShare", default=0.0)
    if valor_patrimonial_cota == 0.0:
        valor_patrimonial_cota = existing_fii.get("valor_patrimonial_cota", 0.0)

    # P/VP (priceToBook)
    p_vp = extract_field(item, "priceToBook", "priceToBookRatio", "p_vp", default=0.0)
    if p_vp == 0.0 and valor_patrimonial_cota > 0 and preco > 0:
        p_vp = round(preco / valor_patrimonial_cota, 2)
    if p_vp == 0.0:
        p_vp = existing_fii.get("p_vp", 0.0)

    # Dividend Yield
    dy_raw = extract_field(item, "dividendYield", "dy_12m", "dy", default=0.0)
    if 0.0 < dy_raw < 1.0:
        dy_12m = round(dy_raw * 100.0, 2)
    else:
        dy_12m = round(dy_raw, 2)

    if dy_12m == 0.0:
        dy_12m = existing_fii.get("dy_12m", 0.0)

    dy_mensal = round(dy_12m / 12.0, 2) if dy_12m > 0 else existing_fii.get("dy_mensal", 0.0)

    # Patrimônio Líquido (marketCap / patrimonio_liquido)
    patrimonio_liquido = extract_field(item, "marketCap", "patrimonio_liquido", "totalAssets", default=0.0)
    if patrimonio_liquido == 0.0:
        patrimonio_liquido = existing_fii.get("patrimonio_liquido", 0.0)

    # Vacância Física
    vacancia = extract_field(item, "vacancia_fisica", "vacancyRate", default=None)
    if vacancia is None:
        vacancia = existing_fii.get("vacancia_fisica", None)

    fii_data = {
        "nome": nome,
        "segmento": segmento,
        "setor_atuacao": setor_atuacao,
        "preco": float(preco),
        "p_vp": float(p_vp),
        "valor_patrimonial_cota": float(valor_patrimonial_cota),
        "dy_12m": float(dy_12m),
        "dy_mensal": float(dy_mensal),
        "patrimonio_liquido": float(patrimonio_liquido),
        "vacancia_fisica": vacancia,
        "dados_completos": True
    }

    # Preserva histórico de proventos se existirem no fiis.json
    if "ultimo_provento" in existing_fii:
        fii_data["ultimo_provento"] = existing_fii["ultimo_provento"]
    if "proventos_12m" in existing_fii:
        fii_data["proventos_12m"] = existing_fii["proventos_12m"]

    return symbol, fii_data


def fetch_batch(batch_tickers, existing_fiis):
    if not batch_tickers:
        return {}

    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true&modules=summaryProfile,defaultKeyStatistics,summaryDetail{token_param}"

    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            output = {}
            for item in results:
                symbol, fii_data = parse_fii_item(item, existing_fiis.get(item.get("symbol", "").upper()))
                if symbol and fii_data:
                    output[symbol] = fii_data
            return output
    except Exception as e:
        print(f"  ⚠️ Lote [{tickers_str}] apresentou erro ({e}). Tentando ticker por ticker...")
        output = {}
        for single_t in batch_tickers:
            token_p = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
            single_url = f"https://brapi.dev/api/quote/{single_t}?fundamental=true&modules=summaryProfile,defaultKeyStatistics,summaryDetail{token_p}"
            single_req = urllib.request.Request(single_url, headers={'User-Agent': 'Mozilla/5.0'})
            try:
                with urllib.request.urlopen(single_req) as resp:
                    d = json.loads(resp.read().decode('utf-8'))
                    res_list = d.get("results", [])
                    if res_list:
                        sym, f_data = parse_fii_item(res_list[0], existing_fiis.get(single_t))
                        if sym and f_data:
                            output[sym] = f_data
            except Exception as single_err:
                print(f"    ❌ Ticker {single_t} falhou: {single_err}")
            time.sleep(0.1)
        return output


def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers de {TICKERS_FILE}...")

    if not tickers:
        print("⚠️ Nenhum ticker encontrado.")
        return

    if not BRAPI_TOKEN:
        print("⚠️ BRAPI_TOKEN não detectado.")
    else:
        print("✅ BRAPI_TOKEN carregado.")

    existing_fiis = load_existing_data()
    fiis_data = dict(existing_fiis)

    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")

        batch_res = fetch_batch(batch, existing_fiis)
        fiis_data.update(batch_res)
        print(f"  └─ {len(batch_res)} de {len(batch)} FIIs atualizados.")
        time.sleep(0.2)

    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "fonte": "brapi.dev",
        "total_fiis": len(fiis_data),
        "fiis": fiis_data
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 SUCESSO! {len(fiis_data)} FIIs salvos em {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
