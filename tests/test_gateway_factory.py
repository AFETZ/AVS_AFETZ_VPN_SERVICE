from types import SimpleNamespace

from app.bot.payment_gateways.gateway_factory import GatewayFactory


def test_gateway_factory_sorts_gateways_by_runtime_plan_service_order():
    factory = GatewayFactory()
    factory._plan_service = SimpleNamespace(  # noqa: SLF001 - intentional focused unit test
        get_payment_order=lambda: ["pay_yookassa", "pay_telegram_stars"]
    )
    factory._gateways = {  # noqa: SLF001 - intentional focused unit test
        "pay_telegram_stars": SimpleNamespace(callback="pay_telegram_stars", name="Stars"),
        "pay_yookassa": SimpleNamespace(callback="pay_yookassa", name="YooKassa"),
    }

    assert [gateway.callback for gateway in factory.get_gateways()] == [
        "pay_yookassa",
        "pay_telegram_stars",
    ]


def test_gateway_factory_sorts_lazy_gateway_names():
    class LazyName:
        def __init__(self, value):
            self.value = value

        def __str__(self):
            return self.value

    factory = GatewayFactory()
    factory._plan_service = SimpleNamespace(get_payment_order=lambda: [])  # noqa: SLF001
    factory._gateways = {  # noqa: SLF001
        "b": SimpleNamespace(callback="b", name=LazyName("B")),
        "a": SimpleNamespace(callback="a", name=LazyName("A")),
    }

    assert [gateway.callback for gateway in factory.get_gateways()] == ["a", "b"]
