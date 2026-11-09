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


def _resolver_stream(video_id: str, duracion: Optional[int] = None) -> dict:
    clave = f"stream:{video_id}"
    guardado = _cache_get(clave, _TTL_STREAM)
    if guardado:
        return guardado

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
            info = _extraer_con_reintentos(
                f"https://www.youtube.com/watch?v={id}",
                True,
                {"format": "bestaudio[ext=m4a]/bestaudio/best", "outtmpl": salida},
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

@app.get("/api/debug/youtube/{id}")
async def debug_youtube(id: str):
    def _run():
        url = f"https://www.youtube.com/watch?v={id}"
        configs = [
            ("default", None, True),
            ("default_sin_cookies", None, False),
            ("tv", ["tv"], False),
            ("tv_con_cookies", ["tv"], True),
            ("tv_simply", ["tv_simply"], False),
            ("ios", ["ios"], True),
            ("ios_sin_cookies", ["ios"], False),
            ("android", ["android"], True),
            ("android_sin_cookies", ["android"], False),
            ("android_vr", ["android_vr"], False),
            ("mweb", ["mweb"], True),
            ("web_embedded", ["web_embedded"], True),
            ("web_safari", ["web_safari"], True),
            ("web_safari_sin_cookies", ["web_safari"], False),
        ]
        salida = []
        for nombre, clientes, cookies in configs:
            opts = {
                "format": "bestaudio/best", "quiet": True, "no_warnings": True,
                "noplaylist": True, "socket_timeout": 15, "retries": 0,
            }
            if cookies and COOKIES_FILE and os.path.isfile(COOKIES_FILE):
                opts["cookiefile"] = COOKIES_FILE
            if clientes:
                opts["extractor_args"] = {"youtube": {"player_client": clientes}}
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                u = _url_audio(info)
                salida.append({
                    "cliente": nombre, "ok": bool(u),
                    "formato": info.get("format_id"),
                    "googlevideo": bool(u and "googlevideo" in u),
                })
            except Exception as exc:  # noqa: BLE001
                salida.append({"cliente": nombre, "ok": False, "error": str(exc)[:160]})
        return salida
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)
