from backend.services.vpn_service import VPNService
from backend.models.core import Task
from backend.schemas.vpn import VPNConfigCreate
from backend.database import SessionLocal


def test_vpn_service_configuration(monkeypatch):
    db = SessionLocal()
    try:
        # Ensure a task exists
        task = Task(user_id=1, name="t1")
        db.add(task)
        db.commit()
        db.refresh(task)

        service = VPNService(db)
        vpn_config = VPNConfigCreate(provider="htb", config_data="""client
dev tun
remote 1.2.3.4 1194
proto udp
resolv-retry infinite
nobind
persist-key
persist-tun
verb 3
""")

        ok, msg = service.configure_task_vpn(task.id, vpn_config)
        assert ok, msg

        # Validate persisted fields
        refreshed = db.query(Task).filter(Task.id == task.id).first()
        assert refreshed.vpn_enabled is True
        assert refreshed.vpn_provider == "htb"
        assert refreshed.vpn_connection_status == "configured"
    finally:
        db.close()


def test_ovpn_validation():
    db = SessionLocal()
    try:
        service = VPNService(db)
        ok, msg = service.validate_ovpn_content("client\ndev tun\n")
        assert ok is False

        ok2, _ = service.validate_ovpn_content(
            "client\nremote x 1194\ndev tun\nproto udp\n" + ("a" * 60)
        )
        assert ok2 is True
    finally:
        db.close()

