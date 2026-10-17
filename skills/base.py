from abc import ABC, abstractmethod
from typing import Any, Dict

class Skill(ABC):
    name: str = "unnamed"

    @abstractmethod
    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Input:  shared context dict (previous skills write into it)
        Output: dict merged back into the context
        """
        ...