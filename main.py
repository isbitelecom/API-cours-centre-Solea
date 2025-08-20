from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
import os
import re

app = Flask(__name__)

# ---------- Utils

NBSP = u"\xa0"

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "\n").replace("\t", " ")
    s = s.replace(NBSP, " ").replace("–", "-")
    # Compacte espaces multiples mais conserve les retours de ligne.
    s = re.sub(r"[ \u2009\u202f]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def remplacer_h_par_heure(texte: str) -> str:
    # 18h30 -> 18 heure 30 ; 18h -> 18 heure ; 18:30 -> 18 heure 30
    def repl_h(m):
        h, mnt = m.group(1), m.group(2)
        return f"{h} heure {mnt}" if mnt else f"{h} heure"
    texte = re.sub(r"(\d{1,2})h(\d{2})?\b", repl_h, texte, flags=re.IGNORECASE)
    texte = re.sub(r"(\d{1,2})\s?:\s?(\d{2})\b", lambda m: f"{m.group(1)} heure {m.group(2)}", texte)
    # 18 h 30 -> 18 heure 30
    texte = re.sub(r"(\d{1,2})\s?h\s?(\d{2})\b", lambda m: f"{m.group(1)} heure {m.group(2)}", texte, flags=re.IGNORECASE)
    return texte

def select_main_container(soup: BeautifulSoup) -> Tag:
    # Essaie différents sélecteurs plausibles pour le contenu principal
    for sel in [
        "main",                          # sémantique HTML5
        "article",                       # articles
        "[role=main]",
        "div.entry-content",
        "div#content",
        "div.elementor-widget-container",  # sites Elementor
        "div.site-content",
    ]:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return node
    return soup  # fallback

def replace_br_with_newlines(container: Tag):
    for br in container.find_all("br"):
        br.replace_with("\n")

def extract_lines(container: Tag) -> list[str]:
    """Récupère des lignes lisibles en respectant titres/paragraphes/listes et <br>."""
    lines = []
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        txt = el.get_text(separator="\n", strip=True)
        txt = normalize_text(txt)
        if not txt:
            continue
        # Évite les menus/breadcrumbs vides ou doublons exacts
        if txt not in lines:
            lines.append(txt)
    # éclate les blocs multi-lignes issus des <br>
    final = []
    for block in lines:
        for line in block.split("\n"):
            t = normalize_text(line)
            if t:
                final.append(t)
    return final

def parse_adhesion(full_text: str) -> str:
    # Cherche "Adhésion annuelle : 40 €" avec ponctuation/espaces flexibles
    m = re.search(r"Adh[ée]sion\s+annuelle\s*[:\-]?\s*(\d{1,3}(?:[ .]\d{3})?)\s*€", full_text, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1).replace(' ', '').replace('.', ' ')} €".replace("  ", " ")
    # plan B : clé "adhésion" suivie d'un prix
    m2 = re.search(r"Adh[ée]sion[^\d]{0,20}(\d{1,3}(?:[ .]\d{3})?)\s*€", full_text, flags=re.IGNORECASE)
    return f"{m2.group(1)} €" if m2 else "Prix adhésion non disponible"

def parse_tarifs(lines: list[str]) -> dict:
    tarifs = {"trimestre": [], "reductions": None, "modalites": None}
    price_pattern = re.compile(r"(\d+\s?cours?[^:]*:\s*[\d ]+€(?:\s*\|\s*[\d ]+€)?)", re.IGNORECASE)
    reduc_pattern = re.compile(r"(r[ée]duction|tarifs r[ée]duits|plus de 60 ans|ch[oô]meurs|[ée]tudiants|familles)", re.IGNORECASE)
    modalites_pattern = re.compile(r"(r[èe]gler|paiement|ch[eè]ques|trimestre|modalit[ée]s)", re.IGNORECASE)

    for ln in lines:
        if price_pattern.search(ln):
            tarifs["trimestre"].append(ln)
        elif reduc_pattern.search(ln):
            tarifs["reductions"] = (tarifs["reductions"] + " " if tarifs["reductions"] else "") + ln
        elif modalites_pattern.search(ln):
            tarifs["modalites"] = (tarifs["modalites"] + " " if tarifs["modalites"] else "") + ln
    return tarifs

def parse_horaires(lines: list[str]) -> list[str]:
    # Lignes contenant des créneaux ex: "Lundi 18h30-20h"
    time_chunk = re.compile(r"\b(\d{1,2}\s?h\s?\d{0,2}|\d{1,2}h\d{0,2}|\d{1,2}:\d{2})(\s?[-–]\s?(\d{1,2}\s?h\s?\d{0,2}|\d{1,2}h\d{0,2}|\d{1,2}:\d{2}))?\b")
    day = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)"
    level = r"(d[ée]butant|interm[ée]diaire|inter[\s\-]?1|inter[\s\-]?2|avanc[ée]s?|technique|ados?|enfants?|t.?cap|s[ée]villane)"
    day_re = re.compile(day, re.IGNORECASE)
    level_re = re.compile(level, re.IGNORECASE)

    res = []
    for ln in lines:
        if time_chunk.search(ln) and (day_re.search(ln) or level_re.search(ln)):
            res.append(ln)
    return res

# ---------- Routes

@app.route("/")
def accueil():
    return "Bienvenue sur l'API du Centre Soléa !"

@app.route("/infos-cours")
def infos_cours():
    url = "https://www.centresolea.org/horaires-et-tarifs"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")  # lxml = plus robuste

        main = select_main_container(soup)
        replace_br_with_newlines(main)

        lines = extract_lines(main)
        full_text = normalize_text(main.get_text(separator="\n", strip=True))

        # N'applique PAS de filtre par mots-clés : on garderait que des bribes
        # À la place, on post-filtre gentiment pour éviter le chrome de navigation.
        blacklist = re.compile(r"^(accueil|rechercher|menu|newsletter|cookies?)$", re.IGNORECASE)
        informations = [l for l in lines if not blacklist.match(l)]

        # Parsing dédié
        prix_adhesion = parse_adhesion(full_text)
        tarifs = parse_tarifs(informations)
        horaires = parse_horaires(informations)

        # Version "lecture" (sans normalisation horaire) + version "vocale" (18h30 -> 18 heure 30)
        informations_vocal = [remplacer_h_par_heure(l) for l in informations]

        return jsonify({
            "source": url,
            "informations": informations,              # brut propre (avec sauts conservés)
            "informations_vocal": informations_vocal,  # adapté TTS
            "prix_adhesion": prix_adhesion,
            "tarifs": tarifs,
            "horaires": horaires
        })

    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

def parse_tablao_text(texte: str):
    # 12/10/2025 à 20h, Nom Artiste
    pattern = re.compile(r"(\d{2}/\d{2}/\d{4})\s*[àa]\s*(\d{1,2})h(?:\s?(\d{2}))?\s*,\s*([^.,\n]+)", re.IGNORECASE)
    matches = pattern.findall(texte)

    tablaos = []
    for date, heure, minutes, artiste in matches:
        hh = f"{heure}h{minutes}" if minutes else f"{heure}h"
        tablaos.append({
            "date": date,
            "heure": hh,
            "artiste": normalize_text(artiste)
        })
    return tablaos

@app.route("/infos-tablao")
def infos_tablao():
    url = "https://www.centresolea.org"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")
        replace_br_with_newlines(soup)

        texte_complet = normalize_text(soup.get_text(separator="\n", strip=True))
        tablaos = parse_tablao_text(texte_complet)

        # Prix : accepte variantes et espaces insécables
        match = re.search(r"Prix\s+du\s+tablao\s*[:\-]?\s*(\d{1,3})\s*€", texte_complet, re.IGNORECASE)
        prix_tablao = match.group(1) + " €" if match else "Prix tablao non disponible"

        return jsonify({
            "source": url,
            "tablaos": tablaos,
            "prix_tablao": prix_tablao
        })

    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

@app.route("/infos-stage-solea")
def infos_stage_solea():
    url = "https://www.centresolea.org/horaires-et-tarifs"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")

        main = select_main_container(soup)
        replace_br_with_newlines(main)

        lines = extract_lines(main)

        # Horaires = lignes avec heures ; niveaux = lignes mentionnant les niveaux
        horaires = parse_horaires(lines)

        niveaux = []
        for ln in lines:
            if re.search(r"(d[ée]butant|interm[ée]diaire|inter[\s\-]?1|inter[\s\-]?2|avanc[ée]s?|t.?cap|ados?|enfants?|s[ée]villane)", ln, re.IGNORECASE):
                if ln not in niveaux:
                    niveaux.append(ln)

        return jsonify({
            "source": url,
            "horaires": "; ".join(horaires) if horaires else "Horaires non disponibles",
            "niveaux": "; ".join(niveaux) if niveaux else "Niveaux non disponibles"
        })

    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
