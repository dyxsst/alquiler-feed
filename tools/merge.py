"""Incorpora feed/norm.json (lo arma la computadora de Exan cada mañana) al tracker «Búsqueda de alquiler».
Nunca escribe en "state", nunca borra anuncios y no deja volver los eliminados para siempre.
Uso (siempre con python3 -I):
  merge.py prep DB                     parámetros de los ArtifactData list que exportan la base a DB
  merge.py plan NORM DB WORK           compara, prepara escrituras y fotos (imprime FOTOS i/n)
  merge.py finish WORK [--sin-fotos]   después de subir las fotos: imprime los lotes (LOTE i/n)
  merge.py summary WORK                una línea por fuente
"""
import collections, datetime as dt, glob, io, json, os, re, shutil, sys, time, unicodedata, urllib.request
from concurrent.futures import ThreadPoolExecutor

URL = "https://claude.ai/artifact/Eu7HHSCWf9o8ufUsz6j5Uz"
PAN = dt.timezone(dt.timedelta(hours=-5))
ORDER = ("rah", "pre", "inmo", "mls", "e24", "mit")
NAMES = {"rah": "Rent-A-House", "pre": "Panama Real Estate", "inmo": "InmoPanamá", "mls": "MLS ACOBIR",
         "e24": "Encuentra24", "mit": "Mitula"}
NOTE = {"mit": "solo publicados en los últimos 45 días"}
PHOTOS = {"rah": 3, "pre": 3, "inmo": 3, "mls": 3, "e24": 2, "mit": 1}
COVERS, STALE_H, MIT_DAYS = 120, 30, 45
REFRESH = ("url", "title", "kind", "price", "maint", "total", "services", "beds", "baths", "parking", "m2", "furnished",
           "desc", "location", "seller", "amenities", "photoSrc", "photoCount", "published", "sale", "sep", "flags", "fp",
           "source", "sourceKey", "sid")
TEXTY = ("desc", "fp", "services", "maint", "sale", "sep", "flags")
GEO = ("lat", "lng", "geoPrec", "geoFrom", "zone", "area", "corr", "distKm", "conflicts")
RANK = {"exacta": 5, "barrio": 4, "vía": 3, "corregimiento": 2, "distrito": 1, "": 0}
UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"


def jl(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def jd(o, p):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False)


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).lower()
    return re.sub(r"[^a-z0-9$]+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def canon(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: canon(x) for k, x in v.items()}
    if isinstance(v, list):
        return [canon(x) for x in v] or None
    return None if v == "" else v


def same(a, b):
    return json.dumps(canon(a), sort_keys=True) == json.dumps(canon(b), sort_keys=True)


def when(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=PAN)
    except ValueError:
        return None


def shouting(t):
    letters = [c for c in t or "" if c.isalpha()]
    return len(letters) >= 8 and sum(c.isupper() for c in letters) >= 0.7 * len(letters)


def scope(d, crit):
    """Por qué un anuncio nuevo no entra, o "" si entra."""
    ex = set(crit.get("excludeZones") or ["norte", "este", "oeste", "interior"])
    tz, td = set(d.get("titleZones") or []), set(d.get("titleDistricts") or [])
    if d.get("zone") in ex:
        if not (tz and not tz & ex):
            return "zona:" + d["zone"]
    elif d.get("zone") and d.get("geoFrom") != "anuncio" and tz and tz <= ex:
        return "zona:" + sorted(tz)[0]
    elif d.get("geoFrom") != "anuncio" and td & ex:
        return "zona:" + sorted(td & ex)[0]
    if d.get("price") is not None and d["price"] > float(crit.get("maxTotal") or 800) and d.get("sale") != "solo":
        return "precio"
    return ""


def shingles(t, n=4):
    w = norm(t).split()
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def groups(docs):
    """dupKey de los anuncios que parecen la misma propiedad (mismo texto, precio y recámaras)."""
    items = [(k, d, shingles((d.get("desc") or "")[:600])) for k, d in docs.items()]
    par = {k: k for k, _, _ in items}

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    by = collections.defaultdict(list)
    for k, d, sh in items:
        if d.get("price") is not None and len(sh) >= 6:
            by[int(d["price"] // 25)].append((k, d, sh))
    for b in sorted(by):
        for i, (a, da, sa) in enumerate(by[b]):
            for c, dc, sc in by[b][i + 1:] + by.get(b + 1, []):
                if abs(da["price"] - dc["price"]) > max(25, 0.05 * da["price"]):
                    continue
                if da.get("beds") is not None and dc.get("beds") is not None and da["beds"] != dc["beds"]:
                    continue
                x = len(sa & sc)
                if x and x / min(len(sa), len(sc)) >= 0.6:
                    par[find(a)] = find(c)
    g = collections.defaultdict(list)
    for k, _, _ in items:
        g[find(k)].append(k)
    return {k: "g-" + min(v) for v in g.values() if len(v) > 1 for k in v}


def moved(have, doc):
    """La ubicación cambió de verdad (no solo unos metros, que algunos sitios mueven el pin en cada visita)."""
    for k in ("geoPrec", "geoFrom", "zone", "area", "corr"):
        if k in doc and not same(doc[k], have.get(k)):
            return True
    for k in ("lat", "lng"):
        a, b = have.get(k), doc.get(k)
        if (a is None) != (b is None) or (a is not None and abs(a - b) >= 0.002):
            return True
    if "conflicts" in doc:
        key = lambda cs: sorted((c.get("kind"), c.get("name")) for c in cs or [])
        return key(doc["conflicts"]) != key(have.get("conflicts"))
    return False


def refresh(have, doc, today):
    """Campos de un anuncio que ya está en el tracker que la fuente ahora describe mejor."""
    p = {}
    for k in REFRESH:
        if k not in doc:
            continue
        v = doc[k]
        if v in (None, "", []) and have.get(k) not in (None, "", []):
            continue
        if k == "title":
            if not v:
                continue
            if norm(v) == norm(have.get("title")):
                if v != have.get("title") and shouting(have.get("title")):
                    p[k] = v
                continue
        if k in ("sale", "sep") and have.get("sep") == "venta" and not doc.get("sep"):
            continue
        if k == "flags":
            keep = [f for f in have.get("flags") or [] if not f.startswith(("Precio de $", "Mantenimiento por confirmar"))]
            v = list(dict.fromkeys(keep + list(v or [])))
        if not same(v, have.get(k)):
            p[k] = v
    if RANK.get(doc.get("geoPrec") or "", 0) >= RANK.get(have.get("geoPrec") or "", 0) and moved(have, doc):
        for k in GEO:
            if k in doc and not same(doc[k], have.get(k)):
                p[k] = doc[k]
    if doc.get("parking") is not None and "parkingMin" in have:
        p["parkingMin"] = {"__delete__": True}
    elif doc.get("parkingMin") and doc.get("parking") is None and have.get("parkingMin") != doc["parkingMin"]:
        p["parkingMin"] = doc["parkingMin"]
    a, b = have.get("price"), doc.get("price")
    if a is not None and b is not None and a != b:
        p["priceChange"] = {"from": a, "to": b, "at": today}
    return {} if set(p) <= {"published", "photoCount", "photoSrc"} else p


def rows():
    """Resultados de herramientas de esta sesión (el registro local de la conversación), en orden."""
    files, out = set(), []
    for r in {os.path.expanduser("~/.claude/projects"), "/root/.claude/projects"}:
        files |= set(glob.glob(os.path.join(r, "**", "*.jsonl"), recursive=True))
    for f in files:
        if time.time() - os.path.getmtime(f) > 36 * 3600:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "toolUseResult" in line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return sorted(out, key=lambda o: o.get("timestamp") or "")


def pending_pages(db):
    """Colecciones cuyo último ArtifactData list hacia DB todavía tenía más páginas: {colección: cursor}."""
    last = {}
    for o in rows():
        x = (o.get("toolUseResult") or {}).get("db_read") if isinstance(o.get("toolUseResult"), dict) else None
        if isinstance(x, dict) and x.get("op") == "list" and \
                os.path.abspath(str((x.get("saved") or {}).get("dir") or "")).startswith(os.path.abspath(db)):
            last[x.get("collection")] = x.get("next_cursor") or ""
    return {c: v for c, v in last.items() if v}


def transcript(since):
    """Ids de las fotos subidas y versiones de los documentos, leídos de los resultados de esta sesión."""
    s = when(since)
    su = s.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if s else ""
    assets, vers, done = {}, {}, set()
    for o in rows():
        t = o.get("toolUseResult")
        if not isinstance(t, dict):
            continue
        late = bool(su) and (o.get("timestamp") or "") >= su
        stack = [t]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                fp, i = x.get("file_path"), x.get("id")
                if late and isinstance(fp, str) and isinstance(i, str) and re.fullmatch(r"[0-9a-f]{32}", i) and \
                        x.get("status", "uploaded") == "uploaded":
                    assets[fp] = assets[os.path.basename(fp)] = i  # solo las subidas hechas después del plan
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
        for key in ("db_read", "db_write"):
            x = t.get(key)
            if not isinstance(x, dict):
                continue
            c, ent = x.get("collection"), []
            if c and x.get("doc_id") and isinstance(x.get("version"), int):
                ent.append((c, x["doc_id"], x["version"]))
            for d in x.get("docs") or []:
                if c and d.get("id") and isinstance(d.get("version"), int):
                    ent.append((c, d["id"], d["version"]))
            for d in (x.get("saved") or {}).get("files") or []:
                if d.get("id") and isinstance(d.get("version"), int):
                    ent.append((c or os.path.basename(os.path.dirname(d.get("path") or "")), d["id"], d["version"]))
            for d in x.get("results") or []:
                if d.get("collection") and d.get("doc_id") and isinstance(d.get("version"), int):
                    ent.append((d["collection"], d["doc_id"], d["version"]))
            for c2, i2, v2 in ent:
                vers[(c2, i2)] = v2
                if key == "db_write" and late and x.get("committed", True):
                    done.add((c2, i2))
    return assets, vers, done


def grab(jobs, fdir):
    from PIL import Image
    os.makedirs(fdir, exist_ok=True)

    def one(job):
        name, u = job
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})
            with urllib.request.urlopen(req, timeout=40) as r:
                im = Image.open(io.BytesIO(r.read())).convert("RGB")
            im.thumbnail((960, 960))
            im.save(os.path.join(fdir, name), "JPEG", quality=74, optimize=True, progressive=True)
            return name
        except Exception:  # noqa: BLE001
            return None
    with ThreadPoolExecutor(8) as ex:
        return {n for n in ex.map(one, jobs) if n}


def load(db):
    out = {}
    for c in ("listings", "tombstones", "sources", "meta"):
        m = {}
        for f in glob.glob(os.path.join(db, c, "*.json")):
            d = jl(f)
            if isinstance(d, dict):
                if set(d) >= {"id", "data"} and isinstance(d["data"], dict):
                    d = d["data"]
                m[os.path.basename(f)[:-5]] = d
        out[c] = m
    return out


def prep(db):
    db = os.path.abspath(db)
    print("Exporta la base (url %s) con ArtifactData action=list, una llamada por línea:" % URL)
    for c in ("listings", "tombstones", "sources", "meta"):
        print("  " + json.dumps({"collection": c, "out_dir": db, "query": {"limit": 1000}}))
    print("Si un resultado trae next_cursor, repite esa llamada con query.cursor hasta que no traiga más.")


def plan(normp, db, work):
    t0 = dt.datetime.now(PAN)
    today = t0.date().isoformat()
    for sub in ("f", "w"):
        shutil.rmtree(os.path.join(work, sub), ignore_errors=True)
    D = load(db)
    L, T, S, M = D["listings"], D["tombstones"], D["sources"], D["meta"]
    if not L:
        sys.exit("No hay export de listings en %s: haz primero los ArtifactData list de prep." % db)
    more = pending_pages(db)
    if more:
        sys.exit("Falta exportar páginas. Llama ArtifactData list con el mismo out_dir y estos cursores, y vuelve a "
                 "correr plan:\n" + "\n".join("  collection %s, query.cursor %s" % (c, v) for c, v in more.items()))
    crit = M.get("criteria") or {}
    ex = set(crit.get("excludeZones") or ["norte", "este", "oeste", "interior"])
    nj = jl(normp)
    if not isinstance(nj, dict) or "sources" not in nj:
        sys.exit("No pude leer %s." % normp)
    gen = nj.get("generatedAt") or ""
    g = when(gen)
    stale = not g or (t0 - g).total_seconds() / 3600 > STALE_H
    seen_before = all((S.get(k) or {}).get("feedAt") == gen for k in ORDER)
    tids = set(T)
    tfps = {x.get("fp") for x in T.values() if isinstance(x, dict) and x.get("fp")}
    stored = sum(len(d.get("photos") or []) for d in L.values())
    cap = 0 if stored >= 4600 else (1 if stored >= 3800 else 99)
    new, pat, jobs, pho, res, n = {}, {}, [], collections.defaultdict(list), {}, [0]

    def queue(lid, urls, k):
        k = min(k, cap) if lid in new else (1 if cap > 1 else 0)
        for u in (urls or [])[:k]:
            n[0] += 1
            jobs.append(("%d.jpg" % n[0], u))
            pho[lid].append("%d.jpg" % n[0])
    for key in ORDER:
        src = nj["sources"].get(key) or {"status": "error", "error": "no viene en el archivo"}
        st = {"status": src.get("status", "error"), "error": src.get("error") or "", "found": src.get("found", 0),
              "added": 0, "updated": 0, "inZone": 0, "skipped": collections.Counter(), "seen": src.get("seen") or [],
              "skip": {}}
        if stale:
            st["status"] = "stale"
            st["error"] = ("tu computadora no ha mandado datos nuevos desde %s: revisa que esté encendida y que su "
                           "tarea de las 5:30 corra" % (gen[:16].replace("T", " ") or "nunca"))
        docs = {} if (stale and seen_before) or src.get("status") == "error" else (src.get("docs") or {})
        for lid, doc in docs.items():
            sid = str(doc.get("sid") or lid)
            if lid in tids:
                st["skipped"]["eliminado"] += 1
                continue
            have = L.get(lid)
            if have:
                if have.get("manual"):
                    continue
                st["inZone"] += 0 if have.get("zone") in ex else 1
                if len(have.get("desc") or "") > len(doc.get("desc") or "") + 10:
                    doc = {k: v for k, v in doc.items() if k not in TEXTY + ("conflicts",)}
                p = refresh(have, doc, today)
                if p:
                    pat.setdefault(lid, {}).update(p)
                    st["updated"] += 1
                if not have.get("photos") and doc.get("photoSrc") and \
                        sum(len(v) for x, v in pho.items() if x not in new) < COVERS:
                    queue(lid, doc["photoSrc"], 1)
                continue
            if doc.get("fp") and doc["fp"] in tfps:
                st["skipped"]["eliminado"] += 1
                continue
            why = scope(doc, crit)
            if not why and key == "mit" and doc.get("published"):
                try:
                    if (t0.date() - dt.date.fromisoformat(doc["published"][:10])).days > MIT_DAYS:
                        why = "antiguo"
                except ValueError:
                    pass
            if why:
                st["skip"][sid] = why
                st["skipped"][why.split(":")[0]] += 1
                continue
            d = {k: v for k, v in doc.items() if k not in ("titleZones", "titleDistricts")}
            d.update(firstSeen=today, photos=[])
            new[lid] = d
            st["added"] += 1
            st["inZone"] += 1
            queue(lid, d.get("photoSrc"), PHOTOS[key])
        st["skipped"] = dict(st["skipped"])
        res[key] = st
    pool = {k: dict(d, **pat.get(k, {})) for k, d in L.items()}
    pool.update(new)
    mem = collections.defaultdict(list)
    for k, gk in groups(pool).items():
        mem[gk].append(k)
    for gk, ids in mem.items():
        if not any(i in new for i in ids):
            continue
        olds = collections.Counter(L[i].get("dupKey") for i in ids if i in L and L[i].get("dupKey"))
        key = olds.most_common(1)[0][0] if olds else gk
        for i in ids:
            if i in new:
                new[i]["dupKey"] = key
            elif L[i].get("dupKey") != key and not L[i].get("manual"):
                pat.setdefault(i, {})["dupKey"] = key
    got = grab(jobs, os.path.join(work, "f")) if jobs else set()
    photos = {k: [x for x in v if x in got] for k, v in pho.items()}
    photos = {k: v for k, v in photos.items() if v}
    files = [os.path.relpath(os.path.join(work, "f", x)) for k in sorted(photos) for x in photos[k]]
    ups = [files[i:i + 25] for i in range(0, len(files), 25)]
    jd(new, os.path.join(work, "new.json"))
    jd(pat, os.path.join(work, "patches.json"))
    jd({"at": t0.isoformat(timespec="seconds"), "today": today, "gen": gen, "res": res, "photos": photos,
        "stored": stored, "prev": S, "hasSync": "sync" in M, "placeholders": nj.get("placeholders")},
       os.path.join(work, "plan.json"))
    print("Plan del %s · datos de tu computadora del %s" % (t0.isoformat(timespec="seconds"), gen or "?"))
    for k in ORDER:
        r = res[k]
        print("  %-18s %-6s leídos %-4s en tus zonas %-4s nuevos %-4s actualizados %-4s %s" % (
            NAMES[k], r["status"], r["found"], r["inZone"], r["added"], r["updated"], r["error"]))
    for i, u in enumerate(ups, 1):
        print("FOTOS %d/%d: %s" % (i, len(ups), json.dumps(u)))
    if not ups:
        print("No hay fotos que subir.")


def finish(work, nofotos=False):
    pl = jl(os.path.join(work, "plan.json"))
    if not pl:
        sys.exit("Falta el plan: corre primero plan.")
    new = jl(os.path.join(work, "new.json"), {})
    pat = jl(os.path.join(work, "patches.json"), {})
    assets, vers, done = transcript(pl["at"])
    want, have = sum(len(v) for v in pl["photos"].values()), 0
    for lid, names in pl["photos"].items():
        ids = [assets.get(os.path.abspath(os.path.join(work, "f", x))) or
               assets.get(os.path.relpath(os.path.join(work, "f", x))) or assets.get(x) for x in names]
        ids = [i for i in ids if i]
        have += len(ids)
        if ids and lid in new:
            new[lid]["photos"] = ids
        elif ids:
            pat.setdefault(lid, {})["photos"] = ids
    if want and have < want * 0.5 and not nofotos:
        sys.exit("Solo encuentro %d de %d fotos subidas: haz las llamadas FOTOS del plan y vuelve a correr finish "
                 "(o finish --sin-fotos si alguna no se puede subir)." % (have, want))
    at, res, prev, n = pl["at"], pl["res"], pl["prev"], [0]

    def f(o):
        n[0] += 1
        p = os.path.abspath(os.path.join(work, "w", "%d.json" % n[0]))
        jd(o, p)
        return p
    writes, miss, late = [], [], 0
    for lid in sorted(new):
        writes.append({"op": "set", "collection": "listings", "doc_id": lid, "file_path": f(new[lid])})
    for lid in sorted(pat):
        v = vers.get(("listings", lid))
        if v:
            writes.append({"op": "update", "collection": "listings", "doc_id": lid, "file_path": f(pat[lid]),
                           "if_version": v})
        else:
            late += 1
    tail, tot = [], collections.Counter()
    for k in ORDER:
        r, p = res[k], prev.get(k) or {}
        d = dict(p, name=NAMES[k], lastRun=at, note=NOTE.get(k, ""), feedAt=pl["gen"])
        if r["status"] in ("ok", "parcial"):
            d.update(status=r["status"], error=r["error"] if r["status"] == "parcial" else "", lastOk=at,
                     found=r["found"], inZone=r["inZone"], added=r["added"], updated=r["updated"],
                     skipped=r["skipped"], skip=r["skip"],
                     seen=r["seen"] if r["status"] == "ok" else sorted(set(p.get("seen") or []) | set(r["seen"])))
        else:
            d.update(status="error", error=r["error"], added=r["added"], updated=r["updated"])
        if k == "rah" and pl.get("placeholders"):
            d["placeholders"] = pl["placeholders"]
        for x in ("added", "updated", "found"):
            tot[x] += r[x]
        e = {"op": "set", "collection": "sources", "doc_id": k, "file_path": f(d)}
        if k in prev:
            if not vers.get(("sources", k)):
                miss.append("sources/" + k)
                continue
            e["if_version"] = vers[("sources", k)]
        tail.append(e)
    sy = {"lastRun": at, "added": tot["added"], "updated": tot["updated"], "found": tot["found"],
          "sources": {k: res[k]["status"] for k in ORDER}, "photosStored": pl["stored"] + have}
    if sy["photosStored"] >= 3800:
        sy["note"] = "Queda poco espacio para fotos (%d de 5000): elimina definitivamente los descartados." % sy[
            "photosStored"]
    e = {"op": "set", "collection": "meta", "doc_id": "sync", "file_path": f(sy)}
    if pl["hasSync"] and not vers.get(("meta", "sync")):
        miss.append("meta/sync")
    else:
        if pl["hasSync"]:
            e["if_version"] = vers[("meta", "sync")]
        tail.append(e)
    skip = [x for x in writes + tail if (x["collection"], x["doc_id"]) in done]
    writes = [x for x in writes if (x["collection"], x["doc_id"]) not in done]
    tail = [x for x in tail if (x["collection"], x["doc_id"]) not in done]
    batches, cur, size = [], [], 0
    for x in writes:
        s = os.path.getsize(x["file_path"]) + 300
        if cur and (len(cur) >= 50 or size + s > 900000):
            batches.append(cur)
            cur, size = [], 0
        cur.append(x)
        size += s
    if cur:
        batches.append(cur)
    if tail:
        batches.append(tail)
    jd({"at": at, "res": {k: {x: res[k][x] for x in ("status", "error", "found", "inZone", "added", "updated")}
                          for k in ORDER}, "photos": have, "photosPlanned": want, "missing": miss},
       os.path.join(work, "summary.json"))
    if skip:
        print("Ya escritos en esta sesión (se omiten): %d" % len(skip))
    if late:
        print("Sin versión para %d anuncios: se actualizan en la próxima corrida." % late)
    if miss:
        print("AVISO: no encuentro la versión de %s. Haz ArtifactData get de esos documentos y vuelve a correr finish."
              % ", ".join(miss))
    print("Fotos: %d de %d subidas." % (have, want))
    print("ESCRIBIR %d lotes con ArtifactData action=batch, url=%s:" % (len(batches), URL))
    for i, b in enumerate(batches, 1):
        print("LOTE %d/%d: %s" % (i, len(batches), json.dumps(b, ensure_ascii=False, separators=(",", ":"))))


def summary(work):
    s = jl(os.path.join(work, "summary.json"))
    if not s:
        sys.exit("Sin resumen: corre plan y finish.")
    lab = {"ok": "bien", "parcial": "parcial", "error": "falló", "stale": "sin datos nuevos"}
    for k in ORDER:
        r = s["res"][k]
        print("%s: %s · leídos %s · en tus zonas %s · nuevos %s · actualizados %s%s" % (
            NAMES[k], lab.get(r["status"], r["status"]), r["found"], r["inZone"], r["added"], r["updated"],
            (" · " + r["error"]) if r["error"] else ""))
    print("Fotos subidas: %d de %d" % (s["photos"], s["photosPlanned"]))


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("prep", "plan", "finish", "summary"):
        sys.exit(__doc__)
    if a[0] == "prep":
        prep(a[1])
    elif a[0] == "plan":
        plan(a[1], a[2], a[3])
    elif a[0] == "finish":
        finish(a[1], "--sin-fotos" in a)
    else:
        summary(a[1])
