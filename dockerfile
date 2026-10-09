FROM python:3.12-slim

# ---------- Sistema + ffmpeg + Deno ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# install.sh padrão joga o binário em ~/.deno/bin, que NÃO entra no PATH da imagem.
# O symlink antigo (ln -s /usr/local/bin/deno) apontava para um arquivo que não existia,
# então o yt-dlp subia sem runtime JS e o YouTube entregava stream throttled (~50 KB/s).
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import yt_dlp_ejs; print('yt-dlp-ejs', yt_dlp_ejs.__file__)"

COPY app.py index.html ./
RUN mkdir -p /app/downloads /app/previews

ENV PORT=8000
ENV RENDER=true
ENV PATH="/usr/local/bin:${PATH}"
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
