from flask import Flask, jsonify, request
import requests
from bs4 import BeautifulSoup, Tag
import os
import re
from urllib.parse import urljoin

app = Flask(__name__)

# ---------- Utils

NBSP = u"\xa0"

def normalize_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "\n").replace("\t", " ")
    s = s.replace(NBSP, " ").replace("–", "-").replace("—", "-")
    s = re.sub(r"[ \u2009\u202f]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def remplacer_h_par_heure(texte: str) -> str:
    if not texte:
        return ""
    def repl(match: re.Match) -> str:
        h = int(match.group(1))
        mnt = match.group(2)
        if mnt is None or re.fullmatch(r"0+", mnt):
            return f"{h} heure"
        return f"{h} heure {int(mnt)}"
    texte = re.sub(r"\b(\d{1,2})\s*(?:h|:)\s*([0-5]?\d)?\b", repl, texte, flags=re.IGNORECASE)
    texte = re.sub(r"\s?[-–—]\s?", " - ", texte)
    texte = re.sub(r"\s{2,}", " ", texte).strip()
    return texte

def select_main_container(soup: BeautifulSoup) -> Tag:
    for sel in [
        "main", "article", "[role=main]",
        "div.entry-content", "div#content",
        "div.elementor-widget-container", "div.site-content",
    ]:
        node = soup.select_one(sel)
        if node and node.get_text(strip=True):
            return node
    return soup

def replace_br_with_newlines(container: Tag):
    for br in container.find_all("br"):
        br.replace_with("\n")

def extract_lines(container: Tag) -> list[str]:
    lines = []
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li"]):
        txt = el.get_text(separator="\n", strip=True)
        txt = normalize_text(txt)
        if not txt:
            continue
        if txt not in lines:
            lines.append(txt)
    final = []
    for block in lines:
        for line in block.split("\n"):
            t = normalize_text(line)
            if t:
                final.append(t)
    return final

def parse_adhesion(full_text: str) -> str:
    m = re.search(r"Adh[ée]sion\s+annuelle\s*[:\-]?\s*(\d{1,3}(?:[ .]\d{3})?)\s*€", full_text, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1).replace(' ', '').replace('.', ' ')} €".replace("  ", " ")
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

def extract_sevillane_levels(lines: list[str]) -> list[str]:
    levels = []
    seen = set()
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
    return levels

# ---------- Helpers dates/tablao (robuste)

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "févr": 2, "fevr": 2, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
    # autoriser avec point
    "janv.": 1, "févr.": 2, "fevr.": 2, "sept.": 9, "oct.": 10, "nov.": 11, "déc.": 12, "dec.": 12
}

def _parse_date_str(s: str):
    s = (s or "").strip().lower()
    # 26/09(/2025)
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if y else None
        if y is not None and y < 100:
            y += 2000
        return (y, mth, d)
    # (ven.) 26 sept(.?) (2025)
    m2 = re.search(r"(?:\b(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b[\s,]*)?(\d{1,2})\s+([a-zéûîôàèùç\.]{3,9})\.?\s*(\d{4})?\b", s, flags=re.IGNORECASE)
    if m2:
        d = int(m2.group(1))
        mon_raw = m2.group(2).lower()
        mon = MONTHS_FR.get(mon_raw, MONTHS_FR.get(mon_raw.rstrip("."), None))
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

def _fetch_text(url: str, headers: dict) -> str:
    r = requests.get(url, headers=headers, timeout=(5, 45))
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    replace_br_with_newlines(soup)
    return normalize_text(soup.get_text(separator="\n", strip=True))

def _fetch_soup(url: str, headers: dict) -> BeautifulSoup:
    r = requests.get(url, headers=headers, timeout=(5, 45))
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    replace_br_with_newlines(soup)
    return soup

def parse_tablao_text_robuste(full_text: str):
    """
    Parse robuste de la home : accepte jours/mois abrégés (avec/ sans point),
    année optionnelle, heure optionnelle. Groupes d'heure NOMMÉS.
    """
    txt = normalize_text(full_text)

    # Jours optionnels
    jours = r"(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"

    # Mois (complets et abréviations avec/ sans point)
    mois = (
        r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|"
        r"janv\.?|févr\.?|fevr\.?|sept\.?|oct\.?|nov\.?|déc\.?|dec\.?)"
    )

    date_num = r"(?:\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?)"
    date_txt = rf"(?:\d{{1,2}}\s+{mois}(?:\s+\d{{4}})?)"
    date_any = rf"(?:(?:{jours}\s+)??(?:{date_num}|{date_txt}))"

    # Heures NOMMÉES et OPTIONNELLES
    heure = r"(?P<h>\d{1,2})\s*(?:h|:)\s*(?P<mn>[0-5]?\d)?"

    # [date] [séparateur?] [heure?] [ - , : ] [titre?]
    pat = re.compile(
        rf"(?P<date>{date_any})\s*(?:[àa]|[-–—]|,)?\s*(?:{heure})?(?:\s*[–—\-,:]\s*(?P<title>[^\n\r]+))?",
        flags=re.IGNORECASE
    )

    seen = set()
    out = []

    for m in pat.finditer(txt):
        date_raw = m.group("date")
        titre = _clean_title(m.group("title") or "")

        # Normalise la date si possible
        ymd = _parse_date_str(date_raw)
        date_fmt = _fmt_ddmmyyyy(*ymd) if ymd else date_raw
        if not date_fmt:
            date_fmt = date_raw

        # Heure (optionnelle)
        h = m.group("h")
        mn = m.group("mn")
        heure_fmt = ""
        if h:
            try:
                heure_fmt = f"{int(h)}h{mn}" if mn else f"{int(h)}h"
            except ValueError:
                heure_fmt = ""

        key = (date_fmt, heure_fmt, titre)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "date": date_fmt,
            "heure": heure_fmt,
            "titre": titre,
            "artiste": titre
        })

    return out

def parse_stage_text_robuste(full_text: str) -> list[dict]:
    """
    Corrigé: on nomme les heures dans les deux branches d'alternative,
    et on récupère h/mn proprement.
    """
    txt = normalize_text(full_text)
    blocks = [b.strip() for b in re.split(r"\n{1,}", txt) if b.strip()]
    out = []
    seen = set()
    mois = r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|janv|févr|fevr|sept|oct|nov|déc|dec)"
    date_num = r"(?:\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?)"
    date_txt = rf"(?:\d{{1,2}}\s+{mois}(?:\s+\d{{4}})?)"
    date_any = rf"(?:{date_num}|{date_txt})"

    pat = re.compile(
        rf"(?i)\bstages?\b.*?(?P<date>{date_any}).{{0,60}}(?P<h>\d{{1,2}})\s*(?:h|:)\s*(?P<mn>[0-5]?\d)?"
        rf"|(?P<date2>{date_any}).{{0,60}}(?P<h2>\d{{1,2}})\s*(?:h|:)\s*(?P<mn2>[0-5]?\d)?.*?\bstages?\b"
    )

    for b in blocks:
        if not re.search(r"(?i)\bstages?\b", b):
            continue
        for m in pat.finditer(b):
            d_raw = m.group("date") or m.group("date2")
            if not d_raw:
                continue
            ymd = _parse_date_str(d_raw)
            date_fmt = _fmt_ddmmyyyy(*ymd) if ymd else d_raw

            h = m.group("h") or m.group("h2")
            mn = m.group("mn") or m.group("mn2")
            if not h:
                continue
            heure_fmt = f"{int(h)}h{mn}" if mn else f"{int(h)}h"
            titre = re.sub(r"(?i)\bstages?\b", "", b)
            titre = _clean_title(titre)
            key = (date_fmt, heure_fmt, titre)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "date": date_fmt,
                "heure": heure_fmt,
                "titre": titre,
                "tag": "stage"
            })
    return out

# ---------- Enrichissement des heures via pages « En savoir plus »

H_RE = re.compile(r"\b(\d{1,2})\s*h\s*([0-5]\d)?\b", re.IGNORECASE)

def _extract_times_from_text(txt: str) -> list[str]:
    times = []
    for mh in H_RE.finditer(txt):
        h = int(mh.group(1))
        mn = mh.group(2)
        times.append(f"{h}h{mn}" if mn else f"{h}h")
    # dédupliquer en conservant l'ordre
    seen = set()
    uniq = []
    for t in times:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def _find_tablao_detail_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    links = []
    for a in soup.select("a[href]"):
        t = a.get_text(" ", strip=True).upper()
        href = a["href"]
        if "TABLAO" in t or "TABLAO" in href.upper():
            links.append(urljoin(base_url, href))
    # dédupliquer tout en gardant l'ordre
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _ddmmyyyy_as_tuple(s: str):
    """Transforme '08/09/2025' ou '08/09' en (y,m,d) ou (None,m,d) pour faciliter la comparaison."""
    m = re.fullmatch(r"(\d{2})/(\d{2})(?:/(\d{4}))?", s)
    if not m:
        return None
    d = int(m.group(1)); mo = int(m.group(2)); y = m.group(3)
    return (int(y) if y else None, mo, d)

def _same_calendar_day(d1: str, d2: str) -> bool:
    """Compare deux dates formatées 'dd/mm(/yyyy)' en ignorant l'année si manquante."""
    t1 = _ddmmyyyy_as_tuple(d1)
    t2 = _ddmmyyyy_as_tuple(d2)
    if not t1 or not t2:
        return False
    # même jour/mois; si l'une n'a pas d'année, on ne bloque pas.
    return (t1[1] == t2[1]) and (t1[2] == t2[2]) and ((t1[0] == t2[0]) or (t1[0] is None) or (t2[0] is None))

def enrich_tablao_hours_from_details(tablaos: list[dict], home_soup: BeautifulSoup, headers: dict, base_url: str, limit_pages: int = 4):
    """
    Pour chaque tablao sans heure, suit jusqu'à 'limit_pages' liens « En savoir plus »
    contenant 'tablao' (dans le texte ou l'URL), récupère les horaires et, si la date
    correspond, complète l'heure.
    """
    # Liens candidats depuis la home
    candidate_links = _find_tablao_detail_links(home_soup, base_url)
    if not candidate_links:
        return tablaos

    # Sélectionner uniquement si au moins un item manque l'heure
    needs = [t for t in tablaos if not t.get("heure")]
    if not needs:
        return tablaos

    to_visit = candidate_links[:max(0, limit_pages)]
    detail_items = []  # [(url, [("dd/mm(/yyyy)", ["19h30","20h"]) ... ])]

    for url in to_visit:
        try:
            soup = _fetch_soup(url, headers)
            txt = normalize_text(soup.get_text(separator="\n", strip=True))
            # extraire toutes occurrences de date + heure dans la page détail
            # stratégie simple : on repère toutes les dates, et indépendamment on liste les heures,
            # puis on associe si la page ne contient qu'une date; sinon on tentera un matching exact plus bas.
            dates_found = []
            for m in re.finditer(r"(\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?|\d{1,2}\s+[a-zéûîôàèùç\.]{3,9}\.?\s*(\d{4})?)", txt, flags=re.IGNORECASE):
                raw = m.group(0)
                ymd = _parse_date_str(raw)
                fmt = _fmt_ddmmyyyy(*ymd) if ymd else None
                if fmt:
                    dates_found.append(fmt)
            times_found = _extract_times_from_text(txt)
            dates_found = list(dict.fromkeys(dates_found))  # déduplique

            if not dates_found and times_found:
                # Pas de date détectée, mais des heures trouvées : gardons quand même.
                detail_items.append((url, [("", times_found)]))
            elif dates_found:
                if len(dates_found) == 1:
                    detail_items.append((url, [(dates_found[0], times_found)]))
                else:
                    # plusieurs dates sur la page : on stocke chaque date avec le même pool d'heures
                    detail_items.append((url, [(d, times_found) for d in dates_found]))
        except Exception:
            continue  # on ignore silencieusement en cas d'erreur de réseau ou parse

    # Tentative d'enrichissement
    for t in tablaos:
        if t.get("heure"):
            continue
        d = t.get("date", "")
        # cherche un couple (date, heures) correspondant
        matched_time = None
        for _, pairs in detail_items:
            for (d_detail, hours) in pairs:
                if d_detail and _same_calendar_day(d, d_detail) and hours:
                    matched_time = hours[0]  # on prend la première heure trouvée
                    break
            if matched_time:
                break
        # fallback : si aucune date n'a matché mais une seule page a donné exactement 1 heure, on la prend
        if not matched_time:
            single_hours = [hours[0] for _, pairs in detail_items for _, hours in pairs if len(hours) == 1]
            if len(set(single_hours)) == 1:
                matched_time = single_hours[0]
        if matched_time:
            t["heure"] = matched_time
    return tablaos

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
        soup = BeautifulSoup(response.text, "lxml")
        main = select_main_container(soup)
        replace_br_with_newlines(main)
        lines = extract_lines(main)
        full_text = normalize_text(main.get_text(separator="\n", strip=True))
        blacklist = re.compile(r"^(accueil|rechercher|menu|newsletter|cookies?)$", re.IGNORECASE)
        informations = [l for l in lines if not blacklist.match(l)]
        prix_adhesion = parse_adhesion(full_text)
        tarifs = parse_tarifs(informations)
        horaires = parse_horaires(informations)
        niveaux_sevillane = extract_sevillane_levels(informations)
        informations_vocal = [remplacer_h_par_heure(l) for l in informations]
        return jsonify({
            "source": url,
            "informations": informations,
            "informations_vocal": informations_vocal,
            "prix_adhesion": prix_adhesion,
            "tarifs": tarifs,
            "horaires": horaires,
            "niveaux_sevillane": niveaux_sevillane
        })
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

@app.route("/infos-tablao")
def infos_tablao():
    base_url = "https://www.centresolea.org"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}
    deep = request.args.get("deep") == "1"
    deep_limit = int(request.args.get("limit", "4"))  # nombre max de pages détail à suivre
    try:
        # Récup home
        response = requests.get(base_url, headers=headers, timeout=20)
        response.raise_for_status()
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "lxml")
        replace_br_with_newlines(soup)
        texte_complet = normalize_text(soup.get_text(separator="\n", strip=True))

        # Parse des tablaos sur la home
        tablaos = parse_tablao_text_robuste(texte_complet)

        # Enrichissement optionnel des heures
        if deep and tablaos:
            tablaos = enrich_tablao_hours_from_details(tablaos, soup, headers, base_url, limit_pages=deep_limit)

        tablaos_vocal = [
            {**t, "heure_vocal": remplacer_h_par_heure(t.get("heure", ""))}
            for t in tablaos
        ]
        match = re.search(r"Prix\s+du\s+tablao\s*[:\-]?\s*(\d{1,3})\s*€", texte_complet, re.IGNORECASE)
        prix_tablao = match.group(1) + " €" if match else "Prix tablao non disponible"

        return jsonify({
            "source": base_url,
            "deep_followed": deep,
            "tablaos": tablaos,
            "tablaos_vocal": tablaos_vocal,
            "prix_tablao": prix_tablao
        })
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

@app.route("/infos-stage-solea")
def infos_stage_solea():
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}
    try:
        txt_home = _fetch_text("https://www.centresolea.org", headers)
        txt_hor = _fetch_text("https://www.centresolea.org/horaires-et-tarifs", headers)
        txt = f"{txt_home}\n\n{txt_hor}"
        stages = parse_stage_text_robuste(txt)
        stages_vocal = [
            {**s, "heure_vocal": remplacer_h_par_heure(s.get("heure", ""))}
            for s in stages
        ]
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
    except requests.exceptions.Timeout:
        return jsonify({"erreur": "Timeout lors de la récupération des pages sources."}), 504
    except Exception as e:
        return jsonify({"erreur": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
