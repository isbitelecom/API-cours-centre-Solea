from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup
import os
import re

app = Flask(__name__)

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

            if any(mot in texte.lower() for mot in [
                    "horaire", "cours", "débutant", "intermédiaire", "avancé",
                    "stage", "tablao", "tarif"
            ]):
                if texte not in seen:
                    infos.append(texte)
                    seen.add(texte)

        # Recherche du prix d'adhésion
        texte_complet = soup.get_text(separator=' ', strip=True)
        match = re.search(r'Adhésion annuelle\s*:\s*(\d+)\s*€', texte_complet, re.IGNORECASE)
        prix_adhesion = match.group(1) + ' €' if match else "Prix adhésion non disponible"

        return jsonify({
            "informations": infos,
            "prix_adhesion": prix_adhesion
        })

    except Exception as e:
        return jsonify({"erreur": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
