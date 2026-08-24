import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.i18n import gettext as _
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import IsAdmin
from app.bot.models import ServicesContainer
from app.bot.utils.constants import (
    MAIN_MEDIA_MESSAGE_ID_KEY,
    MAIN_MESSAGE_ID_KEY,
)
from app.bot.utils.navigation import NavMain
from app.config import Config
from app.db.models import Invite, Referral, User

from .keyboard import main_menu_keyboard

logger = logging.getLogger(__name__)
router = Router(name=__name__)
REPLY_KB_MESSAGE_ID_KEY = "reply_kb_message_id"
MAIN_MENU_REPLY_BUTTON_TEXT = "Меню"
MAIN_MENU_REPLY_KB_HINT = "Меню всегда доступно по кнопке ниже 🔽"


def main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MAIN_MENU_REPLY_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Меню",
    )


async def present_main_menu(
    *,
    bot: Bot,
    user: User,
    services: ServicesContainer,
    config: Config,
) -> dict[str, int]:
    """Send the main menu and (re)install the persistent "Меню" reply keyboard.

    The reply keyboard is attached to a message we keep alive: the start banner
    when one is configured, otherwise a lightweight hint message. Telegram drops a
    reply keyboard when its carrier message is deleted, so the carrier must NOT be
    deleted here — it is tracked and cleaned up only on the next menu open. Returns
    the ids of the messages created so the caller can persist them in state.
    """
    reply_keyboard = main_menu_reply_keyboard()
    created: dict[str, int] = {}

    media_message_id: int | None = None
    if config.bot.START_IMAGE:
        try:
            start_image = (
                FSInputFile(config.bot.START_IMAGE)
                if Path(config.bot.START_IMAGE).exists()
                else config.bot.START_IMAGE
            )
            start_media = await bot.send_photo(
                chat_id=user.tg_id,
                photo=start_image,
                reply_markup=reply_keyboard,
            )
            media_message_id = start_media.message_id
        except Exception as exception:
            logger.error(f"Failed to send start image for user {user.tg_id}: {exception}")

    if media_message_id is not None:
        created[MAIN_MEDIA_MESSAGE_ID_KEY] = media_message_id
    else:
        # No banner to carry the keyboard, so attach it to a small hint message.
        try:
            carrier = await bot.send_message(
                chat_id=user.tg_id,
                text=MAIN_MENU_REPLY_KB_HINT,
                reply_markup=reply_keyboard,
            )
            created[REPLY_KB_MESSAGE_ID_KEY] = carrier.message_id
        except Exception as exception:
            logger.debug(
                "Failed to install main menu reply keyboard for user %s: %s",
                user.tg_id,
                exception,
            )

    is_admin = await IsAdmin()(user_id=user.tg_id)
    text = _("main_menu:message:main").format(name=user.first_name)
    main_menu = await bot.send_message(
        chat_id=user.tg_id,
        text=text,
        reply_markup=main_menu_keyboard(
            is_admin,
            is_referral_available=config.shop.REFERRER_REWARD_ENABLED,
            is_trial_available=await services.subscription.is_trial_available(user),
            is_referred_trial_available=await services.referral.is_referred_trial_available(user),
        ),
    )
    created[MAIN_MESSAGE_ID_KEY] = main_menu.message_id
    return created


async def process_invite_attribution(session: AsyncSession, user: User, invite_hash: str) -> bool:
    logger.info(f"Checking invite {invite_hash} for user {user.tg_id}")
    try:
        invite = await Invite.get_by_hash(session=session, hash_code=invite_hash)
        if not invite or not invite.is_active:
            logger.info(f"Invalid or inactive invite hash: {invite_hash}")
            return False

        user.source_invite_name = invite.name
        await session.commit()

        await Invite.increment_clicks(session=session, invite_id=invite.id)

        logger.info(f"User {user.tg_id} attributed to invite {invite.name}")
        return True
    except Exception as exception:
        logger.critical(f"Invite attribution error for user {user.tg_id}: {exception}")
        return False


async def process_creating_referral(session: AsyncSession, user: User, referrer_id: int) -> bool:
    logger.info(f"Assigning user {user.tg_id} as a referred to a referrer user {referrer_id}")
    try:
        referrer = await User.get(session=session, tg_id=referrer_id)
        if not referrer or referrer.tg_id == user.tg_id:
            logger.info(
                f"Failed to assign user {user.tg_id} as a referred to a referrer user {referrer_id}."
                f"Invalid string received."
            )
            return False

        await Referral.create(
            session=session, referrer_tg_id=referrer.tg_id, referred_tg_id=user.tg_id
        )
        logger.info(
            f"User {user.tg_id} assigned as referred to a referrer with tg id {referrer.tg_id}"
        )
        return True
    except Exception as exception:
        logger.critical(
            f"Referral creation error for {user.tg_id} (arg: {referrer_id}): {exception}"
        )
        return False


@router.message(Command(NavMain.START, NavMain.MENU))
async def command_main_menu(
    message: Message,
    user: User,
    state: FSMContext,
    services: ServicesContainer,
    config: Config,
    session: AsyncSession,
    command: CommandObject,
    is_new_user: bool,
) -> None:
    logger.info(f"User {user.tg_id} opened main menu page.")
    previous_message_id = await state.get_value(MAIN_MESSAGE_ID_KEY)
    previous_media_message_id = await state.get_value(MAIN_MEDIA_MESSAGE_ID_KEY)
    previous_reply_kb_id = await state.get_value(REPLY_KB_MESSAGE_ID_KEY)

    for message_id in {previous_message_id, previous_media_message_id, previous_reply_kb_id} - {None}:
        try:
            await message.bot.delete_message(chat_id=user.tg_id, message_id=message_id)
            logger.debug(f"Main message {message_id} for user {user.tg_id} deleted.")
        except Exception as exception:
            logger.error(f"Failed to delete main message {message_id} for user {user.tg_id}: {exception}")

    await state.clear()

    if command.args and is_new_user:
        if command.args.isdigit():
            await process_creating_referral(
                session=session, user=user, referrer_id=int(command.args)
            )
        else:
            await process_invite_attribution(session=session, user=user, invite_hash=command.args)

    created = await present_main_menu(
        bot=message.bot,
        user=user,
        services=services,
        config=config,
    )
    await state.update_data(created)


@router.message(F.text == MAIN_MENU_REPLY_BUTTON_TEXT)
async def reply_button_main_menu(
    message: Message,
    user: User,
    services: ServicesContainer,
    state: FSMContext,
    config: Config,
) -> None:
    logger.info(f"User {user.tg_id} opened main menu via reply keyboard.")
    previous_message_id = await state.get_value(MAIN_MESSAGE_ID_KEY)
    previous_media_message_id = await state.get_value(MAIN_MEDIA_MESSAGE_ID_KEY)
    previous_reply_kb_id = await state.get_value(REPLY_KB_MESSAGE_ID_KEY)

    for message_id in {previous_message_id, previous_media_message_id, previous_reply_kb_id} - {None}:
        try:
            await message.bot.delete_message(chat_id=user.tg_id, message_id=message_id)
        except Exception as exception:
            logger.debug("Failed to delete previous main menu message %s: %s", message_id, exception)

    await state.clear()
    created = await present_main_menu(
        bot=message.bot,
        user=user,
        services=services,
        config=config,
    )
    await state.update_data(created)


@router.callback_query(F.data == NavMain.MAIN_MENU)
async def callback_main_menu(
    callback: CallbackQuery,
    user: User,
    services: ServicesContainer,
    state: FSMContext,
    config: Config,
) -> None:
    logger.info(f"User {user.tg_id} returned to main menu page.")
    await state.clear()
    is_admin = await IsAdmin()(user_id=user.tg_id)
    reply_markup = main_menu_keyboard(
        is_admin,
        is_referral_available=config.shop.REFERRER_REWARD_ENABLED,
        is_trial_available=await services.subscription.is_trial_available(user),
        is_referred_trial_available=await services.referral.is_referred_trial_available(user),
    )
    text = _("main_menu:message:main").format(name=user.first_name)

    await callback.message.edit_text(text=text, reply_markup=reply_markup)
    await state.update_data({MAIN_MESSAGE_ID_KEY: callback.message.message_id})
    await callback.answer()


async def redirect_to_main_menu(
    bot: Bot,
    user: User,
    services: ServicesContainer,
    config: Config,
    storage: RedisStorage | None = None,
    state: FSMContext | None = None,
) -> None:
    logger.info(f"User {user.tg_id} redirected to main menu page.")

    if not state:
        state: FSMContext = FSMContext(
            storage=storage,
            key=StorageKey(bot_id=bot.id, chat_id=user.tg_id, user_id=user.tg_id),
        )

    main_message_id = await state.get_value(MAIN_MESSAGE_ID_KEY)
    is_admin = await IsAdmin()(user_id=user.tg_id)
    reply_markup = main_menu_keyboard(
        is_admin,
        is_referral_available=config.shop.REFERRER_REWARD_ENABLED,
        is_trial_available=await services.subscription.is_trial_available(user),
        is_referred_trial_available=await services.referral.is_referred_trial_available(user),
    )
    text = _("main_menu:message:main").format(name=user.first_name)

    try:
        await bot.edit_message_text(
            text=text,
            chat_id=user.tg_id,
            message_id=main_message_id,
            reply_markup=reply_markup,
        )
    except Exception as exception:
        logger.error(f"Error redirecting to main menu page: {exception}")
