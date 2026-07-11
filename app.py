"""
PDF Book Finder — Web App (Streamlit)
Busca libros y papers en PDF descargables y gratuitos.

Usa book_finder.py como motor de búsqueda (validación rápida por
cabeceras HTTP y magic bytes, sin descargar el archivo completo).

Ejecutar en local:
    streamlit run app.py
"""

import re
from urllib.parse import unquote, urlparse

import streamlit as st

APP_VERSION = "2.0"


def _pdf_filename(url: str) -> str:
    """Extrae un nombre de archivo legible desde la URL para dar referencia."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1] or "")
    name = re.sub(r"\.pdf$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[_\-+]+", " ", name).strip()
    return name or "documento"

st.set_page_config(
    page_title="PDF Book Finder",
    page_icon="📚",
    layout="centered",
)

# El motor imprime a consola (colorama); en Streamlit va a los logs, no molesta
from book_finder import (
    normalize_reference,
    build_query_ladder,
    search_duckduckgo,
    search_archive,
    search_semantic_scholar,
    search_unpaywall_by_doi,
    extract_pdf_links,
    _quick_check,
    _looks_like_pdf_url,
    _is_free_library,
    _is_paywall_url,
    _domain,
    ENOUGH_PDF_LIKE,
    MAX_TOTAL_CANDIDATES,
)

MAX_RESULTS = 5


# ---------------------------------------------------------------------------
# Encabezado
# ---------------------------------------------------------------------------

st.title("📚 PDF Book Finder")
st.caption(f"v{APP_VERSION}")

st.markdown(
    "Buscá **libros y papers en PDF** descargables y gratuitos, "
    "por título y autor."
)

with st.expander("ℹ️ ¿Cómo se usa?"):
    st.markdown(
        """
1. Escribí el **título** de la obra (obligatorio).
2. Agregá el **autor** — mejora mucho la precisión de los resultados.
3. Si buscás un **paper académico**, activá el modo académico.
4. Presioná **Buscar** y esperá unos instantes mientras se realiza la búsqueda.

**Resultados:** vas a recibir hasta 5 enlaces a PDFs descargables.
Abrí cada enlace y verificá que sea la edición y versión que buscás —
vos conocés el libro mejor que nadie 😉

★ = el enlace proviene de una biblioteca libre (Archive.org, Gutenberg, etc.)
        """
    )

# ---------------------------------------------------------------------------
# Formulario
# ---------------------------------------------------------------------------

with st.form("busqueda"):
    titulo = st.text_input("Título del libro o paper *", placeholder="El Principito")
    autor = st.text_input("Autor (recomendado)", placeholder="Antoine de Saint-Exupéry")
    academico = st.toggle(
        "Modo académico (papers, estudios, artículos científicos)", value=False
    )
    buscar = st.form_submit_button("🔍 Buscar", use_container_width=True)


# ---------------------------------------------------------------------------
# Orquestación (validación rápida, sin descargar los archivos)
# ---------------------------------------------------------------------------

def run_search(title: str, author: str, academic: bool) -> list[dict]:
    resultados: list[dict] = []

    with st.status("Buscando…", expanded=True) as status:

        # Normalización de la referencia (corrige título/autor)
        st.write("📖 Preparando la búsqueda…")
        canon = normalize_reference(title, author)
        if canon["corrected"]:
            st.info(f"Buscando como: **{canon['title']}** — {canon['author']}")

        # Escalera de queries en buscadores
        st.write("🔎 Consultando fuentes…")
        queries = build_query_ladder(title, author, canon)
        candidates: list[str] = []
        seen: set[str] = set()

        def add(urls):
            for u in urls:
                if u not in seen and not _is_paywall_url(u):
                    seen.add(u)
                    candidates.append(u)

        for q in queries:
            pdf_like = sum(1 for u in candidates if _looks_like_pdf_url(u))
            if pdf_like >= ENOUGH_PDF_LIKE or len(candidates) >= MAX_TOTAL_CANDIDATES:
                break
            add(search_duckduckgo(q))

        # Fuentes estructuradas
        c_title, c_author = canon["title"], canon["author"]
        add(search_archive(c_title, c_author))
        if academic:
            add(search_semantic_scholar(c_title, c_author))
            add(search_unpaywall_by_doi(c_title, c_author))

        # Validación rápida de enlaces (cabeceras + magic bytes, sin descarga)
        priority = [u for u in candidates if _looks_like_pdf_url(u) or _is_free_library(u)]
        normal = [u for u in candidates if u not in priority]
        ordered = priority + normal

        st.write(f"🧐 Analizando {len(ordered)} enlaces…")
        barra = st.progress(0.0)
        detalle = st.empty()

        from collections import deque
        queue: deque[tuple[str, int]] = deque((u, 0) for u in ordered)
        tried: set[str] = set()
        procesados = 0
        total_estimado = max(len(ordered), 1)

        while queue and len(resultados) < MAX_RESULTS:
            url, depth = queue.popleft()
            if url in tried:
                continue
            tried.add(url)
            procesados += 1
            barra.progress(min(procesados / total_estimado, 1.0))
            detalle.caption(f"Analizando enlace {procesados} de ~{total_estimado}…")

            info = _quick_check(url)
            if not info["valid"]:
                # Si es una página que contiene el PDF, extraer el link interno
                if depth == 0 and info["reason"].startswith("no es PDF"):
                    inner = [u for u in extract_pdf_links(url) if u not in tried]
                    if inner:
                        total_estimado += len(inner)
                        for u in reversed(inner):
                            queue.appendleft((u, 1))
                continue

            resultados.append({
                "url": url,
                "domain": _domain(url),
                "filename": _pdf_filename(url),
                "size_kb": info["size_kb"],
                "free_library": _is_free_library(url),
            })

        barra.progress(1.0)
        detalle.empty()

        # Bibliotecas libres primero, luego por tamaño (más grande = más completo)
        resultados.sort(key=lambda r: (not r["free_library"],
                                       -(r["size_kb"] or 0)))

        status.update(
            label=f"Búsqueda completa: {len(resultados)} enlace(s) encontrado(s)",
            state="complete", expanded=False,
        )

    return resultados


# ---------------------------------------------------------------------------
# Ejecución y resultados
# ---------------------------------------------------------------------------

if buscar:
    if not titulo.strip():
        st.error("El título es obligatorio.")
    else:
        resultados = run_search(titulo.strip(), autor.strip(), academico)

        st.divider()
        if not resultados:
            st.warning(
                "😕 No se encontraron PDFs descargables.\n\n"
                "**Sugerencias:** probá el título en otro idioma, sin subtítulo "
                "ni número de edición, o activá el modo académico si es un paper."
            )
        else:
            st.subheader(f"Resultados para «{titulo}»" + (f" — {autor}" if autor else ""))
            st.caption(
                "Abrí cada enlace y verificá que sea la edición que buscás."
            )

            for i, r in enumerate(resultados, 1):
                with st.container(border=True):
                    badges = []
                    if r["free_library"]:
                        badges.append("★ Biblioteca libre")
                    if r["size_kb"]:
                        badges.append(f"💾 {r['size_kb']:,} KB".replace(",", "."))
                    badge_str = " · ".join(badges)

                    st.markdown(
                        f"**[{i}] {r['domain']}** &nbsp;|&nbsp; {r['filename']}"
                        + (f" &nbsp;·&nbsp; {badge_str}" if badge_str else "")
                    )
                    st.link_button("🔗 Abrir PDF", r["url"], use_container_width=True)

# ---------------------------------------------------------------------------
# Pie de página
# ---------------------------------------------------------------------------

st.divider()
st.markdown(
    f"App creada por [**clasemartinez**](https://www.linkedin.com/in/claudiomartinez1/) "
    f"· PDF Book Finder v{APP_VERSION}"
)
