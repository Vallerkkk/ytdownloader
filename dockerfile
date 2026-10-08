FROM python:3.12-slim

# ---------- Sistema + ffmpeg + Deno ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh \
    && ln -s /usr/local/bin/deno /usr/bin/deno

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