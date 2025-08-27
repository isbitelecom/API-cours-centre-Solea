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

# ---------- Dates & parsing évènements

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

# ---------- Scraping helpers (requests/BS4)

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

# ---------- Extraction des évènements (classement + titre + artiste + lieu + heure)

H_RE = re.compile(r"\b(?P<h>\d{1,2})\s*h\s*(?P<mn>[0-5]\d)?\b", re.IGNORECASE)

def _extract_times_from_text(txt: str) -> list[str]:
    times = []
    for mh in H_RE.finditer(txt):
        h = int(mh.group("h"))
        mn = mh.group("mn")
        times.append(f"{h}h{mn}" if mn else f"{h}h")
    # dédupliquer en gardant l'ordre
    seen = set()
    uniq = []
    for t in times:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def _classify_type(title: str, context: str) -> str:
    blob = f"{title}\n{context}".lower()
    return "tablao" if re.search(r"\btablao\b", blob) else "evenement"

# heuristiques simples pour artiste/lieu
ART_RE = re.compile(r"(?:\bavec\s+)([^—\-:,()\n]+)", re.IGNORECASE)
LIEU_RE = re.compile(r"(?:\b(?:à|au|aux|chez)\s+)([^—\-:,()\n]+)", re.IGNORECASE)

def _extract_artist_and_place(title: str, context: str, typ: str):
    blob = f"{title}\n{context}"
    artiste = ""
    lieu = ""
    ma = ART_RE.search(blob)
    if ma:
        artiste = _clean_title(ma.group(1))
    ml = LIEU_RE.search(blob)
    if ml:
        lieu = _clean_title(ml.group(1))
    # fallback tablao: si rien trouvé, on laisse artiste=titre (souvent « Tablao avec X »)
    if typ == "tablao" and not artiste:
        artiste = title
    return artiste, lieu

def parse_evenements_robuste(full_text: str):
    """
    Trouve des lignes évènementielles: [date] (heure optionnelle) [-,:] [titre].
    Classe en 'tablao' ou 'evenement'. Extrait heuristiquement artiste et lieu.
    """
    txt = normalize_text(full_text)

    jours = r"(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)"
    mois = (
        r"(?:janvier|février|fevrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre|"
        r"janv\.?|févr\.?|fevr\.?|sept\.?|oct\.?|nov\.?|déc\.?|dec\.?)"
    )
    date_num = r"(?:\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?)"
    date_txt = rf"(?:\d{{1,2}}\s+{mois}(?:\s+\d{{4}})?)"
    date_any = rf"(?:(?:{jours}\s+)??(?:{date_num}|{date_txt}))"
    heure = r"(?P<h>\d{1,2})\s*(?:h|:)\s*(?P<mn>[0-5]?\d)?"

    pat = re.compile(
        rf"(?P<date>{date_any})\s*(?:[àa]|[-–—]|,)?\s*(?:{heure})?(?:\s*[–—\-,:]\s*(?P<title>[^\n\r]+))?",
        flags=re.IGNORECASE
    )

    out = []
    seen = set()
    for m in pat.finditer(txt):
        raw_date = m.group("date")
        h, mn = m.group("h"), m.group("mn")
        titre = _clean_title(m.group("title") or "")
        # contexte proche
        start, end = m.start(), m.end()
        ctx = txt[max(0, start-140): min(len(txt), end+140)]

        ymd = _parse_date_str(raw_date)
        date_fmt = _fmt_ddmmyyyy(*ymd) if ymd else raw_date
        heure_fmt = ""
        if h:
            try:
                heure_fmt = f"{int(h)}h{mn}" if mn else f"{int(h)}h"
            except ValueError:
                heure_fmt = ""

        ev_type = _classify_type(titre, ctx)
        artiste, lieu = _extract_artist_and_place(titre, ctx, ev_type)

        key = (date_fmt or raw_date, heure_fmt, titre, ev_type, artiste, lieu)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "date": date_fmt or raw_date,
            "heure": heure_fmt,
            "titre": titre,
            "type": ev_type,
            "artiste": artiste,
            "lieu": lieu
        })
    return out

# ---------- Enrichissement des heures via pages « détails » (tablaos seulement)

def _find_tablao_detail_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    links = []
    for a in soup.select("a[href]"):
        txt = a.get_text(" ", strip=True)
        href = a["href"]
        blob = f"{txt}\n{href}".lower()
        if "tablao" in blob:
            links.append(urljoin(base_url, href))
    # dédoublonner en gardant l'ordre
    seen = set()
    out = []
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def _ddmmyyyy_as_tuple(s: str):
    m = re.fullmatch(r"(\d{2})/(\d{2})(?:/(\d{4}))?", s)
    if not m:
        return None
    d = int(m.group(1)); mo = int(m.group(2)); y = m.group(3)
    return (int(y) if y else None, mo, d)

def _same_calendar_day(d1: str, d2: str) -> bool:
    t1 = _ddmmyyyy_as_tuple(d1)
    t2 = _ddmmyyyy_as_tuple(d2)
    if not t1 or not t2:
        return False
    return (t1[1] == t2[1]) and (t1[2] == t2[2]) and ((t1[0] == t2[0]) or (t1[0] is None) or (t2[0] is None))

def enrich_tablao_hours_from_details(items: list[dict], home_soup: BeautifulSoup, headers: dict, base_url: str, limit_pages: int = 4):
    needs = [t for t in items if (t.get("type") == "tablao" and not t.get("heure"))]
    if not needs:
        return items

    candidate_links = _find_tablao_detail_links(home_soup, base_url)[:max(0, limit_pages)]
    if not candidate_links:
        return items

    detail_pairs = []  # (date_fmt, [heures])
    for url in candidate_links:
        try:
            soup = _fetch_soup(url, headers)
            txt = normalize_text(soup.get_text(separator="\n", strip=True))
            dates_found = []
            for m in re.finditer(r"(\d{1,2}[\/\-.]\d{1,2}(?:[\/\-.]\d{2,4})?|\d{1,2}\s+[a-zéûîôàèùç\.]{3,9}\.?\s*(\d{4})?)", txt, flags=re.IGNORECASE):
                raw = m.group(0)
                ymd = _parse_date_str(raw)
                fmt = _fmt_ddmmyyyy(*ymd) if ymd else None
                if fmt:
                    dates_found.append(fmt)
            dates_found = list(dict.fromkeys(dates_found))
            times_found = _extract_times_from_text(txt)
            if not dates_found and times_found:
                detail_pairs.append(("", times_found))
            elif dates_found:
                for d in dates_found:
                    detail_pairs.append((d, times_found))
        except Exception:
            continue

    for t in items:
        if t.get("type") != "tablao" or t.get("heure"):
            continue
        d = t.get("date", "")
        matched = None
        for (dd, hh) in detail_pairs:
            if dd and _same_calendar_day(d, dd) and hh:
                matched = hh[0]
                break
        if not matched:
            flat = [h for (_, hs) in detail_pairs for h in hs]
            uniq = list(dict.fromkeys(flat))
            if len(uniq) == 1:
                matched = uniq[0]
        if matched:
            t["heure"] = matched
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
      - limit: nombre max de pages détail à suivre (par défaut 4)
    """
    base_url = "https://www.centresolea.org"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CentreSoleaBot/1.0)"}
    deep = request.args.get("deep") == "1"
    deep_limit = int(request.args.get("limit", "4"))
    all_dates = (request.args.get("all_dates", "false").lower() == "true")
    only = request.args.get("only", "").lower()  # "tablao" pour filtrer
    tz = "Europe/Madrid"

    try:
        soup = _fetch_soup(base_url, headers)
        texte_complet = normalize_text(soup.get_text(separator="\n", strip=True))

        # 1) Tous les items détectés (type, titre, date, heure, artiste, lieu)
        items = parse_evenements_robuste(texte_complet)

        # 2) Option: filtrer uniquement les tablaos
        if only == "tablao":
            items = [it for it in items if it.get("type") == "tablao"]

        # 3) Ne garder que les dates futures (sauf si all_dates=true)
        next_items = []
        for it in items:
            ymd = _parse_date_str(it["date"])
            if not ymd:
                continue
            pydate = _infer_date_to_pydate(*ymd, tz=tz)
            if not pydate:
                continue
            it["_pydate"] = pydate
            if all_dates or _is_future_or_today(pydate, tz=tz):
                next_items.append(it)

        # 4) Tri par date croissante
        next_items.sort(key=lambda x: x["_pydate"])

        # 5) Enrichissement horaire optionnel pour tablaos
        if deep and next_items:
            next_items = enrich_tablao_hours_from_details(next_items, soup, headers, base_url, limit_pages=deep_limit)

        # 6) Version "vocale"
        evenements = []
        evenements_vocal = []
        for it in next_items:
            it.pop("_pydate", None)
            evenements.append(it)
            evenements_vocal.append({**it, "heure_vocal": remplacer_h_par_heure(it.get("heure", ""))})

        # 7) Prix tablao si présent
        match = re.search(r"Prix\s+du\s+tablao\s*[:\-]?\s*(\d{1,3})\s*€", texte_complet, re.IGNORECASE)
        prix_tablao = match.group(1) + " €" if match else "Prix tablao non disponible"

        return jsonify({
            "source": base_url,
            "deep_followed": deep,
            "all_dates": all_dates,
            "only": only or "all",
            "evenements": evenements,            # chaque item: titre, artiste, lieu, date, heure, type
            "evenements_vocal": evenements_vocal,
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

# --------- Parse stages (corrigé, inchangé)
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
