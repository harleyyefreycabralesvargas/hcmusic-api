import asyncio
import os
import re
import shutil
import tempfile
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL

app = FastAPI(title="Audio Público API")

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
        ydl_opts = {
            "format": "bestaudio",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url_completa, download=False)
                if info is None:
                    raise ValueError("Contenido no encontrado o restringido.")
                url = info.get("url")
                if not url:
                    formats = info.get("formats", [])
                    audio_formats = [f for f in formats if f.get("vcodec") == "none"]
                    if audio_formats:
                        best = max(audio_formats, key=lambda f: f.get("abr", 0))
                        url = best.get("url")
                if not url:
                    raise ValueError("No se encontró un flujo de audio para el contenido.")
                return {"id": id, "stream_url": url}
        except Exception as exc:
            raise ValueError(f"Error al resolver el flujo: {exc}")

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
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": os.path.join(tmpdir, "%(title).120s.%(ext)s"),
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={id}", download=True)
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
