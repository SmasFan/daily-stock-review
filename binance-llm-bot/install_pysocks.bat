@echo off
chcp 65001 >nul
set PY=C:\Users\z7280\AppData\Local\Programs\Python\Python312\python.exe
"%PY%" -m pip install --quiet pysocks
echo pysocks installed
