import asyncio
import json
import os
import re
import shutil
import tempfile
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


def _ydl_opts(extra: dict) -> dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    opts.update(extra or {})
    return opts


# Clientes de YouTube a probar. El normal y alternativas que a veces pasan sin cookies.
# Ojo: no usar "tv" junto con cookies (puede invalidar la sesión de la cuenta).
_CLIENTES = [None, ["web_safari"], ["mweb"], ["android_vr"], ["web_embedded"]]


def _url_audio(info):
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


def _titulo_y_autor_youtube(video_id: str):
    """Título y autor del video sin descargarlo (oEmbed no aplica el anti-bot)."""
    try:
        url = f"https://www.youtube.com/oembed?url=https%3A//www.youtube.com/watch%3Fv%3D{video_id}&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "HCMusic/1.4"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return (d.get("title") or ""), (d.get("author_name") or "")
    except Exception:
        return "", ""


def _consulta_respaldo(video_id: str) -> str:
    titulo, autor = _titulo_y_autor_youtube(video_id)
    return " ".join(p for p in (titulo, autor) if p).strip() or video_id


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
            # Solo merece la pena probar otro cliente si fue el chequeo anti-bot.
            if "confirm you're not a bot" not in msg and "Sign in" not in msg:
                break
    raise ValueError(str(ultimo))


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
async def stream(id: str):
    def _resolve():
        url_completa = f"https://www.youtube.com/watch?v={id}"
        yt_error = None
        try:
            info = _extraer_con_reintentos(url_completa, False, {"format": "bestaudio/best"})
            url = _url_audio(info)
            if url:
                return {"id": id, "stream_url": url, "source": "youtube"}
            yt_error = "No se encontró un flujo de audio para el contenido."
        except Exception as exc:  # noqa: BLE001
            yt_error = str(exc)

        # Respaldo: buscar la misma canción en SoundCloud (no suele aplicar el anti-bot de YouTube).
        try:
            info = _extraer_con_reintentos(
                f"scsearch1:{_consulta_respaldo(id)}",
                False,
                {"format": "http_mp3_128/http_mp3/bestaudio/best"},
            )
            if isinstance(info, dict) and info.get("entries"):
                info = info["entries"][0]
            url = _url_audio(info)
            if url:
                return {"id": id, "stream_url": url, "source": "soundcloud"}
        except Exception:  # noqa: BLE001
            pass

        raise ValueError(f"Error al resolver el flujo: {yt_error}")

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _resolve)
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
                info = _extraer_con_reintentos(
                    f"scsearch1:{_consulta_respaldo(id)}",
                    True,
                    {"format": "http_mp3_128/http_mp3/bestaudio/best", "outtmpl": salida},
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
