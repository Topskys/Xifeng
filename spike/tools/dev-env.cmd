@echo off
REM VoiceIME dev env. Usage:  tools\dev-env.cmd   then run commands in the same window.
REM Why not .ps1: local PowerShell execution policy blocks unsigned scripts.
REM Docs: docs/02-guides/tauri-env.md

set "RUSTUP_HOME=D:\env\rust\.rustup"
set "CARGO_HOME=D:\env\rust\.cargo"
set "PATH=D:\env\rust\.cargo\bin;%PATH%"

REM link.exe / maturin create temp files with explicit SDDL; default TEMP is denied (G35)
if not exist "D:\project\workspace\voice-ime\.rust-temp" goto skip_temp
set "TEMP=D:\project\workspace\voice-ime\.rust-temp"
set "TMP=D:\project\workspace\voice-ime\.rust-temp"
:skip_temp

REM NSIS (only if installed; needed by "tauri build")
if not exist "D:\env\NSIS\Bin" goto skip_nsis
set "PATH=D:\env\NSIS\Bin;%PATH%"
:skip_nsis

REM WebView2 needs a writable user-data folder. In this sandbox %LOCALAPPDATA%,
REM %APPDATA% and %TEMP% are ALL read-only for our processes, so the default
REM location makes webview creation fail with HRESULT 0x800700AA (ERROR_BUSY).
if not exist "D:\project\workspace\voice-ime\.rust-temp\wv2" goto skip_wv2
set "WEBVIEW2_USER_DATA_FOLDER=D:\project\workspace\voice-ime\.rust-temp\wv2"
:skip_wv2
echo [dev-env] CARGO_HOME=%CARGO_HOME%
echo [dev-env] TEMP=%TEMP%
cargo --version 2>nul
