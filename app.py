import asyncio
import os
import re
import sys
import uuid
import shutil
import logging
import threading
import time
import mimetypes
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp


# ============================================================
# AMBIENTE
# ============================================================
IS_SERVER = os.environ.get("RENDER", "").lower() == "true" or os.environ.get("PORT") is not None
HOST = "0.0.0.0" if IS_SERVER else "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))

PROXY_URL = os.environ.get("PROXY_URL")


# ============================================================
# LOG
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ============================================================
# CAMINHOS
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
PREVIEW_DIR = os.path.join(BASE_DIR, "previews")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)
INDEX_FILE = os.path.join(BASE_DIR, "index.html")


# ============================================================
# PROXY SELETIVO
# ============================================================
PROXY_REQUIRED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "music.youtube.com",
    "m.youtube.com",
)


def needs_proxy(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        return any(host == d or host.endswith("." + d) for d in PROXY_REQUIRED_DOMAINS)
    except Exception:
        return False


def youtube_wants_proxy(url: str) -> bool:
    """YouTube no datacenter costuma precisar de proxy, mas o proxy é lento.
    Default: tenta direto. YT_FORCE_PROXY=1 volta ao comportamento antigo."""
    if not needs_proxy(url) or not PROXY_URL:
        return False
    return os.environ.get("YT_FORCE_PROXY", "").lower() in ("1", "true", "yes")


# ============================================================
# COOKIES
# ============================================================
COOKIES_FILE = None
_candidates = ["/etc/secrets/cookies.txt", os.path.join(BASE_DIR, "cookies.txt")]
_source = next((c for c in _candidates if os.path.exists(c)), None)

if _source:
    try:
        if _source.startswith("/etc/secrets"):
            _tmp = "/tmp/cookies.txt"
            with open(_source, "rb") as fsrc:
                data = fsrc.read()
            with open(_tmp, "wb") as fdst:
                fdst.write(data)
            COOKIES_FILE = _tmp
            logger.info(f"✅ Cookies copiados: {_source} → {_tmp}")
        else:
            COOKIES_FILE = _source
            logger.info(f"✅ Cookies: {_source}")
    except Exception as e:
        logger.warning(f"⚠️  Erro ao preparar cookies: {e}")
        COOKIES_FILE = None
else:
    logger.info("ℹ️  Nenhum cookies.txt — rodando só com proxy (quando necessário)")


# ============================================================
# FFMPEG
# ============================================================
def find_ffmpeg() -> Optional[str]:
    ff = shutil.which("ffmpeg")
    if ff:
        logger.info(f"✅ ffmpeg: {ff}")
        return os.path.dirname(ff)
    logger.warning("⚠️  ffmpeg não encontrado")
    return None


FFMPEG_LOCATION = find_ffmpeg()


# ============================================================
# DENO
# ============================================================
def find_deno() -> Optional[str]:
    deno = shutil.which("deno")
    if not deno:
        for candidate in ("/usr/local/bin/deno", "/root/.deno/bin/deno", "/usr/bin/deno"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                deno = candidate
                break
    if deno:
        logger.info(f"✅ Deno: {deno}")
        d = os.path.dirname(deno)
        if d not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return deno
    logger.warning(
        "⚠️  Deno não encontrado — YouTube vai throttlear (~50 KB/s). "
        "Instale com DENO_INSTALL=/usr/local."
    )
    return None


DENO_PATH = find_deno()


def _ejs_available() -> bool:
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except Exception:
        return False


EJS_OK = _ejs_available()
if EJS_OK:
    logger.info("✅ yt-dlp-ejs disponível (solver do challenge n)")
else:
    logger.warning("⚠️  yt-dlp-ejs ausente — YouTube pode continuar throttled")


# ============================================================
# APP
# ============================================================
app = FastAPI(title="NeonVD API", version="2.4.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length"],
)


@app.get("/", include_in_schema=False)
async def serve_index():
    if not os.path.exists(INDEX_FILE):
        raise HTTPException(status_code=404, detail="index.html não encontrado.")
    return FileResponse(INDEX_FILE)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ffmpeg": bool(FFMPEG_LOCATION),
        "deno": bool(DENO_PATH),
        "deno_path": DENO_PATH,
        "ejs": _ejs_available(),
        "cookies": bool(COOKIES_FILE),
        "proxy": bool(PROXY_URL),
        "server": IS_SERVER,
        "cache_size": len(analyze_cache),
        "previews": len(previews_state),
    }


# ============================================================
# ESTADO
# ============================================================
tasks_state: Dict[str, Dict[str, Any]] = {}
previews_state: Dict[str, Dict[str, Any]] = {}
connections: Dict[str, WebSocket] = {}
state_lock = threading.Lock()

analyze_cache: Dict[str, dict] = {}
CACHE_TTL = 3600


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format: str
    quality: str = "best"


class PreviewRequest(BaseModel):
    url: str


# ============================================================
# HELPERS
# ============================================================
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_ansi(t: str) -> str:
    return ANSI_RE.sub("", str(t or "")).strip()


def base_opts(use_proxy: bool = True) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": USER_AGENT},
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        "http_chunk_size": 10 * 1024 * 1024,
        "buffersize": 1024 * 1024,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "continuedl": True,
        "noresizebuffer": False,
    }

    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if use_proxy and PROXY_URL:
        opts["proxy"] = PROXY_URL

    # Sem o solver do parâmetro `n`, o CDN do YouTube entrega o mesmo arquivo
    # a ~40–100 KB/s. Outros sites não passam por esse challenge, por isso
    # só o YouTube parecia “capado”.
    if DENO_PATH:
        opts["js_runtimes"] = {"deno": {"path": DENO_PATH}}
    if not EJS_OK:
        opts["remote_components"] = ["ejs:github"]

    # Não force 5 player clients: cada um é um round-trip antes do download.
    # O default do yt-dlp acompanha o que o YouTube aceita hoje.
    # Sobrescreva com YT_PLAYER_CLIENT=android,web se um client quebrar.
    clients = os.environ.get("YT_PLAYER_CLIENT", "").strip()
    if clients:
        opts["extractor_args"] = {
            "youtube": {"player_client": [c.strip() for c in clients.split(",") if c.strip()]},
        }
    return opts


MIME_MAP = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/opus",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}


def get_mime_type(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return MIME_MAP.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def cleanup_previews(max_age_hours: int = 1):
    """Remove previews antigos."""
    try:
        now = time.time()
        for f in os.listdir(PREVIEW_DIR):
            path = os.path.join(PREVIEW_DIR, f)
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > max_age_hours * 3600:
                os.remove(path)
    except Exception:
        pass


def ensure_extension(path: str, fallback: str = ".mp4") -> str:
    """Garante que o arquivo tenha extensão — evita o navegador salvar como .txt
    quando o mime cai em application/octet-stream."""
    ext = os.path.splitext(path)[1].lower()
    if ext and ext not in (".unknown_video", ".part"):
        return path
    new_path = path + fallback
    try:
        os.rename(path, new_path)
        logger.warning(f"Extensão corrigida: {path} → {new_path}")
        return new_path
    except Exception:
        return path


# ============================================================
# ANALISAR
# ============================================================
@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    logger.info(f"Analisando: {url}")

    cached = analyze_cache.get(url)
    if cached and (time.time() - cached["ts"]) < CACHE_TTL:
        logger.info(f"✅ Cache hit: {url}")
        return cached["data"]

    use_proxy = youtube_wants_proxy(url)
    logger.info(f"Proxy: {'SIM' if use_proxy else 'NÃO'} ({urlparse(url).hostname})")

    opts = base_opts(use_proxy=use_proxy)
    opts["extract_flat"] = False
    opts["format"] = None

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_run)
        result = {
            "title": info.get("title", "Sem título"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "channel": info.get("uploader", info.get("channel", "Desconhecido")),
            "platform": info.get("extractor_key", "Web"),
        }

        analyze_cache[url] = {"ts": time.time(), "data": result}
        if len(analyze_cache) > 200:
            oldest = sorted(analyze_cache.items(), key=lambda x: x[1]["ts"])[:50]
            for k, _ in oldest:
                analyze_cache.pop(k, None)

        return result

    except Exception as e:
        if not use_proxy and PROXY_URL and needs_proxy(url):
            logger.warning(f"Analyze sem proxy falhou, tentando com proxy: {e}")
            opts_proxy = base_opts(use_proxy=True)
            opts_proxy["extract_flat"] = False
            opts_proxy["format"] = None
            try:
                info = await asyncio.to_thread(
                    lambda: yt_dlp.YoutubeDL(opts_proxy).extract_info(url, download=False)
                )
                result = {
                    "title": info.get("title", "Sem título"),
                    "thumbnail": info.get("thumbnail", ""),
                    "duration": info.get("duration", 0),
                    "channel": info.get("uploader", info.get("channel", "Desconhecido")),
                    "platform": info.get("extractor_key", "Web"),
                }
                analyze_cache[url] = {"ts": time.time(), "data": result}
                return result
            except Exception as e2:
                logger.error(f"Erro analyze (proxy): {e2}")
                raise HTTPException(status_code=400, detail=str(e2))
        logger.error(f"Erro analyze: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================
# PRÉ-VISUALIZAÇÃO
# ============================================================
def run_preview(preview_id: str, url: str):
    """Baixa a pior qualidade do vídeo (rápido) para preview."""
    outtmpl = os.path.join(PREVIEW_DIR, f"{preview_id}.%(ext)s")
    use_proxy = youtube_wants_proxy(url)

    def _opts(with_proxy: bool):
        opts = base_opts(use_proxy=with_proxy)
        opts.update({
            "outtmpl": outtmpl,
            # Prévia: arquivo único progressivo, sem merge.
            "format": "b[ext=mp4][height<=360]/w[ext=mp4]/b[height<=360]/worst[ext=mp4]/worst",
            "postprocessors": [],
        })
        return opts

    previews_state[preview_id]["status"] = "downloading"

    try:
        try:
            with yt_dlp.YoutubeDL(_opts(use_proxy)) as ydl:
                ydl.download([url])
        except Exception:
            if use_proxy or not PROXY_URL:
                raise
            logger.warning(f"[preview {preview_id}] Direto falhou, tentando proxy")
            with yt_dlp.YoutubeDL(_opts(True)) as ydl:
                ydl.download([url])

        files = [
            f for f in os.listdir(PREVIEW_DIR)
            if f.startswith(preview_id) and not f.endswith((".part", ".temp", ".ytdl"))
        ]
        if not files:
            raise FileNotFoundError("Preview não encontrado")

        path = os.path.join(PREVIEW_DIR, files[0])
        path = ensure_extension(path, ".mp4")

        previews_state[preview_id]["path"] = path
        previews_state[preview_id]["status"] = "ready"
        logger.info(f"[preview {preview_id}] OK: {path}")

    except Exception as e:
        logger.error(f"[preview {preview_id}] Erro: {e}")
        previews_state[preview_id]["status"] = "error"
        previews_state[preview_id]["message"] = str(e)


@app.post("/api/preview")
async def start_preview(req: PreviewRequest):
    url = req.url.strip()
    preview_id = str(uuid.uuid4())

    cleanup_previews(max_age_hours=1)

    with state_lock:
        previews_state[preview_id] = {"status": "queued", "path": None, "url": url}

    logger.info(f"[preview {preview_id}] Iniciado: {url}")
    asyncio.create_task(asyncio.to_thread(run_preview, preview_id, url))

    return {"preview_id": preview_id, "status": "queued"}


@app.get("/api/preview-status/{preview_id}")
async def preview_status(preview_id: str):
    with state_lock:
        state = previews_state.get(preview_id)
    if not state:
        raise HTTPException(status_code=404, detail="Preview não encontrado")
    return {
        "status": state["status"],
        "ready": state.get("path") is not None,
        "message": state.get("message", ""),
    }


@app.get("/api/preview-file/{preview_id}")
async def preview_file(preview_id: str):
    with state_lock:
        state = previews_state.get(preview_id)
    if not state or not state.get("path"):
        raise HTTPException(status_code=404, detail="Preview não disponível")

    path = state["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Arquivo de preview não encontrado")

    # Deixa o Starlette montar o Content-Disposition (RFC 5987 correto)
    return FileResponse(
        path=path,
        filename=os.path.basename(path),
        media_type=get_mime_type(path),
        content_disposition_type="inline",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=3600",
        },
    )


# ============================================================
# DOWNLOAD — worker em thread
# ============================================================
def run_download(task_id: str, url: str, req: DownloadRequest, loop, use_proxy: bool = True):
    # ⚠️  .80s (chars) e não .100B (bytes) — evita truncar no meio de UTF-8
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).80s.%(ext)s")

    def emit(data: dict):
        with state_lock:
            tasks_state.setdefault(task_id, {}).update(data)
            ws = connections.get(task_id)
        if ws:
            try:
                asyncio.run_coroutine_threadsafe(ws.send_json(data), loop)
            except Exception:
                pass

    def hook(d):
        if d.get("status") == "downloading":
            raw = clean_ansi(d.get("_percent_str", "0%")).rstrip("%")
            try:
                pct = float(raw)
            except ValueError:
                got = d.get("downloaded_bytes") or 0
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                pct = (got / tot * 100) if tot else 0
            emit({
                "status": "downloading",
                "percent": round(pct, 2),
                "speed": clean_ansi(d.get("_speed_str", "-- MB/s")) or "-- MB/s",
                "eta": clean_ansi(d.get("_eta_str", "--:--")) or "--:--",
            })
        elif d.get("status") == "finished":
            emit({"status": "processing", "percent": 99, "message": "Processando..."})

    def build_opts(use_proxy_inner: bool):
        o = base_opts(use_proxy=use_proxy_inner)
        o.update({"outtmpl": outtmpl, "progress_hooks": [hook]})

        if req.format == "audio":
            # Modo rápido: mantém formato original (m4a/opus) sem reencoding
            if req.quality == "audio-original":
                o["format"] = "ba[ext=m4a]/ba[ext=opus]/ba[ext=webm]/ba/b"
                logger.info(f"[{task_id}] Áudio ORIGINAL (sem conversão)")
            else:
                bitrate = req.quality.replace("audio-", "") if "audio-" in req.quality else "192"
                o["format"] = "ba/b/bestaudio/best"
                o["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": bitrate,
                }]
                logger.info(f"[{task_id}] Áudio MP3 {bitrate}kbps")

        else:
            o["format_sort"] = ["res", "vcodec:h264", "acodec:aac", "ext:mp4", "proto:https"]
            if needs_proxy(url):
                o["concurrent_fragment_downloads"] = 16

            if req.quality == "best":
                o["format"] = (
                    "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]"
                    "/bv*[ext=mp4]+ba[ext=m4a]"
                    "/b[ext=mp4]/bv*+ba/b"
                )
            else:
                q = req.quality
                if str(q) in ("480", "720"):
                    o["format"] = (
                        f"b[ext=mp4][height<={q}]/"
                        f"bv*[vcodec^=avc1][height<={q}][ext=mp4]+ba[ext=m4a]/"
                        f"bv*[height<={q}]+ba/b[height<={q}]/b"
                    )
                else:
                    o["format"] = (
                        f"bv*[vcodec^=avc1][height<={q}][ext=mp4]+ba[ext=m4a]/"
                        f"bv*[ext=mp4][height<={q}]+ba[ext=m4a]/"
                        f"bv*[height<={q}]+ba/b[height<={q}]/b"
                    )
            o["merge_output_format"] = "mp4"
        return o

    try:
        with yt_dlp.YoutubeDL(build_opts(use_proxy)) as ydl:
            ydl.download([url])

    except Exception as e:
        if not use_proxy and PROXY_URL:
            logger.warning(f"[{task_id}] Falhou sem proxy, tentando com proxy...")
            try:
                with yt_dlp.YoutubeDL(build_opts(True)) as ydl:
                    ydl.download([url])
            except Exception as e2:
                logger.error(f"[{task_id}] Erro final: {e2}", exc_info=True)
                emit({"status": "error", "percent": 0, "message": str(e2)})
                return
        else:
            logger.error(f"[{task_id}] Erro: {e}", exc_info=True)
            emit({"status": "error", "percent": 0, "message": str(e)})
            return

    try:
        files = [
            f for f in os.listdir(DOWNLOAD_DIR)
            if f.startswith(task_id) and not f.endswith((".part", ".temp", ".ytdl"))
        ]
        if not files:
            raise FileNotFoundError("Arquivo não encontrado após download.")

        files.sort(key=lambda f: os.path.getsize(os.path.join(DOWNLOAD_DIR, f)), reverse=True)
        path = os.path.join(DOWNLOAD_DIR, files[0])

        # ⚠️  Garante extensão coerente (evita .txt no navegador)
        fallback_ext = ".mp3" if req.format == "audio" else ".mp4"
        path = ensure_extension(path, fallback_ext)

        with state_lock:
            tasks_state[task_id].update({
                "file_path": path, "status": "finished", "percent": 100
            })

        emit({"status": "finished", "percent": 100, "message": "Concluído!"})
        logger.info(f"[{task_id}] OK: {path}")

    except Exception as e:
        logger.error(f"[{task_id}] Erro pós-download: {e}", exc_info=True)
        emit({"status": "error", "percent": 0, "message": str(e)})


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    task_id = str(uuid.uuid4())

    use_proxy = youtube_wants_proxy(url)
    logger.info(
        f"[{task_id}] Iniciado: {url} | "
        f"proxy={'SIM' if use_proxy else 'NÃO'} ({urlparse(url).hostname}) | "
        f"deno={'SIM' if DENO_PATH else 'NÃO'}"
    )

    with state_lock:
        tasks_state[task_id] = {
            "status": "queued", "percent": 0, "speed": "--",
            "eta": "--", "file_path": None, "message": "Na fila...",
            "using_proxy": use_proxy,
        }

    loop = asyncio.get_running_loop()
    asyncio.create_task(
        asyncio.to_thread(run_download, task_id, url, req, loop, use_proxy)
    )

    return {
        "task_id": task_id,
        "message": "Download iniciado",
        "using_proxy": use_proxy,
    }


@app.get("/api/progress/{task_id}")
async def progress(task_id: str):
    with state_lock:
        state = tasks_state.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return state


@app.websocket("/api/ws/progress/{task_id}")
async def ws_progress(websocket: WebSocket, task_id: str):
    await websocket.accept()
    with state_lock:
        connections[task_id] = websocket
        current = tasks_state.get(task_id)

    if current:
        try:
            await websocket.send_json(current)
        except Exception:
            pass

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        with state_lock:
            connections.pop(task_id, None)


@app.get("/api/download-file/{task_id}")
async def download_file(task_id: str, inline: bool = False):
    with state_lock:
        state = tasks_state.get(task_id)
        path = state.get("file_path") if state else None

    if not state:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Arquivo indisponível")

    # Remove o prefixo UUID do nome exibido
    base = os.path.basename(path)
    clean_name = base.replace(f"{task_id}_", "", 1) or base

    # Garante extensão no nome exibido (defesa extra)
    if not os.path.splitext(clean_name)[1]:
        clean_name += ".mp4"

    media_type = get_mime_type(clean_name)

    # ⚠️  CRÍTICO: NÃO passe "Content-Disposition" em headers.
    # Deixe o Starlette gerar — ele usa RFC 5987 (filename*=utf-8''...)
    # que lida corretamente com acentos, emojis e aspas.
    return FileResponse(
        path=path,
        filename=clean_name,
        media_type=media_type,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "Access-Control-Expose-Headers": "Content-Disposition, Content-Length",
            "Cache-Control": "private, max-age=3600",
        },
    )


@app.post("/api/check-proxy")
async def check_proxy(req: AnalyzeRequest):
    url = req.url.strip()
    use = needs_proxy(url)
    return {
        "url": url,
        "uses_proxy": use,
        "reason": (
            "YouTube bloqueia IPs de datacenter — proxy é necessário"
            if use else
            "Site funciona sem proxy — mais rápido e sem custo"
        ),
    }


# ============================================================
# START
# ============================================================
def main():
    import uvicorn
    logger.info(f"Servidor: http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="info")


if __name__ == "__main__":
    main()