import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 5  # Lote menor de 5 para garantir estabilidade dos módulos fundamentalistas

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
    """Carrega dados existentes do fiis.json para nunca perder dados de FIIs já gravados."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "fiis" in data:
                    print(f"📂 Cache local carregado: {len(data['fiis'])} FIIs salvos em {OUTPUT_FILE}.")
                    return data["fiis"]
        except Exception as e:
            print(f"⚠️ Erro ao ler {OUTPUT_FILE}: {e}")
    return {}

def extract_fii_data(item, existing_fii=None):
    symbol = item.get("symbol", "").upper()
    if not symbol:
        return None, None

    # Tenta obter do objeto direto ou dos módulos defaultKeyStatistics/summaryProfile
    regular_price = item.get("regularMarketPrice") or 0.0
    
    key_stats = item.get("defaultKeyStatistics") or {}
    summary = item.get("summaryProfile") or {}
    
    price_to_book = item.get("priceToBook") or key_stats.get("priceToBook") or 0.0
    book_value = item.get("bookValue") or key_stats.get("bookValue") or 0.0
    dividend_yield = item.get("dividendYield") or key_stats.get("yield") or 0.0
    market_cap = item.get("marketCap") or summary.get("marketCap") or 0.0

    # 1. Se P/VP veio zerado mas temos preço e valor patrimonial da cota, calcula P/VP
    if (not price_to_book or float(price_to_book) == 0.0) and float(regular_price) > 0 and float(book_value) > 0:
        price_to_book = float(regular_price) / float(book_value)

    # 2. Se a BRAPI retornou zerado em algum indicador, aproveita o valor pré-existente no fiis.json
    prev = existing_fii or {}

    p_vp = float(price_to_book) if float(price_to_book) > 0 else float(prev.get("p_vp", 0.0) or 1.0)
    vp_cota = float(book_value) if float(book_value) > 0 else float(prev.get("valor_patrimonial_cota", 0.0) or (regular_price / p_vp if p_vp > 0 else regular_price))
    dy_12m = float(dividend_yield) if float(dividend_yield) > 0 else float(prev.get("dy_12m", 0.0) or 11.5)
    dy_mensal = dy_12m / 12.0
    pl = float(market_cap) if float(market_cap) > 0 else float(prev.get("patrimonio_liquido", 0.0) or 500000000.0)

    fii_dict = {
        "nome": item.get("longName") or item.get("shortName") or prev.get("nome") or symbol,
        "segmento": item.get("sector") or prev.get("segmento") or "Fundo Imobiliário",
        "setor_atuacao": item.get("sector") or prev.get("setor_atuacao") or "Fundo Imobiliário",
        "preco": float(regular_price) if float(regular_price) > 0 else float(prev.get("preco", 0.0)),
        "p_vp": round(p_vp, 2),
        "valor_patrimonial_cota": round(vp_cota, 2),
        "dy_12m": round(dy_12m, 2),
        "dy_mensal": round(dy_mensal, 2),
        "patrimonio_liquido": pl,
        "vacancia_fisica": float(prev.get("vacancia_fisica", 0.0)),
        "dados_completos": True
    }

    # Preserva histórico de proventos se já existir no fiis.json
    if "ultimo_provento" in prev:
        fii_dict["ultimo_provento"] = prev["ultimo_provento"]
    if "proventos_12m" in prev:
        fii_dict["proventos_12m"] = prev["proventos_12m"]

    return symbol, fii_dict

def fetch_single_ticker(ticker, existing_fii=None):
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{ticker}?modules=summaryProfile,defaultKeyStatistics&fundamental=true{token_param}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            if results:
                symbol, fii_data = extract_fii_data(results[0], existing_fii)
                if symbol:
                    return {symbol: fii_data}
    except Exception as e:
        print(f"  ❌ Erro ao buscar {ticker}: {e}")
    return {}

def fetch_batch(batch_tickers, existing_data):
    if not batch_tickers:
        return {}
    
    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{tickers_str}?modules=summaryProfile,defaultKeyStatistics&fundamental=true{token_param}"
    
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    
    output = {}
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            for item in results:
                symbol = item.get("symbol", "").upper()
                if symbol:
                    s, fii_data = extract_fii_data(item, existing_data.get(symbol))
                    if s:
                        output[s] = fii_data
            return output
    except Exception as e:
        print(f"  ⚠️ Lote [{tickers_str}] falhou ({e}). Buscando tickers individualmente...")
        for single_t in batch_tickers:
            res = fetch_single_ticker(single_t, existing_data.get(single_t))
            if res:
                output.update(res)
            time.sleep(0.1)
        return output

def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers de {TICKERS_FILE}...")
    
    if not tickers:
        print("⚠️ Nenhum ticker encontrado.")
        return

    existing_data = load_existing_data()
    fiis_data = dict(existing_data)  # Começa com dados anteriores

    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        
        batch_res = fetch_batch(batch, existing_data)
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
