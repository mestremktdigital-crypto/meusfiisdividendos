import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

BRAPI_TOKEN = os.environ.get("BRAPI_TOKEN", "").strip()
TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "fiis.json"
BATCH_SIZE = 10  # limite do endpoint legado /api/quote em planos não-Pro


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
    # fundamental=true e dividends=true não custam nada a mais no Free — se o
    # plano não liberar o dado, o campo simplesmente não vem, sem erro.
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true&dividends=true{token_param}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )

    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
            results = data.get("results", [])
            output = {}
            cutoff = datetime.utcnow() - timedelta(days=365)

            for item in results:
                symbol = item.get("symbol", "").upper()
                if not symbol:
                    continue

                preco = item.get("regularMarketPrice")

                # Campos que a doc lista na RAIZ da resposta (fora de modules
                # pagos) — testar se vêm preenchidos no seu plano real.
                dy_12m = item.get("dividendYield")
                last_div_valor = item.get("lastDividendValue")
                last_div_data = item.get("lastDividendDate")

                # Histórico via ?dividends=true, se o plano liberar
                cash_dividends = (item.get("dividendsData") or {}).get("cashDividends") or []
                proventos_12m = []
                for d in cash_dividends:
                    data_pgto = d.get("paymentDate") or d.get("approvedOn")
                    valor = d.get("rate")
                    if not data_pgto or valor is None:
                        continue
                    try:
                        dt = datetime.fromisoformat(data_pgto[:10])
                    except ValueError:
                        continue
                    if dt >= cutoff:
                        proventos_12m.append({"data": data_pgto[:10], "valor": float(valor)})
                proventos_12m.sort(key=lambda x: x["data"])

                # último provento: prioriza o histórico (mais confiável),
                # cai para lastDividendValue/lastDividendDate se só isso vier
                if proventos_12m:
                    ultimo_provento = proventos_12m[-1]
                elif last_div_valor is not None and last_div_data:
                    ultimo_provento = {"data": str(last_div_data)[:10], "valor": float(last_div_valor)}
                else:
                    ultimo_provento = None

                fii_entry = {
                    "nome": item.get("longName") or item.get("shortName") or symbol,
                    "segmento": item.get("sector") or "Fundo Imobiliário",
                    "setor_atuacao": item.get("sector") or "Fundo Imobiliário",
                    "preco": float(preco) if preco is not None else None,
                    # Estes três não existem em nenhum lugar do endpoint
                    # /api/quote para FII — só no /v2/fii/indicators (Pro).
                    # Deixo null (não 0.0) para não passar a impressão de
                    # que o fundo tem esse indicador zerado de verdade.
                    "p_vp": None,
                    "valor_patrimonial_cota": None,
                    "patrimonio_liquido": None,
                    "vacancia_fisica": None,
                    "dy_12m": float(dy_12m) * 100.0 if dy_12m is not None else None,
                    "dy_mensal": (float(dy_12m) * 100.0 / 12.0) if dy_12m is not None else None,
                    "ultimo_provento": ultimo_provento,
                    "proventos_12m": proventos_12m,
                    "dados_completos": preco is not None,
                }
                output[symbol] = fii_entry
            return output
    except Exception as e:
        print(f"  ❌ Erro no lote [{tickers_str}]: {e}")
        if len(batch_tickers) > 1:
            print("  🔄 Tentando buscar tickers do lote individualmente...")
            single_output = {}
            for single_t in batch_tickers:
                single_output.update(fetch_batch([single_t]))
                time.sleep(0.2)
            return single_output
        return {}


def main():
    tickers = read_tickers()
    print(f"📌 Lendo {len(tickers)} tickers únicos de {TICKERS_FILE}...")

    if not tickers:
        print("⚠️ Nenhum ticker encontrado. Encerrando.")
        return

    fiis_data = {}
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")

        batch_res = fetch_batch(batch)
        fiis_data.update(batch_res)

        com_preco = sum(1 for v in batch_res.values() if v["preco"] is not None)
        com_dy = sum(1 for v in batch_res.values() if v["dy_12m"] is not None)
        com_provento = sum(1 for v in batch_res.values() if v["ultimo_provento"] is not None)
        print(f"  └─ preço: {com_preco}/{len(batch_res)} · DY: {com_dy}/{len(batch_res)} · provento: {com_provento}/{len(batch_res)}")
        time.sleep(0.3)

    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "fonte": "brapi.dev (plano Free/Startup — indicadores fundamentalistas de FII exigem plano Pro)",
        "total_fiis": len(fiis_data),
        "fiis": fiis_data,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 SUCESSO! {len(fiis_data)} FIIs salvos em {OUTPUT_FILE}.")


if __name__ == "__main__":
    main()
