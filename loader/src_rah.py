"""Rent-A-House (rentahouse.com.pa) reader."""
import html as H
import re
from concurrent.futures import ThreadPoolExecutor

from net import fetch

BASE = "https://rentahouse.com.pa"
SEARCHES = {
    "Apartamento": BASE + "/apartamento_en_alquiler_en_panama_st.html?priceMin=0&priceMax={max}&m2Min=0&m2Max=0"
                          "&orderBy=entryTimestamp%20desc&country=panama&operationFromSlug=Alquiler"
                          "&propertyTypeFromSlug=Apartamento&countrySlug=panama",
    "Casa": BASE + "/casa_en_alquiler_en_panama_st.html?priceMin=0&priceMax={max}&m2Min=0&m2Max=0"
                   "&orderBy=entryTimestamp%20desc&country=panama&operationFromSlug=Alquiler"
                   "&propertyTypeFromSlug=Casa&countrySlug=panama",
}


def text(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s, flags=re.I)
    s = H.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*", "\n", s).strip()


def num(s):
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", s.replace("\xa0", " "))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return int(v) if v.is_integer() else v


def parse_search(page):
    """Cards on one results page."""
    total = re.search(r"Resultados:\s*(?:<[^>]+>\s*)*(\d+)", page)
    cards = []
    for block in re.split(r'<div class="card bg-light mb-3 property-list">', page)[1:]:
        m = re.search(r'href="(https://rentahouse\.com\.pa/[^"]+_rah-(\d+-\d+)\.html)"', block)
        if not m:
            continue
        url, code = m.group(1), m.group(2)
        def cell(cls):
            c = re.search(r'<td class="%s"[^>]*>(.*?)</td>' % cls, block, re.S)
            return num(text(c.group(1))) if c else None
        title = re.search(r'<h4 class="card-title">(.*?)</h4>', block, re.S)
        cover = re.search(r'data-src="(https://cdn\.photos\.sparkplatform\.com/[^"]+)"', block)
        photos = re.search(r'title="(\d+) photos"', block)
        days = re.search(r"(\d+)\s+d[ií]as en el mercado", block)
        price = re.search(r'<div class="price">\s*<strong>(.*?)</strong>', block, re.S)
        cards.append({
            "code": code, "url": url,
            "price": num(price.group(1)) if price else None,
            "beds": cell("dormitorio"), "baths": cell("baño"), "parking": cell("puesto"),
            "m2": cell("superficie"),
            "title": re.sub(r"\s+", " ", text(title.group(1))) if title else "",
            "cover": cover.group(1) if cover else "",
            "photoCount": int(photos.group(1)) if photos else None,
            "daysOnMarket": int(days.group(1)) if days else None,
        })
    pages = [int(x) for x in re.findall(r"[?&](?:amp;)?page=(\d+)", page)]
    return {"total": int(total.group(1)) if total else None, "cards": cards, "maxPage": max(pages or [1])}


def field(page, label):
    m = re.search(r'<span class="float-left">\s*%s\s*:?\s*</span>\s*<span class="float-right"[^>]*>(.*?)</span>'
                  % re.escape(label), page, re.S | re.I)
    return re.sub(r"\s+", " ", text(m.group(1))).strip() if m else None


def parse_detail(page):
    d = {}
    desc = re.search(r'<div class="propertyDescription">\s*<h2>[^<]*</h2>\s*<p>(.*?)</p>', page, re.S)
    d["desc"] = re.sub(r"\s+", " ", text(desc.group(1))).strip() if desc else ""
    h1 = re.search(r"<h1>(.*?)</h1>", page, re.S)
    d["h1"] = re.sub(r"\s+", " ", text(h1.group(1))) if h1 else ""
    price = re.search(r'<div class="DescripcionGeneral[^"]*">\s*<h2>[^<]*</h2>\s*<div class="price"[^>]*>\s*<strong>(.*?)</strong>',
                      page, re.S)
    d["price"] = num(price.group(1)) if price else None
    for key, label in (("tipo", "Tipo de Propiedad"), ("area", "Área Privada"), ("areaTerreno", "Área de Terreno"),
                       ("beds", "Dormitorios"), ("baths", "Total Baños"), ("parkingType", "Tipo de Estacionamiento"),
                       ("parking", "Puestos De Estacionamiento"), ("amoblado", "Amoblado"),
                       ("country", "País"), ("province", "Provincia"), ("city", "Ciudad"), ("sector", "Sector"),
                       ("maint", "Mantenimiento"), ("maint2", "Cuota de Mantenimiento"), ("estado", "Estado Del Inmueble")):
        d[key] = field(page, label)
    for k in ("area", "areaTerreno", "beds", "baths", "parking"):
        d[k] = num(d[k]) if d[k] else None
    lat = re.search(r'data-latitude="(-?\d+\.\d+)"', page)
    lng = re.search(r'data-longitude="(-?\d+\.\d+)"', page)
    d["lat"] = float(lat.group(1)) if lat else None
    d["lng"] = float(lng.group(1)) if lng else None
    d["amenities"] = [re.sub(r"^[✅\s#]+", "", text(x)).strip() for x in
                      re.findall(r'<ul class="property-detailes-list-min">(.*?)</ul>', page, re.S)
                      for x in re.findall(r"<li>(.*?)</li>", x, re.S)]
    d["amenities"] = [a for a in d["amenities"] if a and len(a) < 60]
    agent = re.search(r"Tu consulta llegará a\s*(.*?)\s*y a la oficina\s*(.*?)\s*,\s*a cargo", text(page), re.S)
    d["seller"] = (agent.group(1).strip() + " · " + agent.group(2).strip()) if agent else ""
    photos = []
    for u in re.findall(r"https://cdn\.resize\.sparkplatform\.com/pa/800x600/true/([0-9]+)-o\.jpg", page):
        if u not in photos:
            photos.append(u)
    d["photos"] = ["https://cdn.resize.sparkplatform.com/pa/800x600/true/%s-o.jpg" % p for p in photos[:12]]
    return d


def crawl(max_price=800, details=True, known=frozenset(), log=print, workers=4):
    """Yield normalized raw records for all rentals up to max_price (0 = no limit)."""
    out, seen = [], set()
    for kind, tpl in SEARCHES.items():
        url1 = tpl.format(max=max_price)
        first = parse_search(fetch(url1))
        pages = [first]
        for p in range(2, first["maxPage"] + 1 if first["total"] is None else (first["total"] + 11) // 12 + 1):
            pages.append(parse_search(fetch(url1 + "&page=%d" % p, referer=url1)))
        n = 0
        for pg in pages:
            for c in pg["cards"]:
                if c["code"] in seen:
                    continue
                seen.add(c["code"])
                c["kind"] = kind
                out.append(c)
                n += 1
        log(f"rah {kind}: {n} anuncios (sitio dice {first['total']})")
    if details:
        todo = [c for c in out if "rah-" + c["code"] not in known]

        def one(c):
            try:
                c["detail"] = parse_detail(fetch(c["url"]))
            except Exception as e:  # noqa: BLE001
                c["detailError"] = str(e)
            return c
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(one, todo))
    return out
