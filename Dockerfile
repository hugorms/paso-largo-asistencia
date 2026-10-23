# Imagen de Paso Largo para Dokploy (o cualquier servidor con Docker)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASISTENCIA_DATA=/data \
    TZ=America/Caracas

# tzdata: la hora del contenedor se usa para calcular la asistencia y corregir el reloj del equipo
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Fecha de esta versión (se muestra al pie del menú para saber si ya llegó una actualización)
RUN date +"%d/%m/%Y %H:%M" > /app/VERSION

# Base de datos, fotos y clave de sesión: montar un volumen aquí para no perderlos en cada despliegue
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4)"

# Un solo proceso: el hilo que lee los eventos del equipo no debe duplicarse
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
