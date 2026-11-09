import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.request
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL

app = FastAPI(title="Audio Público API")

# Archivo de cookies de YouTube (formato Netscape). En Render se monta como Secret File.
# Sin cookies, YouTube puede bloquear la IP del servidor ("confirm you're not a bot").
# yt-dlp intenta escribir en el archivo: los secretos son de solo lectura, así que copiamos a /tmp.
_COOKIES_SRC = os.environ.get("YTDLP_COOKIES_FILE", "/etc/secrets/cookies.txt")
COOKIES_FILE = "/tmp/yt_cookies.txt"
if os.path.isfile(_COOKIES_SRC):
    try:
        shutil.copyfile(_COOKIES_SRC, COOKIES_FILE)
        os.chmod(COOKIES_FILE, 0o600)
    except OSError:
        COOKIES_FILE = None
else:
    COOKIES_FILE = None


# --- Caché en memoria: las URLs de audio caducan, así que se reutilizan por un rato. ---
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_TTL_STREAM = 3 * 3600
_TTL_META = 24 * 3600


def _cache_get(clave, ttl):
    item = _CACHE.get(clave)
    if item and (time.time() - item[0]) < ttl:
        return item[1]
    return None


def _cache_set(clave, valor):
    with _CACHE_LOCK:
        if len(_CACHE) > 1000:
            _CACHE.clear()
        _CACHE[clave] = (time.time(), valor)


def _ydl_opts(extra: dict) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 2,
    }
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    opts.update(extra or {})
    return opts


# Clientes de YouTube a probar. Ojo: no usar "tv" junto con cookies (puede invalidar la sesión).
_CLIENTES = [None, ["web_safari"]]


def _extraer_con_reintentos(url: str, download: bool, extra: dict):
    ultimo = None
    for cliente in _CLIENTES:
        opts = _ydl_opts(extra)
        if cliente:
            opts["extractor_args"] = {"youtube": {"player_client": cliente}}
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        except Exception as exc:  # noqa: BLE001
            ultimo = exc
            msg = str(exc)
            # Solo merece la pena probar otro cliente si fue el chequeo anti-bot de YouTube.
            if "confirm you're not a bot" not in msg and "Sign in" not in msg:
                break
    raise ValueError(str(ultimo))


def _url_audio(info):
    """URL directa elegida por yt-dlp (YouTube)."""
    if not info:
        return None
    url = info.get("url")
    if url:
        return url
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    audio = [f for f in formats if f.get("vcodec") == "none"]
    if audio:
        return max(audio, key=lambda f: f.get("abr") or 0).get("url")
    return formats[-1].get("url") if formats else None


def _url_hls(info):
    """URL HLS de audio completo (SoundCloud solo entrega el audio completo por m3u8)."""
    if not info:
        return None
    formats = [f for f in (info.get("formats") or []) if f.get("url")]
    hls = [
        f for f in formats
        if "m3u8" in (f.get("url") or "") or str(f.get("protocol") or "").startswith("m3u8")
    ]
    if not hls:
        return None
    audio = [f for f in hls if f.get("vcodec") == "none"] or hls
    return max(audio, key=lambda f: f.get("abr") or 0).get("url")


def _sin_acentos(texto: str) -> str:
    return (
        unicodedata.normalize("NFKD", texto or "")
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )


def _normalizar(texto: str) -> str:
    t = _sin_acentos(texto)
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", t)
    t = re.sub(
        r"\b(feat|ft|featuring|official|video|audio|lyric|lyrics|letra|hd|hq|remaster|remastered|topic|prod|by)\b",
        " ",
        t,
    )
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _similitud(a: str, b: str) -> float:
    ta = set(_normalizar(a).split())
    tb = set(_normalizar(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _meta_youtube(video_id: str) -> dict:
    """Título, autor y duración sin descargar el audio."""
    clave = f"meta:{video_id}"
    guardado = _cache_get(clave, _TTL_META)
    if guardado:
        return guardado
    titulo = autor = ""
    duracion = None
    try:
        url = (
            "https://www.youtube.com/oembed?url=https%3A//www.youtube.com/watch%3Fv%3D"
            f"{video_id}&format=json"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "HCMusic/1.4"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        titulo = d.get("title") or ""
        autor = d.get("author_name") or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        cancion = YTMusic().get_song(video_id) or {}
        titulo = titulo or (cancion.get("title") or "")
        duracion = cancion.get("duration_seconds")
        if not autor:
            artistas = cancion.get("artists") or []
            if artistas:
                autor = artistas[0].get("name") or ""
    except Exception:  # noqa: BLE001
        pass
    meta = {"title": titulo, "author": autor, "duration": duracion}
    _cache_set(clave, meta)
    return meta


def _consultas_soundcloud(meta: dict, video_id: str):
    titulo = meta.get("title") or ""
    autor = re.sub(r"\s*-\s*Topic\s*$", "", meta.get("author") or "", flags=re.IGNORECASE).strip()
    titulo_sin_feat = re.sub(r"\(?\b(feat|ft|featuring)\b[^)]*\)?", " ", titulo, flags=re.IGNORECASE)
    variantes = []
    for q in (f"{titulo} {autor}", titulo, f"{autor} {titulo}", titulo_sin_feat):
        q = re.sub(r"\s+", " ", q).strip()
        if q and q not in variantes:
            variantes.append(q)
    return variantes or [video_id]


def _puntaje_candidato(titulo: str, duracion, entrada: dict) -> float:
    puntaje = _similitud(titulo, entrada.get("title") or "")
    d = entrada.get("duration")
    if duracion and d:
        diferencia = abs(duracion - d)
        puntaje += 0.35 * max(0.0, 1.0 - diferencia / max(30.0, duracion * 0.25))
    # Penalizar vistas previas de 30 s cuando la canción dura más.
    if duracion and duracion > 60 and (d or 0) < 40:
        puntaje -= 0.6
    return puntaje


def _buscar_candidatos_soundcloud(meta: dict, video_id: str):
    """Resultados de búsqueda "planos" (sin extraer cada pista, que puede fallar por DRM)."""
    candidatos = []
    vistos = set()
    for consulta in _consultas_soundcloud(meta, video_id):
        try:
            with YoutubeDL({
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "extract_flat": True,
                "socket_timeout": 20,
            }) as ydl:
                info = ydl.extract_info(f"scsearch10:{consulta}", download=False)
        except Exception:  # noqa: BLE001
            continue
        for entrada in (info.get("entries") or []):
            if not isinstance(entrada, dict):
                continue
            url = entrada.get("url") or entrada.get("webpage_url")
            if not url or url in vistos:
                continue
            vistos.add(url)
            candidatos.append(entrada)
        if len(candidatos) >= 15:
            break
    return candidatos


def _resolver_soundcloud(video_id: str, meta: dict):
    """Devuelve el HLS completo de la mejor coincidencia en SoundCloud, o None."""
    duracion = meta.get("duration")
    titulo = meta.get("title") or ""
    candidatos = _buscar_candidatos_soundcloud(meta, video_id)
    candidatos.sort(key=lambda e: _puntaje_candidato(titulo, duracion, e), reverse=True)
    for entrada in candidatos[:6]:
        url_track = entrada.get("url") or entrada.get("webpage_url")
        if not url_track:
            continue
        try:
            info = _extraer_con_reintentos(url_track, False, {"format": "bestaudio/best"})
            url = _url_hls(info)
            if url:
                return url
        except Exception:  # noqa: BLE001
            continue
    return None


def _resolver_stream(video_id: str, duracion: Optional[int] = None) -> dict:
    clave = f"stream:{video_id}"
    guardado = _cache_get(clave, _TTL_STREAM)
    if guardado:
        return guardado

    meta = _meta_youtube(video_id)
    if duracion and not meta.get("duration"):
        meta = {**meta, "duration": duracion}
        _cache_set(f"meta:{video_id}", meta)

    error_youtube = "YouTube no devolvió audio."
    try:
        info = _extraer_con_reintentos(
            f"https://www.youtube.com/watch?v={video_id}", False, {"format": "bestaudio/best"}
        )
        url = _url_audio(info)
        if url:
            resultado = {"id": video_id, "stream_url": url, "source": "youtube"}
            _cache_set(clave, resultado)
            return resultado
    except Exception as exc:  # noqa: BLE001
        error_youtube = str(exc)

    # Respaldo: SoundCloud (audio completo por HLS; no suele aplicar el anti-bot de YouTube).
    try:
        url = _resolver_soundcloud(video_id, meta)
        if url:
            resultado = {"id": video_id, "stream_url": url, "source": "soundcloud"}
            _cache_set(clave, resultado)
            return resultado
    except Exception:  # noqa: BLE001
        pass

    raise ValueError(f"Error al resolver el flujo: {error_youtube}")


# Modelo de respuesta del endpoint de búsqueda
class SearchResult(BaseModel):
    id: str
    title: str
    author: str
    duration: Optional[int]  # segundos
    thumbnail: Optional[str] = None  # URL de la carátula

@app.get("/api/search", response_model=List[SearchResult])
async def search(query: str):
    q = query.strip()
    if not q:
        return []

    def _search():
        try:
            yt = YTMusic()
            results = yt.search(q, filter="songs", limit=10)
        except Exception as exc:
            raise ValueError(f"No se pudo buscar: {exc}")
        items = []
        for item in results:
            video_id = item.get("videoId")
            title = item.get("title") or "Sin título"
            artists = item.get("artists", [])
            author = artists[0].get("name") if artists else "Desconocido"
            duration = item.get("duration_seconds")
            thumbs = item.get("thumbnails") or []
            thumbnail = max(thumbs, key=lambda t: t.get("width", 0)).get("url") if thumbs else None
            if video_id:
                items.append(SearchResult(
                    id=video_id,
                    title=title,
                    author=author,
                    duration=duration,
                    thumbnail=thumbnail
                ))
        return items

    # Ejecución asíncrona con executor para no bloquear el event loop
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _search)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/stream/{id}")
async def stream(id: str, duration: Optional[int] = None):
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _resolver_stream, id, duration)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/download/{id}")
async def download(id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", id):
        raise HTTPException(status_code=400, detail="ID inválido")

    def _download():
        tmpdir = tempfile.mkdtemp(prefix="hcm_")
        salida = os.path.join(tmpdir, "%(title).120s.%(ext)s")
        try:
            try:
                info = _extraer_con_reintentos(
                    f"https://www.youtube.com/watch?v={id}",
                    True,
                    {"format": "bestaudio[ext=m4a]/bestaudio/best", "outtmpl": salida},
                )
            except Exception:  # noqa: BLE001
                url_sc = _resolver_soundcloud(id, _meta_youtube(id))
                if not url_sc:
                    raise ValueError("No se encontró la canción en SoundCloud.")
                info = _extraer_con_reintentos(
                    url_sc,
                    True,
                    {"format": "bestaudio/best", "outtmpl": salida},
                )
            archivos = [f for f in os.listdir(tmpdir) if not f.endswith(".part")]
            if not archivos:
                raise ValueError("No se descargó ningún archivo.")
            return tmpdir, os.path.join(tmpdir, archivos[0]), info or {}
        except Exception as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise ValueError(f"Error al descargar: {exc}")

    loop = asyncio.get_running_loop()
    try:
        tmpdir, path, info = await loop.run_in_executor(None, _download)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ext = os.path.splitext(path)[1].lstrip(".").lower()
    media_types = {
        "mp3": "audio/mpeg", "m4a": "audio/mp4", "webm": "audio/webm",
        "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    titulo = str(info.get("title") or "audio").strip() or "audio"
    nombre_seguro = re.sub(r"[\\\\/:*?\"<>|]", "_", titulo)[:120]
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"{nombre_seguro}.{ext}",
        background=BackgroundTask(shutil.rmtree, tmpdir, True),
    )


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "cookies": bool(COOKIES_FILE and os.path.isfile(COOKIES_FILE)),
        "cache": len(_CACHE),
    }


@app.get("/api/debug/soundcloud/{id}")
async def debug_soundcloud(id: str):
    """Diagnóstico temporal: estado de la búsqueda de respaldo en SoundCloud."""

    def _run():
        meta = _meta_youtube(id)
        titulo = meta.get("title") or ""
        duracion = meta.get("duration")
        candidatos = _buscar_candidatos_soundcloud(meta, id)
        candidatos.sort(key=lambda e: _puntaje_candidato(titulo, duracion, e), reverse=True)
        out = {"meta": meta, "candidatos": []}
        for e in candidatos[:6]:
            url_track = e.get("url") or e.get("webpage_url")
            prueba = {
                "title": e.get("title"),
                "duration": e.get("duration"),
                "puntaje": round(_puntaje_candidato(titulo, duracion, e), 3),
            }
            try:
                info = _extraer_con_reintentos(url_track, False, {"format": "bestaudio/best"})
                prueba["hls"] = bool(_url_hls(info))
            except Exception as exc:  # noqa: BLE001
                prueba["error"] = str(exc)[:150]
            out["candidatos"].append(prueba)
        return out

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)
