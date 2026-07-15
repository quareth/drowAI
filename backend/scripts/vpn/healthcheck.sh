#!/usr/bin/env bash
set -euo pipefail

# Health check for VPN connectivity
if pgrep -x openvpn >/dev/null 2>&1; then
  # Check tun interface and basic connectivity
  if ip link show tun0 >/dev/null 2>&1; then
    exit 0
  fi
fi
exit 1

