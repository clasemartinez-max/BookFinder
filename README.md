# 📚 Book PDF Finder

Aplicación web para buscar **libros y papers en PDF** descargables y gratuitos, por título y autor.

## ¿Cómo se usa?

1. Escribí el **título** de la obra (obligatorio).
2. Agregá el **autor** — mejora mucho la precisión de los resultados.
3. Si buscás un **paper académico**, activá el modo académico.
4. Presioná **Buscar**: en unos instantes vas a recibir hasta 5 enlaces a PDFs descargables.
5. Abrí cada enlace y verificá que sea la edición y versión que buscás.

★ = el enlace proviene de una biblioteca libre (Archive.org, Gutenberg, etc.)

## Ejecutar en local

```bash
pip install -r requirements.txt
streamlit run app.py
```

También se puede usar el motor por línea de comandos:

```bash
python book_finder.py "Título del libro" "Nombre del Autor"
python book_finder.py "Título del paper" "Autor" --academic
```

## Estructura

| Archivo | Descripción |
|---|---|
| `app.py` | Aplicación web (Streamlit) |
| `book_finder.py` | Motor de búsqueda (usable también como CLI) |
| `requirements.txt` | Dependencias |

## Deploy en Streamlit Community Cloud

1. Subir este repositorio a GitHub.
2. En [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Seleccionar el repositorio, rama `main` y archivo `app.py`.
4. **Deploy** — las dependencias se instalan automáticamente desde `requirements.txt`.

---

App creada por [**clasemartinez**](https://www.linkedin.com/in/claudiomartinez1/)
