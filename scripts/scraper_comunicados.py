import os
import re
import json
import time
import urllib.request
import urllib.error
from datetime import datetime

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE = "comunicados.json"

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

def fetch_comunicados_for_ticker(ticker, retries=2):
    url = f"https://www.fundsexplorer.com.br/funds/{ticker.lower()}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8'
    }
    
    comunicados = []
    seen_keys = set()

    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                html = response.read().decode('utf-8', errors='ignore')
                
                if 'id="comunicados"' in html:
                    section = html.split('id="comunicados"')[1][:30000]
                    
                    # 1. Documentos PDF (Fatos Relevantes, Relatórios, etc)
                    regex_doc = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')
                    matches_doc = regex_doc.findall(section)
                    
                    count = 0
                    for match in matches_doc:
                        if count >= 8:
                            break
                        url_original = match[0].replace("&amp;", "&").strip()
                        titulo = match[1].strip()
                        data = match[2].strip()
                        
                        # Evita documentos repetidos para o mesmo fundo
                        key = (ticker, url_original)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        
                        id_doc = ""
                        if "id=" in url_original:
                            id_doc = url_original.split("id=")[1].split("&")[0]
                        if not id_doc:
                            id_doc = str(abs(hash(url_original)))[:9]
                            
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
                    
                    # 2. Rendimentos (Aviso aos Cotistas) - MANTÉM APENAS O 1 MAIS RECENTE
                    regex_rend = re.compile(r'<div class="communicated__grid__row communicated__grid__rend">.*?Rendimento no valor de (.*?) por cota no dia (.*?)</p>.*?<li><b>(.*?)</b> Data base', re.DOTALL)
                    matches_rend = regex_rend.findall(section)
                    
                    if matches_rend:
                        # Pega apenas o rendimento mais recente (primeiro da lista)
                        match = matches_rend[0]
                        valor = match[0].strip()
                        data_pagamento = match[1].strip()
                        data_base = match[2].strip()
                        
                        titulo = f"Rendimento: {valor} (Pag: {data_pagamento})"
                        rend_url = f"{url}#comunicados"
                        
                        key = (ticker, rend_url, titulo)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            comunicados.append({
                                "id": f"c_{ticker}_rend_latest",
                                "ticker": ticker,
                                "tipo": "Aviso aos Cotistas",
                                "titulo": titulo,
                                "data": data_base,
                                "urlOriginal": rend_url, 
                                "resumoIa": None
                            })

                # Sucesso: encerra as tentativas de retry
                break

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(2) # Se houver rate limit, aguarda e tenta novamente
                continue
            elif e.code in (404, 500):
                # Ticker não encontrado ou instabilidade do servidor
                break
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
                continue

    return ticker, comunicados

def main():
    tickers = read_tickers()
    if not tickers:
        print("⚠️ Nenhum ticker encontrado em tickers.txt")
        return
        
    print(f"📌 Lendo {len(tickers)} tickers únicos de {TICKERS_FILE}...")
    all_comunicados = []
    total = len(tickers)
    
    # Processamento sequencial com delay de 0.6s e retries inteligentes
    for i, ticker in enumerate(tickers, start=1):
        print(f"🚀 [{i}/{total}] Processando {ticker}...")
        t, comunicados = fetch_comunicados_for_ticker(ticker)
        if comunicados:
            all_comunicados.extend(comunicados)
            print(f"  └─ {len(comunicados)} comunicados obtidos")
        else:
            print(f"  └─ Nenhum comunicado encontrado")
        time.sleep(0.6)
        
    result = {
        "gerado_em": datetime.utcnow().isoformat() + "Z",
        "comunicados": all_comunicados
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        
    print(f"\n🎉 SUCESSO! {len(all_comunicados)} comunicados salvos em {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
