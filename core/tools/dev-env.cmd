@echo off
REM Xifeng dev env. Usage:  core\tools\dev-env.cmd   then run commands in the same window.
REM Why not .ps1: local PowerShell execution policy blocks unsigned scripts.
REM Docs: docs/02-guides/tauri-env.md

set "RUSTUP_HOME=D:\env\rust\.rustup"
set "CARGO_HOME=D:\env\rust\.cargo"
set "PATH=D:\env\rust\.cargo\bin;%PATH%"

REM Repo root = this file's ..\..  (tools -> core -> repo root).
REM No hard-coded absolute repo path here: the directory may be renamed/moved.
for %%I in ("%~dp0..\..") do set "REPO=%%~fI"
set "XIFENG_TMP=%REPO%\.cache\rust-temp"
if not exist "%XIFENG_TMP%" mkdir "%XIFENG_TMP%" 2>nul

REM link.exe / maturin create temp files with explicit SDDL; default TEMP is denied (G35)
if not exist "%XIFENG_TMP%" goto skip_temp
set "TEMP=%XIFENG_TMP%"
set "TMP=%XIFENG_TMP%"
:skip_temp

REM NSIS (only if installed; needed by "tauri build")
if not exist "D:\env\NSIS\Bin" goto skip_nsis
set "PATH=D:\env\NSIS\Bin;%PATH%"
:skip_nsis

REM WebView2 needs a writable user-data folder. In this sandbox %LOCALAPPDATA%,
REM %APPDATA% and %TEMP% are ALL read-only for our processes, so the default
REM location makes webview creation fail with HRESULT 0x800700AA (ERROR_BUSY).
set "WV2=%XIFENG_TMP%\wv2"
if not exist "%WV2%" mkdir "%WV2%" 2>nul
if not exist "%WV2%" goto skip_wv2
set "WEBVIEW2_USER_DATA_FOLDER=%WV2%"
:skip_wv2

echo [dev-env] REPO=%REPO%
echo [dev-env] CARGO_HOME=%CARGO_HOME%
echo [dev-env] TEMP=%TEMP%
cargo --version 2>nul
