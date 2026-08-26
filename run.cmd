@echo off
setlocal enabledelayedexpansion
rem ---------------------------------------------------------------------------
rem  One command to bring the whole thing up and prove it works.
rem
rem    run.cmd            start, migrate, seed, verify, open the UIs
rem    run.cmd --no-open  same, but do not launch browser tabs
rem    run.cmd --stop     tear the stack down and remove volumes
rem
rem  Written for cmd.exe. Everything it needs is docker + the project venv;
rem  it deliberately does not use `uv`, which is not on the PATH here.
rem ---------------------------------------------------------------------------

cd /d "%~dp0"
set COMPOSE=docker compose -f deploy/docker-compose.yml
set PY=.venv\Scripts\python.exe

if /I "%1"=="--stop" goto stop

rem Trace export is off by default in this repo; turning it on here is what
rem makes the Tempo tab show something instead of an empty search screen.
set OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318

echo.
echo ===========================================================
echo  Common Application Base  -  starting
echo ===========================================================
echo.
echo [1/5] Building and starting 10 services (first run takes a few minutes)...
%COMPOSE% --profile tracing up -d --build
if errorlevel 1 goto failed

echo.
echo [2/5] Waiting for the API to answer its liveness probe...
set /a tries=0
:wait
set /a tries+=1
curl.exe -s -o nul http://localhost:8000/health/live && goto ready
if !tries! GEQ 60 (
  echo        API did not come up. Recent logs:
  %COMPOSE% logs --tail=40 app
  goto failed
)
rem Absolute path: if Git for Windows is on PATH, its coreutils `timeout`
rem shadows the cmd builtin and rejects /t.
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
goto wait
:ready
echo        API is up after !tries! attempt(s).

echo.
echo [3/5] Applying database migrations...
%COMPOSE% exec -T app alembic upgrade head
if errorlevel 1 goto failed

echo.
echo [4/5] Seeding traffic, so no dashboard or log panel looks empty...
%PY% scripts\seed_demo.py
if errorlevel 1 goto failed

echo.
echo [5/5] Verifying every block end to end...
%PY% scripts\smoke.py
if errorlevel 1 (
  echo.
  echo   The smoke test did NOT pass. The stack is still running, so you can
  echo   investigate: %COMPOSE% logs app
  goto failed
)

echo.
echo ===========================================================
echo  READY  -  open these
echo ===========================================================
echo.
echo   Swagger UI  http://localhost:8000/docs        try the endpoints here
echo   ReDoc       http://localhost:8000/redoc
echo   Grafana     http://localhost:3001/d/common-app-base   (no login)
echo   Prometheus  http://localhost:9090/targets
echo   MinIO       http://localhost:9001             minioadmin / minioadmin
echo.
echo   Visual checklist: VERIFY.md
echo   Stop everything:  run.cmd --stop
echo.

if /I "%1"=="--no-open" goto done

echo Opening browser tabs...
start "" "http://localhost:8000/docs"
start "" "http://localhost:3001/d/common-app-base"
start "" "http://localhost:9090/targets"
start "" "http://localhost:9001"

:done
endlocal
exit /b 0

:stop
echo Stopping the stack and removing volumes...
%COMPOSE% --profile tracing down -v
endlocal
exit /b 0

:failed
echo.
echo *** Startup failed. See the output above. ***
endlocal
exit /b 1
