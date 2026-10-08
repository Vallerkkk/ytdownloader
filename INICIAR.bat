@echo off
chcp 65001 >nul
title NeonVD
cd /d "%~dp0"

echo.
echo ============================================================
echo   NeonVD - Universal Video Downloader
echo ============================================================
echo.

REM ---------- Verifica Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado.
    echo Baixe em: https://www.python.org/downloads/
    pause & exit /b 1
)

REM ---------- Verifica Deno ----------
where deno >nul 2>&1
if errorlevel 1 (
    echo [AVISO] Deno nao encontrado. YouTube pode falhar.
    echo Instale com: winget install DenoLand.Deno
    echo.
)

REM ---------- Verifica ffmpeg ----------
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo [AVISO] ffmpeg nao encontrado. Merge de video+audio vai falhar.
    echo Instale com: winget install Gyan.FFmpeg
    echo.
)

REM ---------- Verifica cookies ----------
if not exist "cookies.txt" (
    echo [AVISO] cookies.txt nao encontrado.
    echo YouTube vai falhar com "Sign in to confirm you're not a bot".
    echo.
)

REM ---------- Instala dependencias se faltar ----------
echo [1/3] Verificando dependencias...
set NEED=0
python -c "import fastapi"    2>nul || set NEED=1
python -c "import uvicorn"    2>nul || set NEED=1
python -c "import yt_dlp"     2>nul || set NEED=1
python -c "import websockets" 2>nul || set NEED=1

if %NEED%==1 (
    echo       Instalando pacotes necessarios ^(primeira vez^)...
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet fastapi "uvicorn[standard]" yt-dlp websockets
    python -m pip install --quiet --upgrade yt-dlp
    echo       OK.
) else (
    echo       Tudo instalado.
)

REM ---------- Encerra instancia anterior na porta 8000 ----------
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo [2/3] Encerrando instancia anterior ^(PID %%a^)...
    taskkill /F /PID %%a >nul 2>&1
)

REM ---------- Sobe o app ----------
echo [3/3] Iniciando servidor...
echo.
echo ============================================================
echo   Abrindo em: http://127.0.0.1:8000
echo   Para PARAR, feche esta janela ou aperte CTRL+C.
echo ============================================================
echo.

python app.py

echo.
echo Servidor encerrado.
pause