import requests
import re
import json
import datetime

# Lista principal de FIIs que queremos monitorar (você pode adicionar mais aqui no futuro)
tickers = [
    "MXRF11", "HGLG11", "GARE11", "VISC11", "KNRI11", 
    "CPTS11", "XPLG11", "VGHF11", "KNCR11", "BCCR11"
]

def fetch_comunicados():
    comunicados = []
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    # Regex para extrair a URL, título e data direto do HTML (conforme testamos antes)
    regex = re.compile(r'<a href="([^"]+)"[^>]*>([^<]+)</a>\s*<p>([^<]+)</p>')

    for ticker in tickers:
        print(f"Buscando comunicados para {ticker}...")
        try:
            url = f"https://www.fundsexplorer.com.br/funds/{ticker.lower()}"
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                html = response.text
                section_idx = html.find('id="comunicados"')
                if section_idx != -1:
                    section = html[section_idx:section_idx+15000]
                    matches = regex.findall(section)
                    
                    count = 0
                    for url_match, title, date in matches:
                        if count >= 10: break
                        
                        url_clean = url_match.replace("&amp;", "&")
                        title_clean = title.strip()
                        date_clean = date.strip()
                        
                        try:
                            id_str = url_clean.split('id=')[1].split('&')[0]
                        except:
                            id_str = str(datetime.datetime.now().timestamp()).replace('.','')
                        
                        tipo_parts = title_clean.split(",")
                        tipo = tipo_parts[0].strip() if len(tipo_parts) > 1 else "Comunicado"
                        
                        comunicados.append({
                            "id": f"c_{ticker}_{id_str}",
                            "ticker": ticker,
                            "tipo": tipo,
                            "titulo": title_clean,
                            "data": date_clean,
                            "urlOriginal": url_clean,
                            "resumoIa": None
                        })
                        count += 1
                else:
                    print(f"Não encontrou a seção de comunicados para {ticker}")
            else:
                print(f"Erro {response.status_code} ao acessar {ticker}")
                
        except Exception as e:
            print(f"Erro na requisição do {ticker}: {e}")

    # Monta a estrutura final do JSON
    output = {
        "gerado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "comunicados": comunicados
    }

    # Salva o resultado no arquivo comunicados.json na raiz
    with open('comunicados.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"Salvo {len(comunicados)} comunicados no arquivo comunicados.json")

if __name__ == "__main__":
    fetch_comunicados()
