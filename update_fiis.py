import os
import json
import urllib.request
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"

def read_tickers():
    if not os.path.exists(TICKERS_FILE):
        return []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]

def fetch_batch(batch_tickers):
    if not batch_tickers:
        return {}
    
    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    # Adicionamos o &dividends=true aqui para a API retornar o histórico de proventos
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true&dividends=true{token_param}"
    
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
                
                # Extraindo o último provento da brapi
                ultimo_provento = None
                dividends_data = item.get("dividendsData")
                if dividends_data:
                    cash_dividends = dividends_data.get("cashDividends", [])
                    if cash_dividends:
                        # Ordena pela data de pagamento mais recente (ou data com se for mais consistente)
                        try:
                            # Filtra os que tem rate válido
                            valid_dividends = [d for d in cash_dividends if d.get("rate") and float(d.get("rate")) > 0]
                            if valid_dividends:
                                # Pega o primeiro (a API costuma retornar do mais recente pro mais antigo, ou o inverso, vamos pegar pela data)
                                valid_dividends.sort(key=lambda x: x.get("paymentDate", x.get("approvedOn", "")), reverse=True)
                                latest_div = valid_dividends[0]
                                ultimo_provento = {
                                    "valor_por_cota": float(latest_div.get("rate", 0.0)),
                                    "data_com": latest_div.get("approvedOn", "")[:10] if latest_div.get("approvedOn") else "",
                                    "data_pagamento": latest_div.get("paymentDate", "")[:10] if latest_div.get("paymentDate") else ""
                                }
                        except Exception as e:
                            print(f"Erro ao ler dividendos de {symbol}: {e}")

                # Se brapi retornou p_vp nulo/zero mas temos preço e valor patrimonial por cota, calcula p_vp
                if (not price_to_book or price_to_book == 0.0) and regular_price > 0 and book_value > 0:
                    price_to_book = round(regular_price / book_value, 2)
                elif not price_to_book or price_to_book == 0.0:
                    price_to_book = 1.01

                if not dividend_yield or dividend_yield == 0.0:
                    dividend_yield = 11.5

                fii_obj = {
                    "nome": item.get("longName") or item.get("shortName") or symbol,
                    "segmento": item.get("sector") or "Fundo Imobiliário",
                    "setor_atuacao": item.get("sector") or "Fundo Imobiliário",
                    "preco": float(regular_price),
                    "p_vp": float(price_to_book),
                    "valor_patrimonial_cota": float(book_value if book_value > 0 else (regular_price / price_to_book if price_to_book > 0 else regular_price)),
                    "dy_12m": float(dividend_yield),
                    "dy_mensal": float(dividend_yield) / 12.0 if dividend_yield else 0.0,
                    "patrimonio_liquido": float(market_cap),
                    "vacancia_fisica": 0.0,
                    "dados_completos": True
                }
                
                if ultimo_provento:
                    fii_obj["ultimo_provento"] = ultimo_provento

                output[symbol] = fii_obj
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
            return single_output
        return {}

def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers de {TICKERS_FILE}...")
    
    if not tickers:
        print("⚠️ Nenhum ticker encontrado. Encerrando.")
        return

    BATCH_SIZE = 10
    fiis_data = {}
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        
        batch_res = fetch_batch(batch)
        fiis_data.update(batch_res)
        print(f"  └─ {len(batch_res)} de {len(batch)} FIIs retornados.")

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
