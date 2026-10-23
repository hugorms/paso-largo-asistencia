@echo off
rem Copia el programa y la base de datos de esta PC al servidor Ubuntu y lo deja corriendo alli.
rem Solo para la primera vez: si el servidor ya tiene base de datos, no la pisa.
cd /d "%~dp0.."
set SERVIDOR=paso_largo@192.168.1.40

echo.
echo  1) Cierra la ventana de iniciar.bat (el programa debe estar apagado para copiar la base de datos).
echo  2) Te va a pedir la clave del servidor (usuario paso_largo) un par de veces. Si pregunta yes/no, escribe yes.
echo.
pause

tar -czf "%TEMP%\paso-largo.tgz" --exclude=.venv --exclude=.git --exclude=__pycache__ . || goto error
scp "%TEMP%\paso-largo.tgz" %SERVIDOR%:/tmp/paso-largo.tgz || goto error
ssh -t %SERVIDOR% "if [ -f ~/paso-largo/datos/asistencia.db ]; then echo 'El servidor ya tiene base de datos, no se copia nada.'; exit 1; fi; mkdir -p ~/paso-largo && tar -xzf /tmp/paso-largo.tgz -C ~/paso-largo && sed -i 's/\r$//' ~/paso-largo/servidor/*.sh && bash ~/paso-largo/servidor/instalar.sh" || goto error

echo.
echo  Listo. Desde ahora usa http://192.168.1.40:8000 y NO vuelvas a abrir iniciar.bat en esta PC.
pause
exit /b

:error
echo.
echo  Algo fallo. Copia el mensaje de arriba y mandaselo a Claude.
pause
