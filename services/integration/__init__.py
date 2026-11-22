"""External integrations -- MaiBot, knowledge graphs, memory engines."""

from .maibot_integration_factory import MaiBotIntegrationFactory
from .maibot_adapters import MaiBotStyleAnalyzer, MaiBotLearningStrategy, MaiBotQualityMonitor
from .maibot_enhanced_learning_manager import MaiBotEnhancedLearningManager
from .exemplar_library import ExemplarLibrary
from .knowledge_graph_manager import KnowledgeGraphManager
from .lightrag_knowledge_manager import LightRAGKnowledgeManager
from .training_data_exporter import TrainingDataExporter


def __getattr__(name: str):
    """Load optional memory engines only when a caller explicitly requests one."""
    if name == "Mem0MemoryManager":
        from .mem0_memory_manager import Mem0MemoryManager

        return Mem0MemoryManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "MaiBotIntegrationFactory",
    "MaiBotStyleAnalyzer",
    "MaiBotLearningStrategy",
    "MaiBotQualityMonitor",
    "MaiBotEnhancedLearningManager",
    "ExemplarLibrary",
    "KnowledgeGraphManager",
    "LightRAGKnowledgeManager",
    "Mem0MemoryManager",
    "TrainingDataExporter",
]
