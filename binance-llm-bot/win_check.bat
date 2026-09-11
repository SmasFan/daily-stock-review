@echo off
chcp 65001 >nul
set PY=C:\Users\z7280\AppData\Local\Programs\Python\Python312\python.exe
echo === check imports ===
"%PY%" -c "import ccxt; print('ccxt', ccxt.__version__)"
"%PY%" -c "import dotenv; print('dotenv ok')"
"%PY%" -c "import openai; print('openai ok')"
echo === done ===
