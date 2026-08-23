from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.routers.admin_tools.plan_handler import _render_plan_editor


class LazyName:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


@pytest.mark.asyncio
async def test_render_plan_editor_accepts_lazy_gateway_names(monkeypatch):
    monkeypatch.setattr(
        "app.bot.routers.admin_tools.plan_handler.plan_editor_keyboard",
        lambda: None,
    )

    message = SimpleNamespace(edit_text=AsyncMock())
    services = SimpleNamespace(
        plan=SimpleNamespace(
            get_all_plan_records=lambda: [
                SimpleNamespace(is_public=True),
                SimpleNamespace(is_public=False),
            ],
            get_durations=lambda: [30, 60],
        )
    )
    gateway_factory = SimpleNamespace(
        get_gateways=lambda: [
            SimpleNamespace(name=LazyName("Stars")),
            SimpleNamespace(name=LazyName("YooKassa")),
        ]
    )

    await _render_plan_editor(
        message,
        services=services,
        gateway_factory=gateway_factory,
    )

    text = message.edit_text.await_args.kwargs["text"]
    assert "Stars, YooKassa" in text
