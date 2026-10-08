FROM python:3.12-slim

# ---------- Dependências do sistema ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------- Instala Deno (JS runtime para resolver challenges do YouTube) ----------
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -s /usr/local/bin/deno /usr/bin/deno

# ---------- Verificação (aparece no build log) ----------
RUN ffmpeg -version | head -1 && deno --version

WORKDIR /app

# ---------- Dependências Python ----------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Código ----------
COPY app.py index.html ./

# ---------- Pasta de downloads ----------
RUN mkdir -p /app/downloads

# ---------- Render usa $PORT ----------
ENV PORT=8000
ENV RENDER=true
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]