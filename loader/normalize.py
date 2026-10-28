"""Turn each source's raw records into listing documents for the tracker."""
import re

from core import (Geo, flags_for, km, locate, maint_from_text, nonres, norm, placeholder_coords, sale_status,
                  services)

NAME = {"e24": "Encuentra24", "rah": "Rent-A-House", "pre": "Panama Real Estate", "inmo": "InmoPanamá",
        "mls": "MLS ACOBIR", "mit": "Mitula", "manual": "Manual"}
EXCLUDED_DEFAULT = ("norte", "este", "oeste", "interior")
# pins Rent-A-House agents leave on many unrelated listings (seen 2026-10-28)
KNOWN_PLACEHOLDERS = {(9.0281, -79.4508)}
MAX_DESC = 1800
MAX_PHOTOS = 8


def clean(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


SMALL_WORDS = {"de", "del", "la", "las", "el", "los", "en", "y", "a", "al", "con", "por", "para", "o", "e", "u", "sin",
               "su", "sus", "un", "una"}


def tidy_title(t):
    """Titles written in capitals ("ALQUILO CASA EN ARRAIJÁN") read as normal titles ("Alquilo Casa en Arraiján")."""
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 8 or sum(1 for c in letters if c.isupper()) < 0.7 * len(letters):
        return t
    out = []
    for i, w in enumerate(t.lower().split(" ")):
        core = re.sub(r"[^\w]", "", w)
        if i > 0 and core in SMALL_WORDS:
            out.append(w)
        elif any(ch.isdigit() for ch in w) or (core and not re.search(r"[aeiouáéíóúü]", core)):
            out.append(w.upper())  # 3R/1B, PH
        else:
            out.append(re.sub(r"[^\W\d_]", lambda m: m.group(0).upper(), w, count=1))
    return " ".join(out)


def phone_digits(p):
    d = re.sub(r"\D", "", str(p or ""))
    if len(d) in (7, 8):
        d = "507" + d
    return d if len(d) >= 10 else ""


def dedupe_seller(s):
    s = clean(s)
    w = s.split(" ")
    for k in range(min(6, len(w) // 2), 1, -1):
        if w[:k] == w[-k:]:
            return " ".join(w[:-k]).strip(" ·,")
    return s


def fingerprint(doc):
    base = norm(doc.get("desc") or doc.get("title"))
    return " ".join(base.split()[:30])


def shingles(text, n=4):
    w = norm(text).split()
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


class Normalizer:
    def __init__(self, geo_json, criteria=None, today=""):
        self.criteria = criteria or {}
        self.geo = Geo(geo_json, self.criteria)
        self.ref = self.geo.ref
        self.excluded = set(self.criteria.get("excludeZones", EXCLUDED_DEFAULT))
        self.max_price = float(self.criteria.get("maxTotal", 800))
        self.today = today
        self.placeholders = set(KNOWN_PLACEHOLDERS)

    def learn_placeholders(self, rah_records):
        recs = [{"lat": (r.get("detail") or {}).get("lat"), "lng": (r.get("detail") or {}).get("lng"),
                 "area": (r.get("detail") or {}).get("sector")} for r in rah_records]
        self.placeholders |= placeholder_coords(recs)

    # ---------------------------------------------------------------- common tail
    def finish(self, src, sid, base, reg_parts, lat=None, lng=None, province="", fee="", maint=None,
               parking_min=None):
        doc = {"source": NAME[src], "sourceKey": src, "sid": sid}
        doc.update(base)
        doc["title"] = tidy_title(clean(doc.get("title"))) or "Anuncio sin título"
        doc["desc"] = clean(doc.get("desc"))[:MAX_DESC]
        loc = locate(self.geo, reg_parts, doc["title"], doc["desc"], lat, lng, self.placeholders, province)
        doc.update(loc)
        if doc["lat"] is not None:
            doc["lat"], doc["lng"] = round(doc["lat"], 5), round(doc["lng"], 5)
            doc["distKm"] = round(km(self.ref["lat"], self.ref["lng"], doc["lat"], doc["lng"]), 1)
        else:
            doc["distKm"] = None
        text = doc["title"] + ". " + doc["desc"] + ". " + doc.get("priceNote", "")
        svc = services(text, fee)
        if maint is None:
            maint = maint_from_text(text)
        if maint is not None and maint <= 5:
            maint = None  # "$1" style placeholders
        doc["services"] = svc
        doc["maint"] = maint
        price = doc.get("price")
        if price is not None and maint and svc.get("mant") is not True:
            doc["total"] = price + maint
        else:
            doc["total"] = price
        doc["sale"] = sale_status(doc["title"], doc["desc"], price)
        doc["sep"] = "venta" if doc["sale"] == "solo" else ("no-vivienda" if nonres(doc["title"]) else "")
        fl = flags_for(doc)
        if doc["sale"] == "mixto":
            fl.append("También se vende u ofrece opción a compra")
        if doc["sep"] == "no-vivienda":
            fl.append("Parece bodega, local u oficina, no vivienda")
        doc["flags"] = fl
        if parking_min and doc.get("parking") is None:
            doc["parkingMin"] = parking_min
        doc["fp"] = fingerprint(doc)
        doc.setdefault("firstSeen", self.today)
        return doc

    def scope(self, doc):
        """Why a new listing should not be imported, or '' to import it."""
        tz = set(doc.get("titleZones") or [])
        if doc["zone"] in self.excluded:
            # keep it only when its own title places it, without doubt, in a zone you want
            if not (tz and not (tz & self.excluded)):
                return "zona:" + doc["zone"]
        elif doc["zone"] and doc.get("geoFrom") != "anuncio" and tz and tz <= self.excluded:
            return "zona:" + sorted(tz)[0]  # registry says one thing, the title only names excluded places
        elif doc.get("geoFrom") != "anuncio" and set(doc.get("titleDistricts") or []) & self.excluded:
            return "zona:" + sorted(set(doc["titleDistricts"]) & self.excluded)[0]  # "... en Chiriquí", "... Arraiján"
        if doc.get("price") is not None and doc["price"] > self.max_price and doc["sale"] != "solo":
            return "precio"
        return ""

    # ---------------------------------------------------------------- per source
    def rah(self, r):
        d = r.get("detail") or {}
        sid = r["code"]
        h1 = d.get("h1") or ""
        title = h1 if re.search(r"alquiler|venta|renta", h1, re.I) else (r.get("title") or h1)
        sector = d.get("sector")
        if not sector:
            m = re.search(r"_en_([a-z0-9-]+)_rah-", r["url"])
            sector = m.group(1).replace("-", " ") if m else ""
        base = {
            "url": r["url"], "title": title, "kind": r.get("kind") or d.get("tipo") or "Apartamento",
            "price": d.get("price") if d.get("price") is not None else r.get("price"), "priceNote": "",
            "beds": d.get("beds") if d.get("beds") is not None else r.get("beds"),
            "baths": d.get("baths") if d.get("baths") is not None else r.get("baths"),
            "parking": d.get("parking") if d.get("parking") is not None else r.get("parking"),
            "m2": d.get("area") or r.get("m2"),
            "furnished": {"Si": "Amoblado", "No": "Sin muebles", "Sin Muebles": "Sin muebles",
                          "Parcialmente": "Semi amoblado"}.get(d.get("amoblado"), d.get("amoblado") or ""),
            "desc": d.get("desc", ""),
            "location": ", ".join(x for x in (sector.title() if sector and not d.get("sector") else d.get("sector"),
                                              d.get("city"), d.get("province")) if x),
            "seller": d.get("seller", ""), "phone": "",
            "amenities": (d.get("amenities") or [])[:24],
            "photoSrc": (d.get("photos") or ([r["cover"]] if r.get("cover") else []))[:MAX_PHOTOS],
            "photoCount": r.get("photoCount") or len(d.get("photos") or []),
        }
        prov = d.get("province") or ""
        if norm(d.get("city")) in ("panama oeste", "la chorrera", "arraijan", "chame", "san carlos", "capira"):
            prov = "Panama Oeste"
        return "rah-" + sid, self.finish("rah", sid, base, [d.get("province"), d.get("city"), sector],
                                         d.get("lat"), d.get("lng"), province=prov)

    def pre(self, r):
        d = r.get("detail") or {}
        sid = r["sid"]
        crumbs = d.get("crumbs") or []
        photos = [u for u in (d.get("photos") or []) if "/listings/%s/" % sid in u] or ([r["cover"]] if r.get("cover") else [])
        base = {
            "url": r["url"], "title": r.get("title"), "kind": r.get("kind") or "Apartamento",
            "price": d.get("price") if d.get("price") is not None else r.get("price"), "priceNote": "",
            "beds": d.get("beds") if d.get("beds") is not None else r.get("beds"),
            "baths": d.get("baths") if d.get("baths") is not None else r.get("baths"),
            "parking": d.get("parking") if d.get("parking") is not None else r.get("parking"),
            "m2": d.get("m2") or r.get("m2"), "furnished": "",
            "desc": d.get("desc") or r.get("short", ""),
            "location": d.get("location") or r.get("location", ""),
            "seller": d.get("seller", ""), "phone": phone_digits(r.get("phone")),
            "amenities": [], "photoSrc": photos[:MAX_PHOTOS], "photoCount": len(photos),
            "published": d.get("updated", ""),
        }
        prov = crumbs[0] if crumbs and norm(crumbs[0]) != "panama" else ""
        return "pre-" + sid, self.finish("pre", sid, base, crumbs or [r.get("location")], province=prov,
                                         maint=d.get("maint"), parking_min=1)

    def inmo(self, r):
        d = r.get("detail") or {}
        sid = r["sid"]
        base = {
            "url": r["url"], "title": r.get("title"), "kind": r.get("kind") or "Apartamento",
            "price": d.get("price") if d.get("price") is not None else r.get("price"), "priceNote": "",
            "beds": d.get("beds") if d.get("beds") is not None else r.get("beds"),
            "baths": d.get("baths") if d.get("baths") is not None else r.get("baths"),
            "parking": d.get("parking"), "m2": d.get("m2") or r.get("m2"),
            "furnished": d.get("condition", ""),
            "desc": d.get("desc", ""), "location": r.get("location") or d.get("zone", ""),
            "seller": d.get("seller") or r.get("seller", ""), "phone": "",
            "amenities": [], "photoSrc": (d.get("photos") or ([r["cover"]] if r.get("cover") else []))[:MAX_PHOTOS],
            "photoCount": len(d.get("photos") or []),
            "published": next(iter((d.get("dates") or {}).values()), ""),
        }
        if d.get("preId"):
            base["xref"] = ["pre-" + str(d["preId"]).lstrip("#")]
        return "inmo-" + sid, self.finish("inmo", sid, base, [d.get("zone"), r.get("location")], d.get("lat"), d.get("lng"),
                                          maint=d.get("maint"))

    def mls(self, r):
        d = r.get("detail") or {}
        sid = r["sid"]
        base = {
            "url": r["url"], "title": r.get("title"), "kind": r.get("kind") or "Apartamento",
            "price": d.get("price") if d.get("price") is not None else r.get("price"), "priceNote": "",
            "beds": d.get("beds"), "baths": d.get("baths"), "parking": d.get("parking"), "m2": d.get("m2"),
            "furnished": "Amoblado" if "amoblado" in norm(d.get("amenities")) else "",
            "desc": d.get("desc", ""),
            "location": ", ".join(x for x in (d.get("neighborhood"), d.get("corregimiento"), d.get("district")) if x),
            "seller": dedupe_seller(d.get("seller")), "phone": phone_digits(d.get("phone")),
            "amenities": [a.strip() for a in (d.get("amenities") or "").split(",") if a.strip()][:24],
            "photoSrc": (d.get("photos") or [])[:MAX_PHOTOS], "photoCount": len(d.get("photos") or []),
        }
        prov = d.get("province") or ""
        return "mls-" + sid, self.finish("mls", sid, base,
                                         [d.get("province"), d.get("district"), d.get("corregimiento"), d.get("neighborhood")],
                                         d.get("lat"), d.get("lng"), province=prov if norm(prov) != "panama" else "",
                                         fee=d.get("feeIncludes", ""), maint=d.get("maint"))

    def e24(self, a):
        """A compact Encuentra24 record (src_e24.parse) from the home computer's feed."""
        sid = str(a["id"])
        parts = [p.strip() for p in (a.get("locality") or "").split(",") if p.strip()]
        kind = "Casa" if (norm(a.get("type")).startswith("casa") or "-alquiler-casas/" in (a.get("url") or "")) else "Apartamento"
        amen = list(a.get("amenities") or [])
        base = {
            "url": a.get("url", ""), "title": a.get("title", ""), "kind": kind,
            "price": a.get("price"), "priceNote": "",
            "beds": a.get("beds"), "baths": a.get("baths"), "parking": a.get("parking"), "m2": a.get("m2"),
            "furnished": "Amoblado" if "Amoblado" in amen else "",
            "desc": a.get("desc", ""), "location": a.get("locality", ""),
            "seller": clean(a.get("seller")), "phone": "", "amenities": amen,
            "photoSrc": (a.get("images") or [])[:MAX_PHOTOS], "photoCount": len(a.get("images") or []),
            "published": a.get("created", ""),
        }
        p0 = norm(parts[0]) if parts else ""
        prov = "Panama Oeste" if p0.startswith("panama oeste") else ("" if p0.startswith("panama") else (parts[0] if parts else ""))
        return sid, self.finish("e24", sid, base, parts, a.get("lat"), a.get("lng"), province=prov, maint=a.get("maint"))

    def mit(self, a):
        """A Mitula record from the home computer's feed (alquiler_vm.parse_mitula)."""
        sid = str(a["id"])
        parts = [p for p in (a.get("region"), a.get("city"), a.get("district"), a.get("neighbourhood")) if p]
        typ = norm(a.get("type"))
        kind = "Casa" if ("house" in typ or "residence" in typ or re.search(r"\bcasa\b", norm(a.get("title")))) else "Apartamento"
        amen = list(a.get("amenities") or [])
        base = {
            "url": a.get("url", ""), "title": a.get("title", ""), "kind": kind,
            "price": a.get("price"), "priceNote": "",
            "beds": a.get("beds") if a.get("beds") else None, "baths": a.get("baths"), "parking": a.get("parking"),
            "m2": a.get("m2"), "furnished": "Amoblado" if re.search(r"amoblad|amueblad", norm(a.get("desc"))) else "",
            "desc": a.get("desc", ""), "location": a.get("location", ""),
            "seller": clean(a.get("seller")), "phone": "", "amenities": amen[:24],
            "photoSrc": (a.get("images") or [])[:MAX_PHOTOS], "photoCount": len(a.get("images") or []),
            "published": a.get("published", ""),
        }
        reg = norm(a.get("region"))
        prov = "" if (not reg or reg in ("provincia de panama", "panama")) else ("Panama Oeste" if "oeste" in reg else a.get("region"))
        return "mit-" + sid, self.finish("mit", sid, base, parts, a.get("lat"), a.get("lng"), province=prov,
                                         parking_min=1 if (a.get("garage") and a.get("parking") is None) else None)

    def e24_existing(self, lid, l):
        """Recompute derived fields of a listing already in the tracker (from the old Encuentra24 loader)."""
        parts = [p.strip() for p in (l.get("location") or "").split(",") if p.strip()]
        if not parts and l.get("area"):
            parts = [p.strip() for p in l["area"].split("·")]
        m = re.search(r"\+\s*\$\s*([\d,]+)\s*mant", l.get("priceNote") or "")
        maint = int(m.group(1).replace(",", "")) if m else None
        base = {k: l.get(k) for k in ("url", "title", "kind", "price", "priceNote", "beds", "baths", "m2", "desc",
                                      "location", "seller", "firstSeen")}
        base["parking"] = l.get("parking")
        prov = parts[0] if parts and not norm(parts[0]).startswith("panama") else ""
        if parts and norm(parts[0]).startswith("panama oeste"):
            prov = "Panama Oeste"
        doc = self.finish("e24", str(lid), base, parts, province=prov, maint=maint, parking_min=1)
        old = [f for f in (l.get("flags") or []) if not f.startswith("Parece venta")]
        if any(f.startswith("Parece venta") for f in (l.get("flags") or [])) and not doc["sale"]:
            doc["sale"] = "solo"
            doc["sep"] = "venta"
        doc["flags"] = list(dict.fromkeys(old + doc["flags"]))
        return doc


def group_duplicates(docs):
    """dupKey shared by listings that look like the same property (same text, price, rooms)."""
    items = []
    for lid, d in docs.items():
        sh = shingles((d.get("desc") or "")[:600])
        items.append((lid, d, sh))
    parent = {lid: lid for lid, _, _ in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_price = {}
    for lid, d, sh in items:
        if d.get("price") is None or len(sh) < 6:
            continue
        by_price.setdefault(int(d["price"] // 25), []).append((lid, d, sh))
    keys = sorted(by_price)
    for k in keys:
        cand = by_price.get(k, []) + by_price.get(k + 1, [])
        for i, (a, da, sa) in enumerate(by_price[k]):
            for b, db, sb in cand:
                if a >= b and (b in [x[0] for x in by_price[k]]):
                    continue
                if a == b:
                    continue
                if abs(da["price"] - db["price"]) > max(25, 0.05 * da["price"]):
                    continue
                if da.get("beds") is not None and db.get("beds") is not None and da["beds"] != db["beds"]:
                    continue
                inter = len(sa & sb)
                if inter and inter / min(len(sa), len(sb)) >= 0.6:
                    parent[find(a)] = find(b)
    groups = {}
    for lid, _, _ in items:
        groups.setdefault(find(lid), []).append(lid)
    out = {}
    for root, ids in groups.items():
        if len(ids) > 1:
            key = "g-" + sorted(ids)[0]
            for i in ids:
                out[i] = key
    return out
