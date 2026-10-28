"""Shared normalization: geography, services, sale detection and flags."""
import math
import re
import unicodedata

ZONE_LABEL = {"ok": "En tus zonas", "norte": "Panamá Norte", "este": "Panamá Este", "oeste": "Panamá Oeste",
              "interior": "Interior", "": "Sin ubicar"}
KIND_RANK = {"barrio": 5, "estacion": 5, "via": 3, "corregimiento": 2, "distrito": 1}
PREC_OF_KIND = {"barrio": "barrio", "estacion": "barrio", "via": "vía", "corregimiento": "corregimiento",
                "distrito": "distrito"}
# tolerance (km) between a listing's own map pin and the place its registry names
PIN_TOL = {"barrio": 2.5, "estacion": 2.5, "via": 4.0, "corregimiento": 4.5, "distrito": 8.0}
PROVINCES = r"cocle|chiriqui|herrera|los santos|veraguas|bocas del toro|bocas|colon|darien|panama oeste"


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "")).lower()
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9$]+", " ", s).strip()


def km(a_lat, a_lng, b_lat, b_lng):
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def in_ring(lat, lng, ring):
    inside, j = False, len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


EXTRA = (  # coarse names the gazetteer lacks: (name, zone, lat, lng, aliases)
    ("San Miguelito", "ok", 9.0420, -79.4950, []),
    ("Ciudad de Panamá", "", None, None, ["Ciudad de Panama", "Panama City", "Panamá Centro"]),
    ("Arraiján", "oeste", 8.95, -79.65, ["Arraijan"]),
    ("La Chorrera", "oeste", 8.88, -79.78, ["Chorrera"]),
    ("Taboga", "interior", 8.79, -79.55, ["Isla Taboga"]),
    ("Portobelo", "interior", 9.55, -79.65, []),
    ("Punta Barco", "oeste", 8.62, -79.93, []),
    ("Punta Chame", "oeste", 8.65, -79.70, []),
    ("Corona", "oeste", 8.50, -80.06, ["Playa Corona"]),
    ("Nueva Gorgona", "oeste", 8.55, -79.89, []),
    ("Santa Fe de Veraguas", "interior", 8.51, -81.08, ["Santa Fe, Veraguas"]),
)


class Geo:
    def __init__(self, geo, criteria=None):
        self.geo = geo
        self.ref = (criteria or {}).get("ref") or geo["ref"]
        self.corr = geo["corr"]
        overrides = (criteria or {}).get("zoneOverrides", {}) or {}
        self.corr_zone = {c["key"]: overrides.get(c["key"], c["z"]) for c in self.corr}
        self.corr_name = {c["key"]: c["n"] for c in self.corr}
        self.entries = []
        for e in geo["gaz"]:
            for nm in [e["name"]] + list(e.get("aliases", [])):
                self.entries.append((norm(nm), e))
        for c in self.corr:  # corregimientos as coarse entries
            e = {"name": c["n"], "corr": c["key"], "zone": c["z"], "lat": c["rp"][0],
                 "lng": c["rp"][1], "kind": "corregimiento", "district": c["d"]}
            for nm in {c["n"], c["key"], c["n"].replace("La Exposición o ", "")}:
                self.entries.append((norm(nm), e))
        for nm, z, la, lo, al in EXTRA:
            e = {"name": nm, "corr": None, "zone": z, "lat": la, "lng": lo, "kind": "distrito"}
            for a in [nm] + al:
                self.entries.append((norm(a), e))
        self.entries.sort(key=lambda t: -len(t[0]))

    def zone_of(self, e):
        if e.get("corr") and e["corr"] in self.corr_zone and e.get("kind") in ("corregimiento", "barrio", "estacion", "via"):
            return self.corr_zone[e["corr"]]
        return e.get("zone", "")

    # -- lookups
    def find(self, text):
        """Non-overlapping gazetteer hits, longest names first; (entry, start, end, normalized text)."""
        t = " " + norm(text) + " "
        used, out = [], []
        for key, e in self.entries:
            if not key or key not in t:
                continue
            start = 0
            while True:
                i = t.find(" " + key + " ", start)
                if i < 0:
                    break
                a, b = i + 1, i + 1 + len(key)
                if not any(a < ub and b > ua for ua, ub in used):
                    used.append((a, b))
                    out.append((e, a, b, t))
                start = i + 1
        out.sort(key=lambda x: x[1])
        return out

    def best_registry(self, parts):
        """Most specific gazetteer entry among registry parts (ordered general -> specific)."""
        best = None
        for part in reversed([p for p in parts if p]):
            hits = self.find(part)
            if hits:
                hit = max(hits, key=lambda h: (KIND_RANK.get(h[0]["kind"], 0), h[2] - h[1]))[0]
                if best is None or KIND_RANK.get(hit["kind"], 0) > KIND_RANK.get(best["kind"], 0):
                    best = hit
                if KIND_RANK.get(best["kind"], 0) >= 5:
                    break
        return best

    def corr_at(self, lat, lng):
        for c in self.corr:
            if any(in_ring(lat, lng, r) for r in c["r"]):
                return c
        return None

    def nearest_name(self, lat, lng):
        best, bd = None, 1e9
        for _, e in self.entries:
            if e.get("lat") is None or e["kind"] not in ("barrio", "estacion"):
                continue
            d = km(lat, lng, e["lat"], e["lng"])
            if d < bd:
                best, bd = e, d
        return (best["name"], bd) if best else (None, None)


PROX = re.compile(
    r"(cerca|cercan[oa]s?|pasos|minutos?|min|metros|mts|cuadras|km|kms|frente|proxim[oa]s?|junto|lado|acceso|"
    r"colinda\w*|contiguo|detras|saliendo|rumbo|hacia|camino|estacion|estaciones|metro|linea|mall|plaza|"
    r"hospital|clinica|colegio|escuela|universidad|supermercado|super|centro comercial|aeropuerto|terminal|"
    r"entrada|salida|antes|despues|desde|hasta|vecin[oa]s?|alrededores|via a|vista a|vista al|vista hacia|"
    r"conecta\w*|rapido|facil|comunica\w*|minuto|segundos|carretera|corredor|autopista|puente|parada|"
    r"iglesia|banco|restaurantes?|tiendas?|farmacias?|gasolineras?|via|avenida|ave|av|calle|boulevard|"
    r"(?:aeropuerto|hospital|colegio|universidad|mall|plaza|estacion|metro|clinica|escuela)\s+[a-z]{4,})"
    r"\s*(?:a|al|de|del|la|el|los|las|y|en|con|sur|norte|para)?\s*(?:la|el|los|las)?\s*$")
ASSERT = re.compile(r"(ubicad[oa]s?|situad[oa]s?|localizad[oa]s?|queda|en|sector|barriada|urbanizacion|urb|"
                    r"residencial|corregimiento|area de|zona de|ph|edificio|torre)\s*(?:de|del|la|el)?\s*$")
LIST_SEP = re.compile(r"^\s*(?:,|y|o|e|/|-)\s*(?:la|el|los|las)?\s*$")


def mentions(geo, text):
    """Places a text names, as (entry, assertive). Places used as reference points ("cerca de X",
    "a 5 min de X, Y y Z") are dropped; assertive marks "en X" / "ubicado en X" style claims."""
    out, prev_prox_end = [], None
    for e, a, b, t in geo.find(text):
        before = t[max(0, a - 40):a]
        prox = bool(PROX.search(before))
        if not prox and prev_prox_end is not None and LIST_SEP.match(t[prev_prox_end:a]):
            prox = True  # continuation of a list of reference points
        if prox:
            prev_prox_end = b
            continue
        prev_prox_end = None
        out.append((e, bool(ASSERT.search(before))))
    return out


SVC_KEYS = ("mant", "agua", "luz", "gas", "internet")
SVC_WORDS = {
    "mant": r"(?:cuota\s+de\s+)?(?:mantenimiento|mant|mmto|administracion)",
    "agua": r"agua(?!\s+caliente)",
    "luz": r"(?:luz|electricidad|energia\s+electrica)",
    "gas": r"gas",
    "internet": r"(?:internet|wifi|wi\s?fi|cable)",
}


def soft(s):
    """Lowercase, no accents, punctuation kept (sentences matter for 'incluye ... / no incluye ...')."""
    s = unicodedata.normalize("NFD", str(s or "")).lower()
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def _keys_in(span):
    return [k for k, w in SVC_WORDS.items() if re.search(r"\b" + w + r"\b", span)]


def services(text, fee_includes=""):
    """Which services the monthly payment covers: True / False / None (not said)."""
    t = " " + soft(text) + " "
    out = {k: None for k in SVC_KEYS}
    # conditional offers ("$890 con opción a todos los servicios incluidos") say nothing about this price
    t = re.sub(r"(?:con\s+)?opcion\s+(?:a|de)\s+(?:todos\s+)?(?:los\s+)?servicios\s+incluid\w*", " ", t)
    # 1) negatives, then blank them out so "no incluye luz" cannot read as "incluye luz"
    for m in list(re.finditer(r"\bno\s+(?:se\s+)?(?:incluye|incluyen|esta\s+incluid\w*|estan\s+incluid\w*)\b\s*:?([^.;!?]{0,90})", t)):
        span = m.group(1)
        span = re.split(r"\b(?:pero|solo|sí|si)\b", span)[0]
        keys = _keys_in(span)
        if keys:
            for k in keys:
                out[k] = False
        elif "servicio" in span:
            for k in ("agua", "luz", "gas", "internet"):
                out[k] = False
        t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]
    for k, w in SVC_WORDS.items():
        if re.search(r"\b" + w + r"\s+(?:no\s+(?:esta\s+)?incluid\w*|por\s+cuenta\s+del\s+(?:inquilino|arrendatario)|"
                     r"a\s+cargo\s+del\s+(?:inquilino|arrendatario)|aparte|adicional|separad\w*)", t):
            out[k] = False
    if re.search(r"(?:\bmas\b|\+)\s*(?:\$\s*\d+\s*(?:de\s+)?)?(?:mantenimiento|mant\b|mmto)", t):
        out["mant"] = False
    if re.search(r"(?:\bmas\b|\+)\s*servicios|servicios\s+(?:por\s+cuenta|a\s+cargo)\s+del\s+(?:inquilino|arrendatario)|"
                 r"servicios\s+(?:aparte|no\s+incluid\w*)", t):
        for k in ("agua", "luz", "gas", "internet"):
            if out[k] is None:
                out[k] = False
    # 2) positives: "incluye X, Y y Z" (same sentence), "X incluido", "todo incluido"
    for m in re.finditer(r"\b(?:incluye|incluyen|incluido|incluidos|incluida|incluidas|con)\b\s*:?([^.;!?]{0,80})", t):
        word = t[m.start():m.start() + 4]
        span = m.group(1)
        if word == "con " and not re.search(r"incluid", span):
            continue
        for k in _keys_in(span):
            if out[k] is None:
                out[k] = True
        if re.search(r"\b(?:los\s+)?servicios(?:\s+basicos)?\b", span) and not _keys_in(span):
            for k in ("agua", "luz"):
                if out[k] is None:
                    out[k] = True
    for k, w in SVC_WORDS.items():
        if re.search(r"\b" + w + r"(?:\s+y\s+\w+)?\s+(?:esta\s+|estan\s+)?incluid", t) and out[k] is None:
            out[k] = True
    if re.search(r"todo\s+incluido|all\s+inclusive", t):
        for k in ("agua", "luz"):
            if out[k] is None:
                out[k] = True
    f = soft(fee_includes)
    if f:
        if "mantenimiento" in f or "administracion" in f:
            out["mant"] = True
        if "agua" in f:
            out["agua"] = True
        if "electricidad" in f or "luz" in f:
            out["luz"] = True
        if re.search(r"\bgas\b", f):
            out["gas"] = True
        if "internet" in f or "cable" in f:
            out["internet"] = True
    return out


def maint_from_text(text):
    t = norm(text)
    m = re.search(r"(?:mantenimiento|cuota\s+de\s+mantenimiento|mmto|mant)\s+(?:de\s+|es\s+de\s+|:)?\s*\$\s*(\d{2,3})\b", t) or \
        re.search(r"\$\s*(\d{2,3})\s+(?:de\s+)?(?:mantenimiento|mmto|mant)\b", t) or \
        re.search(r"(?:mantenimiento|mmto)\s+(?:de\s+)?(\d{2,3})\s+(?:dolares|balboas|usd)", t)
    if m:
        v = int(m.group(1))
        return v if 5 < v < 1000 else None
    return None


RENT = re.compile(r"\b(alquil\w*|renta\w*|arriend\w*|arrend\w*|for rent|rent)\b")
SALE = re.compile(r"\b(se vende|vendo|venta|precio de venta|en venta|vende|for sale|sale)\b")
NONRES = re.compile(r"\b(bodega|bodegas|galera|galeras|local comercial|locales comerciales|oficina|oficinas|"
                    r"consultorio|terreno|lote|lotes|finca)\b")


def sale_status(title, desc, price):
    t, d = norm(title), norm(desc)
    both = t + " " + d
    if re.search(r"\b(solo|solamente|unicamente)\s+(?:para\s+)?venta\b|\bno\s+se\s+alquila\b|\bno\s+es\s+alquiler\b", both):
        return "solo"
    if price and price >= 15000:
        return "solo"
    if re.search(r"opcion\s+(?:a|de)\s+compra|alquiler\s+o\s+venta|venta\s+o\s+alquiler|alquiler\s+y\s+venta|venta\s+y\s+alquiler"
                 r"|se\s+vende\s+o\s+(?:se\s+)?alquila|se\s+alquila\s+o\s+(?:se\s+)?vende|vende\s+o\s+alquila|alquila\s+o\s+vende", both):
        return "mixto"
    if SALE.search(t) and not RENT.search(t):
        return "solo" if not RENT.search(d) else "mixto"
    if SALE.search(t) and RENT.search(t):
        return "mixto"
    return ""


def nonres(title):
    t = norm(title)
    if NONRES.search(t) and not re.search(r"\b(apartamento|apto|casa|vivienda|habitaciones?|recamaras?|estudio)\b", t):
        return True
    return False


def flags_for(doc):
    f = []
    t, d, n = norm(doc.get("title")), norm(doc.get("desc")), norm(doc.get("priceNote"))
    p = doc.get("price")
    if p is not None and p <= 10:
        f.append("Precio de $%s: no es el precio real" % p)
    if "noche" in n or re.search(r"\bpor\s+noche\b|\bla\s+noche\b|\bnoches?\b", t):
        f.append("Precio por noche")
    if re.search(r"fin\s+de\s+semana", n + " " + t):
        f.append("Alquiler de fin de semana")
    if re.search(r"\b(alquiler\s+de\s+habitacion\w*|alquilo\s+habitacion\w*|se\s+alquila\s+habitacion\w*|habitaciones?\s+privadas?|"
                 r"habitaciones?\s+amuebladas?|solo\s+damas?|cuarto\s+en\s+alquiler|alquiler\s+de\s+cuarto)\b", t):
        f.append("Es alquiler de habitación")
    if re.search(r"\bdesde\b", t) and "$" in (doc.get("title") or ""):
        f.append("Precio «desde»: varía según la unidad")
    if re.search(r"\b(ano\s+nuevo|semana\s+santa|vacacional|temporada|carnavales)\b", d + " " + t) and \
            "Alquiler de fin de semana" not in f:
        f.append("Parece alquiler vacacional")
    if re.search(r"\b(airbnb|por\s+dia|por\s+dias|diario)\b", t + " " + n):
        f.append("Parece alquiler por días")
    m = doc.get("maint")
    if m and p and m >= min(p, 1000):
        f.append("Mantenimiento por confirmar: la ficha dice $%s/mes" % m)
    return f


def placeholder_coords(records, key=lambda r: (r.get("lat"), r.get("lng")), label=lambda r: r.get("area")):
    """Centres of pins shared by 3+ listings with different neighbourhood names (an agency's default pin)."""
    cells = {}
    for r in records:
        la, lo = key(r)
        if la is None or lo is None:
            continue
        cells.setdefault((round(la, 3), round(lo, 3)), []).append((la, lo, norm(label(r) or "?")))
    out = set()
    for (a, b) in cells:
        pts = [p for da in (-0.001, 0, 0.001) for db in (-0.001, 0, 0.001)
               for p in cells.get((round(a + da, 3), round(b + db, 3)), [])]
        if len({p[2] for p in pts}) >= 3:
            out.add((round(sum(p[0] for p in pts) / len(pts), 4), round(sum(p[1] for p in pts) / len(pts), 4)))
    return out


def valid_coords(lat, lng, placeholders=frozenset()):
    if lat is None or lng is None or not (7.0 < lat < 10.0 and -83.5 < lng < -77.0):
        return False
    return not any(km(lat, lng, a, b) <= 0.35 for a, b in placeholders)


def locate(geo, reg_parts, title, desc, lat=None, lng=None, placeholders=frozenset(), province=""):
    """Position, precision, zone and conflicts for one listing.

    reg_parts: the site's own location fields, general -> specific.
    The registry is what the listing claims; the pin and the text are checked against it."""
    out = {"lat": None, "lng": None, "geoPrec": "", "geoFrom": "", "zone": "", "area": "", "corr": "",
           "conflicts": []}
    pv = norm(province)
    if pv and re.search(r"\b(%s)\b" % PROVINCES, pv):
        out["zone"] = "oeste" if "oeste" in pv else "interior"
    reg = geo.best_registry(reg_parts)
    pin_ok = valid_coords(lat, lng, placeholders)
    pin_corr = geo.corr_at(lat, lng) if pin_ok else None

    if reg:
        out["area"] = reg["name"]
        out["zone"] = out["zone"] or geo.zone_of(reg)
        if reg.get("corr"):
            out["corr"] = geo.corr_name.get(reg["corr"], "")

    if pin_ok:
        if reg and reg.get("lat") is not None:
            d = km(lat, lng, reg["lat"], reg["lng"])
            same_corr = pin_corr is not None and reg.get("corr") == pin_corr["key"]
            if d <= PIN_TOL.get(reg["kind"], 2.5) or (reg["kind"] in ("corregimiento", "distrito", "via") and same_corr):
                out.update(lat=lat, lng=lng, geoPrec="exacta", geoFrom="anuncio")
                if reg["kind"] in ("corregimiento", "distrito", "via"):
                    near, nd = geo.nearest_name(lat, lng)
                    if near and nd is not None and nd <= 1.2:
                        out["area"] = near
            else:
                near, _ = geo.nearest_name(lat, lng)
                zone_pin = geo.corr_zone.get(pin_corr["key"], "") if pin_corr else ""
                out["conflicts"].append({"kind": "mapa", "name": near or (pin_corr["n"] if pin_corr else "otro punto"),
                                         "km": round(d, 1), "lat": round(lat, 5), "lng": round(lng, 5), "zone": zone_pin})
        elif reg is None or reg.get("lat") is None:
            out.update(lat=lat, lng=lng, geoPrec="exacta", geoFrom="anuncio")
            if pin_corr:
                out["zone"] = out["zone"] or geo.corr_zone.get(pin_corr["key"], "")
                out["corr"] = pin_corr["n"]
            elif not out["zone"] and km(lat, lng, geo.ref["lat"], geo.ref["lng"]) > 30:
                out["zone"] = "oeste" if (lng < -79.6 and 8.3 < lat < 9.1) else "interior"
            near, nd = geo.nearest_name(lat, lng)
            out["area"] = (near if nd is not None and nd <= 1.5 else "") or out["area"] or (pin_corr["n"] if pin_corr else "")

    if out["lat"] is None and reg and reg.get("lat") is not None:
        out.update(lat=reg["lat"], lng=reg["lng"], geoPrec=PREC_OF_KIND.get(reg["kind"], "barrio"), geoFrom="registro")

    # text: refine a coarse position, then look for claims elsewhere
    tms = mentions(geo, title or "")
    out["titleZones"] = sorted({geo.zone_of(e) for e, _ in tms if geo.zone_of(e)})
    out["titleDistricts"] = sorted({geo.zone_of(e) for e, _ in tms if e["kind"] == "distrito" and geo.zone_of(e)})
    ms = [(e, a, True) for e, a in mentions(geo, title or "")] + \
         [(e, a, False) for e, a in mentions(geo, desc or "")]
    coarse = out["geoPrec"] in ("", "vía", "corregimiento", "distrito")
    if coarse:
        for e, _assertive, _in_title in ms:
            if e["kind"] not in ("barrio", "estacion") or e.get("lat") is None:
                continue
            if out["lat"] is not None and km(out["lat"], out["lng"], e["lat"], e["lng"]) > PIN_TOL.get(
                    {"vía": "via"}.get(out["geoPrec"], out["geoPrec"]) or "distrito", 8.0) + 1.0:
                continue
            out.update(lat=e["lat"], lng=e["lng"], geoPrec="barrio", geoFrom="descripción", area=e["name"])
            out["zone"] = geo.zone_of(e) if not out["zone"] or out["zone"] == "ok" else out["zone"]
            if e.get("corr"):
                out["corr"] = geo.corr_name.get(e["corr"], out["corr"])
            break
    seen = {norm(out["area"])}
    for e, assertive, in_title in ms:
        nm = norm(e["name"])
        if nm in seen:
            continue
        seen.add(nm)
        ez = geo.zone_of(e)
        where = "título" if in_title else "descripción"
        if e.get("lat") is not None and out["lat"] is not None and e["kind"] in ("barrio", "estacion"):
            d = km(out["lat"], out["lng"], e["lat"], e["lng"])
            if d > 3.0 and (assertive or in_title or ez != out["zone"]):
                out["conflicts"].append({"kind": where, "name": e["name"], "km": round(d, 1),
                                         "lat": e["lat"], "lng": e["lng"], "zone": ez})
        elif e["kind"] == "distrito" and ez and out["zone"] and ez != out["zone"] and ez != "ok" and (assertive or in_title):
            c = {"kind": where, "name": e["name"], "zone": ez}
            if e.get("lat") is not None:
                c.update(lat=e["lat"], lng=e["lng"])
            out["conflicts"].append(c)
    if out["lat"] is not None and out["geoFrom"] != "anuncio" and not out["corr"]:
        c = geo.corr_at(out["lat"], out["lng"])
        if c:
            out["corr"] = c["n"]
    out["conflicts"] = out["conflicts"][:4]
    return out
