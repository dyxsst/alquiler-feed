"""HTTP helpers shared by the source readers."""
import hashlib
import os
import random
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Mobile Safari/537.36")
CACHE = os.environ.get("RENT_CACHE", "")


class FetchError(Exception):
    def __init__(self, url, reason, status=None):
        super().__init__(f"{reason} ({url})")
        self.url, self.reason, self.status = url, reason, status


def fetch(url, timeout=40, retries=2, binary=False, referer=None):
    """GET a URL; raise FetchError with a human-readable reason."""
    if CACHE and not binary:
        key = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".html")
        if os.path.exists(key):
            with open(key, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "es-PA,es;q=0.9,en;q=0.6",
            **({"Referer": referer} if referer else {}),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if binary:
                    return data
                text = data.decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
                if "Attention Required! | Cloudflare" in text or "cf-chl-" in text[:5000]:
                    raise FetchError(url, "el sitio bloqueó la lectura automática (Cloudflare)", resp.status)
                if CACHE:
                    os.makedirs(CACHE, exist_ok=True)
                    with open(key, "w", encoding="utf-8") as fh:
                        fh.write(text)
                return text
        except FetchError:
            raise
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(4000).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (403, 429) and "cloudflare" in body.lower():
                raise FetchError(url, "el sitio bloqueó la lectura automática (Cloudflare)", e.code)
            if e.code == 404:
                raise FetchError(url, "la página no existe (404)", 404)
            last = FetchError(url, f"el sitio respondió con error {e.code}", e.code)
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            if "403" in reason or "Tunnel" in reason or "CONNECT" in reason:
                raise FetchError(url, "el dominio no está permitido para la carga (revisa Configuración → Capacidades)")
            last = FetchError(url, f"no se pudo conectar ({reason[:80]})")
        except TimeoutError:
            last = FetchError(url, "el sitio tardó demasiado en responder")
        except Exception as e:  # noqa: BLE001
            last = FetchError(url, f"error al leer la página ({type(e).__name__})")
        time.sleep(1.5 + attempt * 2 + random.random())
    raise last
