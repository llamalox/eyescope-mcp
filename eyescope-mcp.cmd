@echo off
setlocal EnableExtensions
set "DIR=%~dp0"
set "SCRIPT=%DIR%eyescope_mcp.py"
set "PY="

if defined EYESCOPE_PYTHON if exist "%EYESCOPE_PYTHON%" set "PY=%EYESCOPE_PYTHON%"
if not defined PY if exist "%DIR%..\backend\.venv\Scripts\python.exe" set "PY=%DIR%..\backend\.venv\Scripts\python.exe"
if not defined PY if defined EYESCOPE_DATA_DIR if exist "%EYESCOPE_DATA_DIR%\mcp-python.json" call :read_python "%EYESCOPE_DATA_DIR%\mcp-python.json"
if not defined PY if exist "%APPDATA%\eyescope-organizer-dev\backend\mcp-python.json" call :read_python "%APPDATA%\eyescope-organizer-dev\backend\mcp-python.json"
if not defined PY if exist "%APPDATA%\eyescope-organizer\backend\mcp-python.json" call :read_python "%APPDATA%\eyescope-organizer\backend\mcp-python.json"
if not defined PY if exist "%APPDATA%\Eyescope\backend\mcp-python.json" call :read_python "%APPDATA%\Eyescope\backend\mcp-python.json"
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY goto :missing

"%PY%" -u "%SCRIPT%"
exit /b %ERRORLEVEL%

:read_python
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$p=(Get-Content -Raw -LiteralPath '%~1' | ConvertFrom-Json).python; if ($p) { Write-Output $p }" ^<nul`) do (
  if exist "%%P" set "PY=%%P"
)
exit /b 0

:missing
echo Open Eyescope. The local engine is not running. 1>&2
echo {"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"Open Eyescope. The local engine is not running."}}
exit /b 1
