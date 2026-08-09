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
    # Inclui fundamental=true e dividends=true para trazer dados fundamentalistas e histórico de proventos
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
                
                regular_price = float(item.get("regularMarketPrice") or 0.0)
                price_to_book = float(item.get("priceToBook") or 0.0)
                book_value = float(item.get("bookValue") or 0.0)
                market_cap = float(item.get("marketCap") or 0.0)
                
                # Extrai histórico de proventos (disponível via ?dividends=true)
                divs_data = item.get("dividendsData", {})
                cash_divs = divs_data.get("cashDividends", []) if isinstance(divs_data, dict) else []
                
                parsed_divs = []
                for d in cash_divs:
                    if not isinstance(d, dict):
                        continue
                    rate = float(d.get("rate") or 0.0)
                    if rate <= 0:
                        continue
                    d_com_raw = d.get("lastDatePrior") or d.get("approvedOn") or ""
                    d_pag_raw = d.get("paymentDate") or ""
                    d_com = str(d_com_raw)[:10] if len(str(d_com_raw)) >= 10 else ""
                    d_pag = str(d_pag_raw)[:10] if len(str(d_pag_raw)) >= 10 else ""
                    parsed_divs.append({
                        "valor_por_cota": round(rate, 4),
                        "data_com": d_com,
                        "data_pagamento": d_pag
                    })
                
                # Ordena proventos da data mais recente para a mais antiga
                parsed_divs.sort(key=lambda x: x["data_com"] or x["data_pagamento"], reverse=True)
                
                # Seleciona histórico de proventos dos últimos meses (até 18 registros)
                proventos_12m = [
                    {
                        "valor_por_cota": p["valor_por_cota"],
                        "data_com": p["data_com"],
                        "data_pagamento": p["data_pagamento"]
                    }
                    for p in parsed_divs[:18]
                ]
                
                # Obtém o último provento anunciado/pago
                ultimo_provento = proventos_12m[0] if proventos_12m else {
                    "valor_por_cota": 0.0,
                    "data_com": "",
                    "data_pagamento": ""
                }
                
                # Tratamento do Dividend Yield 12M
                dividend_yield_raw = item.get("dividendYield")
                if dividend_yield_raw and float(dividend_yield_raw) > 0:
                    dy_val = float(dividend_yield_raw)
                    dy_12m = dy_val * 100.0 if dy_val <= 1.0 else dy_val
                elif proventos_12m and regular_price > 0:
                    soma_12m = sum(p["valor_por_cota"] for p in proventos_12m[:12])
                    dy_12m = (soma_12m / regular_price) * 100.0
                else:
                    dy_12m = 0.0
                
                # Tratamento do Dividend Yield Mensal
                if ultimo_provento["valor_por_cota"] > 0 and regular_price > 0:
                    dy_mensal = (ultimo_provento["valor_por_cota"] / regular_price) * 100.0
                elif dy_12m > 0:
                    dy_mensal = dy_12m / 12.0
                else:
                    dy_mensal = 0.0
                
                output[symbol] = {
                    "nome": item.get("longName") or item.get("shortName") or symbol,
                    "segmento": item.get("sector") or "Fundo Imobiliário",
                    "setor_atuacao": item.get("sector") or "Fundo Imobiliário",
                    "preco": round(regular_price, 2),
                    "p_vp": round(price_to_book, 2),
                    "valor_patrimonial_cota": round(book_value, 2),
                    "dy_12m": round(dy_12m, 2),
                    "dy_mensal": round(dy_mensal, 2),
                    "patrimonio_liquido": float(market_cap),
                    "vacancia_fisica": 0.0,
                    "ultimo_provento": ultimo_provento,
                    "proventos_12m": proventos_12m,
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
