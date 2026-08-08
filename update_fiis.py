import os
import json
import urllib.request
import urllib.error
from datetime import datetime

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")

def get_all_fii_tickers():
    """Busca a lista de todos os FIIs negociados na B3 pela API da brapi."""
    url = f"https://brapi.dev/api/quote/list?type=fii"
    if BRAPI_TOKEN:
        url += f"&token={BRAPI_TOKEN}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            stocks = data.get("stocks", [])
            tickers = [s["stock"] for s in stocks if s.get("stock")]
            print(f"Obtidos {len(tickers)} tickers de FIIs da B3.")
            return tickers
    except Exception as e:
        print(f"Erro ao listar FIIs da B3: {e}. Usando lista padrao.")
        return ["MXRF11", "HGLG11", "GGRC11", "KNRI11", "XPLG11", "VISC11", "XPML11", "CPTS11", "VGHF11", "TGAR11", "GARE11", "TRXF11"]

def fetch_fii_data(ticker):
    """Busca os dados e historico de proventos de um FII especifico."""
    url = f"https://brapi.dev/api/quote/{ticker}?fundamental=true&dividends=true"
    if BRAPI_TOKEN:
        url += f"&token={BRAPI_TOKEN}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp:
            raw = json.loads(resp.read().decode('utf-8'))
            results = raw.get("results", [])
            if not results:
                return None
            res = results[0]
            
            # Preco e indicadores
            preco = float(res.get("regularMarketPrice") or 0.0)
            nav = float(res.get("priceToBook") or 0.0) # P/VP
            
            # Proventos
            divs_raw = res.get("dividendsData", {}).get("cashDividends", [])
            proventos = []
            for d in divs_raw[:12]:
                val = float(d.get("rate") or 0.0)
                data_com = (d.get("approvedOn") or d.get("paymentDate") or "")[:10]
                data_pag = (d.get("paymentDate") or "")[:10]
                if val > 0:
                    proventos.append({
                        "valor_por_cota": val,
                        "data_com": data_com,
                        "data_pagamento": data_pag
                    })
            
            ultimo_prov = proventos[0] if proventos else None
            
            # Calculo DY 12m
            soma_12m = sum(p["valor_por_cota"] for p in proventos)
            dy_12m = (soma_12m / preco * 100) if preco > 0 else 0.0
            
            return {
                "nome": res.get("longName") or res.get("shortName") or ticker,
                "segmento": "tijolo" if "LOG" in ticker or "MALL" in ticker else "papel",
                "setor_atuacao": res.get("sector") or "Fundo Imobiliário",
                "preco": preco,
                "p_vp": nav,
                "valor_patrimonial_cota": round(preco / nav, 2) if nav > 0 else preco,
                "dy_12m": round(dy_12m, 2),
                "dy_mensal": round(ultimo_prov["valor_por_cota"] / preco * 100, 2) if ultimo_prov and preco > 0 else 0.0,
                "patrimonio_liquido": float(res.get("marketCap") or 0.0),
                "vacancia_fisica": None,
                "ultimo_provento": ultimo_prov,
                "proventos_12m": proventos,
                "dados_completos": True
            }
    except Exception as e:
        print(f"Falha ao buscar {ticker}: {e}")
        return None

def main():
    tickers = get_all_fii_tickers()
    output = {
        "gerado_em": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "brapi",
        "fiis": {}
    }
    
    for t in tickers:
        data = fetch_fii_data(t)
        if data:
            output["fiis"][t] = data
            
    with open("fiis.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print("fiis.json atualizado com sucesso!")

if __name__ == "__main__":
    main()
