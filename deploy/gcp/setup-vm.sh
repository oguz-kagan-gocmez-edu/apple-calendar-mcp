#!/usr/bin/env bash
# Prepare a fresh Debian 12 VM (e.g. the Google Cloud free e2-micro) to run the connector.
# Run on the VM:  bash setup-vm.sh
set -euo pipefail

# 1 GB of RAM is tight for building the image: add 1 GB of swap, once.
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# Docker Engine + compose plugin from Docker's official apt repository.
sudo apt-get update
sudo apt-get install -y ca-certificates curl git unattended-upgrades
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"

# The code, and a private .env to fill in.
if [ ! -d apple-calendar-mcp ]; then
  git clone https://github.com/oguz-kagan-gocmez-edu/apple-calendar-mcp.git
fi
cd apple-calendar-mcp
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
fi

ip=$(curl -fs -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)
cat <<NEXT

Done. Next:
  1. Log out and back in (so docker works without sudo).
  2. nano ~/apple-calendar-mcp/.env  and set:
       CALDAV_USERNAME, CALDAV_PASSWORD, CONNECTOR_PASSWORD,
       DOMAIN=${ip//./-}.sslip.io
       PUBLIC_URL=https://${ip//./-}.sslip.io
  3. cd ~/apple-calendar-mcp && docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build
NEXT
