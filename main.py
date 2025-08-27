from flask import Flask, jsonify, request
import requests
from bs4 import BeautifulSoup, Tag
import os
import re
from urllib.parse import urljoin
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

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

# ---------- Dates & helpers

MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
    "janv": 1, "févr": 2, "fevr": 2, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
    "janv.": 1, "févr.": 2, "fevr.": 2, "sept.": 9, "oct.": 10, "nov.": 11, "déc.": 12, "dec.": 12
}

def _parse_date_str(s: str):
    s = (s or "").strip().lower()
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), m.group(3)
        y = int(y) if y else None
        if y is not None and y < 100:
            y += 2000
        return (y, mth, d)
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
    return normalize_text(t)[:240]

def _infer_date_to_pydate(y, m, d, tz="Europe/Madrid") -> date | None:
    if not m or not d:
        return None
    try:
        if y is None:
            y = datetime.now(ZoneInfo(tz) if ZoneInfo else None).year
        return date(int(y), int(m), int(d))
    except Exception:
        return None

def _is_future_or_today(pydate: date, tz="Europe/Madrid") -> bool:
    today = datetime.now(ZoneInfo(tz) if ZoneInfo else None).date()
    return pydate >= today

# ---------- HTTP helpers

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

# ---------- Parsing DOM de la home (événements à venir)

DAY_TXT = r"(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
MONTH_TXT = r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|sept\.?|septembre|oct\.?|octobre|nov\.?|novembre|d[ée]c\.?|d[ée]cembre)"
DATE_LINE_RE = re.compile(rf"^\s*(?:{DAY_TXT}\s+)?(\d{{1,2}})\s+({MONTH_TXT})(?:\s+(\d{{4}}))?\s*$", re.IGNORECASE)

def _month_fr_to_int(mon: str) -> int | None:
    mon = (mon or "").lower().rstrip(".")
    return MONTHS_FR.get(mon)

def _normalize_home_date(line: str, tz="Europe/Madrid") -> str | None:
    """
    Convertit 'dim. 07 sept.' en '07/09/AAAA' (année courante si absente).
    """
    m = DATE_LINE_RE.match(line or "")
    if not m:
        return None
    d = int(m.group(1))
    mon = _month_fr_to_int(m.group(2))
    y = m.group(3)
    if not mon:
        return None
    if y:
        y = int(y)
    else:
        y = datetime.now(ZoneInfo(tz) if ZoneInfo else None).year
    return f"{d:02d}/{mon:02d}/{y:04d}"

def _looks_like_time(s: str) -> bool:
    return bool(re.search(r"\b\d{1,2}\s*h\s*\d{0,2}\b", (s or ""), re.IGNORECASE))

H_RE = re.compile(r"\b(?P<h>\d{1,2})\s*h\s*(?P<mn>[0-5]\d)?\b", re.IGNORECASE)

def _extract_times_from_text(txt: str) -> list[str]:
    times = []
    for mh in H_RE.finditer(txt):
        h = int(mh.group("h"))
        mn = mh.group("mn")
        times.append(f"{h}h{mn}" if mn else f"{h}h")
    # dédoublonner
    seen = set(); uniq = []
    for t in times:
        if t not in seen:
            seen.add(t); uniq.append(t)
    return uniq

ART_RE = re.compile(r"(?:\bavec\s+)([^—\-:,()\n]+)", re.IGNORECASE)
LIEU_RE = re.compile(r"(?:\b(?:à|au|aux|chez)\s+)([^—\-:,()\n]+)", re.IGNORECASE)

def parse_evenements_home_dom(home_soup: BeautifulSoup, base_url: str, tz="Europe/Madrid") -> list[dict]:
    """
    Récupère les cartes sous 'Les événements à venir' via le DOM :
    - titre = texte du lien /events/ (hors 'En savoir plus')
    - date = ligne juste sous le titre (normalisée)
    - lieu = ligne suivante (si ce n'est pas une heure)
    - artiste = heuristique 'avec ...' sinon '' (pour tablao: fallback = titre)
    - url_detail = href du lien titre
    - type = 'tablao' si titre contient 'tablao', sinon 'evenement'
    """
    events = []

    # 1) Trouver la section "Les événements à venir"
    hroot = home_soup
    for cand in home_soup.find_all(["h2", "h3"]):
        if "événements à venir" in cand.get_text(" ", strip=True).lower():
            hroot = cand.parent
            break

    # 2) Repérer les liens de titres vers /events/
    title_links = []
    for a in hroot.select("a[href]"):
        href = a.get("href", "")
        txt = a.get_text(" ", strip=True)
        if not href or "en savoir plus" in (txt or "").lower():
            continue
        if "/events/" in href:
            title_links.append(a)

    # 3) Pour chaque lien-titre, lire les frères suivants : date puis lieu
    for a in title_links:
        titre = _clean_title(a.get_text(" ", strip=True))
        url = urljoin(base_url, a.get("href"))
        date_line = ""
        lieu_line = ""
        # on parcours quelques siblings (sécurité)
        steps = 0
        for sib in a.parent.next_siblings:
            steps += 1
            if steps > 8:  # limite de sécurité
                break
            t = ""
            if isinstance(sib, Tag):
                t = sib.get_text(" ", strip=True)
            else:
                t = str(sib).strip()
            if not t:
                continue
            if (isinstance(sib, Tag) and sib.name == "a" and "/events/" in sib.get("href", "")) or ("en savoir plus" in t.lower()):
                break
            if not date_line:
                date_line = t
                continue
            if not lieu_line:
                if not _looks_like_time(t):
                    lieu_line = t
                break

        # Normaliser date
        ymd = _parse_date_str(date_line)
        if ymd:
            date_fmt = _fmt_ddmmyyyy(*ymd)
        else:
            date_fmt = _normalize_home_date(date_line, tz=tz)
        if not date_fmt:
            # sans date exploitable, on ignore la carte
            continue

        typ = "tablao" if re.search(r"\btablao\b", titre, re.IGNORECASE) else "evenement"

        # Artiste
        m_art = re.search(r"\bavec\s+(.+)$", titre, re.IGNORECASE)
        artiste = _clean_title(m_art.group(1)) if m_art else (titre if typ == "tablao" else "")

        # Lieu propre
        lieu = _clean_title(lieu_line)
        if _looks_like_time(lieu):
            lieu = ""

        events.append({
            "titre": titre,
            "artiste": artiste,
            "lieu": lieu,
            "date": date_fmt,
            "heure": "",
            "type": typ,
            "url_detail": url
        })

    # dédoublonner par (titre, date, url_detail)
    seen = set(); uniq = []
    for e in events:
        key = (e["titre"], e["date"], e["url_detail"])
        if key in seen:
            continue
        seen.add(key); uniq.append(e)
    return uniq

# ---------- Enrichissement des heures depuis les pages détails (tablaos)

def enrich_tablao_hours_from_details_dom(items: list[dict], headers: dict, limit_pages: int = 6):
    """
    Visite jusqu'à N pages détail (seulement pour items type='tablao' sans heure)
    et complète l'heure quand la page contient la même date (ou une seule heure).
    """
    to_visit = [e for e in items if e.get("type") == "tablao" and not e.get("heure") and e.get("url_detail")]
    to_visit = to_visit[:max(0, limit_pages)]
    if not to_visit:
        return items

    for e in to_visit:
        url = e["url_detail"]
        try:
            soup = _fetch_soup(url, headers)
            txt = normalize_text(soup.get_text(separator="\n", strip=True))

            # Collecte dates & heures
            dates_found = []
            for m in re.finditer(r"(\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?|\d{1,2}\s+[a-zéûîôàèùç\.]{3,9}\.?\s*(\d{4})?)", txt, flags=re.IGNORECASE):
                ymd = _parse_date_str(m.group(0))
                if ymd:
                    fmt = _fmt_ddmmyyyy(*ymd)
                    if fmt:
                        dates_found.append(fmt)
            dates_found = list(dict.fromkeys(dates_found))
            hours = _extract_times_from_text(txt)

            if not hours:
                continue
            if e["date"] in dates_found:
                e["heure"] = hours[0]
            else:
                if len(hours) == 1:
                    e["heure"] = hours[0]
        except Exception:
            continue
    return items

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
    """
    Paramètres:
      - all_dates: "true"/"false" (par défaut false → ne renvoie QUE les dates à venir)
      - only: "tablao" pour ne garder que les tablaos (sinon tous les événements)
      - deep: "1" pour enrichir l'heure depuis les pages détail (SEULEMENT pour type=tablao)
      - limit: nombre max de pages détail à suivre (par défaut 6)
    """
    base_url = "https://www.centresolea.org"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}
    deep = request.args.get("deep") == "1"
    deep_limit = int(request.args.get("limit", "6"))
    all_dates = (request.args.get("all_dates", "false").lower() == "true")
    only = request.args.get("only", "").lower()  # "tablao" pour filtrer
    tz = "Europe/Madrid"

    try:
        soup = _fetch_soup(base_url, headers)

        # 1) Parsing DOM des événements
        items = parse_evenements_home_dom(soup, base_url, tz=tz)

        # 2) Filtre optionnel only=tablao
        if only == "tablao":
            items = [e for e in items if e["type"] == "tablao"]

        # 3) Ne garder que les dates futures (sauf all_dates=true)
        future_items = []
        for e in items:
            ymd = _parse_date_str(e["date"])
            if not ymd:
                continue
            pydate = _infer_date_to_pydate(*ymd, tz=tz)
            if not pydate:
                continue
            e["_pydate"] = pydate
            if all_dates or _is_future_or_today(pydate, tz=tz):
                future_items.append(e)

        # 4) Tri par date croissante
        future_items.sort(key=lambda x: x["_pydate"])

        # 5) Enrichissement heure (tablaos uniquement)
        if deep and future_items:
            future_items = enrich_tablao_hours_from_details_dom(future_items, headers, limit_pages=deep_limit)

        # 6) Projection finale & version vocale
        evenements = []
        evenements_vocal = []
        for e in future_items:
            e.pop("_pydate", None)
            out_item = {k: e.get(k, "") for k in ["titre", "artiste", "lieu", "date", "heure", "type"]}
            evenements.append(out_item)
            evenements_vocal.append({**out_item, "heure_vocal": remplacer_h_par_heure(out_item.get("heure", ""))})

        return jsonify({
            "source": base_url,
            "deep_followed": deep,
            "all_dates": all_dates,
            "only": only or "all",
            "evenements": evenements,
            "evenements_vocal": evenements_vocal,
            "prix_tablao": "Prix tablao non disponible"
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

# --------- Parse stages (corrigé)
def parse_stage_text_robuste(full_text: str) -> list[dict]:
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
