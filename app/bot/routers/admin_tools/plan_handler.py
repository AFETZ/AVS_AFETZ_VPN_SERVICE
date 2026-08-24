import json
import logging
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsAdmin
from app.bot.models import Plan, ServicesContainer
from app.bot.payment_gateways import GatewayFactory
from app.bot.routers.misc.keyboard import back_keyboard
from app.bot.utils.navigation import NavAdminTools
from app.db.models import User

from .keyboard import (
    confirm_delete_plan_keyboard,
    payment_order_keyboard,
    plan_details_keyboard,
    plan_editor_keyboard,
    plan_list_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name=__name__)

PLAN_EDIT_CODE_KEY = "plan_edit_code"
PLAN_MAIN_MESSAGE_ID_KEY = "plan_main_message_id"


class PlanEditorStates(StatesGroup):
    waiting_add_json = State()
    waiting_edit_json = State()


@router.callback_query(F.data == NavAdminTools.PLAN_EDITOR, IsAdmin())
async def callback_plan_editor(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info("Admin %s opened plan editor.", user.tg_id)
    await _render_plan_editor(
        callback.message,
        services=services,
        gateway_factory=gateway_factory,
    )


@router.callback_query(F.data == NavAdminTools.PLAN_LIST, IsAdmin())
async def callback_plan_list(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    logger.info("Admin %s opened plan list.", user.tg_id)
    await callback.message.edit_text(
        text="📋 <b>Тарифы</b>\n\n👁 публичный тариф, 🙈 скрытый/служебный тариф.",
        reply_markup=plan_list_keyboard(services.plan.get_all_plan_records()),
    )


@router.callback_query(F.data.startswith(NavAdminTools.PLAN_SHOW.value + ":"), IsAdmin())
async def callback_plan_show(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = services.plan.get_plan_by_code(plan_code)
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await callback.message.edit_text(
        text=_build_plan_details_text(plan),
        reply_markup=plan_details_keyboard(plan.code),
    )


@router.callback_query(F.data == NavAdminTools.PLAN_ADD, IsAdmin())
async def callback_plan_add(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
) -> None:
    logger.info("Admin %s started adding a plan.", user.tg_id)
    await state.set_state(PlanEditorStates.waiting_add_json)
    await state.update_data({PLAN_MAIN_MESSAGE_ID_KEY: callback.message.message_id})
    await callback.message.edit_text(
        text=(
            "➕ <b>Добавить тариф</b>\n\n"
            "Отправьте JSON объекта тарифа. Обязательные поля: "
            "<code>code</code>, <code>devices</code>, <code>prices</code>.\n\n"
            "Пример:\n"
            "<pre>{\n"
            '  "code": "p10",\n'
            '  "devices": 10,\n'
            '  "title": "10 устройств",\n'
            '  "prices": {"RUB": {"30": 999}}\n'
            "}</pre>"
        ),
        reply_markup=back_keyboard(NavAdminTools.PLAN_EDITOR),
    )


@router.message(PlanEditorStates.waiting_add_json, IsAdmin())
async def handle_plan_add_json(
    message: Message,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    try:
        plan = services.plan.parse_plan_json(message.text or "")
        services.plan.add_plan(plan)
    except Exception as exception:
        await message.answer(f"❌ Не удалось добавить тариф: <code>{escape(str(exception))}</code>")
        return

    await state.set_state(None)
    data = await state.get_data()
    main_message_id = data.get(PLAN_MAIN_MESSAGE_ID_KEY)
    await message.bot.edit_message_text(
        text=f"✅ Тариф <code>{escape(plan.code)}</code> добавлен.",
        chat_id=message.chat.id,
        message_id=main_message_id,
        reply_markup=plan_details_keyboard(plan.code),
    )


@router.callback_query(F.data.startswith(NavAdminTools.PLAN_EDIT_JSON.value + ":"), IsAdmin())
async def callback_plan_edit_json(
    callback: CallbackQuery,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = services.plan.get_plan_by_code(plan_code)
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    await state.set_state(PlanEditorStates.waiting_edit_json)
    await state.update_data(
        {
            PLAN_EDIT_CODE_KEY: plan.code,
            PLAN_MAIN_MESSAGE_ID_KEY: callback.message.message_id,
        }
    )
    await callback.message.edit_text(
        text=(
            "✏️ <b>Редактировать тариф JSON</b>\n\n"
            "Отправьте полный JSON тарифа. Можно менять code, title, prices, "
            "public/hidden и upgrade-связи.\n\n"
            f"<pre>{escape(_plan_to_json(plan))}</pre>"
        ),
        reply_markup=back_keyboard(f"{NavAdminTools.PLAN_SHOW.value}:{plan.code}"),
    )


@router.message(PlanEditorStates.waiting_edit_json, IsAdmin())
async def handle_plan_edit_json(
    message: Message,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
) -> None:
    data = await state.get_data()
    previous_code = data.get(PLAN_EDIT_CODE_KEY)
    main_message_id = data.get(PLAN_MAIN_MESSAGE_ID_KEY)
    if not previous_code:
        await state.set_state(None)
        await message.answer("❌ Не найден контекст редактирования тарифа.")
        return

    try:
        plan = services.plan.parse_plan_json(message.text or "")
        services.plan.update_plan(previous_code, plan)
    except Exception as exception:
        await message.answer(f"❌ Не удалось сохранить тариф: <code>{escape(str(exception))}</code>")
        return

    await state.set_state(None)
    await message.bot.edit_message_text(
        text=f"✅ Тариф <code>{escape(previous_code)}</code> сохранен как <code>{escape(plan.code)}</code>.",
        chat_id=message.chat.id,
        message_id=main_message_id,
        reply_markup=plan_details_keyboard(plan.code),
    )


@router.callback_query(F.data.startswith(NavAdminTools.PLAN_DELETE.value + ":"), IsAdmin())
async def callback_plan_delete(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = services.plan.get_plan_by_code(plan_code)
    if not plan:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    users_count = await _count_users_on_plan(session=session, plan_code=plan_code)
    if users_count > 0:
        await callback.message.edit_text(
            text=(
                "🛑 <b>Удаление заблокировано</b>\n\n"
                f"Тариф <code>{escape(plan_code)}</code> сейчас указан у пользователей: <b>{users_count}</b>.\n"
                "Чтобы убрать тариф из продажи безопасно, отредактируйте JSON и поставьте "
                "<code>\"is_public\": false</code>."
            ),
            reply_markup=plan_details_keyboard(plan_code),
        )
        return

    await callback.message.edit_text(
        text=f"Удалить тариф <code>{escape(plan_code)}</code>?",
        reply_markup=confirm_delete_plan_keyboard(plan_code),
    )


@router.callback_query(F.data.startswith(NavAdminTools.PLAN_CONFIRM_DELETE.value + ":"), IsAdmin())
async def callback_plan_confirm_delete(
    callback: CallbackQuery,
    user: User,
    session: AsyncSession,
    services: ServicesContainer,
) -> None:
    plan_code = callback.data.split(":", 1)[1]
    users_count = await _count_users_on_plan(session=session, plan_code=plan_code)
    if users_count > 0:
        await callback.answer("У тарифа появились пользователи, удаление отменено.", show_alert=True)
        return

    try:
        services.plan.delete_plan(plan_code)
    except Exception as exception:
        await callback.answer(f"Не удалось удалить: {exception}", show_alert=True)
        return

    await callback.message.edit_text(
        text=f"✅ Тариф <code>{escape(plan_code)}</code> удален.",
        reply_markup=plan_list_keyboard(services.plan.get_all_plan_records()),
    )


@router.callback_query(F.data == NavAdminTools.PAYMENT_ORDER, IsAdmin())
async def callback_payment_order(
    callback: CallbackQuery,
    user: User,
    gateway_factory: GatewayFactory,
) -> None:
    logger.info("Admin %s opened payment order editor.", user.tg_id)
    await _render_payment_order(callback.message, gateway_factory=gateway_factory)


@router.callback_query(F.data.startswith(NavAdminTools.PAYMENT_MOVE_UP.value + ":"), IsAdmin())
@router.callback_query(F.data.startswith(NavAdminTools.PAYMENT_MOVE_DOWN.value + ":"), IsAdmin())
async def callback_payment_move(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    action, callback_name = callback.data.split(":", 1)
    direction = -1 if action == NavAdminTools.PAYMENT_MOVE_UP.value else 1
    try:
        services.plan.move_payment_method(callback_name, direction)
    except Exception as exception:
        await callback.answer(f"Не удалось изменить порядок: {exception}", show_alert=True)
        return

    await callback.answer("Порядок обновлен.")
    await _render_payment_order(callback.message, gateway_factory=gateway_factory)


async def _render_plan_editor(
    message,
    *,
    services: ServicesContainer,
    gateway_factory: GatewayFactory,
) -> None:
    plans = services.plan.get_all_plan_records()
    public_count = sum(1 for plan in plans if plan.is_public)
    gateways = gateway_factory.get_gateways()
    gateway_names = ", ".join(str(gateway.name) for gateway in gateways) or "—"
    await message.edit_text(
        text=(
            "📦 <b>Тарифы и платежи</b>\n\n"
            f"Всего тарифов: <b>{len(plans)}</b>\n"
            f"Публичных тарифов: <b>{public_count}</b>\n"
            f"Доступные сроки: <code>{services.plan.get_durations()}</code>\n"
            f"Порядок платежей: <b>{escape(gateway_names)}</b>"
        ),
        reply_markup=plan_editor_keyboard(),
    )


async def _render_payment_order(message, *, gateway_factory: GatewayFactory) -> None:
    await message.edit_text(
        text=(
            "💳 <b>Порядок платежных кнопок</b>\n\n"
            "Кнопки оплаты в пользовательском выборе будут показаны в этом порядке. "
            "Изменения применяются сразу, без перезапуска."
        ),
        reply_markup=payment_order_keyboard(gateway_factory.get_gateways()),
    )


def _build_plan_details_text(plan: Plan) -> str:
    return (
        "📦 <b>Тариф</b>\n\n"
        f"Code: <code>{escape(plan.code)}</code>\n"
        f"Название: <b>{escape(plan.title or '—')}</b>\n"
        f"Устройств: <b>{plan.devices}</b>\n"
        f"Публичный: <b>{'да' if plan.is_public else 'нет'}</b>\n"
        f"Популярный: <b>{'да' if plan.is_popular else 'нет'}</b>\n"
        f"Обход БС: <b>{'да' if plan.includes_additional_profile else 'нет'}</b>\n"
        f"Upgrade from: <code>{escape(plan.upgrade_from or '—')}</code>\n\n"
        f"<pre>{escape(_plan_to_json(plan))}</pre>"
    )


def _plan_to_json(plan: Plan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)


async def _count_users_on_plan(*, session: AsyncSession, plan_code: str) -> int:
    return (
        await session.execute(
            select(func.count(User.id)).where(User.current_plan_code == plan_code)
        )
    ).scalar() or 0
