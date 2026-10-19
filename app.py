"""
Baixador em lote de vídeos do X.

Recebe uma lista de links (um por linha, ou CSV), processa em background
e serve cada vídeo para o frontend salvar na pasta escolhida.
"""

import glob
import os
import re
import shutil
import tempfile
import threading
import uuid

import yt_dlp
from flask import Flask, abort, jsonify, render_template, request, send_file

app = Flask(__name__)

JOBS = {}  # job_id -> estado do job

# Aceita x.com ou twitter.com, com ou sem www
URL_REGEX = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]+/status/\d+",
    re.IGNORECASE,
)


def extrair_urls(texto: str) -> list[str]:
    """Extrai todos os links de tweets do texto, sem repetir."""
    vistos = set()
    unicos = []
    for url in URL_REGEX.findall(texto):
        if url not in vistos:
            vistos.add(url)
            unicos.append(url)
    return unicos


def baixar_um(url: str, tmpdir: str, indice: int, cookies: str | None) -> str:
    """Baixa um vídeo. Retorna o nome do arquivo gerado."""
    outtmpl = os.path.join(tmpdir, f"{indice:04d}_%(uploader)s_%(id)s.%(ext)s")

    opcoes = {
        "outtmpl": outtmpl,
        "format": "best[ext=mp4]/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    if cookies:
        opcoes["cookiesfrombrowser"] = (cookies,)

    with yt_dlp.YoutubeDL(opcoes) as ydl:
        ydl.extract_info(url, download=True)

    arquivos = glob.glob(os.path.join(tmpdir, f"{indice:04d}_*"))
    if not arquivos:
        raise RuntimeError("Tweet sem vídeo ou conteúdo indisponível")

    return os.path.basename(arquivos[0])


def processar_job(job_id: str, urls: list[str], cookies: str | None) -> None:
    job = JOBS[job_id]
    tmpdir = tempfile.mkdtemp(prefix=f"xdl_{job_id}_")
    job["tmpdir"] = tmpdir

    for i, url in enumerate(urls):
        with job["lock"]:
            job["results"][i]["status"] = "baixando"

        try:
            nome = baixar_um(url, tmpdir, i, cookies)
            with job["lock"]:
                job["results"][i]["status"] = "ok"
                job["results"][i]["filename"] = nome
        except Exception as e:
            with job["lock"]:
                job["results"][i]["status"] = "erro"
                job["results"][i]["error"] = str(e)

    job["finished"] = True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    data = request.get_json() or {}
    texto = data.get("text", "")
    cookies = data.get("cookies") or None

    urls = extrair_urls(texto)
    if not urls:
        return jsonify({"error": "Nenhum link válido do X encontrado."}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "urls": urls,
        "results": [
            {"url": u, "status": "fila", "filename": None, "error": None}
            for u in urls
        ],
        "lock": threading.Lock(),
        "finished": False,
        "tmpdir": None,
    }

    threading.Thread(
        target=processar_job, args=(job_id, urls, cookies), daemon=True
    ).start()

    return jsonify({"job_id": job_id, "total": len(urls)})


@app.route("/status/<job_id>")
def status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job não encontrado"}), 404
    with job["lock"]:
        return jsonify({
            "finished": job["finished"],
            "results": [dict(r) for r in job["results"]],
        })


@app.route("/file/<job_id>/<path:filename>")
def get_file(job_id, filename):
    job = JOBS.get(job_id)
    if not job or not job["tmpdir"]:
        abort(404)
    caminho = os.path.join(job["tmpdir"], os.path.basename(filename))
    if not os.path.isfile(caminho):
        abort(404)
    return send_file(caminho, as_attachment=True, download_name=filename)


@app.route("/cleanup/<job_id>", methods=["POST"])
def cleanup(job_id):
    job = JOBS.pop(job_id, None)
    if job and job["tmpdir"] and os.path.isdir(job["tmpdir"]):
        shutil.rmtree(job["tmpdir"], ignore_errors=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("=" * 50)
    print("  ➡️  Abra http://localhost:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)