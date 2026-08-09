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
    """Carrega dados existentes do fiis.json para não perder histórico/campos prévios."""
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

def extract_number(val):
    """Extrai número de floats, ints, strings ou objetos no formato BRAPI {'raw': 1.23, 'fmt': '1.23'}."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        if "raw" in val and val["raw"] is not None:
            return float(val["raw"])
        if "fmt" in val and val["fmt"] is not None:
            try:
                return float(str(val["fmt"]).replace(",", ".").replace("%", ""))
            except ValueError:
                pass
    if isinstance(val, str):
        try:
            return float(val.replace(",", ".").replace("%", "").strip())
        except ValueError:
            return 0.0
    return 0.0

def find_field(item, field_names):
    """
    Busca um campo no item raiz ou em submódulos da BRAPI
    (defaultKeyStatistics, summaryDetail, financialData, summaryProfile).
    """
    if not isinstance(item, dict):
        return 0.0
        
    for field in field_names:
        # Busca no nível raiz
        if field in item and item[field] is not None:
            v = extract_number(item[field])
            if v != 0.0:
                return v
        
        # Busca dentro de submódulos
        for sub in ["defaultKeyStatistics", "summaryDetail", "financialData", "summaryProfile"]:
            sub_dict = item.get(sub)
            if isinstance(sub_dict, dict) and field in sub_dict and sub_dict[field] is not None:
                v = extract_number(sub_dict[field])
                if v != 0.0:
                    return v

    return 0.0

def parse_item(item, existing_fii=None):
    """
    Extrai e mescla dados do item da BRAPI com dados existentes para evitar campos zerados.
    """
    if existing_fii is None:
        existing_fii = {}

    symbol = item.get("symbol", "").upper()
    if not symbol:
        return None

    # Preço
    raw_price = find_field(item, ["regularMarketPrice", "price"])
    price = raw_price if raw_price > 0 else extract_number(existing_fii.get("preco"))

    # P/VP
    raw_pvp = find_field(item, ["priceToBook", "p_vp", "pToBook"])
    pvp = raw_pvp if raw_pvp > 0 else extract_number(existing_fii.get("p_vp"))

    # VPA (Valor Patrimonial por Cota)
    raw_vpa = find_field(item, ["bookValue", "valor_patrimonial_cota", "vpa"])
    vpa = raw_vpa if raw_vpa > 0 else extract_number(existing_fii.get("valor_patrimonial_cota"))

    # Cálculo automático caso P/VP ou VPA faltem na API
    if pvp == 0.0 and price > 0 and vpa > 0:
        pvp = round(price / vpa, 2)
    elif vpa == 0.0 and price > 0 and pvp > 0:
        vpa = round(price / pvp, 2)

    # Dividend Yield 12m
    raw_dy = find_field(item, ["dividendYield", "dy_12m", "dy12m"])
    dy_12m = raw_dy if raw_dy > 0 else extract_number(existing_fii.get("dy_12m"))
    # Ajusta porcentagem se a API retornar Ex: 11.5 para 11.5% em vez de 0.115
    if dy_12m > 1.0 and dy_12m <= 100.0:
        dy_12m = dy_12m / 100.0

    # Patrimônio Líquido / Market Cap
    raw_pl = find_field(item, ["marketCap", "patrimonio_liquido", "netWorth", "totalAssets"])
    patrimonio = raw_pl if raw_pl > 0 else extract_number(existing_fii.get("patrimonio_liquido"))

    # Nome e Setor
    nome = item.get("longName") or item.get("shortName") or existing_fii.get("nome") or symbol
    sector = item.get("sector") or existing_fii.get("segmento") or "Fundo Imobiliário"

    # Preserva o histórico de proventos e vacância existentes
    ultimo_provento = existing_fii.get("ultimo_provento")
    proventos_12m = existing_fii.get("proventos_12m", [])
    vacancia = extract_number(existing_fii.get("vacancia_fisica"))

    return {
        "nome": nome,
        "segmento": sector,
        "setor_atuacao": sector,
        "preco": float(round(price, 2)),
        "p_vp": float(round(pvp, 2)),
        "valor_patrimonial_cota": float(round(vpa, 2)),
        "dy_12m": float(round(dy_12m, 4)),
        "dy_mensal": float(round(dy_12m / 12.0, 4)),
        "patrimonio_liquido": float(patrimonio),
        "vacancia_fisica": float(vacancia),
        "ultimo_provento": ultimo_provento,
        "proventos_12m": proventos_12m,
        "dados_completos": True
    }

def fetch_single_ticker(ticker, existing_fii=None):
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    modules_param = "modules=summaryProfile,defaultKeyStatistics,financialData,summaryDetail"
    url = f"https://brapi.dev/api/quote/{ticker}?fundamental=true&{modules_param}{token_param}"
    
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            results = data.get("results", [])
            if results:
                parsed = parse_item(results[0], existing_fii)
                if parsed:
                    return {ticker: parsed}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"  ⚠️ Ticker {ticker} não encontrado/deslistado na BRAPI (HTTP 404).")
        else:
            print(f"  ❌ Erro HTTP {e.code} ao buscar {ticker}: {e}")
    except Exception as e:
        print(f"  ❌ Erro ao buscar {ticker}: {e}")
    return {}

def fetch_batch(batch_tickers, existing_fiis):
    if not batch_tickers:
        return {}, []
    
    tickers_str = ",".join(batch_tickers)
    token_param = f"&token={BRAPI_TOKEN}" if BRAPI_TOKEN else ""
    modules_param = "modules=summaryProfile,defaultKeyStatistics,financialData,summaryDetail"
    url = f"https://brapi.dev/api/quote/{tickers_str}?fundamental=true&{modules_param}{token_param}"
    
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
                parsed = parse_item(item, existing_fiis.get(symbol))
                if parsed:
                    output[symbol] = parsed
            return output, []
    except Exception as e:
        print(f"  ⚠️ Lote [{tickers_str}] retornou erro ({e}). Isolando tickers individualmente...")
        single_output = {}
        failed_tickers = []
        for single_t in batch_tickers:
            res = fetch_single_ticker(single_t, existing_fiis.get(single_t))
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

    fiis_data = load_existing_data()
    total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    all_failed_tickers = []

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"🚀 Processando lote {batch_num}/{total_batches} ({len(batch)} tickers)...")
        
        batch_res, batch_failed = fetch_batch(batch, fiis_data)
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
