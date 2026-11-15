@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Telegram Claude Bot
:loop
python bot.py
echo บอทหยุด รีสตาร์ทใน 10 วินาที (ปิดหน้าต่างนี้เพื่อหยุด)
timeout /t 10 >nul
goto loop
