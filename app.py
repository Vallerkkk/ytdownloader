import asyncio
import os
import re
import sys
import uuid
import shutil
import logging
import threading
import webbrowser
import time
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp


# ============================================================
# CAMINHOS (funciona como .py e como .exe)
# ============================================================
def resource_path(rel: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), rel)


def writable_path(rel: str) -> str:
    base = os.path.dirname(sys.executable) if hasattr(sys, "_MEIPASS") \
           else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


# ============================================================
# LOG
# ============================================================
LOG_FILE = writable_path("neonvd.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ============================================================
# FFMPEG
# ============================================================
def find_ffmpeg() -> Optional[str]:
    for base in (resource_path("bin"), writable_path("bin")):
        if os.path.isdir(base):
            for n in ("ffmpeg.exe", "ffmpeg"):
                if os.path.exists(os.path.join(base, n)):
                    logger.info(f"ffmpeg: {base}")
                    return base
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        logger.info(f"ffmpeg no PATH: {sys_ffmpeg}")
        return os.path.dirname(sys_ffmpeg)
    logger.warning("ffmpeg nao encontrado. Merge de video+audio vai falhar.")
    return None


FFMPEG_LOCATION = find_ffmpeg()

DOWNLOAD_DIR = writable_path("downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

INDEX_FILE = resource_path("index.html")


# ============================================================
# APP
# ============================================================
app = FastAPI(title="NeonVD API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def serve_index():
    if not os.path.exists(INDEX_FILE):
        raise HTTPException(status_code=404, detail="index.html nao encontrado.")
    return FileResponse(INDEX_FILE)


@app.get("/api/health")
async def health():
    return {"status": "ok", "ffmpeg": bool(FFMPEG_LOCATION)}


# ============================================================
# ESTADO
# ============================================================
tasks_state: Dict[str, Dict[str, Any]] = {}
connections: Dict[str, WebSocket] = {}
state_lock = threading.Lock()


class AnalyzeRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format: str
    quality: str = "best"


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


def base_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": USER_AGENT},
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    return opts


# ============================================================
# ANALISAR
# ============================================================
@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    logger.info(f"Analisando: {url}")

    opts = base_opts()
    opts["extract_flat"] = False

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await asyncio.to_thread(_run)
        return {
            "title": info.get("title", "Sem titulo"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "channel": info.get("uploader", info.get("channel", "Desconhecido")),
            "platform": info.get("extractor_key", "Web"),
        }
    except Exception as e:
        logger.error(f"Erro analyze: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ============================================================
# DOWNLOAD — worker em thread
# ============================================================
def run_download(task_id: str, url: str, req: DownloadRequest, loop):
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).100B.%(ext)s")

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

    opts = base_opts()
    opts.update({"outtmpl": outtmpl, "progress_hooks": [hook]})

    if req.format == "audio":
        bitrate = req.quality.replace("audio-", "") if "audio-" in req.quality else "192"
        opts["format"] = "ba/b"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate,
        }]
    else:
        opts["format"] = "bv*+ba/b" if req.quality == "best" \
            else f"bv*[height<={req.quality}]+ba/b[height<={req.quality}]/b"
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [f for f in os.listdir(DOWNLOAD_DIR)
                 if f.startswith(task_id) and not f.endswith((".part", ".temp", ".ytdl"))]
        if not files:
            raise FileNotFoundError("Arquivo nao encontrado apos download.")

        files.sort(key=lambda f: os.path.getsize(os.path.join(DOWNLOAD_DIR, f)), reverse=True)
        path = os.path.join(DOWNLOAD_DIR, files[0])

        with state_lock:
            tasks_state[task_id].update({
                "file_path": path, "status": "finished", "percent": 100
            })

        emit({"status": "finished", "percent": 100, "message": "Concluido!"})
        logger.info(f"[{task_id}] OK: {path}")

    except Exception as e:
        logger.error(f"[{task_id}] Erro: {e}", exc_info=True)
        emit({"status": "error", "percent": 0, "message": str(e)})


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    task_id = str(uuid.uuid4())

    with state_lock:
        tasks_state[task_id] = {
            "status": "queued", "percent": 0, "speed": "--",
            "eta": "--", "file_path": None, "message": "Na fila...",
        }

    loop = asyncio.get_running_loop()
    asyncio.create_task(asyncio.to_thread(run_download, task_id, url, req, loop))

    logger.info(f"[{task_id}] Iniciado: {url}")
    return {"task_id": task_id, "message": "Download iniciado"}


@app.get("/api/progress/{task_id}")
async def progress(task_id: str):
    with state_lock:
        state = tasks_state.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="Tarefa nao encontrada")
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
async def download_file(task_id: str):
    with state_lock:
        state = tasks_state.get(task_id)
        path = state.get("file_path") if state else None

    if not state:
        raise HTTPException(status_code=404, detail="Tarefa nao encontrada")
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Arquivo indisponivel")

    return FileResponse(
        path=path,
        filename=os.path.basename(path).replace(f"{task_id}_", "", 1),
        media_type="application/octet-stream",
    )


# ============================================================
# LAUNCHER
# ============================================================
def open_browser(url: str):
    import urllib.request
    start = time.time()
    while time.time() - start < 10:
        try:
            urllib.request.urlopen(url, timeout=1)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.3)
    webbrowser.open(url)


def main():
    import uvicorn
    url = "http://127.0.0.1:8000"
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    logger.info(f"Servidor: {url}")
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="info")


if __name__ == "__main__":
    main()