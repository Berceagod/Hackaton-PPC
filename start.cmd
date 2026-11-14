@echo off
REM Windows entrypoint — PowerShell will not execute .sh files (it opens them).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
