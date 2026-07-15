"""
Container Configuration Management

Centralized configuration for Docker container orchestration with
environment-based settings and secure credential management.
"""

import os
from typing import Dict, Optional, Any
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class ContainerConfig:
    """Container orchestration configuration."""
    
    # Image settings
    image_name: str = "kali-pentest:latest"
    registry_url: Optional[str] = None
    registry_username: Optional[str] = None
    registry_password: Optional[str] = None
    
    # Resource limits
    memory_limit: str = "2g"
    cpu_count: int = 2
    
    # Network settings
    network_mode: str = "bridge"
    
    # Container settings
    working_dir: str = "/workspace"
    container_prefix: str = "kali-container-"
    
    # Timeouts
    start_timeout: int = 60
    stop_timeout: int = 30
    command_timeout: int = 300
    
    # Environment variables
    default_env: Dict[str, str] = None
    
    def __post_init__(self):
        """Initialize default environment variables."""
        if self.default_env is None:
            self.default_env = {
                "PYTHONUNBUFFERED": "1",
                "TASK_MODE": "pentest",
                "DEBIAN_FRONTEND": "noninteractive"
            }
    
    @classmethod
    def from_environment(cls) -> 'ContainerConfig':
        """Create configuration from environment variables."""
        return cls(
            image_name=os.getenv("CONTAINER_IMAGE", "kali-pentest:latest"),
            registry_url=os.getenv("DOCKER_REGISTRY_URL"),
            registry_username=os.getenv("DOCKER_REGISTRY_USERNAME"),
            registry_password=os.getenv("DOCKER_REGISTRY_PASSWORD"),
            memory_limit=os.getenv("CONTAINER_MEMORY_LIMIT", "2g"),
            cpu_count=int(os.getenv("CONTAINER_CPU_COUNT", "2")),
            network_mode=os.getenv("CONTAINER_NETWORK_MODE", "bridge"),
            working_dir=os.getenv("CONTAINER_WORKING_DIR", "/workspace"),
            container_prefix=os.getenv("CONTAINER_PREFIX", "kali-container-"),
            start_timeout=int(os.getenv("CONTAINER_START_TIMEOUT", "60")),
            stop_timeout=int(os.getenv("CONTAINER_STOP_TIMEOUT", "30")),
            command_timeout=int(os.getenv("CONTAINER_COMMAND_TIMEOUT", "300"))
        )
    
    def to_docker_config(self) -> Dict[str, Any]:
        """Convert to Docker container configuration."""
        return {
            "detach": True,
            "network_mode": self.network_mode,
            "mem_limit": self.memory_limit,
            "cpu_count": self.cpu_count,
            "remove": False,
            "working_dir": self.working_dir,
            "environment": self.default_env.copy()
        }
    
    def validate(self) -> bool:
        """Validate configuration settings."""
        try:
            # Validate memory limit format
            if not self.memory_limit.endswith(('m', 'g', 'M', 'G')):
                logger.error(f"Invalid memory limit format: {self.memory_limit}")
                return False
            
            # Validate CPU count
            if self.cpu_count <= 0:
                logger.error(f"Invalid CPU count: {self.cpu_count}")
                return False
            
            # Validate timeouts
            if any(timeout <= 0 for timeout in [self.start_timeout, self.stop_timeout, self.command_timeout]):
                logger.error("All timeouts must be positive integers")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Configuration validation failed: {e}")
            return False

# Global configuration instance
_container_config: Optional[ContainerConfig] = None

def get_container_config() -> ContainerConfig:
    """Get the global container configuration instance."""
    global _container_config
    
    if _container_config is None:
        _container_config = ContainerConfig.from_environment()
        
        if not _container_config.validate():
            logger.warning("Container configuration validation failed, using defaults")
            _container_config = ContainerConfig()
    
    return _container_config

def update_container_config(**kwargs) -> None:
    """Update the global container configuration."""
    global _container_config
    
    config = get_container_config()
    
    for key, value in kwargs.items():
        if hasattr(config, key):
            setattr(config, key, value)
        else:
            logger.warning(f"Unknown configuration key: {key}")
    
    if not config.validate():
        logger.error("Updated configuration is invalid")
        raise ValueError("Invalid container configuration")

def reset_container_config() -> None:
    """Reset container configuration to defaults."""
    global _container_config
    _container_config = None