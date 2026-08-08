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

def fetch_batch(batch_tickers):
    if not batch_tickers:
        return {}
    
    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true{token_param}"
    
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
                symbol = item.get("symbol", "").upper()
                if not symbol:
                    continue
                regular_price = item.get("regularMarketPrice") or 0.0
                price_to_book = item.get("priceToBook") or 0.0
                book_value = item.get("bookValue") or 0.0
                dividend_yield = item.get("dividendYield") or 0.0
                market_cap = item.get("marketCap") or 0.0
                
                output[symbol] = {
                    "nome": item.get("longName") or item.get("shortName") or symbol,
                    "segmento": item.get("sector") or "Fundo Imobiliário",
                    "setor_atuacao": item.get("sector") or "Fundo Imobiliário",
                    "preco": float(regular_price),
                    "p_vp": float(price_to_book),
                    "valor_patrimonial_cota": float(book_value),
                    "dy_12m": float(dividend_yield),
                    "dy_mensal": float(dividend_yield) / 12.0 if dividend_yield else 0.0,
                    "patrimonio_liquido": float(market_cap),
                    "vacancia_fisica": 0.0,
                    "dados_completos": True
                }
            return output
    except Exception as e:
        print(f"  ❌ Erro no lote [{tickers_str}]: {e}")
        # Se falhou o lote (ex: ticker inválido no grupo), tenta ticker por ticker
        if len(batch_tickers) > 1:
            print("  🔄 Tentando buscar tickers do lote individualmente...")
            single_output = {}
            for single_t in batch_tickers:
                res = fetch_batch([single_t])
                single_output.update(res)
                time.sleep(0.2)
            return single_output
        return {}

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

    fiis_data = {}
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        
        batch_res = fetch_batch(batch)
        fiis_data.update(batch_res)
        print(f"  └─ {len(batch_res)} de {len(batch)} FIIs retornados neste lote.")
        time.sleep(0.3)

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
