import os
import re
import json
import urllib.request
from datetime import datetime
import concurrent.futures

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
                section = html.split('id="comunicados"')[1][:20000]
                
                # Regex para extrair a URL, Título e Data (Documentos PDF como Fatos Relevantes e Relatórios)
                regex_doc = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')
                matches_doc = regex_doc.findall(section)
                
                # Regex para extrair os "Rendimentos" informados na aba de comunicados
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
                
                # Alguns fundos só exibem rendimentos nos comunicados recentes (ex: GARE11)
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
                        "urlOriginal": url, # Redireciona para a página do fundo
                        "resumoIa": None
                    })
                    count += 1

    except urllib.error.HTTPError as e:
        if e.code == 500:
            pass # FundsExplorer as vezes retorna 500 para tickers inativos/incorporados, ignorar.
        else:
            print(f"Erro HTTP {e.code} para {ticker}: {e.reason}")
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
    
    # Processamento paralelo com 10 workers para acelerar o scraping significativamente
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_comunicados_for_ticker, ticker): ticker for ticker in tickers}
        
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                t, comunicados = future.result()
                all_comunicados.extend(comunicados)
                print(f"[{t}] Encontrados {len(comunicados)} comunicados.")
            except Exception as e:
                print(f"Erro processando {ticker}: {e}")
        
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "comunicados": all_comunicados
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"Salvo {len(all_comunicados)} comunicados no total em {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
