"""适配器共享辅助：把 LoopController 的 GovernanceResult 转成自然语言。

注意：本模块已从 loop_controller 核心包移出，仅保留作为旧示例兼容。
新代码应直接从 loop_controller.formatting 导入 format_governance_result。
"""

from __future__ import annotations

from loop_controller.formatting import format_governance_result

__all__ = ["format_governance_result"]
