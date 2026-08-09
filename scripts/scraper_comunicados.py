import os
import re
import json
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
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    )
    
    comunicados = []
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('utf-8')
            
            # Pega apenas a seção de comunicados para evitar varrer o HTML todo
            if 'id="comunicados"' in html:
                section = html.split('id="comunicados"')[1][:15000]
                
                # Regex para extrair a URL, Título e Data
                regex = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')
                matches = regex.findall(section)
                
                count = 0
                for match in matches:
                    if count >= 10:
                        break
                    url_original = match[0].replace("&amp;", "&")
                    titulo = match[1].strip()
                    data = match[2].strip()
                    
                    id_doc = ""
                    if "id=" in url_original:
                        id_doc = url_original.split("id=")[1].split("&")[0]
                    if not id_doc:
                        id_doc = str(hash(url_original))
                        
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
    except Exception as e:
        print(f"Erro ao buscar comunicados para {ticker}: {e}")
        
    return comunicados

def main():
    tickers = read_tickers()
    print(f"Lendo {len(tickers)} tickers para buscar comunicados...")
    
    all_comunicados = []
    for ticker in tickers:
        print(f"Buscando {ticker}...")
        comunicados = fetch_comunicados_for_ticker(ticker)
        all_comunicados.extend(comunicados)
        print(f"Encontrados {len(comunicados)} comunicados.")
        
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "comunicados": all_comunicados
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"Salvo {len(all_comunicados)} comunicados em {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
