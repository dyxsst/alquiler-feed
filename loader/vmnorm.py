"""Builds feed/norm.json on the home computer.

After alquiler_vm.py reads Encuentra24 and Mitula with the browser, this module reads the four sites that answer
from anywhere (Rent-A-House, Panama Real Estate, InmoPanamá, MLS ACOBIR), normalizes the six sources with the same
rules the tracker uses (zone, distance to Villa Lucre, services, sale-only, location conflicts...) and writes one
file. The daily task in the cloud only merges that file into the tracker: it never runs code from this repository.

Each source is read on its own: if one fails, the file says so for that source and the others carry on.
Detail pages are cached for CACHE_DAYS (or until the price on the search page changes) so a daily run only opens
the listings that are new or changed.
"""
import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from net import fetch  # noqa: E402
from normalize import Normalizer  # noqa: E402
import src_others  # noqa: E402
import src_rah  # noqa: E402

PANAMA = dt.timezone(dt.timedelta(hours=-5))
CACHE_DAYS = 10
CLOUD = ("rah", "pre", "inmo", "mls")
MES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9, "set": 9,
       "oct": 10, "nov": 11, "dic": 12}
MOJI = re.compile(r"Ã|Â|â€")


def fix_text(v):
    """Undo UTF-8 read as Windows-1252/Latin-1 ("PanamÃ¡" -> "Panamá"); correct text is left alone."""
    if isinstance(v, str):
        if not MOJI.search(v):
            return v
        out = bytearray()
        for ch in v:
            try:
                out += ch.encode("cp1252")
            except UnicodeEncodeError:
                if ord(ch) >= 256:
                    return v
                out.append(ord(ch))
        try:
            return bytes(out).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(out).decode("utf-8", "ignore")
    if isinstance(v, list):
        return [fix_text(x) for x in v]
    if isinstance(v, dict):
        return {k: fix_text(x) for k, x in v.items()}
    return v


def age_days(pub, today):
    p = (pub or "").lower().strip()
    if p.startswith("hace"):
        n = 0
        for q, unit in re.findall(r"(\d+)\s+(minutos?|horas?|d[ií]as?|semanas?|mes(?:es)?)", p):
            n += int(q) * (30 if unit.startswith("mes") else 7 if unit.startswith("sem") else 1 if unit[0] == "d" else 0)
        return n
    m = re.match(r"(\d{1,2})\s+([a-z]+)\.?\s+(\d{4})", p)
    if m:
        mo = MES.get(m.group(2)) or MES.get(m.group(2)[:4]) or MES.get(m.group(2)[:3])
        if mo:
            try:
                return (today - dt.date(int(m.group(3)), mo, int(m.group(1)))).days
            except ValueError:
                return None
    return None


def reason(e):
    s = str(getattr(e, "reason", "") or e) or type(e).__name__
    return re.sub(r"\s+", " ", s)[:240]


PLURAL = {"Apartamento": "apartamentos", "Casa": "casas"}


def shortfall(f, share=0.8):
    """Encuentra24: the browser sometimes stops paging early and still reports success. Compare what arrived with the
    totals the site shows for each search; "" when the read looks complete."""
    tot = f.get("total")
    if not isinstance(tot, dict):
        return ""
    got = {}
    for a in f.get("ads") or []:
        got[a.get("type")] = got.get(a.get("type"), 0) + 1
    short = ["%d de %d %s" % (got.get(k, 0), n, PLURAL.get(k, k)) for k, n in tot.items()
             if isinstance(n, int) and n >= 20 and got.get(k, 0) < share * n]
    return ("Lectura incompleta: llegaron " + " y ".join(short)) if short else ""


# ---------------------------------------------------------------- the four sites that answer from anywhere
CARDS = {
    "rah": lambda log: src_rah.crawl(max_price=800, details=False, log=log),
    "pre": lambda log: src_others.crawl_pre(log=log, with_details=False),
    "inmo": lambda log: src_others.crawl_inmo(log=log, with_details=False),
    "mls": lambda log: src_others.crawl_mls(log=log, with_details=False),
}
DETAIL = {"rah": src_rah.parse_detail, "pre": src_others.pre_parse_detail, "inmo": src_others.inmo_parse_detail,
          "mls": src_others.mls_parse_detail}


def sid_of(key, c):
    return str(c.get("code") if key == "rah" else c.get("sid"))


def read_cloud(key, cache, log):
    cards = CARDS[key](log)
    now = time.time()
    todo = []
    for c in cards:
        ent = cache.get(sid_of(key, c))
        fresh = ent and now - ent.get("at", 0) < CACHE_DAYS * 86400 and \
            (c.get("price") is None or ent.get("cardPrice") == c.get("price"))
        if fresh:
            c["detail"] = ent["detail"]
        else:
            todo.append(c)

    def one(c):
        try:
            c["detail"] = DETAIL[key](fetch(c["url"]))
            cache[sid_of(key, c)] = {"detail": c["detail"], "at": now, "cardPrice": c.get("price")}
        except Exception as e:  # noqa: BLE001
            c["detailError"] = reason(e)
        return c
    with ThreadPoolExecutor(2 if key == "mls" else 4) as ex:
        list(ex.map(one, todo))
    log("%s: %d anuncios, %d fichas nuevas o cambiadas" % (key, len(cards), len(todo)))
    return cards


def build(feed_dir, cache_path, log=print, today=None):
    """Read the four cloud sites, normalize the six sources and write feed_dir/norm.json."""
    today = today or dt.datetime.now(PANAMA).date()
    geo = json.load(open(os.path.join(HERE, "geo.json"), encoding="utf-8"))
    N = Normalizer(geo, {}, today=today.isoformat())
    try:
        cache = json.load(open(cache_path, encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    out = {"generatedAt": dt.datetime.now(PANAMA).isoformat(timespec="seconds"), "sources": {}}

    raw = {}
    for key in CLOUD:
        try:
            raw[key] = ("ok", read_cloud(key, cache.setdefault(key, {}), log), "")
        except Exception as e:  # noqa: BLE001
            raw[key] = ("error", [], reason(e))
            log("%s falló: %s" % (key, raw[key][2]))
    for key, fname in (("e24", "e24.json"), ("mit", "mitula.json")):
        try:
            f = json.load(open(os.path.join(feed_dir, fname), encoding="utf-8"))
        except (OSError, ValueError) as e:
            raw[key] = ("error", [], "no se pudo leer %s (%s)" % (fname, reason(e)))
            continue
        ads = f.get("ads") or []
        if f.get("keptFrom"):  # the browser read failed today; the file still has yesterday's listings
            raw[key] = ("error", [], f.get("error") or "la lectura falló")
        elif not f.get("ok") and not ads:
            raw[key] = ("error", [], f.get("error") or "la lectura falló")
        else:
            raw[key] = ("ok" if f.get("ok") else "parcial", ads, "" if f.get("ok") else (f.get("error") or ""))
            short = shortfall(f) if key == "e24" and f.get("ok") else ""
            if short:  # partial: the tracker adds what arrived and does not mark the rest as gone
                raw[key] = ("parcial", ads, short)
                log("e24: " + short)

    if raw["rah"][1]:
        N.learn_placeholders([c for c in raw["rah"][1] if c.get("detail")])
    for key in CLOUD + ("e24", "mit"):
        status, recs, err = raw[key]
        src = {"status": status, "error": err, "found": len(recs), "seen": [], "docs": {}, "noDetail": 0}
        for r in recs:
            sid = sid_of(key, r) if key in CLOUD else str(r.get("id"))
            src["seen"].append(sid)
            if key in CLOUD and not r.get("detail"):
                src["noDetail"] += 1
                continue
            try:
                lid, doc = getattr(N, key)(fix_text(r) if key == "e24" else r)
            except Exception as e:  # noqa: BLE001
                log("%s %s: no se pudo normalizar (%s)" % (key, sid, reason(e)))
                continue
            if key == "mit":
                ag = age_days(r.get("published"), today)
                if ag is not None:
                    doc["published"] = (today - dt.timedelta(days=ag)).isoformat()
            doc.pop("firstSeen", None)
            doc["photoSrc"] = (doc.get("photoSrc") or [])[:1 if key == "mit" else 3]  # what the tracker may upload
            src["docs"][lid] = doc
        out["sources"][key] = src
    out["placeholders"] = sorted(N.placeholders)
    tmp = os.path.join(feed_dir, "norm.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(feed_dir, "norm.json"))
    with open(cache_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False)
    os.replace(cache_path + ".tmp", cache_path)
    return {k: (v["status"], v["found"], len(v["docs"])) for k, v in out["sources"].items()}


if __name__ == "__main__":
    # manual run: python3 vmnorm.py <feed dir> <cache file>
    print(build(sys.argv[1], sys.argv[2]))
