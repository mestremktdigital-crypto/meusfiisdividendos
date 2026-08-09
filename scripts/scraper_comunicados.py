import os
import re
import json
import time
import urllib.request
import concurrent.futures
from datetime import datetime

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "comunicados.json"

def read_tickers():
    if not os.path.exists(TICKERS_FILE):
        return []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]

def fetch_comunicados_for_ticker(ticker):
    # Pequeno delay preventivo por thread para não sobrecarregar o site
    time.sleep(0.3)
    url = f"https://www.fundsexplorer.com.br/funds/{ticker.lower()}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }
    )
    
    comunicados = []
    seen_urls = set()

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8')
            
            if 'id="comunicados"' in html:
                section = html.split('id="comunicados"')[1][:30000]
                
                # 1. Documentos PDF (Fatos Relevantes, Relatórios, Informes, etc)
                regex_doc = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')
                matches_doc = regex_doc.findall(section)
                
                count = 0
                for match in matches_doc:
                    if count >= 8:
                        break
                    url_original = match[0].replace("&amp;", "&")
                    
                    # Evita links duplicados para o mesmo documento PDF
                    if url_original in seen_urls:
                        continue
                    seen_urls.add(url_original)
                    
                    titulo = match[1].strip()
                    data = match[2].strip()
                    
                    id_doc = ""
                    if "id=" in url_original:
                        id_doc = url_original.split("id=")[1].split("&")[0]
                    if not id_doc:
                        id_doc = str(hash(url_original))[1:10]
                        
                    tipo = titulo.split(",")[0].strip() if "," in titulo else "Comunicado"
                    
                    comunicados.append({
                        "id": f"c_{ticker}_{id_doc}",
                        "ticker": ticker,
                        "tipo": tipo,
                        "titulo": titulo,
                        "data": data,
                        "urlOriginal": url_original,
                        "resumoIa": None
                    })
                    count += 1
                
                # 2. Rendimentos (Aviso aos Cotistas)
                # Mantém APENAS O 1 MAIS RECENTE por ticker para evitar repetir o mesmo link da página em datas passadas
                regex_rend = re.compile(r'<div class="communicated__grid__row communicated__grid__rend">.*?Rendimento no valor de (.*?) por cota no dia (.*?)</p>.*?<li><b>(.*?)</b> Data base', re.DOTALL)
                matches_rend = regex_rend.findall(section)
                
                if matches_rend:
                    match = matches_rend[0] # Primeiro item = mais recente
                    valor = match[0].strip()
                    data_pagamento = match[1].strip()
                    data_base = match[2].strip()
                    
                    titulo = f"Rendimento: {valor} (Pag: {data_pagamento})"
                    
                    comunicados.append({
                        "id": f"c_{ticker}_rend_latest",
                        "ticker": ticker,
                        "tipo": "Aviso aos Cotistas",
                        "titulo": titulo,
                        "data": data_base,
                        "urlOriginal": url, 
                        "resumoIa": None
                    })

    except urllib.error.HTTPError as e:
        if e.code != 500:
            print(f"[{ticker}] Erro HTTP {e.code}: {e.reason}")
    except Exception as e:
        pass
        
    return ticker, comunicados

def main():
    tickers = read_tickers()
    if not tickers:
        print("Nenhum ticker encontrado em tickers.txt")
        return
        
    print(f"Lendo {len(tickers)} tickers para buscar comunicados...")
    all_comunicados = []
    
    # Processamento paralelo otimizado (3 workers com 0.3s de delay por thread)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fetch_comunicados_for_ticker, ticker): ticker for ticker in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                t, comunicados = future.result()
                all_comunicados.extend(comunicados)
            except Exception as e:
                print(f"Erro ao processar {ticker}: {e}")
        
    # Deduplicação final rigorosa por Ticker + URL Original
    dedup_map = {}
    for c in all_comunicados:
        key = (c["ticker"], c["urlOriginal"])
        if key not in dedup_map:
            dedup_map[key] = c

    final_comunicados = list(dedup_map.values())

    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "comunicados": final_comunicados
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"Salvo {len(final_comunicados)} comunicados no total em {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
