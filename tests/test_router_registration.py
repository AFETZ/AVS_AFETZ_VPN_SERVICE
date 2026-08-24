from pathlib import Path


def test_admin_tool_routers_are_registered() -> None:
    source = Path("app/bot/routers/__init__.py").read_text(encoding="utf-8")

    for router_name in (
        "admin_tools.health_handler.router",
        "admin_tools.plan_handler.router",
    ):
        assert router_name in source
