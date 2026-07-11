"""
Book PDF Finder v2.0
Busca libros y papers en PDF descargables, verificando que el archivo
encontrado sea realmente la obra pedida (autor + título confirmados).

Mejoras v2.0:
  1. Normalización previa: CrossRef + OpenLibrary corrigen título/autor
     ("Taros" → "Yaros") y aportan el número de páginas esperado.
  2. Escalera de queries: 6 variantes por búsqueda, de estricta a laxa,
     hasta juntar suficientes candidatos.
  3. Verificación de identidad: descarga el PDF y confirma con pypdf que
     los metadatos / primeras páginas mencionan el título y el autor.

Dependencias:
    pip install googlesearch-python ddgs requests pypdf colorama
"""

import sys
import io

# Fuerza UTF-8 en Windows para evitar errores de codificación en la consola
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import re
import time
import unicodedata
from urllib.parse import urlparse

import requests
from colorama import init, Fore, Style

init(autoreset=True)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

PAYWALL_DOMAINS = {
    "scribd.com", "amazon.com", "amazon.es", "amazon.com.mx",
    "books.google.com", "play.google.com", "issuu.com",
    "jstor.org", "springer.com", "elsevier.com",
    "tandfonline.com", "wiley.com", "sagepub.com",
    "overdrive.com", "hoopla.com", "kobo.com", "barnesandnoble.com",
    "ebooks.com", "vitalsource.com", "chegg.com", "coursehero.com",
    "bookshare.org", "perlego.com", "everand.com",
    "igi-global.com", "irma-international.org",
}

PAYWALL_URL_PATTERNS = [
    r"/preview", r"/sample", r"/excerpt", r"/look-inside",
    r"[?&]preview=", r"[?&]sample=", r"/read-online",
    r"/kindle-edition", r"/hardcover", r"/paperback",
    r"checkout", r"/buy", r"/purchase", r"/subscribe",
]

FREE_LIBRARY_DOMAINS = {
    "archive.org", "gutenberg.org", "pdfdrive.com",
    "manybooks.net", "standardebooks.org", "openlibrary.org",
    "marxists.org", "intechopen.com", "doabooks.org",
    "oapen.org", "core.ac.uk", "semanticscholar.org",
    "ncbi.nlm.nih.gov", "freecomputerbooks.com",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

MIN_PDF_SIZE_BYTES   = 100_000      # 100 KB — filtra muestras/portadas
MAX_VERIFY_BYTES     = 30_000_000   # 30 MB — tope de descarga para verificar
REQUEST_TIMEOUT      = 12
ENOUGH_PDF_LIKE      = 6            # candidatos con pinta de PDF para frenar la escalera
MAX_TOTAL_CANDIDATES = 40           # tope duro de candidatos acumulados
VERIFY_PAGES_TO_READ = 6            # páginas iniciales donde buscar título/autor

# Palabras que no aportan al comparar títulos
_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o",
    "the", "a", "an", "of", "in", "and", "or", "for", "to", "on",
    "su", "sus", "con", "por", "para",
}


# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def _tokens(text: str) -> set[str]:
    """Tokens significativos: minúsculas, sin acentos, sin stopwords."""
    clean = _strip_accents(text.lower())
    words = re.findall(r"[a-z0-9]{3,}", clean)
    return {w for w in words if w not in _STOPWORDS}


def _clean_edition(title: str) -> str:
    """Quita sufijos de edición: '3ra Ed.', 'tercera edición', '2nd edition'…"""
    pattern = (
        r"[\s\-–—,\.]*("
        r"\d+\s*(ra|da|ta|va|a|era)?\.?\s*(ed|edicion|edición)\.?"
        r"|(primera|segunda|tercera|cuarta|quinta)\s+(ed|edicion|edición)\.?"
        r"|\d+(st|nd|rd|th)?\s*(ed|edition)\.?"
        r")\s*$"
    )
    return re.sub(pattern, "", title, flags=re.IGNORECASE).strip()


def _author_lastname(author: str) -> str:
    """Último apellido significativo del autor (ignora iniciales)."""
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", author)]
    return words[-1] if words else author.strip()


def _domain(url: str) -> str:
    try:
        parts = urlparse(url).netloc.lower().split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else urlparse(url).netloc.lower()
    except Exception:
        return ""


def _is_paywall_url(url: str) -> bool:
    domain = _domain(url)
    if any(pw in domain for pw in PAYWALL_DOMAINS):
        return True
    url_lower = url.lower()
    return any(re.search(pat, url_lower) for pat in PAYWALL_URL_PATTERNS)


def _is_free_library(url: str) -> bool:
    return any(lib in _domain(url) for lib in FREE_LIBRARY_DOMAINS)


def _looks_like_pdf_url(url: str) -> bool:
    url_lower = url.lower().split("?")[0]
    return url_lower.endswith(".pdf") or "/pdf/" in url_lower


# ---------------------------------------------------------------------------
# MEJORA 1 — Normalización previa (CrossRef + OpenLibrary)
# ---------------------------------------------------------------------------

def normalize_reference(title: str, author: str) -> dict:
    """
    Consulta OpenLibrary (libros) y CrossRef (papers) para corregir
    título/autor y obtener páginas esperadas.
    Devuelve: {title, author, expected_pages, source, corrected}
    """
    print(Fore.CYAN + "\n  Normalizando referencia (OpenLibrary + CrossRef)…")
    best = {
        "title": title, "author": author,
        "expected_pages": None, "source": None, "corrected": False,
    }
    query_tokens = _tokens(title)

    # --- OpenLibrary (libros) ---
    try:
        r = requests.get(
            "https://openlibrary.org/search.json",
            params={"title": title, "author": author, "limit": 3,
                    "fields": "title,author_name,number_of_pages_median"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            for doc in r.json().get("docs", []):
                ol_title = doc.get("title", "")
                overlap = len(query_tokens & _tokens(ol_title)) / max(len(query_tokens), 1)
                if overlap >= 0.5:
                    ol_author = (doc.get("author_name") or [author])[0]
                    best.update({
                        "title": ol_title, "author": ol_author,
                        "expected_pages": doc.get("number_of_pages_median"),
                        "source": "OpenLibrary",
                    })
                    break
    except Exception as e:
        print(Fore.YELLOW + f"  [!] OpenLibrary: {e}")

    # --- CrossRef (papers / capítulos; también corrige autores mal escritos) ---
    if best["source"] is None:
        try:
            r = requests.get(
                "https://api.crossref.org/works",
                params={"query": f"{title} {author}", "rows": 3,
                        "select": "DOI,title,author"},
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
            if r.status_code == 200:
                for item in r.json().get("message", {}).get("items", []):
                    cr_title = (item.get("title") or [""])[0]
                    overlap = len(query_tokens & _tokens(cr_title)) / max(len(query_tokens), 1)
                    if overlap >= 0.6:
                        authors = item.get("author", [])
                        cr_author = " ".join(
                            f"{a.get('given', '')} {a.get('family', '')}".strip()
                            for a in authors[:1]
                        ) or author
                        best.update({
                            "title": cr_title, "author": cr_author,
                            "source": f"CrossRef (DOI {item.get('DOI', '')})",
                        })
                        break
        except Exception as e:
            print(Fore.YELLOW + f"  [!] CrossRef: {e}")

    # ¿Hubo corrección real?
    if best["source"]:
        title_changed  = _tokens(best["title"])  != _tokens(title)
        author_changed = _tokens(best["author"]) != _tokens(author)
        best["corrected"] = title_changed or author_changed
        if best["corrected"]:
            print(Fore.GREEN + f"  ✓ Referencia corregida vía {best['source']}:")
            print(Fore.GREEN + f"    Título: {best['title']}")
            print(Fore.GREEN + f"    Autor : {best['author']}")
        else:
            print(Fore.CYAN + f"  Referencia confirmada vía {best['source']}")
        if best["expected_pages"]:
            print(Fore.CYAN + f"  Páginas esperadas: ~{best['expected_pages']}")
    else:
        print(Fore.YELLOW + "  Sin coincidencia en catálogos — se usa la referencia tal cual")

    return best


# ---------------------------------------------------------------------------
# MEJORA 2 — Escalera de queries
# ---------------------------------------------------------------------------

def build_query_ladder(title: str, author: str, canon: dict) -> list[str]:
    """
    Genera variantes de query de más estricta a más laxa.
    Combina la referencia original y la canónica (si difieren).
    """
    lastname = _author_lastname(author)
    base_title = _clean_edition(title)
    queries: list[str] = []

    def add(q: str):
        if q and q not in queries:
            queries.append(q)

    # 1. Estricta: título y autor entre comillas + filetype
    add(f'"{base_title}" "{author}" filetype:pdf')
    # 2. Canónica corregida (si difiere de la original)
    if canon.get("corrected"):
        c_title = _clean_edition(canon["title"])
        c_last = _author_lastname(canon["author"])
        add(f'"{c_title}" "{c_last}" filetype:pdf')
        add(f'{c_title} {c_last} pdf')
    # 3. Título entre comillas + apellido suelto
    add(f'"{base_title}" {lastname} filetype:pdf')
    # 4. Todo suelto, sin comillas (la que encontró a Cattaneo)
    add(f'{base_title} {lastname} pdf')
    # 5. Sin acentos
    plain = _strip_accents(f"{base_title} {lastname}")
    add(f"{plain} pdf")
    # 6. Apellido + palabras clave del título (por si el título exacto varía)
    key_words = " ".join(sorted(_tokens(base_title))[:4])
    add(f"{lastname} {key_words} pdf descargar")

    return queries


# ---------------------------------------------------------------------------
# Motores de búsqueda
# ---------------------------------------------------------------------------

def search_google(query: str, max_results: int = 10) -> list[str]:
    """Google via googlesearch-python. Suele estar bloqueado: tolerar fallo rápido."""
    try:
        from googlesearch import search as gsearch
    except ImportError:
        return []
    urls = []
    try:
        for url in gsearch(query, num_results=max_results, sleep_interval=1, lang="es"):
            urls.append(url)
    except Exception:
        pass  # Google bloquea scraping con frecuencia; DDG es el motor primario real
    return urls


def search_duckduckgo(query: str, max_results: int = 10) -> list[str]:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print(Fore.YELLOW + "  [!] ddgs no instalado: pip install ddgs")
            return []
    urls = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                urls.append(r["href"])
    except Exception as e:
        print(Fore.YELLOW + f"  [!] DuckDuckGo: {e}")
    return urls


def run_query_ladder(queries: list[str]) -> list[str]:
    """Ejecuta la escalera hasta juntar MAX_CANDIDATES candidatos."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(urls):
        for u in urls:
            if u not in seen and not _is_paywall_url(u):
                seen.add(u)
                candidates.append(u)

    for i, q in enumerate(queries, 1):
        # Frenar solo cuando hay suficientes candidatos CON PINTA DE PDF:
        # las queries laxas del final suelen ser las que encuentran el archivo real.
        pdf_like = sum(1 for u in candidates if _looks_like_pdf_url(u))
        if pdf_like >= ENOUGH_PDF_LIKE or len(candidates) >= MAX_TOTAL_CANDIDATES:
            break
        print(Fore.CYAN + f"\n  Query {i}/{len(queries)} › {q}")
        if i == 1:
            add(search_google(q))          # solo intenta Google en la primera
        add(search_duckduckgo(q))
        pdf_like = sum(1 for u in candidates if _looks_like_pdf_url(u))
        print(Fore.WHITE + f"  Acumulado: {len(candidates)} candidatos ({pdf_like} parecen PDF)")
        time.sleep(1.5)

    return candidates


def search_archive(title: str, author: str, max_results: int = 5) -> list[str]:
    """Internet Archive vía API pública."""
    print(Fore.CYAN + "\n  Archive.org API…")
    query_parts = [f'title:"{title}"']
    if author:
        query_parts.append(f'creator:"{author}"')
    query_parts += ["mediatype:texts", "format:PDF"]
    urls = []
    try:
        resp = requests.get(
            "https://archive.org/advancedsearch.php",
            params={"q": " AND ".join(query_parts),
                    "fl[]": ["identifier"], "rows": max_results, "output": "json"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        for doc in resp.json().get("response", {}).get("docs", []):
            identifier = doc.get("identifier")
            if identifier:
                urls.append(f"https://archive.org/download/{identifier}/{identifier}.pdf")
    except Exception as e:
        print(Fore.YELLOW + f"  [!] Archive.org: {e}")
    return urls


def search_semantic_scholar(title: str, author: str, max_results: int = 5) -> list[str]:
    """PDFs de acceso abierto vía Semantic Scholar (papers)."""
    print(Fore.CYAN + "\n  Semantic Scholar API…")
    urls = []
    try:
        resp = requests.get(
            "https://api.semanticscholar.org/graph/v1/paper/search",
            params={"query": f"{title} {author}",
                    "fields": "title,openAccessPdf", "limit": max_results},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            for paper in resp.json().get("data", []):
                oa = paper.get("openAccessPdf")
                if oa and oa.get("url"):
                    urls.append(oa["url"])
        elif resp.status_code == 429:
            print(Fore.YELLOW + "  [!] Semantic Scholar: límite de solicitudes")
    except Exception as e:
        print(Fore.YELLOW + f"  [!] Semantic Scholar: {e}")
    return urls


def search_unpaywall_by_doi(title: str, author: str) -> list[str]:
    """CrossRef → DOI → Unpaywall para PDFs de acceso abierto."""
    print(Fore.CYAN + "\n  CrossRef + Unpaywall (acceso abierto por DOI)…")
    urls = []
    try:
        r = requests.get(
            "https://api.crossref.org/works",
            params={"query": f"{title} {author}", "rows": 3, "select": "DOI"},
            headers=HEADERS, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return urls
        for item in r.json().get("message", {}).get("items", []):
            doi = item.get("DOI", "")
            if not doi:
                continue
            uw = requests.get(
                f"https://api.unpaywall.org/v2/{doi}?email=search@bookfinder.py",
                headers=HEADERS, timeout=REQUEST_TIMEOUT,
            )
            if uw.status_code == 200 and uw.json().get("is_oa"):
                loc = uw.json().get("best_oa_location") or {}
                pdf_url = loc.get("url_for_pdf") or loc.get("url")
                if pdf_url:
                    print(Fore.GREEN + f"  ✓ Acceso abierto vía DOI {doi}")
                    urls.append(pdf_url)
    except Exception as e:
        print(Fore.YELLOW + f"  [!] CrossRef/Unpaywall: {e}")
    return urls


# ---------------------------------------------------------------------------
# MEJORA 3 — Descarga y verificación de identidad con pypdf
# ---------------------------------------------------------------------------

DOWNLOAD_TIME_BUDGET = 45   # segundos máximos por descarga (servidores lentos)


def _download_pdf(url: str) -> bytes | None:
    """
    Descarga un PDF con tope de tamaño Y de tiempo total.
    El timeout de requests solo aplica entre chunks: sin presupuesto de
    tiempo, un servidor lento puede gotear bytes indefinidamente.
    Devuelve bytes completos o None (una descarga truncada no sirve:
    pypdf no puede leerla y el botón de descarga entregaría un archivo roto).
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT * 2,
                         stream=True, allow_redirects=True)
        if r.status_code not in (200, 206):
            return None
        chunks, total = [], 0
        start = time.monotonic()
        for chunk in r.iter_content(65536):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_VERIFY_BYTES or \
               time.monotonic() - start > DOWNLOAD_TIME_BUDGET:
                return None
        data = b"".join(chunks)
        return data if data[:4] == b"%PDF" else None
    except Exception:
        return None


def verify_pdf_identity(data: bytes, title: str, author: str,
                        expected_pages: int | None = None) -> dict:
    """
    Confirma con pypdf que el PDF corresponde a la obra buscada.
    Busca tokens del título y el apellido del autor en metadatos
    y en las primeras páginas.
    """
    result = {
        "pages": None, "meta_title": "", "meta_author": "",
        "title_score": 0.0, "author_found": False,
        "verified": False, "pages_ok": None,
    }
    try:
        from pypdf import PdfReader
        pdf = PdfReader(io.BytesIO(data))
        result["pages"] = len(pdf.pages)

        meta = pdf.metadata or {}
        result["meta_title"]  = str(meta.get("/Title", "") or "")
        result["meta_author"] = str(meta.get("/Author", "") or "")

        # Texto de las primeras páginas (portada, créditos, índice)
        sample_text = ""
        for i in range(min(VERIFY_PAGES_TO_READ, len(pdf.pages))):
            try:
                sample_text += (pdf.pages[i].extract_text() or "") + "\n"
            except Exception:
                continue

        haystack = _strip_accents(
            f"{result['meta_title']} {result['meta_author']} {sample_text}".lower()
        )

        # Puntaje de título: fracción de tokens presentes
        title_tokens = _tokens(_clean_edition(title))
        if title_tokens:
            hits = sum(1 for t in title_tokens if t in haystack)
            result["title_score"] = hits / len(title_tokens)

        # Autor: apellido presente en metadatos o texto
        lastname = _strip_accents(_author_lastname(author).lower())
        result["author_found"] = bool(lastname) and lastname in haystack

        # Páginas esperadas (con tolerancia del 20 %)
        if expected_pages and result["pages"]:
            result["pages_ok"] = result["pages"] >= expected_pages * 0.8

        # Veredicto: autor confirmado + medio título. El título solo alcanza
        # únicamente si es largo/específico (≥4 tokens): títulos cortos como
        # "informe psicológico" matchean cualquier documento del género.
        strong_title = result["title_score"] >= 0.8 and len(title_tokens) >= 4
        result["verified"] = (
            (result["author_found"] and result["title_score"] >= 0.4)
            or strong_title
        )
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Scraping de segundo nivel: extraer links PDF de páginas HTML contenedoras
# ---------------------------------------------------------------------------

def extract_pdf_links(page_url: str, max_links: int = 3) -> list[str]:
    """
    Descarga una página HTML y extrae links a PDFs (absolutos y relativos).
    Muchos resultados de búsqueda son páginas que CONTIENEN el PDF
    (pdfcoffee, studylib, páginas de cátedra) en lugar del PDF directo.
    """
    from urllib.parse import urljoin
    try:
        r = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                         allow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "").lower():
            return []
        html = r.text[:800_000]

        links: list[str] = []
        # Absolutos que terminan en .pdf
        links += re.findall(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', html, re.I)
        # Relativos en href/src
        for rel in re.findall(r'(?:href|src)=["\']([^"\']+\.pdf[^"\']*)["\']', html, re.I):
            links.append(urljoin(r.url, rel))
        # Endpoints de descarga habituales en hosts de documentos
        for rel in re.findall(
                r'(?:href|src)=["\']([^"\']*(?:/download/|download_file|viewer\.html\?file=)[^"\']*)["\']',
                html, re.I):
            links.append(urljoin(r.url, rel))

        # Dedup conservando orden, sin paywalls ni la propia página
        seen, out = set(), []
        for u in links:
            u = re.sub(r"(%20|\s)+$", "", u)   # basura al final de la URL
            if u not in seen and u != page_url and not _is_paywall_url(u):
                seen.add(u)
                out.append(u)
            if len(out) >= max_links:
                break
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Validación de URLs (filtro rápido antes de descargar)
# ---------------------------------------------------------------------------

def _quick_check(url: str) -> dict:
    """HEAD/GET parcial: ¿es un PDF descargable y de tamaño razonable?"""
    result = {"valid": False, "reason": "", "size_kb": None}

    if _is_paywall_url(url):
        result["reason"] = "muro de pago"
        return result

    try:
        resp = requests.head(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                             allow_redirects=True)
        ct = resp.headers.get("Content-Type", "").lower()
        cl = resp.headers.get("Content-Length")

        if resp.status_code in (405, 501) or "pdf" not in ct:
            resp = requests.get(url, headers={**HEADERS, "Range": "bytes=0-4"},
                                timeout=REQUEST_TIMEOUT, allow_redirects=True)
            ct = resp.headers.get("Content-Type", "").lower()
            if resp.content[:4] == b"%PDF":
                ct = "application/pdf"

        if resp.status_code not in (200, 206):
            result["reason"] = f"HTTP {resp.status_code}"
            return result
        if "pdf" not in ct and resp.content[:4] != b"%PDF":
            result["reason"] = f"no es PDF ({ct or 'desconocido'})"
            return result
        if cl:
            size = int(cl)
            result["size_kb"] = size // 1024
            if size < MIN_PDF_SIZE_BYTES:
                result["reason"] = f"muy pequeño ({size // 1024} KB) — probable muestra"
                return result

        result["valid"] = True
        return result

    except requests.exceptions.SSLError:
        result["reason"] = "error SSL"
    except requests.exceptions.ConnectionError:
        result["reason"] = "no se pudo conectar"
    except requests.exceptions.Timeout:
        result["reason"] = "timeout"
    except Exception as e:
        result["reason"] = f"error: {e}"
    return result


# ---------------------------------------------------------------------------
# Orquestador principal
# ---------------------------------------------------------------------------

def find_book(title: str, author: str = "", max_results: int = 5,
              academic: bool = False) -> list[dict]:
    print(Fore.WHITE + Style.BRIGHT +
          f"\n{'='*60}\n  Buscando: {title}" +
          (f"  /  Autor: {author}" if author else "") +
          (" [modo ACADEMICO]" if academic else "") +
          f"\n{'='*60}")

    # MEJORA 1: normalizar referencia
    canon = normalize_reference(title, author)
    expected_pages = canon.get("expected_pages")

    # MEJORA 2: escalera de queries
    queries = build_query_ladder(title, author, canon)
    candidates = run_query_ladder(queries)

    # Fuentes estructuradas (siempre con la referencia canónica)
    c_title, c_author = canon["title"], canon["author"]
    for extra in search_archive(c_title, c_author):
        if extra not in candidates:
            candidates.append(extra)
    if academic:
        for fn in (search_semantic_scholar, search_unpaywall_by_doi):
            for extra in fn(c_title, c_author):
                if extra not in candidates:
                    candidates.append(extra)

    # Priorizar URLs que parecen PDF o vienen de bibliotecas libres
    priority = [u for u in candidates if _looks_like_pdf_url(u) or _is_free_library(u)]
    normal   = [u for u in candidates if u not in priority]
    ordered  = priority + normal

    print(Fore.WHITE + Style.BRIGHT +
          f"\n  Validando y verificando {len(ordered)} candidatos…\n")

    from collections import deque
    queue: deque[tuple[str, int]] = deque((u, 0) for u in ordered)  # (url, profundidad)
    tried: set[str] = set()

    valid_results: list[dict] = []
    while queue:
        if len(valid_results) >= max_results:
            break
        url, depth = queue.popleft()
        if url in tried:
            continue
        tried.add(url)

        print(f"  › {url[:80]}{'…' if len(url) > 80 else ''}")
        info = _quick_check(url)
        if not info["valid"]:
            # Si es una página HTML, explorarla UNA vez buscando el PDF interno
            if depth == 0 and info["reason"].startswith("no es PDF"):
                inner = [u for u in extract_pdf_links(url) if u not in tried]
                if inner:
                    print(Fore.CYAN + f"    ↳ página contenedora: {len(inner)} link(s) PDF interno(s)")
                    for u in reversed(inner):
                        queue.appendleft((u, 1))
                    continue
            print(Fore.RED + f"    ✗ {info['reason']}")
            continue

        if info["size_kb"] and info["size_kb"] * 1024 > MAX_VERIFY_BYTES:
            print(Fore.RED + f"    ✗ demasiado grande para verificar ({info['size_kb']} KB)")
            continue

        # MEJORA 3: descarga y verificación de identidad
        data = _download_pdf(url)
        if data is None:
            print(Fore.RED + "    ✗ no se pudo descargar para verificar")
            continue

        ident = verify_pdf_identity(data, canon["title"], canon["author"],
                                    expected_pages)
        # Segundo intento con la referencia original (por si el catálogo cambió mucho)
        if not ident["verified"] and canon["corrected"]:
            ident_orig = verify_pdf_identity(data, title, author, expected_pages)
            if ident_orig["verified"]:
                ident = ident_orig

        size_kb = len(data) // 1024
        entry = {
            "url": url,
            "domain": _domain(url),
            "size_kb": size_kb,
            "pages": ident["pages"],
            "verified": ident["verified"],
            "author_found": ident["author_found"],
            "title_score": ident["title_score"],
            "pages_ok": ident["pages_ok"],
            "free_library": _is_free_library(url),
        }

        if ident["verified"]:
            extra = f", ~{ident['pages']} págs" if ident["pages"] else ""
            print(Fore.GREEN + f"    ✓ VERIFICADO (autor: "
                  f"{'sí' if ident['author_found'] else 'no'}, "
                  f"título: {ident['title_score']:.0%}{extra}, {size_kb} KB)")
        else:
            print(Fore.YELLOW + f"    ? PDF válido pero SIN CONFIRMAR "
                  f"(título: {ident['title_score']:.0%}, "
                  f"autor: {'sí' if ident['author_found'] else 'no'}, {size_kb} KB)")

        valid_results.append(entry)
        time.sleep(0.3)

    # Verificados primero; luego bibliotecas libres; luego por tamaño
    valid_results.sort(key=lambda r: (not r["verified"],
                                      not r["free_library"],
                                      -(r["size_kb"] or 0)))
    return valid_results


# ---------------------------------------------------------------------------
# Presentación de resultados
# ---------------------------------------------------------------------------

def print_results(results: list[dict], title: str, author: str) -> None:
    print(Fore.WHITE + Style.BRIGHT + f"\n{'='*60}")
    if not results:
        print(Fore.RED + "  No se encontraron PDFs descargables válidos.")
        print(Fore.YELLOW + "  Sugerencias: probar el título en otro idioma, "
              "sin subtítulo, o el modo --academic para papers.")
        return

    label = f"  Resultados para «{title}»" + (f" — {author}" if author else "")
    print(Fore.GREEN + Style.BRIGHT + label)
    print(Fore.WHITE + Style.BRIGHT + f"{'='*60}\n")

    for i, r in enumerate(results, 1):
        badges = []
        if r["verified"]:
            badges.append("✓ VERIFICADO")
        if r["free_library"]:
            badges.append("★ biblioteca libre")
        if r["pages"]:
            badges.append(f"{r['pages']} págs")
        if r["size_kb"]:
            badges.append(f"{r['size_kb']} KB")
        badge_str = "  ·  ".join(badges)

        color = Fore.GREEN if r["verified"] else Fore.YELLOW
        print(color + Style.BRIGHT + f"  [{i}] {r['domain']}  —  {badge_str}")
        print(Fore.CYAN + f"      {r['url']}\n")

    print(Fore.WHITE + "  ✓ VERIFICADO = autor/título confirmados dentro del PDF")
    print(Fore.WHITE + f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    print(Fore.WHITE + Style.BRIGHT + """
+--------------------------------------------------+
|          Book PDF Finder  -  v2.0               |
|  Busca y VERIFICA libros y papers en PDF        |
+--------------------------------------------------+
  Uso: python book_finder.py "Titulo" "Autor" [--academic]
""")

    args = sys.argv[1:]
    academic = "--academic" in args
    args = [a for a in args if a != "--academic"]

    if len(args) >= 1:
        title  = args[0]
        author = args[1] if len(args) >= 2 else ""
    else:
        title    = input(Fore.WHITE + "  Título del libro/paper : ").strip()
        author   = input(Fore.WHITE + "  Autor (opcional)        : ").strip()
        modo_str = input(Fore.WHITE + "  ¿Modo académico? (s/N)  : ").strip().lower()
        academic = modo_str in ("s", "si", "sí", "y", "yes")

    if not title:
        print(Fore.RED + "  El título es obligatorio.")
        sys.exit(1)

    results = find_book(title, author, max_results=5, academic=academic)
    print_results(results, title, author)


if __name__ == "__main__":
    main()
