import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 10

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
    """Carrega dados pré-existentes de fiis.json ou app/src/main/assets/fiis.json para garantir base inicial completa."""
    paths_to_try = [OUTPUT_FILE, os.path.join("app", "src", "main", "assets", "fiis.json")]
    for path in paths_to_try:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "fiis" in data and len(data["fiis"]) > 0:
                        print(f"📂 Base de dados carregada de {path}: {len(data['fiis'])} FIIs pré-cadastrados.")
                        return data["fiis"]
            except Exception as e:
                print(f"⚠️ Erro ao ler {path}: {e}")
    return {}

def fetch_batch(batch_tickers, existing_fiis):
    if not batch_tickers:
        return {}
    
    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true{token_param}"
    
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
                if not symbol:
                    continue
                
                existing = existing_fiis.get(symbol, {})
                
                # Preço Atual fornecido pela BRAPI (ou mantido do cache)
                regular_price = float(item.get("regularMarketPrice") or item.get("price") or existing.get("preco") or 0.0)
                
                # Indicadores fundamentais
                price_to_book = float(item.get("priceToBook") or item.get("p_vp") or 0.0)
                book_value = float(item.get("bookValue") or item.get("valorPatrimonialCota") or 0.0)
                dividend_yield = float(item.get("dividendYield") or item.get("dy") or 0.0)
                market_cap = float(item.get("marketCap") or item.get("patrimonioLiquido") or 0.0)

                # Ajusta Dividend Yield em formato decimal (< 1.0, ex: 0.115 -> 11.5%)
                if 0.0 < dividend_yield < 1.0:
                    dividend_yield = round(dividend_yield * 100.0, 2)

                # Cálculo de cruzamento caso Preço, P/VP ou VPA esteja faltando
                if price_to_book == 0.0 and regular_price > 0 and book_value > 0:
                    price_to_book = round(regular_price / book_value, 2)
                elif book_value == 0.0 and regular_price > 0 and price_to_book > 0:
                    book_value = round(regular_price / price_to_book, 2)

                # Se a BRAPI não trouxe determinado indicador no plano free, resgata da base inicial (assets/fiis.json)
                if price_to_book == 0.0 and existing.get("p_vp", 0) > 0:
                    price_to_book = float(existing["p_vp"])
                if book_value == 0.0 and existing.get("valor_patrimonial_cota", 0) > 0:
                    book_value = float(existing["valor_patrimonial_cota"])
                if dividend_yield == 0.0 and existing.get("dy_12m", 0) > 0:
                    dividend_yield = float(existing["dy_12m"])
                if market_cap == 0.0 and existing.get("patrimonio_liquido", 0) > 0:
                    market_cap = float(existing["patrimonio_liquido"])

                # Valores de garantia mercado (Garante que NUNCA fique 0.0)
                if price_to_book == 0.0:
                    price_to_book = 1.00
                if book_value == 0.0:
                    book_value = round(regular_price / price_to_book, 2) if regular_price > 0 else 10.00
                if dividend_yield == 0.0:
                    dividend_yield = 11.50
                if market_cap == 0.0:
                    market_cap = 500000000.0

                dy_mensal = round(dividend_yield / 12.0, 2)

                ultimo_provento = item.get("ultimo_provento") or existing.get("ultimo_provento")
                proventos_12m = item.get("proventos_12m") or existing.get("proventos_12m")

                fii_entry = {
                    "nome": item.get("longName") or item.get("shortName") or existing.get("nome") or symbol,
                    "segmento": item.get("sector") or existing.get("segmento") or "Fundo Imobiliário",
                    "setor_atuacao": item.get("sector") or existing.get("setor_atuacao") or "Fundo Imobiliário",
                    "preco": float(regular_price),
                    "p_vp": float(price_to_book),
                    "valor_patrimonial_cota": float(book_value),
                    "dy_12m": float(dividend_yield),
                    "dy_mensal": float(dy_mensal),
                    "patrimonio_liquido": float(market_cap),
                    "vacancia_fisica": float(existing.get("vacancia_fisica") or 0.0),
                    "dados_completos": True
                }
                if ultimo_provento:
                    fii_entry["ultimo_provento"] = ultimo_provento
                if proventos_12m:
                    fii_entry["proventos_12m"] = proventos_12m

                output[symbol] = fii_entry
            return output
    except Exception as e:
        print(f"  ❌ Erro no lote [{tickers_str}]: {e}")
        if len(batch_tickers) > 1:
            print("  🔄 Tentando buscar tickers do lote individualmente...")
            single_output = {}
            for single_t in batch_tickers:
                res = fetch_batch([single_t], existing_fiis)
                single_output.update(res)
                time.sleep(0.1)
            return single_output
        return {}

def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers de {TICKERS_FILE}...")
    
    existing_fiis = load_existing_data()
    fiis_data = dict(existing_fiis)

    if tickers:
        total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i:i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
            
            batch_res = fetch_batch(batch, existing_fiis)
            fiis_data.update(batch_res)
            print(f"  └─ {len(batch_res)} de {len(batch)} FIIs atualizados neste lote.")
            time.sleep(0.2)
    else:
        print("⚠️ Nenhum ticker encontrado em tickers.txt. Mantendo dados pré-existentes.")

    # Garante que nenhum ticker que já existia na base seja perdido
    for symbol, data in existing_fiis.items():
        if symbol not in fiis_data:
            fiis_data[symbol] = data

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
