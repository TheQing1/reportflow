"""架构约束的可执行版本：依赖方向只能单向。

文档里写"分层"很容易腐化——半年后某个人 `from ..persistence import SqlTaskStore`
加在调度器里，没人会注意。这里用 AST 把它变成会失败的测试。

允许的依赖方向（左 <- 右，右侧不得反向 import 左侧）：

    contracts <- core <- {persistence, agents} <- orchestrator <- assembler/cli
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: 每层允许 import 的本项目模块前缀
ALLOWED: dict[str, set[str]] = {
    "contracts": {"app.contracts"},
    "core": {"app.core", "app.contracts"},
    "persistence": {"app.persistence", "app.contracts", "app.core"},
    "agents": {"app.agents", "app.contracts", "app.core"},
    "llm": {"app.llm", "app.contracts", "app.core"},
    "rag": {"app.rag", "app.contracts", "app.core"},
    # 工具层：与 rag/agents 平级的基础设施。只能依赖契约与 core，
    # **不能**依赖 agents（否则 Agent↔工具 双向依赖，加工具就要改 Agent）
    "tools": {"app.tools", "app.contracts", "app.core"},
    "orchestrator": {"app.orchestrator", "app.contracts", "app.core"},
    # 评测是应用层：可以读 runtime、复用 agents 的 JSON 解析，但不该被生产路径依赖
    "evaluation": {
        "app.evaluation",
        "app.contracts",
        "app.core",
        "app.agents",
        "app.assembler",
    },
    # 发布前体检：应用层，复用评测里的抽样与审计能力（只标记，不参与写作循环）
    "checkup": {"app.checkup", "app.contracts", "app.core", "app.evaluation"},
    # 装配根与入口允许依赖所有具体实现
    "api": {"app"},
    "assembler": {"app"},
    "cli": {"app"},
    # 开发脚本不受分层约束（不是生产路径）
    "scripts": {"app"},
}


def _dotted(path: Path) -> list[str]:
    rel = path.relative_to(APP_ROOT.parent)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return parts


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parts = _dotted(path)
    # __init__.py 的"当前包"就是自身路径，普通模块的当前包是父路径
    is_package = path.name == "__init__.py"
    package = parts if is_package else parts[:-1]
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            module = node.module or ""
            if level == 0:
                found.add(module)
                continue
            base = package[: len(package) - (level - 1)] if level > 1 else package
            found.add(".".join([*base, module]) if module else ".".join(base))
    return {m for m in found if m == "app" or m.startswith("app.")}


class TestDependencyDirection(unittest.TestCase):
    def test_no_layer_violation(self) -> None:
        violations: list[str] = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            parts = _dotted(path)
            if len(parts) < 3:  # app/__init__.py、app/cli.py 等入口
                continue
            layer = parts[1]
            allowed = ALLOWED.get(layer)
            if allowed is None:
                continue
            for mod in _imported_modules(path):
                if not any(mod == a or mod.startswith(a + ".") for a in allowed):
                    violations.append(f"{path.relative_to(APP_ROOT.parent)} -> {mod}")

        self.assertEqual(violations, [], "存在反向依赖:\n" + "\n".join(violations))

    def test_contracts_is_dependency_free(self) -> None:
        """契约层不允许 import 任何 app 内部模块（只能 import 同层与标准库）。"""
        for path in sorted((APP_ROOT / "contracts").glob("*.py")):
            for mod in _imported_modules(path):
                if not mod.startswith("app.contracts"):
                    self.fail(f"{path.name} 依赖了 {mod}，契约层必须零内部依赖")

    def test_orchestrator_does_not_know_concrete_implementation(self) -> None:
        """编排层不得引用 persistence / agents 的具体实现。"""
        for path in sorted((APP_ROOT / "orchestrator").glob("*.py")):
            for mod in _imported_modules(path):
                self.assertFalse(
                    mod.startswith(("app.persistence", "app.agents")),
                    f"{path.name} 依赖了具体实现 {mod}",
                )

    def test_tools_does_not_depend_on_agents(self) -> None:
        """工具层不得依赖 Agent 层。

        一旦反向依赖，就变成 Agent↔工具 双向耦合：
        想加一个工具得改 Agent 的类型注解，想改 Agent 又得动工具。
        工具只应认识 contracts 里的 Tool/ToolResult/ToolSpec。
        """
        for path in sorted((APP_ROOT / "tools").glob("*.py")):
            for mod in _imported_modules(path):
                self.assertFalse(
                    mod.startswith("app.agents"),
                    f"{path.name} 依赖了 Agent 层 {mod}",
                )


if __name__ == "__main__":
    unittest.main()
