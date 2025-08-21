from flask import Flask, jsonify
import requests
from bs4 import BeautifulSoup, Tag
import os
import re

app = Flask(__name__)

# ---------- Utils

NBSP = u"\xa0"

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "\n").replace("\t", " ")
    s = s.replace(NBSP, " ").replace("–", "-").replace("—", "-")
    # Compacte espaces multiples mais conserve les retours de ligne.
    s = re.sub(r"[ \u2009\u202f]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def remplacer_h_par_heure(texte: str) -> str:
    """
    Convertit les heures pour TTS :
    - 18h30  -> 18 heure 30
    - 18h    -> 18 heure
    - 18:30  -> 18 heure 30
    - 8 h 05 -> 8 heure 5
    - 18h-20h -> 18 heure - 20 heure
    Masque les minutes '00'.
    """
    if not texte:
        return ""

    def repl(match: re.Match) -> str:
        h = int(match.group(1))
        mnt = match.group(2)
        if mnt is None or re.fullmatch(r"0+", mnt):
            return f"{h} heure"
        return f"{h} heure {int(mnt)}"

    # Gère HHhMM, HH:MM, HH h MM, et aussi HHh / HH: / HH h
    texte = re.sub(r"\b(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?\b", repl, texte, flags=re.IGNORECASE)

    # Uniformise les tirets d'intervalles : " - "
    texte = re.sub(r"\s?[-–—]\s?", " - ", texte)

    # Nettoyage des espaces multiples
    texte = re.sub(r"\s{2,}", " ", texte).strip()
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

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# EXTRACTION DES NIVEAUX SÉVILLANE(S)
def extract_sevillane_levels(lines: list[str]) -> list[str]:
    """
    Détecte les niveaux pour Sévillane/Sévillanas partout dans la page.
    Renvoie une liste normalisée (ex: ["Débutants", "Avancés"]).
    """
    levels = []
    seen = set()

    # 1) Lignes contenant "Sévillane/Sévillanas" + un mot de niveau
    pat_sevi = re.compile(r"s[ée]villan[ae]s?", re.IGNORECASE)
    pat_lvl  = re.compile(r"(débutant(?:e|s)?|debutant(?:e|s)?|avanc[ée]s?)", re.IGNORECASE)
    for ln in lines:
        if pat_sevi.search(ln):
            m = pat_lvl.search(ln)
            if m:
                raw = m.group(1).lower()
                norm = "Débutants" if "debut" in raw or "début" in raw else "Avancés"
                if norm not in seen:
                    seen.add(norm)
                    levels.append(norm)

    # 2) Fallback : si header "DANSE SÉVILLANE" et au moins 2 créneaux "Samedi : ...",
    #    on suppose "Débutants" & "Avancés".
    if not levels:
        header_idx = None
        for i, ln in enumerate(lines):
            if re.search(r"^\s*danse\s+s[ée]villan[ae]s?\s*$", ln, re.IGNORECASE):
                header_idx = i
                break
        if header_idx is not None:
            stop_re = re.compile(r"^(planning|tarifs|danse\s+flamenco|horaires|adh[ée]sion)", re.IGNORECASE)
            section = []
            for ln in lines[header_idx+1:]:
                if stop_re.search(ln):
                    break
                section.append(ln)
            samedi_slots = [ln for ln in section if re.search(r"^\s*samedi\s*:", ln, re.IGNORECASE) and re.search(r"\d", ln)]
            if len(samedi_slots) >= 2:
                levels = ["Débutants", "Avancés"]

    # Ordre : Débutants d'abord, puis Avancés
    if len(levels) > 1:
        levels.sort(key=lambda x: 0 if x.startswith("Début") else 1)
    return levels
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# ---------- Helpers dates/tablao (parseur robuste)

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "févr": 2, "fevr": 2, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12
}

def _parse_date_str(s: str):
    """Renvoie (yyyy, mm, dd) si possible, sinon None. Accepte 12/10/2025, 12-10-2025, 12 octobre 2025, 12 oct 2025."""
    s = (s or "").strip().lower()
    # 1) Formats numériques
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if y else None
        if y is not None and y < 100:
            y += 2000
        return (y, mth, d)
    # 2) Formats textuels (12 octobre 2025 / 12 oct 2025)
    m2 = re.search(r"\b(\d{1,2})\s+([a-zéûîôàèùç]{3,9})\s*(\d{4})?\b", s, flags=re.IGNORECASE)
    if m2:
        d = int(m2.group(1))
        mon = m2.group(2).lower()
        mon = MONTHS_FR.get(mon, MONTHS_FR.get(mon[:4], None))
        if mon:
            y = int(m2.group(3)) if m2.group(3) else None
            return (y, mon, d)
    return None

def _fmt_ddmmyyyy(y, m, d):
    if y and m and d:
        return f"{d:02d}/{m:02d}/{y:04d}"
    if m and d:
        return f"{d:02d}/{m:02d}"
    return None

def _clean_title(t: str) -> str:
    t = (t or "").strip(" \n\t-–—,:;")
    return normalize_text(t)[:200]

def parse_tablao_text_robuste(full_text: str):
    """
    Extrait TOUTES les dates/horaires/titres possibles depuis la page d’accueil,
    même si le format varie (12/10/2025 à 20h — Titre) ou (Samedi 12 octobre – 20 h 30 : Titre).
    Retourne une liste de dicts: {date, heure, titre, artiste(=titre)}
    """
    txt = normalize_text(full_text)

    # Pattern combiné: date (numérique OU mois FR) + séparateur + heure + (titre optionnel)
    mois = r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|janv|févr|fevr|sept|oct|nov|déc|dec)"
    date_num = r"(?:\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?)"
    date_txt = rf"(?:\d{{1,2}}\s+{mois}(?:\s+\d{{4}})?)"
    date_any = rf"(?:{date_num}|{date_txt})"

    # heure: 20h, 20 h, 20h30, 20:30
    heure = r"(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?"

    # autoriser différents séparateurs avant l'heure et capturer un titre court après
    pat = re.compile(
        rf"(?P<date>{date_any})\s*(?:[àa]|[-–—]|,)?\s*{heure}(?:\s*[–—\-,:]\s*(?P<title>[^\n\r]+))?",
        flags=re.IGNORECASE
    )

    seen = set()
    out = []

    for m in pat.finditer(txt):
        date_raw = m.group("date")
        h = m.group(1); mn = m.group(2)
        titre = _clean_title(m.group("title") or "")

        ymd = _parse_date_str(date_raw)
        date_fmt = _fmt_ddmmyyyy(*ymd) if ymd else date_raw
        if not date_fmt:
            date_fmt = date_raw

        # heure normalisée style "20h30" ou "20h"
        heure_fmt = f"{int(h)}h{mn}" if mn else f"{int(h)}h"

        key = (date_fmt, heure_fmt, titre)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "date": date_fmt,
            "heure": heure_fmt,
            "titre": titre,
            "artiste": titre,  # compat: certains clients attendent "artiste"
        })

    return out

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

        # Niveaux Sévillane/Sévillanas
        niveaux_sevillane = extract_sevillane_levels(informations)

        # Version "vocale" (18h30 -> 18 heure 30)
        informations_vocal = [remplacer_h_par_heure(l) for l in informations]

        return jsonify({
            "source": url,
            "informations": informations,              # brut propre
            "informations_vocal": informations_vocal,  # adapté TTS
            "prix_adhesion": prix_adhesion,
            "tarifs": tarifs,
            "horaires": horaires,
            "niveaux_sevillane": niveaux_sevillane
        })

    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

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

        # >>> Utiliser le parseur ROBUSTE (toutes les dates)
        tablaos = parse_tablao_text_robuste(texte_complet)
        tablaos_vocal = [
            {
                **t,
                "heure_vocal": remplacer_h_par_heure(t.get("heure", "")),
            } for t in tablaos
        ]

        # Prix : accepte variantes et espaces insécables
        match = re.search(r"Prix\s+du\s+tablao\s*[:\-]?\s*(\d{1,3})\s*€", texte_complet, re.IGNORECASE)
        prix_tablao = match.group(1) + " €" if match else "Prix tablao non disponible"

        return jsonify({
            "source": url,
            "tablaos": tablaos,          # liste complète extraite
            "tablaos_vocal": tablaos_vocal,
            "prix_tablao": prix_tablao
        })

    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

@app.route("/infos-stage-solea")
def infos_stage_solea():
    """
    IMPORTANT: ne pas appeler l'API elle-même (sinon boucle infinie).
    On agrège du site et renvoie un JSON propre pour le voicebot.
    Pour les tests: on renvoie TOUT ce qu'on trouve (même dates passées).
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}
    try:
        # On combine accueil + horaires (sources probables d'annonces de stage)
        txt_home = _fetch_text("https://www.centresolea.org", headers)
        txt_hor  = _fetch_text("https://www.centresolea.org/horaires-et-tarifs", headers)
        txt = f"{txt_home}\n\n{txt_hor}"

        stages = parse_stage_text_robuste(txt)

        # Variante TTS (heures -> "heure")
        stages_vocal = [
            {
                **s,
                "heure_vocal": remplacer_h_par_heure(s.get("heure", "")),
            } for s in stages
        ]

        # Si rien trouvé, on renvoie un message clair (et la source)
        if not stages:
            return jsonify({
                "source": ["https://www.centresolea.org", "https://www.centresolea.org/horaires-et-tarifs"],
                "stages": [],
                "message": "Aucune occurrence de 'stage' avec date/heure détectée dans les pages sources."
            })

        return jsonify({
            "source": ["https://www.centresolea.org", "https://www.centresolea.org/horaires-et-tarifs"],
            "stages": stages,
            "stages_vocal": stages_vocal
        })

    except requests.Timeout:
        return jsonify({"erreur": "Timeout lors de la récupération des pages sources."}), 504
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
