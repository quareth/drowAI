"""VPN configuration/status service for per-task container connectivity.

Scope:
- Validate and persist task VPN configuration.
- Read and update VPN status fields for tasks.
- Probe container VPN runtime health via `check_container_vpn_health`.

Boundary:
- Does NOT start/stop containers directly (delegates to Docker services).
- Does NOT collect general container resource metrics.
- Does NOT materialize provider-owned runtime VPN files.
"""

from typing import Optional, Dict, Tuple
import base64
import logging
import re
from datetime import datetime, timedelta
from backend.core.time_utils import format_iso, utc_now

from sqlalchemy.orm import Session

from backend.models.core import Task
from backend.schemas.vpn import VPNConfigCreate, VPNStatusResponse

logger = logging.getLogger(__name__)

# Throttle map to limit broadcast frequency per task/channel
_last_vpn_broadcast: Dict[int, datetime] = {}


class VPNService:
    """VPN configuration and management service following existing patterns"""

    def __init__(self, db: Session):
        self.db = db

    def configure_task_vpn(self, task_id: int, vpn_config: VPNConfigCreate) -> Tuple[bool, str]:
        """Validate and persist VPN metadata for a task."""
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return False, "Task not found"

            is_valid, validation_msg = self.validate_ovpn_content(vpn_config.config_data)
            if not is_valid:
                return False, f"Invalid VPN configuration: {validation_msg}"

            task.vpn_enabled = True
            task.vpn_provider = vpn_config.provider
            task.vpn_config_data = base64.b64encode(vpn_config.config_data.encode()).decode()
            task.vpn_connection_status = "configured"

            self.db.commit()
            logger.info("Persisted VPN metadata for task %s", task_id)
            return True, "VPN configured successfully"
        except Exception as e:
            logger.error("VPN configuration failed for task %s: %s", task_id, e)
            self.db.rollback()
            return False, str(e)

    def validate_ovpn_content(self, ovpn_content: str) -> Tuple[bool, str]:
        """Validate OVPN file content - basic validation"""
        try:
            if not ovpn_content or len(ovpn_content.strip()) < 50:
                return False, "OVPN content too short"

            required_directives = ["client", "remote", "dev"]
            for directive in required_directives:
                if directive not in ovpn_content:
                    return False, f"Missing required directive: {directive}"

            dangerous_patterns = ["--script-security", "up ", "down "]
            for pattern in dangerous_patterns:
                if pattern in ovpn_content:
                    return False, f"Potentially dangerous directive found: {pattern}"

            return True, "Valid OVPN configuration"
        except Exception as e:
            return False, f"Validation error: {str(e)}"

    async def get_vpn_status(self, task_id: int) -> Optional[VPNStatusResponse]:
        """Get VPN status for a task"""
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task or not task.vpn_enabled:
                return None

            return VPNStatusResponse(
                connection_status=task.vpn_connection_status,
                ip_address=task.vpn_ip_address,
                connected_at=task.vpn_connected_at,
                error_message=task.vpn_error_message,
            )
        except Exception as e:
            logger.error("Failed to get VPN status for task %s: %s", task_id, e)
            return None

    async def update_vpn_status(
        self,
        task_id: int,
        status: str,
        ip_address: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update VPN connection status"""
        try:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if task and task.vpn_enabled:
                task.vpn_connection_status = status
                task.vpn_ip_address = ip_address
                task.vpn_error_message = error_message
                if status == "connected":
                    task.vpn_connected_at = utc_now()
                elif status in {"configured", "connecting", "reconnecting", "disconnected", "failed"}:
                    task.vpn_connected_at = None

                self.db.commit()

                # Broadcast status to VPN WebSocket channel if there are subscribers
                try:
                    from backend.services.websocket.connection_manager import websocket_manager
                    # Throttle broadcasts to at most one every 2 seconds per task
                    now = utc_now()
                    last = _last_vpn_broadcast.get(task_id)
                    if not last or (now - last) >= timedelta(seconds=2):
                        message = {
                            "type": "vpn_status_update",
                            "timestamp": format_iso(now),
                            "data": {
                                "task_id": task_id,
                                "status": status,
                                "ip_address": ip_address,
                                "error_message": error_message,
                                "connection_time": task.vpn_connected_at.isoformat() if task.vpn_connected_at else None,
                            },
                        }
                        if websocket_manager.has_channel_subscribers(task_id, "vpn_status"):
                            await websocket_manager.broadcast_to_task_channel(task_id, "vpn_status", message)
                            _last_vpn_broadcast[task_id] = now
                except Exception as broadcast_error:
                    logger.debug(f"VPN status broadcast failed for task {task_id}: {broadcast_error}")
                return True
        except Exception as e:
            logger.error("Failed to update VPN status for task %s: %s", task_id, e)
            self.db.rollback()
        return False

    async def check_container_vpn_health(self, task_id: int, container) -> None:
        """Detect VPN process/tun0 state inside a running container and update status.

        Best-effort health probe extracted from metrics collection path.
        Any failures are intentionally swallowed to avoid impacting callers.
        """
        try:
            # Run via bash so pipes/redirection work and exit code reflects pgrep.
            run_openvpn_check = container.exec_run(
                ["bash", "-lc", "pgrep -x openvpn >/dev/null 2>&1"],
                stdout=True,
                stderr=True,
            )
            is_running = run_openvpn_check.exit_code == 0

            # Extract IPv4 of tun0; suppress errors via shell redirection.
            ip_cmd = (
                "ip -4 addr show dev tun0 2>/dev/null | "
                "awk '/inet /{print $2}' | cut -d/ -f1"
            )
            ip_res = container.exec_run(["bash", "-lc", ip_cmd], stdout=True, stderr=True)
            raw_ip = ip_res.output.decode().strip() if ip_res and ip_res.output else ""

            # Validate IPv4 format; ignore noisy stderr text.
            ip = raw_ip if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", raw_ip) else ""
            status = (
                "connected"
                if (is_running and ip)
                else ("connecting" if is_running else "disconnected")
            )
            await self.update_vpn_status(task_id, status, ip_address=ip or None)
        except Exception:
            pass
