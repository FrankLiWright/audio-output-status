@echo off
title Windows 音频输出状态
cd /d "%~dp0"
python audio_status.py
if errorlevel 1 pause
