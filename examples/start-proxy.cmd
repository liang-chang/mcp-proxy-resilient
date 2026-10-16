@echo off
setlocal
rem Keep this script, servers.json, and the extracted portable program together.
set "MCP_PROXY_EXE=%~dp0mcp-proxy-resilient.exe"
set "MCP_PROXY_CONFIG=%~dp0servers.json"
if not exist "%MCP_PROXY_EXE%" (
    >&2 echo Missing executable: "%MCP_PROXY_EXE%"
    exit /b 1
)
if not exist "%MCP_PROXY_CONFIG%" (
    >&2 echo Copy servers.example.json to servers.json and configure a backend first.
    exit /b 1
)
if /i "%~1"=="stdio" goto stdio
if not "%~1"=="" if /i not "%~1"=="http" goto usage
set "MCP_PROXY_PORT=%~2"
if not defined MCP_PROXY_PORT set "MCP_PROXY_PORT=18080"
"%MCP_PROXY_EXE%" --resilient --frontend http --host 127.0.0.1 --port "%MCP_PROXY_PORT%" --named-server-config "%MCP_PROXY_CONFIG%" --tool-cache-dir "%~dp0tool-cache"
exit /b %errorlevel%

:stdio
if "%~2"=="" goto usage
rem stdout carries MCP messages; do not echo or pause here.
"%MCP_PROXY_EXE%" --resilient --frontend stdio --named-server-config "%MCP_PROXY_CONFIG%" --server "%~2" --tool-cache-dir "%~dp0tool-cache-stdio"
exit /b %errorlevel%

:usage
>&2 echo Usage: %~nx0 [http [PORT] ^| stdio SERVER_NAME]
exit /b 2
