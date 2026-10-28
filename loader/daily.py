#!/usr/bin/env python3
"""Daily load for the «Búsqueda de alquiler» tracker.

Run from the session's working directory, with this repository cloned as ./alq:

  python3 alq/loader/daily.py prep     # where to export the tracker's database (ArtifactData list ... out_dir)
  python3 alq/loader/daily.py plan     # read every source, compare with the tracker, prepare photos and writes
  python3 alq/loader/daily.py finish   # after the photo uploads: attach photo ids and print the database batches
  python3 alq/loader/daily.py summary  # one line per source, for the final message

What it guarantees:
  * it never writes the `state` collection (your statuses, notes, ratings, visits, overrides);
  * it never deletes a listing; a source that fails leaves its listings exactly as they were;
  * listings you deleted for good (tombstones) never come back, not even under another id with the same text.

Photo ids and document versions come from this session's own transcript (the results of the Artifact
asset uploads and of the ArtifactData reads), so the Artifact/ArtifactData calls must run in the same session.
"""
import collections
import datetime as dt
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # works under `python3 -I` too
from core import norm  # noqa: E402

ROOT = os.path.dirname(HERE)            # the clone (feed/ lives here)
WORK = os.path.join(ROOT, "work")
DB = os.path.join(ROOT, "db")
FEED = os.path.join(ROOT, "feed")
PANAMA = dt.timezone(dt.timedelta(hours=-5))
ARTIFACT = "https://claude.ai/artifact/Eu7HHSCWf9o8ufUsz6j5Uz"

CLOUD = ("rah", "pre", "inmo", "mls")
FEEDS = {"e24": "e24.json", "mit": "mitula.json"}
ORDER = CLOUD + tuple(FEEDS)
NAMES = {"e24": "Encuentra24", "rah": "Rent-A-House", "pre": "Panama Real Estate", "inmo": "InmoPanamá",
         "mls": "MLS ACOBIR", "mit": "Mitula"}
NOTES = {"e24": "", "mit": "solo publicados en los últimos 45 días"}  # the page already says «desde tu computadora»
PHOTOS_NEW = {"rah": 3, "pre": 3, "inmo": 3, "mls": 3, "e24": 2, "mit": 1}
ENRICH_PHOTO_BUDGET = 120     # covers for listings already in the tracker that still have no photo
ASSET_LIMIT = 5000            # files the artifact can store; your own uploads count too
ASSET_COVERS_ONLY = 3800      # past this many stored photos, new listings get only their cover
ASSET_STOP = 4600             # past this, no more photos (delete discarded listings to free space)
MIT_MAX_AGE_DAYS = 45
FEED_STALE_HOURS = 30
FETCH_TIMEOUT = 300           # seconds per cloud source (they run in parallel)
REFRESH = ("url", "title", "kind", "price", "maint", "total", "services", "beds", "baths", "parking", "m2", "furnished",
           "desc", "location", "seller", "amenities", "photoSrc", "photoCount", "published", "sale", "sep", "flags", "fp",
           "source", "sourceKey", "sid")
GEO_FIELDS = ("lat", "lng", "geoPrec", "geoFrom", "zone", "area", "corr", "distKm", "conflicts")
PREC_RANK = {"exacta": 5, "barrio": 4, "vía": 3, "corregimiento": 2, "distrito": 1, "": 0}
MES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "sept": 9, "set": 9,
       "oct": 10, "nov": 11, "dic": 12}


# ---------------------------------------------------------------- small helpers
def now():
    return dt.datetime.now(PANAMA)


def jload(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def jdump(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def canon(v):
    """Comparable form: 6 == 6.0, and None, "" and [] all mean «no value»."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v] or None
    return None if v == "" else v


def same(a, b):
    return json.dumps(canon(a), sort_keys=True, ensure_ascii=False) == json.dumps(canon(b), sort_keys=True,
                                                                                    ensure_ascii=False)


MOJI = re.compile(r"Ã|Â|â€")


def _raw_bytes(s):
    out = bytearray()
    for ch in s:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:
                out.append(ord(ch))
            else:
                return None  # a real non-Latin character: this text is not garbled
    return bytes(out)


def fix_text(v):
    """Undo UTF-8 read as Windows-1252/Latin-1 ("PanamÃ¡" -> "Panamá"), recursively; correct text is left alone."""
    if isinstance(v, str):
        if not MOJI.search(v):
            return v
        b = _raw_bytes(v)
        if b is None:
            return v
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("utf-8", "ignore")
    if isinstance(v, list):
        return [fix_text(x) for x in v]
    if isinstance(v, dict):
        return {k: fix_text(x) for k, x in v.items()}
    return v


def age_days(pub, today):
    """Days since a Mitula date: "Hace 3 días", "Hace 2 semanas", "16 sept 2026"."""
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


def safe_reason(e):
    s = str(e) or type(e).__name__
    return re.sub(r"\s+", " ", s)[:240]


# ---------------------------------------------------------------- transcript: photo ids and document versions
def transcript_lines(max_age_hours=36):
    roots = {os.path.expanduser("~/.claude/projects"), "/root/.claude/projects"}
    files = set()
    for r in roots:
        files |= set(glob.glob(os.path.join(r, "**", "*.jsonl"), recursive=True))
    cutoff = time.time() - max_age_hours * 3600
    for f in sorted((f for f in files if os.path.getmtime(f) >= cutoff), key=os.path.getmtime):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "toolUseResult" in line or "_blob/" in line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue


WRITTEN = set()
UP_RX = re.compile(r'"([^"\n]+?)" \([\d,.]+ (?:bytes|KB|MB|kB)[^)]*\) → "/_blob/([0-9a-f]{32})"')


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def from_transcript(since=None):
    """({uploaded path or basename: asset id}, {(collection, doc_id): version}), latest wins.
    Also records in WRITTEN which documents this session wrote after `since` (the plan), so a re-run of `finish`
    skips what is already in the tracker."""
    assets, versions = {}, {}
    WRITTEN.clear()
    since_utc = ""
    if since:
        try:
            since_utc = dt.datetime.fromisoformat(since).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            since_utc = ""
    rows = sorted(transcript_lines(), key=lambda o: o.get("timestamp") or "")
    for o in rows:
        after_plan = bool(since_utc) and (o.get("timestamp") or "") >= since_utc
        t = o.get("toolUseResult")
        if isinstance(t, dict):
            for d in walk(t):
                fp, aid = d.get("file_path"), d.get("id")
                if isinstance(fp, str) and isinstance(aid, str) and re.fullmatch(r"[0-9a-f]{32}", aid) and \
                        d.get("status", "uploaded") == "uploaded":
                    assets[fp] = aid
                    assets[os.path.basename(fp)] = aid
            for key in ("db_read", "db_write"):
                x = t.get(key)
                if not isinstance(x, dict):
                    continue
                coll = x.get("collection")
                if isinstance(x.get("version"), int) and x.get("doc_id") and coll:
                    versions[(coll, x["doc_id"])] = x["version"]
                    if key == "db_write" and after_plan and x.get("committed", True):
                        WRITTEN.add((coll, x["doc_id"]))
                if key == "db_write" and after_plan and x.get("committed", True):
                    for d in x.get("results") or []:
                        if d.get("collection") and d.get("doc_id") and isinstance(d.get("version"), int):
                            WRITTEN.add((d["collection"], d["doc_id"]))
                for d in x.get("docs") or []:
                    if coll and d.get("id") and isinstance(d.get("version"), int):
                        versions[(coll, d["id"])] = d["version"]
                for d in ((x.get("saved") or {}).get("files") or []):
                    path = d.get("path") or ""
                    if d.get("id") and isinstance(d.get("version"), int):
                        c = coll or os.path.basename(os.path.dirname(path))
                        versions[(c, d["id"])] = d["version"]
                for d in x.get("results") or []:
                    if d.get("collection") and d.get("doc_id") and isinstance(d.get("version"), int):
                        versions[(d["collection"], d["doc_id"])] = d["version"]
        # text form of an upload result, in case the structured one is missing
        msg = o.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                    for name, aid in UP_RX.findall(txt or ""):
                        assets.setdefault(name, aid)
                        assets.setdefault(os.path.basename(name), aid)
    return assets, versions


# ---------------------------------------------------------------- the tracker's database, as exported
def load_db():
    out = {}
    for coll in ("listings", "tombstones", "sources", "meta"):
        docs = {}
        for f in glob.glob(os.path.join(DB, coll, "*.json")):
            d = jload(f)
            if isinstance(d, dict):
                if set(d) >= {"id", "data"} and isinstance(d.get("data"), dict):
                    d = d["data"]
                docs[os.path.basename(f)[:-5]] = d
        out[coll] = docs
    return out


# ---------------------------------------------------------------- cloud readers (each in its own process)
def cmd_fetch(key, out_path, known_path):
    known = frozenset(jload(known_path, []) or [])
    logs = []
    log = logs.append
    if key == "rah":
        import src_rah
        recs = src_rah.crawl(max_price=800, details=True, known=known, log=log)
    else:
        import src_others
        recs = {"pre": src_others.crawl_pre, "inmo": src_others.crawl_inmo, "mls": src_others.crawl_mls}[key](
            log=log, known=known)
    jdump({"records": recs, "log": logs}, out_path)


def fetch_cloud(key, known):
    raw = os.path.join(WORK, "raw", key + ".json")
    kn = os.path.join(WORK, "raw", key + ".known.json")
    jdump(sorted(known), kn)
    if os.path.exists(raw):
        os.remove(raw)
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "fetch", key, raw, kn], capture_output=True,
                           text=True, timeout=FETCH_TIMEOUT, cwd=HERE)
    except subprocess.TimeoutExpired:
        raise RuntimeError("el sitio tardó demasiado en responder (más de %d min)" % (FETCH_TIMEOUT // 60))
    if p.returncode != 0:
        tail = [x for x in (p.stderr or "").strip().splitlines() if x.strip()]
        msg = tail[-1] if tail else "error desconocido"
        m = re.search(r"FetchError: (.*)", msg)
        raise RuntimeError(m.group(1) if m else msg[:240])
    return (jload(raw) or {}).get("records") or []


# ---------------------------------------------------------------- photos
def download_photos(jobs):
    """jobs: [(name, url)] -> {name: path} for the ones that worked."""
    from net import fetch
    from PIL import Image
    os.makedirs(os.path.join(WORK, "f"), exist_ok=True)

    def one(job):
        name, url = job
        path = os.path.join(WORK, "f", name)
        if os.path.exists(path):
            return name, path
        try:
            im = Image.open(io.BytesIO(fetch(url, binary=True, timeout=40, retries=1))).convert("RGB")
            im.thumbnail((960, 960))
            im.save(path, "JPEG", quality=74, optimize=True, progressive=True)
            return name, path
        except Exception:  # noqa: BLE001
            return name, None
    with ThreadPoolExecutor(8) as ex:
        return {n: p for n, p in ex.map(one, jobs) if p}


# ---------------------------------------------------------------- plan
def cmd_plan():
    from normalize import Normalizer, group_duplicates
    import shutil
    t0 = now()
    today = t0.date()
    for sub in ("f", "w"):
        shutil.rmtree(os.path.join(WORK, sub), ignore_errors=True)  # photo names are per run
    os.makedirs(WORK, exist_ok=True)
    db = load_db()
    listings, tombs, sources, meta = db["listings"], db["tombstones"], db["sources"], db["meta"]
    if not listings:
        sys.exit("No encuentro el export de la base en %s/listings. Haz primero los ArtifactData list de `prep`." % DB)
    if len(listings) % 1000 == 0:
        print("AVISO: hay exactamente %d anuncios exportados; si el list trajo next_cursor, falta pedir la página "
              "siguiente (mismo out_dir) antes de seguir." % len(listings))
    criteria = meta.get("criteria") or {}
    geo = jload(os.path.join(HERE, "geo.json"))
    N = Normalizer(geo, criteria, today=today.isoformat())
    if sources.get("rah", {}).get("placeholders"):
        N.placeholders |= {tuple(p) for p in sources["rah"]["placeholders"]}
    excluded = set(criteria.get("excludeZones") or ["norte", "este", "oeste", "interior"])
    tomb_ids = set(tombs)
    tomb_fps = {t.get("fp") for t in tombs.values() if isinstance(t, dict) and t.get("fp")}
    crit_at = criteria.get("updatedAt") or ""

    result = {}
    new, patches, photo_jobs, photo_of = {}, {}, [], collections.defaultdict(list)
    counter = [0]
    stored = sum(len(d.get("photos") or []) for d in listings.values())
    photo_cap = 0 if stored >= ASSET_STOP else (1 if stored >= ASSET_COVERS_ONLY else 99)

    def queue_photos(lid, urls, limit):
        limit = min(limit, photo_cap)
        if photo_cap == 1 and lid not in new:
            limit = 0  # space is short: covers only for new listings
        for u in (urls or [])[:limit]:
            counter[0] += 1
            name = "%d.jpg" % counter[0]
            photo_jobs.append((name, u))
            photo_of[lid].append(name)

    # 1) read every source on its own; one failing never stops the others
    raw = {}
    with ThreadPoolExecutor(len(CLOUD)) as ex:
        futs = {}
        for key in CLOUD:
            prev = sources.get(key) or {}
            known = {lid for lid, d in listings.items() if d.get("sourceKey") == key} | \
                    {lid for lid in tomb_ids if lid.startswith(key + "-")}
            if (prev.get("lastOk") or "") >= crit_at:
                known |= {"%s-%s" % (key, s) for s in (prev.get("skip") or {})}
            futs[key] = ex.submit(fetch_cloud, key, known)
        for key, fu in futs.items():
            try:
                raw[key] = ("ok", fu.result(), "")
            except Exception as e:  # noqa: BLE001
                raw[key] = ("error", [], safe_reason(e))
    for key, fname in FEEDS.items():
        path = os.path.join(FEED, fname)
        feed = jload(path)
        if not isinstance(feed, dict):
            raw[key] = ("error", [], "no pude leer el archivo que manda tu computadora (%s)" % fname)
            continue
        gen = feed.get("generatedAt") or ""
        try:
            age_h = (t0 - dt.datetime.fromisoformat(gen)).total_seconds() / 3600
        except ValueError:
            age_h = 999
        ads = feed.get("ads") or []
        if key == "e24":
            ads = [fix_text(a) for a in ads]
        prev = sources.get(key) or {}
        if age_h > FEED_STALE_HOURS:
            when = gen[:16].replace("T", " ") or "nunca"
            msg = "tu computadora no ha mandado datos nuevos desde %s: revisa que esté encendida y que la tarea corra" % when
            raw[key] = ("stale", ads if gen != prev.get("feedAt") else [], msg, feed)
        elif not feed.get("ok") and not ads:
            raw[key] = ("error", [], feed.get("error") or "la lectura en tu computadora falló", feed)
        elif not feed.get("ok"):
            raw[key] = ("parcial", ads, feed.get("error") or "lectura incompleta", feed)
        else:
            raw[key] = ("ok", ads, "", feed)

    # 2) normalize and decide, source by source
    for key in ORDER:
        status, recs, err = raw[key][0], raw[key][1], raw[key][2]
        feed = raw[key][3] if len(raw[key]) > 3 else None
        prev = sources.get(key) or {}
        st = {"found": len(recs), "added": 0, "updated": 0, "inZone": 0, "skipped": collections.Counter(),
              "seen": [], "skip": {}}
        if key in CLOUD and (prev.get("lastOk") or "") >= crit_at:
            carried = dict(prev.get("skip") or {})
        else:
            carried = {}
        if key == "rah" and recs:
            N.learn_placeholders([r for r in recs if r.get("detail")])
        for r in recs:
            sid = str(r.get("code") if key == "rah" else (r.get("sid") if key in CLOUD else r.get("id")))
            lid = (key + "-" + sid) if key != "e24" else sid
            st["seen"].append(sid)
            if lid in tomb_ids:
                st["skipped"]["eliminado"] += 1
                continue
            have = listings.get(lid)
            if key in CLOUD and not r.get("detail"):
                if have:
                    st["inZone"] += 0 if have.get("zone") in excluded else 1
                elif sid in carried:
                    st["skip"][sid] = carried[sid]
                    st["skipped"][carried[sid].split(":")[0]] += 1
                else:
                    st["skipped"]["sin detalle"] += 1  # the detail page failed: retried tomorrow
                continue
            if have and have.get("manual"):
                continue
            try:
                if key == "e24" and have and len(have.get("desc") or "") > len(r.get("desc") or "") + 10:
                    r = dict(r, desc=have["desc"])
                _, doc = getattr(N, key)(r)
            except Exception as e:  # noqa: BLE001
                st["skipped"]["error al leer"] += 1
                print("  %s %s: no pude normalizar (%s)" % (key, sid, safe_reason(e)))
                continue
            doc["_tz"] = doc.pop("titleZones", None)
            doc["_td"] = doc.pop("titleDistricts", None)
            ag = age_days(r.get("published"), today) if key == "mit" else None
            if ag is not None:
                doc["published"] = (today - dt.timedelta(days=ag)).isoformat()
            if have:
                st["inZone"] += 0 if have.get("zone") in excluded else 1
                if key in FEEDS:
                    p = refresh_patch(have, doc, today)
                    if p:
                        patches.setdefault(lid, {}).update(p)
                        st["updated"] += 1
                    if not have.get("photos") and doc.get("photoSrc") and \
                            enrich_budget(photo_of, new) < ENRICH_PHOTO_BUDGET:
                        queue_photos(lid, doc["photoSrc"], 1)
                continue
            if doc.get("fp") and doc["fp"] in tomb_fps:
                st["skipped"]["eliminado"] += 1
                continue
            scope_doc = dict(doc, titleZones=doc.get("_tz") or [], titleDistricts=doc.get("_td") or [])
            why = N.scope(scope_doc)
            if not why and ag is not None and ag > MIT_MAX_AGE_DAYS:
                why = "antiguo"
            if why:
                st["skip"][sid] = why
                st["skipped"][why.split(":")[0]] += 1
                continue
            st["inZone"] += 1
            doc.pop("_tz", None)
            doc.pop("_td", None)
            doc["firstSeen"] = today.isoformat()
            doc["photos"] = []
            new[lid] = doc
            st["added"] += 1
            queue_photos(lid, doc.get("photoSrc"), PHOTOS_NEW[key])
        st["skipped"] = dict(st["skipped"])
        result[key] = {"status": status, "error": err, "feed": {k: feed.get(k) for k in ("generatedAt", "ok", "partial",
                       "error", "count", "total", "pages")} if feed else None, **st}

    for d in new.values():
        d.pop("_tz", None)
        d.pop("_td", None)

    # 3) duplicates: only groups that gained a new listing are touched
    pool = {lid: dict(d, **patches.get(lid, {})) for lid, d in listings.items()}
    pool.update(new)
    groups = group_duplicates(pool)
    members = collections.defaultdict(list)
    for lid, g in groups.items():
        members[g].append(lid)
    regrouped = 0
    for g, ids in members.items():
        if not any(i in new for i in ids):
            continue
        olds = collections.Counter(listings[i].get("dupKey") for i in ids if i in listings and listings[i].get("dupKey"))
        key = olds.most_common(1)[0][0] if olds else g
        for i in ids:
            if i in new:
                new[i]["dupKey"] = key
            elif listings[i].get("dupKey") != key and not listings[i].get("manual"):
                patches.setdefault(i, {})["dupKey"] = key
                regrouped += 1

    # 4) photos
    got = download_photos(photo_jobs) if photo_jobs else {}
    photos = {lid: [n for n in names if n in got] for lid, names in photo_of.items()}
    photos = {lid: v for lid, v in photos.items() if v}
    files = [os.path.relpath(os.path.join(WORK, "f", n)) for lid in sorted(photos) for n in photos[lid]]
    uploads = [files[i:i + 25] for i in range(0, len(files), 25)]

    plan = {"at": t0.isoformat(timespec="seconds"), "today": today.isoformat(), "result": result,
            "new": sorted(new), "patches": sorted(patches), "photos": photos, "uploads": uploads,
            "storedPhotos": stored, "photoCap": photo_cap,
            "placeholders": sorted(N.placeholders), "regrouped": regrouped, "prevSources": sources,
            "prevSync": meta.get("sync") or {}}
    jdump(new, os.path.join(WORK, "new.json"))
    jdump(patches, os.path.join(WORK, "patches.json"))
    jdump(plan, os.path.join(WORK, "plan.json"))

    print("Plan del %s" % plan["at"])
    for key in ORDER:
        r = result[key]
        print("  %-18s %-7s leídos %-4d en tus zonas %-4d nuevos %-4d actualizados %-4d %s" % (
            NAMES[key], r["status"], r["found"], r["inZone"], r["added"], r["updated"],
            ("· " + r["error"]) if r["error"] else ""))
    print("Duplicados reagrupados: %d" % regrouped)
    if uploads:
        print("\nSUBIR FOTOS: %d llamadas. Cada una: Artifact action=publish, url=%s, asset=true, file_paths=<la lista>:"
              % (len(uploads), ARTIFACT))
        for i, u in enumerate(uploads, 1):
            print("FOTOS %d/%d: %s" % (i, len(uploads), json.dumps(u)))
        print("\nDespués: python3 %s finish" % os.path.relpath(os.path.abspath(__file__)))
    else:
        print("\nNo hay fotos que subir. Sigue con: python3 %s finish" % os.path.relpath(os.path.abspath(__file__)))


def enrich_budget(photo_of, new):
    return sum(len(v) for lid, v in photo_of.items() if lid not in new)


def refresh_patch(have, doc, today):
    """Fields of a listing already in the tracker that the source now describes better."""
    p = {}
    for k in REFRESH:
        if k not in doc:
            continue
        v = doc[k]
        if v in (None, "", []) and have.get(k) not in (None, "", []):
            continue  # the source dropped a value: keep what we had
        if k == "title" and (not v or norm(v) == norm(have.get("title"))):
            continue  # same words, only capitals or punctuation changed
        if k in ("sale", "sep") and have.get("sep") == "venta" and not doc.get("sep"):
            continue  # never take a listing out of «Separados» on our own
        if k == "flags":
            # keep older warnings, except the ones that depend on a price or fee that was just re-read
            keep = [f for f in (have.get("flags") or []) if not f.startswith(("Precio de $", "Mantenimiento por confirmar"))]
            v = list(dict.fromkeys(keep + list(v or [])))
        if not same(v, have.get(k)):
            p[k] = v
    if PREC_RANK.get(doc.get("geoPrec") or "", 0) >= PREC_RANK.get(have.get("geoPrec") or "", 0):
        for k in GEO_FIELDS:
            if k in doc and not same(doc[k], have.get(k)):
                p[k] = doc[k]
    if doc.get("parking") is not None and "parkingMin" in have:
        p["parkingMin"] = {"__delete__": True}
    elif doc.get("parkingMin") and doc.get("parking") is None and have.get("parkingMin") != doc["parkingMin"]:
        p["parkingMin"] = doc["parkingMin"]
    old_price, new_price = have.get("price"), doc.get("price")
    if old_price is not None and new_price is not None and old_price != new_price:
        p["priceChange"] = {"from": old_price, "to": new_price, "at": today.isoformat()}
    if set(p) <= {"published", "photoCount", "photoSrc"}:
        return {}  # nothing a person would notice
    return p


# ---------------------------------------------------------------- finish
def cmd_finish(allow_missing_photos=False):
    plan = jload(os.path.join(WORK, "plan.json"))
    if not plan:
        sys.exit("Falta el plan: corre primero `plan`.")
    new = jload(os.path.join(WORK, "new.json"), {})
    patches = jload(os.path.join(WORK, "patches.json"), {})
    assets, versions = from_transcript(plan["at"])
    planned = sum(len(v) for v in plan["photos"].values())
    found = 0
    for lid, names in plan["photos"].items():
        ids = []
        for n in names:
            full = os.path.join(WORK, "f", n)
            aid = assets.get(full) or assets.get(os.path.relpath(full)) or assets.get(n)
            if aid:
                ids.append(aid)
        found += len(ids)
        if not ids:
            continue
        if lid in new:
            new[lid]["photos"] = ids
        else:
            patches.setdefault(lid, {})["photos"] = ids
    if planned and found < planned * 0.5 and not allow_missing_photos:
        sys.exit("Solo encuentro %d de %d fotos subidas. Haz las llamadas FOTOS del plan (Artifact publish asset=true) "
                 "y vuelve a correr finish; para seguir sin fotos usa: finish --sin-fotos" % (found, planned))

    today = plan["today"]
    at = plan["at"]
    result = plan["result"]
    prev_sources = plan["prevSources"]
    writes, missing = [], []
    n = [0]

    def doc_file(data):
        n[0] += 1
        path = os.path.join(WORK, "w", "%d.json" % n[0])
        jdump(data, path)
        return path

    for lid in sorted(new):
        writes.append({"op": "set", "collection": "listings", "doc_id": lid, "file_path": doc_file(new[lid])})
    for lid in sorted(patches):
        v = versions.get(("listings", lid))
        if not v:
            missing.append("listings/" + lid)
            continue
        writes.append({"op": "update", "collection": "listings", "doc_id": lid, "file_path": doc_file(patches[lid]),
                       "if_version": v})

    tail = []
    totals = collections.Counter()
    for key in ORDER:
        r = result[key]
        prev = prev_sources.get(key) or {}
        doc = dict(prev)
        doc.update({"name": NAMES[key], "lastRun": at})
        if key in NOTES:
            doc["note"] = NOTES[key]
        ok_now = r["status"] in ("ok", "parcial")
        if ok_now:
            doc.update({"status": r["status"], "error": r["error"] if r["status"] == "parcial" else "", "lastOk": at,
                        "found": r["found"], "inZone": r["inZone"], "added": r["added"], "updated": r["updated"],
                        "skipped": r["skipped"], "seen": r["seen"], "skip": r["skip"]})
            if r["status"] == "parcial":
                doc["seen"] = sorted(set(prev.get("seen") or []) | set(r["seen"]))
        else:
            # failed or stale: keep what the last good read said; only the status changes
            doc.update({"status": "error", "error": r["error"], "added": r["added"], "updated": r["updated"]})
            if r["status"] == "stale" and r["added"]:
                doc["inZone"] = (prev.get("inZone") or 0) + r["added"]
        if r.get("feed"):
            doc["feedAt"] = r["feed"].get("generatedAt") or prev.get("feedAt", "")
        if key == "rah":
            doc["placeholders"] = plan["placeholders"]
        totals["added"] += r["added"]
        totals["updated"] += r["updated"]
        totals["found"] += r["found"]
        entry = {"op": "set", "collection": "sources", "doc_id": key, "file_path": doc_file(doc)}
        if key in prev_sources:
            v = versions.get(("sources", key))
            if not v:
                missing.append("sources/" + key)
                continue
            entry["if_version"] = v
        tail.append(entry)
    sync = {"lastRun": at, "added": totals["added"], "updated": totals["updated"], "found": totals["found"],
            "sources": {k: result[k]["status"] for k in ORDER}, "photosStored": plan.get("storedPhotos", 0) + found}
    if sync["photosStored"] >= ASSET_COVERS_ONLY:
        sync["note"] = ("Queda poco espacio para fotos (%d de %d): elimina definitivamente los descartados para liberar."
                        % (sync["photosStored"], ASSET_LIMIT))
    entry = {"op": "set", "collection": "meta", "doc_id": "sync", "file_path": doc_file(sync)}
    if plan.get("prevSync"):
        v = versions.get(("meta", "sync"))
        if v:
            entry["if_version"] = v
            tail.append(entry)
        else:
            missing.append("meta/sync")
    else:
        tail.append(entry)

    done = [e for e in writes + tail if (e["collection"], e["doc_id"]) in WRITTEN]
    writes = [e for e in writes if (e["collection"], e["doc_id"]) not in WRITTEN]
    tail = [e for e in tail if (e["collection"], e["doc_id"]) not in WRITTEN]
    for e in tail + writes:
        if e.get("if_version") and e["op"] == "set" and e["collection"] != "listings":
            e["if_version"] = versions.get((e["collection"], e["doc_id"]), e["if_version"])
    if done:
        print("Ya escritos en esta sesión (se omiten): %d" % len(done))
    batches, cur, size = [], [], 0
    for e in writes:
        s = os.path.getsize(e["file_path"]) + 300
        if cur and (len(cur) >= 50 or size + s > 900_000):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        batches.append(cur)
    if tail:
        batches.append(tail)  # sources and sync last, once the listings are in
    jdump(batches, os.path.join(WORK, "batches.json"))
    summary = {"at": at, "today": today, "result": {k: {x: result[k][x] for x in ("status", "error", "found", "inZone",
                                                                                  "added", "updated")} for k in ORDER},
               "photos": found, "photosPlanned": planned, "missingVersions": missing, "batches": len(batches)}
    jdump(summary, os.path.join(WORK, "summary.json"))
    if missing:
        print("AVISO: no encuentro la versión de %s; esas escrituras quedan para mañana." % ", ".join(missing[:8]))
        if any(not m.startswith("listings/") for m in missing):
            print("  (para no perderlas: haz ArtifactData get de esos documentos y vuelve a correr finish)")
    print("Fotos: %d de %d subidas." % (found, planned))
    print("ESCRIBIR: %d lotes. Cada uno: ArtifactData action=batch, url=%s, writes=<la lista>:" % (len(batches), ARTIFACT))
    for i, b in enumerate(batches, 1):
        print("LOTE %d/%d: %s" % (i, len(batches), json.dumps(b, ensure_ascii=False, separators=(",", ":"))))


def cmd_summary():
    s = jload(os.path.join(WORK, "summary.json"))
    if not s:
        sys.exit("Sin resumen: corre plan y finish.")
    for k in ORDER:
        r = s["result"][k]
        print("%s: %s · leídos %d · en tus zonas %d · nuevos %d · actualizados %d%s" % (
            NAMES[k], {"ok": "bien", "parcial": "parcial", "error": "falló", "stale": "sin datos nuevos"}.get(r["status"], r["status"]),
            r["found"], r["inZone"], r["added"], r["updated"], (" · " + r["error"]) if r["error"] else ""))
    print("Fotos subidas: %d de %d" % (s["photos"], s["photosPlanned"]))


def cmd_prep():
    print("Exporta la base del tracker (url %s) con estas llamadas ArtifactData action=list:" % ARTIFACT)
    for coll in ("listings", "tombstones", "sources", "meta"):
        print("  %s" % json.dumps({"collection": coll, "out_dir": DB, "query": {"limit": 1000}}))
    print("Si un resultado trae next_cursor, repite esa llamada con query.cursor hasta que no traiga más.")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "fetch":
        cmd_fetch(args[1], args[2], args[3])
    elif cmd == "prep":
        cmd_prep()
    elif cmd == "plan":
        cmd_plan()
    elif cmd == "finish":
        cmd_finish("--sin-fotos" in args)
    elif cmd == "summary":
        cmd_summary()
    else:
        sys.exit("Comando desconocido: %s" % cmd)


if __name__ == "__main__":
    main()
