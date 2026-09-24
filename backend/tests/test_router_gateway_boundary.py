"""架构边界断言：路由模块不再直接依赖 RailPlacement 的散落更新路径。

- 路由不得 import / 构造 RailPlacement，不得 import Segment 自行拼段；
- 路由不得出现任何 .active 读写（散落改 active 的退路被封死）；
- 上杆 / 取件 必须经网关写端口改库；
- app 包内 RailPlacement 只允许出现在模型定义、种子数据与占位网关中。
"""

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
ROUTER = APP_DIR / "api" / "router.py"

# RailPlacement 允许出现的文件：模型定义、演示种子、占位网关
ALLOWED_PLACEMENT_FILES = {
    APP_DIR / "models" / "models.py",
    APP_DIR / "services" / "seed.py",
    APP_DIR / "services" / "placement_gateway.py",
}


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_router_does_not_import_railplacement_or_segment():
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    assert "RailPlacement" not in imported
    assert "Segment" not in imported


def test_router_has_no_scattered_active_access():
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "active"
    ]
    assert offenders == [], f"router.py 仍存在散落的 .active 读写，行号: {offenders}"


def test_router_source_has_no_railplacement_reference():
    source = ROUTER.read_text(encoding="utf-8")
    assert "RailPlacement" not in source


def test_router_mutates_placements_only_via_gateway_write_port():
    tree = ast.parse(ROUTER.read_text(encoding="utf-8"))
    gateway_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "placement_gateway"
    }
    # 上杆写入占位、取件释放占位都必须经过写端口
    assert "write_placement" in gateway_calls
    assert "release_for_order" in gateway_calls


def test_railplacement_only_lives_in_allowed_modules():
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path in ALLOWED_PLACEMENT_FILES:
            continue
        if "RailPlacement" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(APP_DIR).as_posix())
    assert offenders == [], f"RailPlacement 散落到了网关之外的模块: {offenders}"
