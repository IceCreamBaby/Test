@echo off
chcp 65001 >nul
title Podcast Animator
cd /d "%~dp0"
echo.
echo   ==========================================================
echo     Podcast Animator wird gestartet ...
echo     Beim ersten Start werden Python und alle Bausteine
echo     automatisch installiert. Das dauert ein paar Minuten.
echo   ==========================================================
echo.
where uv >nul 2>nul
if not errorlevel 1 goto run
if exist "%USERPROFILE%\.local\bin\uv.exe" goto addpath
echo   Installiere uv - den Python-Paketmanager ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
:addpath
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
:run
uv run python -m podcast_animator %*
if errorlevel 1 (
  echo.
  echo   Es ist ein Fehler aufgetreten - Details stehen oben.
  echo   Hilfe: siehe README.md, Abschnitt Probleme.
  pause
)
