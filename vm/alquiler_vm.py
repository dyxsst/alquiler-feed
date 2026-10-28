#!/usr/bin/env python3
"""Lee Encuentra24 y Mitula desde tu computadora y publica los anuncios en tu repositorio alquiler-feed.

Estos dos sitios bloquean la lectura desde la nube, pero responden a un navegador normal desde tu casa.
Cada mañana este script abre Chromium en una pantalla virtual, recorre tus búsquedas con pausas de persona,
guarda los anuncios en alquiler-feed/feed/*.json y los sube a GitHub. La carga diaria de Claude los lee de ahí.

Uso:
    xvfb-run -a ~/Apto/.venv/bin/python ~/Apto/alquiler_vm.py           # corrida normal
    xvfb-run -a ~/Apto/.venv/bin/python ~/Apto/alquiler_vm.py --sin-git # prueba sin subir nada
Si se corre sin pantalla, se relanza solo con xvfb-run.
"""
import base64
import html as H
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("ALQ_REPO", os.path.join(HERE, "alquiler-feed"))
PROFILE = os.path.join(HERE, ".perfil-navegador-visible")
LOG = os.path.join(HERE, "alquiler_vm.log")
PANAMA = timezone(timedelta(hours=-5))

Q = "f_price.-800%7Cf_rooms.1-%7Cf_bath.1-%7CParking.1-%7Ctime.30"
E24 = "https://www.encuentra24.com"
E24_HOME = E24 + "/panama-es"
E24_SEARCHES = {
    "Apartamento": E24 + "/panama-es/bienes-raices-alquiler-apartamentos?q=" + Q + "&sort=f_added&dir=desc",
    "Casa": E24 + "/panama-es/bienes-raices-alquiler-casas?q=" + Q + "&sort=f_added&dir=desc",
}
MIT = "https://casas.mitula.com.pa"
MIT_SEARCH = (MIT + "/find?geoId=R5740665&operationType=rent&propertyType=apartment%2Chouse%2Cranch%2Cvilla"
              "&maxPrice=800&sorting=newest")
MAX_PAGES_E24 = 20
MAX_PAGES_MIT = 25
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['es-PA', 'es', 'en']});
"""


class Blocked(Exception):
    pass


def log(msg):
    line = datetime.now(PANAMA).strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
    print(line, flush=True)


def now_iso():
    return datetime.now(PANAMA).isoformat(timespec="seconds")


# ---------------------------------------------------------------- Encuentra24 parser
def rsc_text(page):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page or "", re.S)
    if not chunks:
        return page or ""
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except ValueError:
            pass
    return "".join(out)


def text_rows(s):
    rows, b, i = {}, s.encode("utf-8"), 0
    head = re.compile(rb"([0-9a-f]{1,6}):")
    while i < len(b):
        m = head.match(b, i)
        if not m:
            j = b.find(b"\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        rid, i = m.group(1).decode(), m.end()
        if b[i:i + 1] == b"T":
            j = b.find(b",", i)
            try:
                n = int(b[i + 1:j], 16)
            except ValueError:
                n = 0
            rows[rid] = b[j + 1:j + 1 + n].decode("utf-8", "replace")
            i = j + 1 + n
        else:
            j = b.find(b"\n", i)
            i = len(b) if j < 0 else j + 1
    return rows


def clean_html(s):
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s or "", flags=re.I)
    s = H.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def money(v):
    if isinstance(v, dict):
        v = (v.get("amount") or {}).get("value")
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
        if m:
            x = float(m.group(0).replace(",", ""))
            return int(x) if x.is_integer() else x
    return None


def num(v):
    if v in (None, "", "$undefined"):
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        return None
    return int(x) if x.is_integer() else x


def parse_e24(page):
    s = rsc_text(page)
    rows = text_rows(s)
    dec, ads, seen = json.JSONDecoder(), [], set()
    for m in re.finditer(r'\{"allowContactByForm":', s):
        try:
            a, _ = dec.raw_decode(s, m.start())
        except ValueError:
            continue
        aid = str(a.get("id") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        desc = a.get("description") or ""
        if isinstance(desc, str) and re.fullmatch(r"\$[0-9a-f]{1,4}", desc):
            desc = rows.get(desc[1:], "")
        loc = a.get("location") or {}
        lat, lon = loc.get("lat") or 0, loc.get("lon") or 0
        user = a.get("user") or {}
        rent = a.get("rent") or {}
        price = a.get("price_value")
        if price is None:
            price = money(rent.get("amount"))
        amen = [label for key, label in (("furnished", "Amoblado"), ("petFriendly", "Acepta mascotas"),
                                         ("swimmingPool", "Piscina"), ("balcon", "Balcón")) if a.get(key)]
        link = str(a.get("link") or "")
        ads.append({
            "id": aid, "url": E24 + link if link.startswith("/") else link, "title": a.get("title", ""),
            "price": price, "maint": money(a.get("maintenanceCost")),
            "beds": num(a.get("rooms")), "baths": num(a.get("bathrooms")), "parking": num(a.get("parking")),
            "m2": num(a.get("square")), "type": a.get("subCategoryType") or "",
            "locality": loc.get("locality") or "", "region": a.get("regionName") or "",
            "lat": lat or None, "lng": lon or None,
            "created": str(a.get("dateCreated") or "").lstrip("$D")[:10],
            "desc": clean_html(desc)[:2500] if isinstance(desc, str) else "",
            "images": [u for u in (a.get("images") or []) if isinstance(u, str)][:10],
            "seller": (user.get("contact") or {}).get("company") or user.get("name") or "",
            "advertiser": a.get("advertiserType") or "", "amenities": amen, "featured": a.get("featured") or "",
        })
    meta = {}
    m = re.search(r'"metadata":\{"total":(\d+),"page":(\d+),"itemsPerPage":(\d+),"totalPages":(\d+)\}', s)
    if m:
        meta = {"total": int(m.group(1)), "page": int(m.group(2)), "perPage": int(m.group(3)), "totalPages": int(m.group(4))}
    return ads, meta


# ---------------------------------------------------------------- Mitula parser
def lines(s):
    s = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    return [H.unescape(x).strip() for x in s.split("\n") if x.strip()]


def block_lines(a, test):
    m = re.search(r'data-test="%s"[^>]*>(.*?)</(?:div|span|p|ul|section)>' % re.escape(test), a, re.S)
    return lines(m.group(1)) if m else []


def block(a, test):
    return " ".join(block_lines(a, test))


def bigger(u):
    """Ask Mitula's image service for an 800 px version instead of the 380 px thumbnail."""
    try:
        tok = u.rsplit("/", 1)[1]
        d = json.loads(base64.b64decode(tok + "=" * (-len(tok) % 4)))
        d.setdefault("edits", {})["resize"] = {"width": 800, "height": 600, "fit": "cover"}
        return u.rsplit("/", 1)[0] + "/" + base64.b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")
    except Exception:  # noqa: BLE001
        return u


def parse_mitula(page):
    ld = []
    m = re.search(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page or "", re.S)
    if m:
        try:
            j = json.loads(m.group(1))
            j = j[0] if isinstance(j, list) else j
            ld = j.get("about") or []
        except ValueError:
            ld = []
    ld_by_name = {}
    for x in ld:
        ld_by_name.setdefault(re.sub(r"\s+", " ", str(x.get("name") or "")).strip().lower(), x)
    total = re.search(r"([\d.,]+)\s+propiedades disponibles", page or "")
    ads = []
    for i, a in enumerate(re.split(r'<article class="listing listing-card', page or "")[1:]):
        a = a[:a.find("</article>")] if "</article>" in a else a
        head = a[:4000]
        attrs = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', head))
        lid = attrs.get("listingid")
        if not lid:
            continue
        imgs = []
        for u in re.findall(r'<img[^>]+src="(https://img\.mitula\.com/[^"]+)"', a):
            if u not in imgs:
                imgs.append(u)
        alt = re.search(r'<img[^>]+alt="([^"]*)"', a)
        title = H.unescape(alt.group(1)).strip() if alt else ""
        x = ld_by_name.get(re.sub(r"\s+", " ", title).strip().lower()) or (ld[i] if i < len(ld) else {})
        geo = x.get("geo") or {}
        dest = attrs.get("clickdestination", "")
        try:
            path = base64.b64decode(dest + "=" * (-len(dest) % 4)).decode().split("?")[0]
        except Exception:  # noqa: BLE001
            path = ""
        when = block(a, "snippet__published-date-and-agency")
        pub = re.match(r"(.*?)\s+en\s+-\s+(.*)$", when)
        desc = (x.get("description") or block(a, "snippet__description") or "").strip()
        baths = re.search(r"(\d+(?:[.,]5)?)", block(a, "bathrooms") or "")
        feat_list = block_lines(a, "snippet__amenities")
        feats = " · ".join(feat_list)
        t = (desc + " " + feats).lower()
        parking = None
        pm = re.search(r"(\d)\s*(?:puestos?|estacionamientos?|parking|estac\.?)\b", t)
        if re.search(r"sin\s+estacionamiento|no\s+tiene\s+estacionamiento|no\s+incluye\s+estacionamiento", t):
            parking = 0
        elif pm:
            parking = int(pm.group(1))
        try:
            lat = float(geo.get("latitude")) if geo.get("latitude") else None
            lng = float(geo.get("longitude")) if geo.get("longitude") else None
        except ValueError:
            lat = lng = None
        ads.append({
            "id": lid, "url": MIT + path if path.startswith("/") else MIT, "title": title or x.get("name", ""),
            "price": num(attrs.get("price")), "beds": num(attrs.get("rooms")),
            "baths": num(baths.group(1).replace(",", ".")) if baths else None, "parking": parking,
            "garage": bool(re.search(r"\bgaraje\b|\bestacionamiento\b|\bparking\b", feats.lower())),
            "m2": num(re.sub(r"[^\d.,]", "", attrs.get("floorarea", "")) or None),
            "neighbourhood": attrs.get("neighbourhood", ""), "district": attrs.get("district", ""),
            "city": attrs.get("city", ""), "region": attrs.get("region", ""), "location": attrs.get("location", ""),
            "lat": lat, "lng": lng, "desc": desc[:2500], "amenities": feat_list[:20],
            "published": pub.group(1) if pub else when, "seller": pub.group(2) if pub else "",
            "type": (x.get("@type") or ""), "images": [bigger(u) for u in imgs][:10],
        })
    meta = {"total": int(re.sub(r"\D", "", total.group(1))) if total else None}
    return ads, meta


# ---------------------------------------------------------------- browser
def blocked(title, body):
    head = (title or "") + " " + (body or "")[:6000]
    return any(x in head for x in ("Just a moment", "Un momento", "Attention Required", "Sorry, you have been blocked",
                                   "Access Denied", "challenge-platform"))


def human_pause(page, a, b):
    t_end = time.time() + random.uniform(a, b)
    while time.time() < t_end:
        try:
            page.mouse.move(random.randint(40, 900), random.randint(120, 700), steps=random.randint(5, 15))
            page.mouse.wheel(0, random.randint(150, 700))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(random.uniform(0.8, 2.2))


def goto(page, url):
    resp = page.goto(url, wait_until="domcontentloaded", timeout=90000)
    for _ in range(20):
        if not any(x in page.title() for x in ("Just a moment", "Un momento")):
            break
        page.wait_for_timeout(2500)
    page.wait_for_timeout(2500)
    html = page.content()
    if blocked(page.title(), html) or (resp and resp.status in (401, 403, 429)):
        raise Blocked("el sitio bloqueó la lectura automática (%s)" % (resp.status if resp else "sin respuesta"))
    return html


def crawl_e24(page):
    res = {"ok": False, "error": "", "ads": {}, "total": {}, "pages": 0, "partial": False}
    try:
        goto(page, E24_HOME)
        human_pause(page, 6, 12)
        for kind, url in E24_SEARCHES.items():
            captured = []

            def handler(r, _c=captured):  # a real function: Playwright tags listeners and builtins refuse it
                _c.append(r)
            page.on("response", handler)
            try:
                html = goto(page, url)
                human_pause(page, 6, 12)
                ads, meta = parse_e24(html)
                for a in ads:
                    res["ads"][a["id"]] = a
                res["pages"] += 1
                res["total"][kind] = meta.get("total")
                pages = min(meta.get("totalPages") or 1, MAX_PAGES_E24)
                log("Encuentra24 %s: página 1 de %s, %d anuncios" % (kind, pages, len(ads)))
                empty = 0
                for n in range(2, pages + 1):
                    captured.clear()
                    nxt = page.query_selector("button[aria-label='Página siguiente']")
                    if not nxt or nxt.get_attribute("aria-disabled") == "true" or nxt.is_disabled():
                        break
                    nxt.scroll_into_view_if_needed(timeout=10000)
                    human_pause(page, 4, 8)
                    nxt.click(timeout=20000)
                    human_pause(page, 18, 30)
                    if blocked(page.title(), page.content()):
                        raise Blocked("bloqueo al pasar a la página %d" % n)
                    done = 0

                    def harvest():
                        nonlocal done
                        got = 0
                        todo, done = captured[done:], len(captured)
                        for r in todo:
                            try:
                                if "encuentra24" not in r.url:
                                    continue
                                ct = (r.headers or {}).get("content-type", "")
                                if "x-component" not in ct and "json" not in ct:
                                    continue
                                # the body is UTF-8 even when the header names no charset
                                body = r.body().decode("utf-8", "replace")
                            except Exception:  # noqa: BLE001
                                continue
                            if '"allowContactByForm"' not in body:
                                continue
                            more, _ = parse_e24(body)
                            for a in more:
                                if a["id"] not in res["ads"]:
                                    got += 1
                                res["ads"][a["id"]] = a
                        return got
                    got = harvest()
                    if not got:  # late responses: give the page a little more time before giving up on it
                        page.wait_for_timeout(10000)
                        got = harvest()
                    res["pages"] += 1
                    log("Encuentra24 %s: página %d, %d anuncios nuevos en esta corrida" % (kind, n, got))
                    empty = 0 if got else empty + 1
                    if empty >= 2:
                        break
            finally:
                page.remove_listener("response", handler)
            human_pause(page, 20, 40)
        res["ok"] = True
    except Blocked as e:
        res["error"] = str(e)
        res["partial"] = bool(res["ads"])
    except Exception as e:  # noqa: BLE001
        res["error"] = "error al leer Encuentra24: %s" % (str(e)[:200],)
        res["partial"] = bool(res["ads"])
        log(traceback.format_exc())
    return res


def crawl_mitula(page):
    res = {"ok": False, "error": "", "ads": {}, "total": None, "pages": 0, "partial": False}
    try:
        goto(page, MIT + "/")
        human_pause(page, 6, 12)
        html = goto(page, MIT_SEARCH)
        for n in range(1, MAX_PAGES_MIT + 1):
            ads, meta = parse_mitula(html)
            if n == 1:
                res["total"] = meta.get("total")
            new = sum(1 for a in ads if a["id"] not in res["ads"])
            for a in ads:
                res["ads"][a["id"]] = a
            res["pages"] += 1
            log("Mitula: página %d, %d anuncios (%d nuevos en esta corrida)" % (n, len(ads), new))
            if not ads or not new:
                break
            if res["total"] and len(res["ads"]) >= res["total"]:
                break
            nxt = page.query_selector("a#pagination-next")
            href = nxt.get_attribute("href") if nxt else ""
            if not nxt or not href or ("page=%d" % (n + 1)) not in href:
                break
            human_pause(page, 12, 22)
            try:
                nxt.scroll_into_view_if_needed(timeout=10000)
                nxt.click(timeout=15000)
                page.wait_for_load_state("domcontentloaded", timeout=90000)
            except Exception:  # noqa: BLE001
                log("Mitula: no pude tocar «Siguiente», abro el enlace directamente")
                page.goto(H.unescape(href), wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            html = page.content()
            if blocked(page.title(), html):
                raise Blocked("bloqueo al pasar a la página %d" % (n + 1))
        res["ok"] = True
    except Blocked as e:
        res["error"] = str(e)
        res["partial"] = bool(res["ads"])
    except Exception as e:  # noqa: BLE001
        res["error"] = "error al leer Mitula: %s" % (str(e)[:200],)
        res["partial"] = bool(res["ads"])
        log(traceback.format_exc())
    return res


# ---------------------------------------------------------------- output
def write_feed(name, res, extra):
    os.makedirs(os.path.join(REPO, "feed"), exist_ok=True)
    path = os.path.join(REPO, "feed", name)
    ads = sorted(res["ads"].values(), key=lambda a: str(a["id"]))
    head = {"generatedAt": now_iso(), "ok": res["ok"], "partial": res["partial"], "error": res["error"],
            "pages": res["pages"], "count": len(ads)}
    head.update(extra)
    if not ads and os.path.exists(path):
        # a failed run never erases yesterday's listings: keep them and only update the status
        try:
            old = json.load(open(path, encoding="utf-8"))
            ads = old.get("ads", [])
            head["keptFrom"] = old.get("generatedAt")
        except Exception:  # noqa: BLE001
            pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(head, ensure_ascii=False)[:-1] + ',"ads":[\n')
        fh.write(",\n".join(json.dumps(a, ensure_ascii=False, sort_keys=True) for a in ads))
        fh.write("\n]}\n")
    return len(ads)


def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args), capture_output=True, text=True, timeout=180)


def push():
    if not os.path.isdir(os.path.join(REPO, ".git")):
        log("No encuentro el repositorio en %s: clónalo primero (ver instrucciones)." % REPO)
        return False
    git("pull", "--rebase", "--autostash")
    git("add", "feed")
    if not git("status", "--porcelain").stdout.strip():
        log("Sin cambios que subir.")
        return True
    c = git("commit", "-m", "Anuncios %s" % datetime.now(PANAMA).strftime("%Y-%m-%d %H:%M"))
    if c.returncode != 0:
        log("git commit falló: %s" % c.stderr.strip()[:300])
        return False
    p = git("push")
    if p.returncode != 0:
        log("git push falló: %s" % p.stderr.strip()[:300])
        return False
    log("Subido a GitHub.")
    return True


def main():
    if not os.environ.get("DISPLAY") and "--sin-pantalla" not in sys.argv and shutil.which("xvfb-run"):
        os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    from playwright.sync_api import sync_playwright
    log("Inicio")
    with sync_playwright() as p:
        kw = dict(user_data_dir=PROFILE, headless=not os.environ.get("DISPLAY"), locale="es-PA", timezone_id="America/Panama",
                  viewport={"width": 1366, "height": 860},
                  args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"])
        try:
            ctx = p.chromium.launch_persistent_context(channel="chromium", **kw)
        except Exception:  # noqa: BLE001
            ctx = p.chromium.launch_persistent_context(**kw)
        ctx.add_init_script(STEALTH)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        e24 = crawl_e24(page)
        human_pause(page, 15, 30)
        mit = crawl_mitula(page)
        ctx.close()
    n1 = write_feed("e24.json", e24, {"source": "e24", "total": e24["total"]})
    n2 = write_feed("mitula.json", mit, {"source": "mit", "total": mit["total"]})
    log("Encuentra24: %d anuncios%s · Mitula: %d anuncios%s" % (
        n1, " (" + e24["error"] + ")" if e24["error"] else "", n2, " (" + mit["error"] + ")" if mit["error"] else ""))
    if "--sin-git" not in sys.argv:
        push()
    log("Fin")


if __name__ == "__main__":
    main()
