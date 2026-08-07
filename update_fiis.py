import json
import os
import sys
import time
from datetime import datetime, timezone
import requests

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "")
BRAPI_PLAN = os.environ.get("BRAPI_PLAN", "startup").strip().lower()
BASE_URL = "https://brapi.dev/api"

QUOTE_BATCH_SIZE = 10
TIMEOUT_SECONDS = 15
MAX_RETRIES = 3

TICKERS_FILE = "tickers.txt"

def carregar_tickers() -> list[str]:
    if not os.path.exists(TICKERS_FILE):
        return ["MXRF11", "HGLG11", "KNRI11", "XPLG11", "GARE11"]
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return [linha.strip().upper() for linha in f if linha.strip() and not linha.startswith("#")]

def _get_com_retry(url: str, params: dict, contexto: str) -> dict | None:
    for tentativa in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (401, 402, 403):
                print(f"  {contexto}: HTTP {resp.status_code} (sem acesso no plano atual)")
                return None
            print(f"  {contexto}: HTTP {resp.status_code} (tentativa {tentativa})")
        except requests.RequestException as e:
            print(f"  {contexto}: erro de rede (tentativa {tentativa}): {e}")
        time.sleep(2 * tentativa)
    return None

def buscar_lote_quote(tickers: list[str]) -> dict:
    symbols = ",".join(tickers)
    url = f"{BASE_URL}/quote/{symbols}"
    params = {"fundamental": "true", "dividends": "true", "token": BRAPI_TOKEN}
    return _get_com_retry(url, params, f"quote({symbols})") or {}

def montar_entrada_basica(resultado: dict) -> dict | None:
    try:
        preco = resultado.get("regularMarketPrice")
        if preco is None:
            return None

        dividends = resultado.get("dividendsData", {}).get("cashDividends", [])
        ultimo_provento = None
        if dividends:
            d = dividends[0]
            ultimo_provento = {
                "valor_por_cota": d.get("rate", 0),
                "data_com": d.get("lastDatePrior", "") or d.get("approvedOn", ""),
                "data_pagamento": d.get("paymentDate", ""),
            }

        return {
            "nome": resultado.get("longName") or resultado.get("shortName", ""),
            "segmento": resultado.get("sector", "Geral") or "Geral",
            "setor_atuacao": resultado.get("sector", "") or "",
            "preco": preco,
            "p_vp": resultado.get("priceToBook", 0) or 0,
            "valor_patrimonial_cota": None,
            "dy_12m": resultado.get("dividendYield", 0) or 0,
            "dy_mensal": None,
            "patrimonio_liquido": resultado.get("marketCap", 0) or 0,
            "vacancia_fisica": None,
            "ultimo_provento": ultimo_provento,
            "proventos_12m": [],
            "dados_completos": False,
        }
    except Exception as e:
        print(f"  Erro ao montar entrada básica: {e}")
        return None

def main():
    if not BRAPI_TOKEN:
        print("ERRO: variável de ambiente BRAPI_TOKEN não definida.")
        sys.exit(1)

    tickers = carregar_tickers()
    print(f"Buscando {len(tickers)} tickers na Brapi...")

    fiis = {}
    for i in range(0, len(tickers), QUOTE_BATCH_SIZE):
        lote = tickers[i:i + QUOTE_BATCH_SIZE]
        dados = buscar_lote_quote(lote)

        for resultado in dados.get("results", []):
            ticker = resultado.get("symbol", "").upper()
            entrada = montar_entrada_basica(resultado)
            if ticker and entrada:
                fiis[ticker] = entrada

        time.sleep(1)

    saida = {
        "gerado_em": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fonte": "brapi",
        "plano_brapi": BRAPI_PLAN,
        "fiis": fiis,
    }

    with open("fiis.json", "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=2)

    print(f"fiis.json gerado com sucesso com {len(fiis)} FIIs.")

if __name__ == "__main__":
    main()
