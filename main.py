from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
import os
import re

app = Flask(__name__)

def remplacer_h_par_heure(texte):
    def repl(m):
        heure = m.group(1)
        minutes = m.group(2)
        if minutes:
            return f"{heure} heure {minutes}"
        else:
            return f"{heure} heure"
    return re.sub(r'(\d{1,2})h(\d{2})?', repl, texte)

@app.route('/')
def accueil():
    return "Bienvenue sur l'API du Centre Soléa !"

@app.route('/infos-cours')
def infos_cours():
    url = "https://isbitelecom.com/prix-cours"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        blocs = soup.find_all(["h1", "h2", "h3", "p", "li"])
        infos = []
        seen = set()

        for bloc in blocs:
            texte = bloc.get_text(strip=True)
            texte = texte.encode('utf-8', errors='ignore').decode('utf-8')
            texte = texte.replace('–', '-').strip()
            texte = ' '.join(texte.split())
            texte = remplacer_h_par_heure(texte)

            if any(mot in texte.lower() for mot in [
                    "horaire", "cours", "débutant", "intermédiaire", "avancé",
                    "stage", "tablao", "tarif"
            ]):
                if texte not in seen:
                    infos.append(texte)
                    seen.add(texte)

        texte_complet = soup.get_text(separator=' ', strip=True)
        match = re.search(r'Adhésion annuelle\s*:\s*(\d+)\s*€', texte_complet, re.IGNORECASE)
        prix_adhesion = match.group(1) + ' €' if match else "Prix adhésion non disponible"

        return jsonify({
            "informations": infos,
            "prix_adhesion": prix_adhesion
        })

    except Exception as e:
        return jsonify({"erreur": str(e)})

def parse_tablao_text(texte):
    pattern = re.compile(r'(\d{2}/\d{2}/\d{4})\s*[àa]\s*(\d{1,2})h,\s*([^.,]+)', re.IGNORECASE)
    matches = pattern.findall(texte)

    tablaos = []
    for date, heure, artiste in matches:
        tablaos.append({
            "date": date,
            "heure": f"{heure}h",
            "artiste": artiste.strip()
        })
    return tablaos

@app.route('/infos-tablao')
def infos_tablao():
    url = "https://isbitelecom.com/prix-tablao"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        texte_complet = soup.get_text(separator=' ', strip=True)
        tablaos = parse_tablao_text(texte_complet)

        match = re.search(r'Prix du tablao\s*:\s*(\d+)\s*€', texte_complet, re.IGNORECASE)
        prix_tablao = match.group(1) + ' €' if match else "Prix tablao non disponible"

        return jsonify({
            "tablaos": tablaos,
            "prix_tablao": prix_tablao
        })

    except Exception as e:
        return jsonify({"erreur": str(e)})

@app.route('/infos-stage-solea')
def infos_stage_solea():
    url = "https://www.flamencoenfrance.fr/stages/stage-de-flamenco-avec-veronica-vallecillo-2025-06-10"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(url, headers=headers)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        horaires = []
        for p in soup.find_all('p'):
            texte = p.get_text(strip=True)
            if any(mot in texte.lower() for mot in ['heure', 'horaires', 'de', 'à']):
                horaires.append(texte)

        niveaux = []
        for li in soup.find_all('li'):
            texte = li.get_text(strip=True)
            if any(mot in texte.lower() for mot in ['débutant', 'intermédiaire', 'avancé', 'niveau']):
                niveaux.append(texte)

        horaires_text = ' ; '.join(horaires) if horaires else "Horaires non disponibles"
        niveaux_text = ' ; '.join(niveaux) if niveaux else "Niveaux non disponibles"

        return jsonify({
            "horaires": horaires_text,
            "niveaux": niveaux_text
        })

    except Exception as e:
        return jsonify({"erreur": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
