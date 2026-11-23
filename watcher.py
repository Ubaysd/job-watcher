"""Job watcher: détecte les nouvelles offres sur les sites carrières et alerte sur Telegram.

Usage:
  python watcher.py            # run normal (1er passage par source = baseline silencieuse)
  python watcher.py --report   # liste l'état de chaque source, sans alerte
  python watcher.py --test     # envoie un message Telegram de test
"""
import json, os, re, sys, time, unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
CFG = yaml.safe_load((ROOT / "companies.yaml").read_text(encoding="utf-8"))
F = CFG["filters"]
INC = re.compile(F["title_include"], re.I)
EXC = re.compile(F["title_exclude"], re.I)
LOC = re.compile(F["location_include"], re.I)
UA = {"User-Agent": "Mozilla/5.0 (job-watcher)"}
S = requests.Session()
S.headers.update(UA)


# ---------- Fetchers : renvoient une liste de dicts {id, title, url, location} ----------

def lever(slug):
    r = S.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=20)
    r.raise_for_status()
    return [{"id": j["id"], "title": j["text"], "url": j["hostedUrl"],
             "location": (j.get("categories") or {}).get("location", "")} for j in r.json()]


def greenhouse(slug):
    r = S.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=20)
    r.raise_for_status()
    return [{"id": str(j["id"]), "title": j["title"], "url": j["absolute_url"],
             "location": (j.get("location") or {}).get("name", "")} for j in r.json()["jobs"]]


def ashby(slug):
    r = S.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=20)
    r.raise_for_status()
    return [{"id": j["id"], "title": j["title"], "url": j.get("jobUrl", ""),
             "location": j.get("location", "")} for j in r.json().get("jobs", [])]


def smartrecruiters(slug):
    out, offset = [], 0
    while True:
        r = S.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                  params={"limit": 100, "offset": offset, "country": "fr"}, timeout=20)
        r.raise_for_status()
        d = r.json()
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append({"id": j["id"], "title": j["name"],
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                        "location": f"{loc.get('city', '')} {loc.get('country', '')}"})
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 1000:
            return out


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
COOKIE_BTNS = ["#onetrust-accept-btn-handler", "#didomi-notice-agree-button",
               "button:has-text('Tout accepter')", "button:has-text('Accepter')",
               "button:has-text('Accept all')", "button:has-text('Accept')"]
DEBUG = ROOT / "debug"


def _debug_shot(p, name):
    try:
        DEBUG.mkdir(exist_ok=True)
        p.screenshot(path=str(DEBUG / (re.sub(r"\W+", "_", name) + ".png")), full_page=True)
    except Exception:
        pass


def workday(c, ctx):
    """Ouvre le site Workday comme un visiteur (cookies), puis appelle son API depuis le navigateur."""
    p = ctx.new_page()
    try:
        p.goto(f"https://{c['host']}/{c['site']}", wait_until="domcontentloaded", timeout=45000)
        p.wait_for_timeout(4000)
        base = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
        out, offset = [], 0
        while offset < 100:
            r = p.request.post(base, data={"appliedFacets": {}, "limit": 20, "offset": offset,
                                           "searchText": "product"},
                               headers={"Accept": "application/json"})
            if not r.ok:
                _debug_shot(p, c["name"])
                raise RuntimeError(f"HTTP {r.status} sur l'API Workday")
            posts = r.json().get("jobPostings", [])
            for j in posts:
                path = j.get("externalPath", "")
                out.append({"id": path, "title": j.get("title", ""),
                            "url": f"https://{c['host']}/{c['site']}{path}",
                            "location": j.get("locationsText", "")})
            if len(posts) < 20:
                break
            offset += 20
            p.wait_for_timeout(1500)
        return out
    finally:
        p.close()


def page(c, ctx):
    p = ctx.new_page()
    try:
        p.goto(c["url"], wait_until="domcontentloaded", timeout=60000)
        for sel in COOKIE_BTNS:  # bandeau cookies : on accepte s'il est là
            try:
                p.click(sel, timeout=1500)
                break
            except Exception:
                pass
        p.wait_for_timeout(8000)  # laisse le temps au JS de charger la liste d'offres
        p.mouse.wheel(0, 20000)
        p.wait_for_timeout(2000)
        links = p.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.href, (e.innerText || '').trim()])")
        rx = re.compile(c["link_regex"])
        seen, out = set(), []
        for href, text in links:
            href = href.split("#")[0]
            if rx.search(href) and href not in seen:
                seen.add(href)
                out.append({"id": href, "title": text.split("\n")[0][:150] or href,
                            "url": href, "location": ""})
        if not out:
            _debug_shot(p, c["name"])
        return out
    except Exception:
        _debug_shot(p, c["name"])
        raise
    finally:
        p.close()


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9+.&']+", " ", s).strip()


BIG = [_norm(l) for l in (ROOT / "big_companies.txt").read_text(encoding="utf-8").splitlines()
       if l.strip() and not l.startswith("#")]
BIG_RX = re.compile(r"\b(" + "|".join(re.escape(b) for b in BIG) + r")\b")


def linkedin(_c=None):
    from bs4 import BeautifulSoup
    cfg = CFG["linkedin"]
    out, ids = [], set()
    # une seule recherche qui regroupe tous les mots-clés (OR) -> 1 à 2 requêtes par passage
    query = " OR ".join(f'"{k}"' for k in cfg["keywords"])
    for kw in [query]:
        for p in range(cfg.get("pages", 2)):
            url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            params = {"keywords": kw, "location": "France", "f_TPR": "r7200",  # fenêtre 2h
                      "sortBy": "DD", "start": p * 25}
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:  # nouvel essai unique après 45 s
                time.sleep(45)
                r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:
                raise RuntimeError("LinkedIn rate-limit (429)")
            r.raise_for_status()
            cards = BeautifulSoup(r.text, "html.parser").select("li")
            for li in cards:
                urn = li.select_one("[data-entity-urn]")
                a = li.select_one("a.base-card__full-link") or li.select_one("a[href*='/jobs/view/']")
                t = li.select_one(".base-search-card__title")
                co = li.select_one(".base-search-card__subtitle")
                loc = li.select_one(".job-search-card__location")
                if not (urn and a and t):
                    continue
                jid = urn["data-entity-urn"].split(":")[-1]
                company = co.get_text(strip=True) if co else ""
                if jid in ids or not BIG_RX.search(_norm(company)):
                    continue
                ids.add(jid)
                out.append({"id": jid, "title": t.get_text(strip=True), "company": company,
                            "url": f"https://www.linkedin.com/jobs/view/{jid}",
                            "location": loc.get_text(strip=True) if loc else "France"})
            if len(cards) < 25:
                break
            time.sleep(4)
        time.sleep(4)
    return out


def oracle(c):
    url = (f"https://{c['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={c['site']},"
           f"keyword=product,limit=100,sortBy=POSTING_DATES_DESC")
    r = S.get(url, timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    reqs = items[0].get("requisitionList", []) if items else []
    return [{"id": str(j["Id"]), "title": j.get("Title", ""),
             "url": f"https://careers.oracle.com/jobs/#en/sites/jobsearch/job/{j['Id']}",
             "location": j.get("PrimaryLocation", "")} for j in reqs]


API = {"lever": lever, "greenhouse": greenhouse, "ashby": ashby, "smartrecruiters": smartrecruiters}


def discover(c):
    """Teste chaque slug sur chaque ATS public ; retient le premier qui renvoie des offres."""
    for slug in c.get("slugs", [c["name"].lower()]):
        for ats, fn in API.items():
            try:
                if fn(slug):
                    return ats, slug
            except Exception:
                pass
    return None, None


# ---------- Filtres / alertes ----------

def relevant(j):
    t = j["title"]
    if not INC.search(t) or EXC.search(t):
        return False
    loc = j["location"]
    # "2 Locations" (Workday multi-sites) : on garde, l'offre peut inclure Paris
    return not loc or bool(LOC.search(loc)) or bool(re.search(r"\d+\s+(locations|sites|lieux)", loc, re.I))


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[telegram non configuré]\n" + text)
        return
    for i in range(0, len(text), 3900):  # limite Telegram 4096 caractères
        S.post(f"https://api.telegram.org/bot{tok}/sendMessage",
               data={"chat_id": chat, "text": text[i:i + 3900],
                     "disable_web_page_preview": "true"}, timeout=20)


# ---------- Main ----------

def main():
    args = set(sys.argv[1:])
    if "--test" in args:
        telegram("✅ Job watcher : Telegram OK")
        return
    report = "--report" in args
    backfill = "--backfill" in args  # écrit toutes les offres pertinentes en ligne dans offres.csv, sans Telegram
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("seen", {}); state.setdefault("ats", {}); state.setdefault("status", {})

    def run_api(c):
        n = c["name"]
        ats, slug = c.get("ats"), c.get("slug")
        if ats == "auto":
            ats, slug = state["ats"].get(n, [None, None])
            if not ats:
                ats, slug = discover(c)
                if not ats:
                    raise RuntimeError("aucun ATS public trouvé (Lever/Greenhouse/Ashby/SmartRecruiters)")
                state["ats"][n] = [ats, slug]
        if ats == "oracle":
            return oracle(c)
        if ats == "linkedin":
            return linkedin(c)
        return API[ats](slug)

    results = {}
    api_cos = [c for c in CFG["companies"] if c["ats"] not in ("page", "workday")]
    # LinkedIn : au plus une fois toutes les 55 min (les autres sources tournent toutes les 15 min)
    li_wait = CFG["linkedin"].get("every_minutes", 60) * 60 - 300
    if time.time() - state.get("last_linkedin", 0) < li_wait:
        api_cos = [c for c in api_cos if c["ats"] != "linkedin"]
    else:
        state["last_linkedin"] = time.time()
    page_cos = [c for c in CFG["companies"] if c["ats"] in ("page", "workday")]

    def safe(c):
        try:
            return c["name"], run_api(c), None
        except Exception as e:
            return c["name"], None, str(e)[:200]

    with ThreadPoolExecutor(8) as ex:
        for name, jobs, err in ex.map(safe, api_cos):
            results[name] = (jobs, err)

    if page_cos:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context(user_agent=BROWSER_UA, locale="fr-FR",
                                viewport={"width": 1366, "height": 900})
            for c in page_cos:
                try:
                    fn = workday if c["ats"] == "workday" else page
                    results[c["name"]] = (fn(c, ctx), None)
                except Exception as e:
                    results[c["name"]] = (None, str(e)[:200])
            b.close()

    alerts, errors = [], []
    alerted = set(state.get("alerted", []))
    rows = []
    for c in CFG["companies"]:
        n = c["name"]
        if n not in results:  # source sautée à ce passage (LinkedIn hors créneau)
            if report:
                print(f"{n:20} SKIP  (prochain passage LinkedIn dans moins d'1h)")
            continue
        jobs, err = results[n]
        prev_status = state["status"].get(n)
        if err:
            state["status"][n] = "error"
            if prev_status != "error":
                errors.append(f"⚠️ {n} : {err}")
            if report:
                print(f"{n:20} ERREUR  {err}")
            continue
        if jobs == [] and c["ats"] == "page":
            err = "0 lien trouvé : vérifier url/link_regex"
            state["status"][n] = "error"
            if prev_status != "error":
                errors.append(f"⚠️ {n} : {err}")
            if report:
                print(f"{n:20} ERREUR  {err}")
            continue
        state["status"][n] = "ok"
        keep = [j for j in jobs if relevant(j)]
        seen = set(state["seen"].get(n, []))
        baseline = n not in state["seen"]
        new = keep if backfill else [j for j in keep if j["id"] not in seen]
        state["seen"][n] = sorted(seen | {j["id"] for j in jobs})[-3000:]
        if report:
            ats = state["ats"].get(n, [c["ats"]])[0]
            print(f"{n:20} OK  ats={ats:15} offres={len(jobs):4} pertinentes={len(keep)}")
        if new and (backfill or not baseline) and not report:
            for j in new:
                co = j.get("company") or n
                key = _norm(co.split()[0] + " " + j["title"])  # anti-doublon source directe / LinkedIn
                if key in alerted:
                    continue
                alerted.add(key)
                rows.append([time.strftime("%Y-%m-%d %H:%M", time.gmtime()), co, j["title"],
                             j["location"] or "", j["url"], n])
                alerts.append(f"🆕 {co} — {j['title']}\n📍 {j['location'] or '-'}  ({n})\n{j['url']}")
    state["alerted"] = sorted(alerted)[-5000:]

    if rows:  # historique lisible, synchronisé vers Google Drive par la tâche planifiée
        import csv
        out = ROOT / "offres.csv"
        new_file = not out.exists()
        with out.open("a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new_file:
                w.writerow(["detecte_utc", "entreprise", "poste", "lieu", "lien_postuler", "source"])
            w.writerows(rows)
    if not report and not backfill:
        if alerts:
            telegram("\n\n".join(alerts))
        if errors:
            telegram("\n".join(errors))
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    print(f"{len(alerts)} nouvelle(s) offre(s), {len(errors)} nouvelle(s) erreur(s)")


if __name__ == "__main__":
    main()
