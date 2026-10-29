"""Panama Real Estate, InmoPanamá and MLS ACOBIR readers."""
import html as H
import json
import re
from concurrent.futures import ThreadPoolExecutor

from net import fetch


def text(s):
    s = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = H.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v\xa0]+", " ", s)
    return re.sub(r"\n\s*", "\n", s).strip()


def one_line(s):
    return re.sub(r"\s+", " ", text(s)).strip()


def num(s):
    if s is None:
        return None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(s).replace("\xa0", " "))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return int(v) if v.is_integer() else v


def pages_until_empty(first_url, page_url, parse, max_pages=30, log=print):
    out, seen = [], set()
    for p in range(1, max_pages + 1):
        page = parse(fetch(first_url if p == 1 else page_url(p), referer=first_url))
        new = [c for c in page["cards"] if c["sid"] not in seen]
        for c in new:
            seen.add(c["sid"])
        out.extend(new)
        total = page.get("total")
        if not new or (total and len(out) >= total):
            break
    return out


def details(cards, parse_detail, workers=4):
    def one(c):
        try:
            c["detail"] = parse_detail(fetch(c["url"]))
        except Exception as e:  # noqa: BLE001
            c["detailError"] = str(e)
        return c
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, cards))


# ---------------------------------------------------------------- Panama Real Estate
PRE = "https://www.panama-real-estate.com"
PRE_SEARCHES = {
    "Apartamento": PRE + "/bienes-raices/en-alquiler/apartamentos/?max_price=800&parkings=1",
    "Casa": PRE + "/bienes-raices/en-alquiler/casas-unifamiliares/?max_price=800&parkings=1",
}


def pre_parse_search(page):
    tot = re.search(r"Mostrando\s*[\d,]+\s*[–-]\s*[\d,]+\s*de\s*([\d,]+)", one_line(page))
    cards = []
    for block in re.split(r'<div class="card overflow-hidden position-relative" data-listing-id="', page)[1:]:
        sid = block.split('"', 1)[0]
        href = re.search(r'href="(/propiedad/[^"]+-%s/)"' % sid, block)
        if not href:
            continue
        price = re.search(r'<h3 class="mb-0 font-weight-bold text-dark">\s*\$([\d,\.]+)', block)
        loc = re.search(r'<div class="location[^"]*">(.*?)</div>', block, re.S)
        title = re.search(r'<h3 class="font-weight-semibold2[^"]*">(.*?)</h3>', block, re.S)
        short = re.search(r'<p class="mb-0 leading-tight mt-0 text-muted lst-card-clamp-2">(.*?)</p>', block, re.S)
        feats = one_line(re.search(r'<p class="mt-2 mb-2">(.*?)</p>', block, re.S).group(1)) if '<p class="mt-2 mb-2">' in block else ""
        def feat(rx):
            m = re.search(rx, feats, re.I)
            return num(m.group(1)) if m else None
        photo = re.search(r'<img src="(/images/listings/%s/[^"]+)"' % sid, block)
        wa = re.search(r'data-wa-phone="(\d+)"', block)
        tag = re.search(r'<div class="bg-info tag-option">(.*?)</div>', block, re.S)
        cards.append({
            "sid": sid, "url": PRE + href.group(1), "price": num(price.group(1)) if price else None,
            "location": one_line(loc.group(1)) if loc else "", "title": one_line(title.group(1)) if title else "",
            "short": one_line(short.group(1)) if short else "",
            "beds": feat(r"([\d.]+)\s*Rec"), "baths": feat(r"([\d.]+)\s*Bañ"), "m2": feat(r"([\d.,]+)\s*m²"),
            "parking": feat(r"([\d.]+)\s*Estac"), "cover": PRE + photo.group(1) if photo else "",
            "phone": wa.group(1) if wa else "", "type": one_line(tag.group(1)) if tag else "",
        })
    return {"total": num(tot.group(1)) if tot else None, "cards": cards}


def pre_parse_detail(page):
    d = {}
    desc = re.search(r'<div class="description-body" id="descBody">(.*?)</div>', page, re.S)
    d["desc"] = one_line(desc.group(1)) if desc else ""
    boxes = dict((one_line(a).lower(), one_line(b)) for a, b in re.findall(
        r'<div class="lpb-label">(.*?)</div>\s*<div class="lpb-value[^"]*"[^>]*>(.*?)</div>', page, re.S))
    d["price"] = num(boxes.get("alquiler mensual") or boxes.get("precio"))
    d["beds"] = num(boxes.get("recámaras"))
    d["baths"] = num(boxes.get("baños"))
    d["m2"] = num(boxes.get("área total") or boxes.get("área"))
    d["parking"] = num(boxes.get("estacionamiento"))
    d["maint"] = num(boxes.get("mantenimiento") or boxes.get("cuota de mantenimiento"))
    crumbs = re.search(r'"@type":"BreadcrumbList","itemListElement":(\[.*?\])\}', page)
    if crumbs:
        try:
            names = [x.get("name", "") for x in json.loads(crumbs.group(1))]
            d["crumbs"] = names[3:-1]  # province, city, sector
        except ValueError:
            d["crumbs"] = []
    line = re.search(r'<p class="text-muted mb-0 fs-13">\s*<i class="fe fe-map-pin[^>]*></i>(.*?)&nbsp;', page, re.S)
    d["location"] = one_line(line.group(1)) if line else ""
    days = re.search(r"(\d+)\s+días en el mercado", page)
    d["daysOnMarket"] = int(days.group(1)) if days else None
    upd = re.search(r"Actualizado el\s+([^<]+)<", page)
    d["updated"] = upd.group(1).strip() if upd else ""
    name = re.search(r'<td class="font-weight-semibold px-0">Nombre</td>\s*<td class="px-0">(.*?)</td>', page, re.S)
    d["seller"] = one_line(name.group(1)) if name else ""
    ph = []
    for u in re.findall(r'(/images/listings/\d+/webp/[0-9a-z-]+?)(?:-\d+)?\.webp', page):
        if u not in ph:
            ph.append(u)
    d["photos"] = [PRE + u + ".webp" for u in ph[:12]]
    return d


def crawl_pre(log=print, known=frozenset(), with_details=True):
    out = []
    for kind, url in PRE_SEARCHES.items():
        cards = pages_until_empty(url, lambda p, u=url: u + "&page=%d" % p, pre_parse_search)
        for c in cards:
            c["kind"] = kind
        log(f"pre {kind}: {len(cards)}")
        out.extend(cards)
    if not with_details:
        return out
    return details([c for c in out if "pre-" + c["sid"] not in known], pre_parse_detail) + \
        [c for c in out if "pre-" + c["sid"] in known]


# ---------------------------------------------------------------- InmoPanamá
INMO = "https://www.inmopanama.com"
INMO_SEARCHES = {
    "Apartamento": INMO + "/alquiler-apartamentos-panama?f_price_max=800&f_bedrooms=1&f_bathrooms=1",
    "Casa": INMO + "/alquiler-casas-panama?f_price_max=800&f_bedrooms=1&f_bathrooms=1",
}


def inmo_parse_search(page):
    cards = []
    for block in page.split('class="property-boxarea ib-property-list-card"')[1:]:
        sid = re.search(r'data-imp-id="(\d+)"', block)
        href = re.search(r'href="(/[^"]+_p-(\d+)\.htm)"', block)
        if not (sid and href):
            continue
        title = re.search(r'class="ib-prop-title">(.*?)</a>', block, re.S)
        zone = re.search(r'<p class="ib-prop-zone">(.*?)</p>', block, re.S)
        typ = re.search(r'<p class="ib-prop-type">(.*?)</p>', block, re.S)
        feats = re.findall(r'<li>\s*<img [^>]*alt="([^"]+)"[^>]*>\s*(.*?)</li>', block, re.S)
        f = {a.lower(): one_line(b) for a, b in feats}
        price = re.search(r'<span class="ib-prop-price">(.*?)</span>', block, re.S)
        agent = re.search(r'<span class="ib-prop-agent-name">(.*?)</span>', block, re.S)
        photo = re.search(r'<img src="(/files/props/\d+/[^"]+)"', block)
        op = re.search(r'<li class="ib-badge-operacion"><a[^>]*>(.*?)</a>', block, re.S)
        cards.append({
            "sid": sid.group(1), "url": INMO + href.group(1), "title": one_line(title.group(1)) if title else "",
            "location": one_line(zone.group(1)) if zone else "", "type": one_line(typ.group(1)) if typ else "",
            "beds": num(f.get("camas")), "baths": num(f.get("baños")), "m2": num(f.get("metraje")),
            "price": num(one_line(price.group(1))) if price else None, "seller": one_line(agent.group(1)) if agent else "",
            "cover": INMO + photo.group(1) if photo else "", "op": one_line(op.group(1)) if op else "",
        })
    return {"total": None, "cards": cards}


def inmo_parse_detail(page):
    d = {}
    facts = {}
    for a, b in re.findall(r'<div class="nb-quick-fact-label">(.*?)</div>\s*<div class="nb-quick-fact-value">(.*?)</div>',
                           page, re.S):
        facts[one_line(a).lower()] = one_line(b)
    d["price"] = num(facts.get("precio"))
    d["beds"] = num(facts.get("habitaciones"))
    d["baths"] = num(facts.get("baños"))
    d["m2"] = num(facts.get("área")) if "consultar" not in (facts.get("área") or "").lower() else None
    d["maint"] = num(facts.get("mantenimiento")) if "consultar" not in (facts.get("mantenimiento") or "").lower() else None
    d["parking"] = num(facts.get("estacionamiento")) if "consultar" not in (facts.get("estacionamiento") or "consultar").lower() else None
    d["condition"] = facts.get("condición", "")
    det = {}
    for a, b in re.findall(r'<(?:div|span) class="nb-prop-detail-label">(.*?)</(?:div|span)>\s*<(?:div|span) class="nb-prop-detail-value">(.*?)</(?:div|span)>',
                           page, re.S):
        det[one_line(a).rstrip(":").lower()] = one_line(b)
    d["zone"] = det.get("zona", "")
    d["ph"] = det.get("ph o urbanización", "")
    pre = re.search(r"PRE ID:?\s*</?[^>]*>?\s*#?(\d+)", page) or re.search(r"PRE ID:\s*#(\d+)", one_line(page))
    d["preId"] = pre.group(1) if pre else ""
    full = re.search(r'<div class="nb-desc-full-content"[^>]*>(.*?)</div>', page, re.S)
    prev = re.search(r'<div class="nb-desc-preview"[^>]*>(.*?)</div>', page, re.S)
    d["desc"] = one_line((full or prev).group(1)) if (full or prev) else ""
    co = re.search(r'data-coords="(-?\d+\.\d+),\s*(-?\d+\.\d+)"', page)
    d["lat"], d["lng"] = (float(co.group(1)), float(co.group(2))) if co else (None, None)
    loc = re.search(r'<p class="nb-prop-location-line">(.*?)</p>', page, re.S)
    d["location"] = one_line(loc.group(1)) if loc else ""
    name = re.search(r'class="nb-agent-name"[^>]*>(.*?)</', page, re.S)
    d["seller"] = one_line(name.group(1)) if name else ""
    dates = {one_line(a).lower(): one_line(b) for a, b in re.findall(
        r'<span class="nb-prop-date-label">(.*?)</span>\s*<span class="nb-prop-date-value">(.*?)</span>', page, re.S)}
    d["dates"] = dates
    ph = []
    for u in re.findall(r'src="(/files/props/\d+/\d+-\d+\.jpg)"', page):
        if u not in ph:
            ph.append(u)
    d["photos"] = [INMO + u for u in ph[:12]]
    return d


def crawl_inmo(log=print, known=frozenset(), with_details=True):
    out = []
    for kind, url in INMO_SEARCHES.items():
        cards = pages_until_empty(url, lambda p, u=url: u + "&page=%d" % p, inmo_parse_search)
        for c in cards:
            c["kind"] = kind
        log(f"inmo {kind}: {len(cards)}")
        out.extend(cards)
    if not with_details:
        return out
    return details([c for c in out if "inmo-" + c["sid"] not in known], inmo_parse_detail) + \
        [c for c in out if "inmo-" + c["sid"] in known]


# ---------------------------------------------------------------- MLS ACOBIR
MLS = "https://www.mlsacobir.com"
MLS_SEARCH = (MLS + "/propiedades/?widget_id=3&kind=0&sf_unit_price=260&sf_min_price=0&sf_max_price=800"
              "&sf_select_listing=38&sf_multiple_property_type=62,63&wplview=property_listing&limit=100"
              "&wplorderby=p.add_date&wplorder=DESC&wplpagination=normal")


def mls_parse_search(page):
    t = one_line(page)
    tot = re.search(r"(\d+) resultados", t)
    cards = []
    for block in re.split(r'<article class="wpl_prp_cont', page)[1:]:
        sid = re.search(r'id="wpl_prp_cont(\d+)"', block)
        url = re.search(r'<meta itemprop="url" content="(https://www\.mlsacobir\.com/propiedades/[^"]+)"', block)
        if not (sid and url):
            continue
        price = re.search(r'<meta itemprop="price" content="(\d+(?:\.\d+)?)"', block)
        title = re.search(r'title="([^"]+)" target', block)
        img = re.search(r'src="(https://www\.mlsacobir\.com/wp-content/uploads/WPL/\d+/[^"]+)"', block)
        typ = re.search(r'<div class="wpl_prp_type">(.*?)</div>', block, re.S)
        lt = re.search(r'<div class="wpl_listing_type">(.*?)</div>', block, re.S)
        cards.append({"sid": sid.group(1), "url": url.group(1), "price": num(price.group(1)) if price else None,
                      "title": H.unescape(title.group(1)) if title else "", "cover": img.group(1) if img else "",
                      "type": one_line(typ.group(1)) if typ else "", "op": one_line(lt.group(1)) if lt else ""})
    return {"total": int(tot.group(1)) if tot else None, "cards": cards}


def mls_parse_detail(page):
    d = {}
    lt = re.search(r'"googlemap_lt"\s*:\s*"?(-?\d+\.\d+)', page)
    ln = re.search(r'"googlemap_ln"\s*:\s*"?(-?\d+\.\d+)', page)
    d["lat"] = round(float(lt.group(1)), 6) if lt else None
    d["lng"] = round(float(ln.group(1)), 6) if ln else None
    t = one_line(page)
    f = {}
    for m in re.finditer(r'<div id="wpl-dbst-show[^"]*"[^>]*>(.*?)</div>', page, re.S):
        inner = m.group(1)
        lab = re.search(r"<label>(.*?)</label>", inner, re.S)
        val = re.search(r"<span>(.*?)</span>", inner, re.S)
        key = one_line(lab.group(1) if lab else inner.split("<span>")[0]).rstrip(" :").strip().lower()
        if key and val:
            f[key] = one_line(val.group(1))
    d["fields"] = f
    d["neighborhood"] = f.get("neighborhood", "")
    d["district"] = f.get("district", "")
    d["province"] = f.get("provincia", "")
    d["corregimiento"] = f.get("corregimiento", "")
    d["feeIncludes"] = f.get("fee includes", "")
    d["amenities"] = ", ".join(x for x in (f.get("amenities"), f.get("internal features"), f.get("outdoor area"),
                                           f.get("servicios compartidos")) if x)
    d["services"] = f.get("other services", "")
    d["building"] = f.get("building name", "")
    desc = re.search(r'itemprop="description"[^>]*>(.*?)</div>\s*</div>', page, re.S)
    d["desc"] = one_line(desc.group(1)) if desc else ""
    d["price"] = num(f.get("precio de alquiler") or f.get("precio"))
    d["beds"] = num(f.get("habitaciones"))
    d["baths"] = num(f.get("baños"))
    d["parking"] = num(f.get("parking spaces") or f.get("estacionamientos"))
    d["m2"] = num(f.get("building size") or f.get("living area"))
    d["maint"] = num(f.get("cuota de mantenimiento") or f.get("hoa fee") or f.get("maintenance fee"))
    phone = re.search(r"\+\(507\)\s*([\d-]{7,9})", t)
    d["phone"] = "507" + phone.group(1).replace("-", "") if phone else ""
    ag = re.search(r"Descargar PDF Añadir a la lista Remove from list\s+(.*?)\s+\+\(507\)", t)
    d["seller"] = ag.group(1).strip() if ag else ""
    ph = []
    for u in re.findall(r"(https://www\.mlsacobir\.com/wp-content/uploads/WPL/\d+/[^\"' ]+?\.(?:webp|jpg|jpeg|png))", page):
        base = re.sub(r"_\d+x\d+(?=\.)", "", u)
        if "th" not in base.rsplit("/", 1)[-1][:2] and base not in ph:
            ph.append(base)
    d["photos"] = ph[:12]
    return d


def crawl_mls(log=print, known=frozenset(), with_details=True):
    first = mls_parse_search(fetch(MLS_SEARCH + "&wplpage=1"))
    cards = first["cards"]
    p = 2
    while first["total"] and len(cards) < first["total"] and p < 10:
        more = mls_parse_search(fetch(MLS_SEARCH + "&wplpage=%d" % p))["cards"]
        if not more:
            break
        cards += more
        p += 1
    for c in cards:
        c["kind"] = "Casa" if c["type"].lower().startswith("casa") else "Apartamento"
    log(f"mls: {len(cards)} (sitio dice {first['total']})")
    if not with_details:
        return cards
    return details([c for c in cards if "mls-" + c["sid"] not in known], mls_parse_detail, workers=2) + \
        [c for c in cards if "mls-" + c["sid"] in known]
