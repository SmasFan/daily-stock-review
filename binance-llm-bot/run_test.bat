@echo off
chcp 65001 >nul
set PY=C:\Users\z7280\AppData\Local\Programs\Python\Python312\python.exe
cd /d C:\Users\z7280\daily-stock-review\binance-llm-bot
"%PY%" test_connection.py
echo === exit code %errorlevel% ===
