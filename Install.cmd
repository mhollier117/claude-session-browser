@echo off
rem Install Claude Session Browser on PCs where Attack Surface Reduction
rem blocks programs started from %TEMP%.
rem
rem Put this file next to ClaudeSessionBrowser-Setup.exe in a folder that is
rem exempt from ASR and run it. Inno Setup unpacks its setup engine into the
rem folder TMP/TEMP point to, so both are set to a folder next to this file,
rem and the app is installed into that folder as well (you can still change
rem the location on the directory page). The app keeps using that folder's
rem _update subfolder for its own updates.
setlocal
set "HERE=%~dp0"
set "APPDIR=%HERE%ClaudeSessionBrowser"
set "WORK=%APPDIR%\_update"
if not exist "%WORK%" mkdir "%WORK%"
set "TMP=%WORK%"
set "TEMP=%WORK%"
if not exist "%HERE%ClaudeSessionBrowser-Setup.exe" (
  echo ClaudeSessionBrowser-Setup.exe was not found next to this file.
  pause
  exit /b 1
)
"%HERE%ClaudeSessionBrowser-Setup.exe" /DIR="%APPDIR%" %*
exit /b %ERRORLEVEL%
