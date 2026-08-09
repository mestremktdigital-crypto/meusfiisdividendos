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
    """Carrega dados existentes do fiis.json para não perder FIIs caso a API falhe para algum ticker específico."""
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "fiis" in data:
                    print(f"📂 Dados existentes carregados: {len(data['fiis'])} FIIs já cadastrados em {OUTPUT_FILE}.")
                    return data["fiis"]
        except Exception as e:
            print(f"⚠️ Não foi possível ler {OUTPUT_FILE} anterior: {e}")
    return {}

def fetch_single_ticker(ticker):
    """Busca apenas 1 ticker na BRAPI (usado quando um lote falha para isolar o ticker com problema)."""
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{ticker}?fundamental=true{token_param}"
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            if results:
                item = results[0]
                symbol = item.get("symbol", "").upper()
                regular_price = item.get("regularMarketPrice") or 0.0
                price_to_book = item.get("priceToBook") or 0.0
                book_value = item.get("bookValue") or 0.0
                dividend_yield = item.get("dividendYield") or 0.0
                market_cap = item.get("marketCap") or 0.0
                
                return {
                    symbol: {
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
                }
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  ⚠️ Ticker {ticker} não encontrado/deslistado na BRAPI (HTTP 404).")
        else:
            print(f"  ❌ Erro HTTP {e.code} ao buscar {ticker}: {e}")
    except Exception as e:
        print(f"  ❌ Erro ao buscar {ticker}: {e}")
    return {}

def fetch_batch(batch_tickers):
    if not batch_tickers:
        return {}, []
    
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
            return output, []
    except Exception as e:
        print(f"  ⚠️ Lote [{tickers_str}] retornou erro ({e}). Isolando tickers individualmente...")
        # Quando um lote falha, isola os tickers para identificar qual ticker causou a falha
        single_output = {}
        failed_tickers = []
        for single_t in batch_tickers:
            res = fetch_single_ticker(single_t)
            if res:
                single_output.update(res)
            else:
                failed_tickers.append(single_t)
            time.sleep(0.1)
        return single_output, failed_tickers

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

    # Carrega os dados existentes de fiis.json para nunca apagar dados de FIIs já gravados
    fiis_data = load_existing_data()

    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    all_failed_tickers = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        
        batch_res, batch_failed = fetch_batch(batch)
        fiis_data.update(batch_res)
        all_failed_tickers.extend(batch_failed)
        print(f"  └─ {len(batch_res)} de {len(batch)} FIIs atualizados neste lote.")
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
    if all_failed_tickers:
        print(f"⚠️ Atenção: Os seguintes tickers falharam na BRAPI (deslistados/não encontrados): {', '.join(all_failed_tickers)}")

if __name__ == "__main__":
    main()
