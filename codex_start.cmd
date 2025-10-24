@echo off
setlocal
rem Run from this script's folder (repo root)
cd /d "%~dp0"

rem Minimal, robust launcher: full access via env; resume most recent; keep window open
set "CODEX_APPROVAL_POLICY=never"
set "CODEX_SANDBOX_MODE=danger-full-access"
set "CODEX_NETWORK_ACCESS=enabled"

echo codex_start: approvals %CODEX_APPROVAL_POLICY%; sandbox %CODEX_SANDBOX_MODE%; network %CODEX_NETWORK_ACCESS%
if exist "AGENTS.md" (
  echo codex_start: AGENTS.md detected ^- the agent will read it on first turn.
) else (
  echo codex_start: AGENTS.md not found in %cd%.
)

where codex >nul 2>nul
if errorlevel 1 (
  echo.
  echo codex executable not found in PATH. Install Codex CLI or open a Codex-enabled shell.
  goto :PAUSE_AND_EXIT
)

echo Opening Codex session picker (no profile, no UUID)...
echo.
cmd /k codex resume
set "CODEX_EXIT=%ERRORLEVEL%"
echo.
echo Codex exited with code %CODEX_EXIT%.

:PAUSE_AND_EXIT
echo.
pause
endlocal
