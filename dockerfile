FROM python:3.12-slim

# ---------- Sistema + ffmpeg ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------- Deno (download direto do GitHub releases) ----------
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \
    && unzip /tmp/deno.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip

# ---------- Verificação ----------
RUN ffmpeg -version | head -1 && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./
RUN mkdir -p /app/downloads

ENV PORT=8000
ENV RENDER=true
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
