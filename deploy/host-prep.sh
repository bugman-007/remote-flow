#!/usr/bin/env bash
# OPS-8: prepare a fresh Ubuntu host for Remote Flow.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root (sudo $0)" >&2
  exit 1
fi

STORAGE_DIR="${STORAGE_DIR:-/srv/remote-flow/storage}"

echo "==> Installing Docker Engine and Compose plugin"
apt-get update
apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades
install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.asc ]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "==> 2 GB swapfile with vm.swappiness=10 (HW-3)"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
if ! grep -q '^vm.swappiness' /etc/sysctl.conf; then
  echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi
sysctl -w vm.swappiness=10

echo "==> Storage directory $STORAGE_DIR"
mkdir -p "$STORAGE_DIR"

echo "==> Unattended security updates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Firewall: 22/80/443 only"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "==> Done. Next: cp deploy/.env.example deploy/.env, edit secrets, then"
echo "    docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build"
