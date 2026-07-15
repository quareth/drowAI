""" knowledge adapter contracts and registry internals.

This package defines deterministic semantic adapter interfaces and dispatch
helpers used by the backend service boundary."""

from .masscan_adapter import MasscanKnowledgeAdapter
from .msfconsole_adapter import MsfconsoleKnowledgeAdapter
from .nmap_adapter import NmapKnowledgeAdapter
from .ffuf_adapter import FfufKnowledgeAdapter
from .fping_adapter import FpingKnowledgeAdapter
from .gobuster_adapter import GobusterKnowledgeAdapter
from .hydra_adapter import HydraKnowledgeAdapter
from .nuclei_adapter import NucleiKnowledgeAdapter
from .sqlmap_adapter import SqlmapKnowledgeAdapter
from .tshark_adapter import TsharkKnowledgeAdapter

__all__ = [
    "FfufKnowledgeAdapter",
    "FpingKnowledgeAdapter",
    "GobusterKnowledgeAdapter",
    "HydraKnowledgeAdapter",
    "MasscanKnowledgeAdapter",
    "MsfconsoleKnowledgeAdapter",
    "NmapKnowledgeAdapter",
    "NucleiKnowledgeAdapter",
    "SqlmapKnowledgeAdapter",
    "TsharkKnowledgeAdapter",
]
