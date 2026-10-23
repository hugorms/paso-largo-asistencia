#!/usr/bin/env bash
# Instala Docker (si falta) y levanta Paso Largo como servicio. Se puede volver a correr sin problema.
set -e
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo "Instalando Docker..."
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

# Nombre fijo en la red: http://restaurante.local:8000 aunque cambie la IP
if ! systemctl is-active --quiet avahi-daemon; then
  sudo apt-get install -y avahi-daemon
fi

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 8000/tcp
  sudo ufw allow 37020/udp
fi

mkdir -p datos
sudo docker compose -f servidor/docker-compose.yml up -d --build

IP=$(hostname -I | awk '{print $1}')
echo
echo "  Paso Largo listo:  http://$IP:8000   o   http://$(hostname).local:8000"
echo "  Ver registros:     sudo docker logs -f paso-largo"
