import os
import re
import json
import time
import urllib.request
from datetime import datetime

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "comunicados.json"

def read_tickers():
    if not os.path.exists(TICKERS_FILE):
        return []
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return [line.strip().upper() for line in f if line.strip()]

def fetch_comunicados_for_ticker(ticker):
    url = f"https://www.fundsexplorer.com.br/funds/{ticker.lower()}"
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        }
    )
    
    comunicados = []
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8')
            
            if 'id="comunicados"' in html:
                section = html.split('id="comunicados"')[1][:30000]
                
                # Documentos PDF (Fatos Relevantes, etc)
                regex_doc = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')
                matches_doc = regex_doc.findall(section)
                
                # Rendimentos (Rendimentos mostrados na aba)
                regex_rend = re.compile(r'<div class="communicated__grid__row communicated__grid__rend">.*?Rendimento no valor de (.*?) por cota no dia (.*?)</p>.*?<li><b>(.*?)</b> Data base', re.DOTALL)
                matches_rend = regex_rend.findall(section)
                
                count = 0
                for match in matches_doc:
                    if count >= 10:
                        break
                    url_original = match[0].replace("&amp;", "&")
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
                
                # Adiciona Rendimentos à lista também (até 5 itens recentes)
                for match in matches_rend:
                    if count >= 15:
                        break
                    valor = match[0].strip()
                    data_pagamento = match[1].strip()
                    data_base = match[2].strip()
                    
                    titulo = f"Rendimento: {valor} (Pag: {data_pagamento})"
                    
                    comunicados.append({
                        "id": f"c_{ticker}_rend_{str(hash(data_pagamento))[1:10]}",
                        "ticker": ticker,
                        "tipo": "Aviso aos Cotistas",
                        "titulo": titulo,
                        "data": data_base,
                        "urlOriginal": url, 
                        "resumoIa": None
                    })
                    count += 1

    except urllib.error.HTTPError as e:
        print(f"[{ticker}] Erro HTTP {e.code}: {e.reason} (Site bloqueou ou ticker inativo)")
    except Exception as e:
        print(f"[{ticker}] Erro: {e}")
        
    return ticker, comunicados

def main():
    tickers = read_tickers()
    if not tickers:
        print("Nenhum ticker encontrado em tickers.txt")
        return
        
    print(f"Lendo {len(tickers)} tickers para buscar comunicados...")
    all_comunicados = []
    
    # Execução sequencial com delay de 1.5s (Muito Importante para não tomar Block)
    for i, ticker in enumerate(tickers):
        print(f"Processando ({i+1}/{len(tickers)}): {ticker}")
        t, comunicados = fetch_comunicados_for_ticker(ticker)
        all_comunicados.extend(comunicados)
        time.sleep(1.5) 
        
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "comunicados": all_comunicados
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"Salvo {len(all_comunicados)} comunicados no total em {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
