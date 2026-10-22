@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Creando entorno e instalando dependencias, solo la primera vez...
  python -m venv .venv || (echo Instala Python 3.10+ desde python.org y marca "Add to PATH" & pause & exit /b)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
.venv\Scripts\python iniciar.py %*
pause
