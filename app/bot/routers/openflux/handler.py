"""OpenFlux pilot for administrators and invited users; independent of billing."""

import json
import shlex
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, CallbackQuery, CopyTextButton, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.routers.misc.keyboard import back_button, back_to_main_menu_button
from app.bot.utils.navigation import NavOpenFlux
from app.bot.utils.openflux_links import mobile_import_url, valid_doc_url
from app.bot.utils.public_links import subscription_base_url
from app.config import DEFAULT_DATA_DIR, Config, OpenFluxConfig

router = Router(name=__name__)
UPSTREAM_COMMIT = "a8a8937c59fbb275603bd53e150ecaf46e92148e"
UPSTREAM_URL = f"https://github.com/p1neappleXpress/OpenFlux/tree/{UPSTREAM_COMMIT}"
ARTIFACT_DIR = DEFAULT_DATA_DIR / "openflux"
ARTIFACTS = {
    NavOpenFlux.DOWNLOAD_WINDOWS: "openflux-windows-amd64.zip",
    NavOpenFlux.DOWNLOAD_LINUX: "openflux-linux-amd64.zip",
    NavOpenFlux.DOWNLOAD_ANDROID: "afz-openflux-android.apk",
    NavOpenFlux.DOWNLOAD_SOURCE: "afz-openflux-mobile-source.zip",
}


def pilot_available(config: Config, user_id: int) -> bool:
    settings = getattr(config, "openflux", OpenFluxConfig())
    return settings.ENABLED and (
        user_id in config.bot.ADMINS or user_id == config.bot.DEV_ID
        or user_id in settings.ALLOWED_USER_IDS
    )


def mobile_import_buttons(config: Config) -> list[list[InlineKeyboardButton]]:
    document = config.openflux.DOC_URL
    if not valid_doc_url(document):
        return []
    rows = []
    base = subscription_base_url(config)
    if base:
        rows.append([InlineKeyboardButton(
            text="Добавить в AFZ OpenFlux", url=mobile_import_url(base, document),
        )])
    if len(document) <= 256:  # Telegram CopyTextButton limit.
        rows.append([InlineKeyboardButton(
            text="Скопировать настройки", copy_text=CopyTextButton(text=document),
        )])
    return rows


def mobile_import_markup(config: Config) -> InlineKeyboardMarkup | None:
    rows = mobile_import_buttons(config)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def client_command(doc_url: str, *, windows: bool) -> str:
    if not valid_doc_url(doc_url):
        raise ValueError("A valid shared Yandex document URL is required")
    binary = ".\\openflux.exe" if windows else "./openflux"
    quoted = "'" + doc_url.replace("'", "''") + "'" if windows else shlex.quote(doc_url)
    return f"{binary} --client --transport yandex --url {quoted} --socks5 127.0.0.1:1080"


def pilot_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Windows", callback_data=NavOpenFlux.WINDOWS),
         InlineKeyboardButton(text="Linux", callback_data=NavOpenFlux.LINUX)],
        [InlineKeyboardButton(text="Android", callback_data=NavOpenFlux.ANDROID),
         InlineKeyboardButton(text="iPhone", callback_data=NavOpenFlux.IOS)],
        [InlineKeyboardButton(text="О проекте OpenFlux", url=UPSTREAM_URL)],
        [back_to_main_menu_button()],
    ])


@router.callback_query(F.data.in_([item.value for item in NavOpenFlux]))
async def callback_openflux(callback: CallbackQuery, config: Config) -> None:
    # Recheck every callback: old keyboards and forged callbacks grant no access.
    if not pilot_available(config, callback.from_user.id):
        await callback.answer("Тест OpenFlux доступен только участникам тестирования.", show_alert=True)
        return
    if (callback.message is None or callback.message.chat.type != ChatType.PRIVATE
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Откройте OpenFlux в личном чате с ботом.", show_alert=True)
        return

    settings = config.openflux
    if callback.data == NavOpenFlux.DOWNLOAD_PROFILE:
        if not valid_doc_url(settings.DOC_URL):
            await callback.answer("Профиль ещё не настроен.", show_alert=True)
            return
        profile = json.dumps({"version": 1, "document_url": settings.DOC_URL}).encode()
        await callback.answer()
        await callback.message.answer_document(
            BufferedInputFile(profile, filename="afz-openflux.json"),
            caption="Нажмите «Добавить в AFZ OpenFlux» ниже — выбирать файл не нужно. JSON оставлен для ручного импорта. Один тестовый клиент одновременно.",
            reply_markup=mobile_import_markup(config),
            protect_content=True,
        )
        return
    if callback.data in ARTIFACTS:
        artifact = ARTIFACT_DIR / ARTIFACTS[callback.data]
        if not artifact.is_file():
            await callback.answer("Сборка клиента ещё не загружена.", show_alert=True)
            return
        await callback.answer()
        caption = "Тестовый клиент OpenFlux. Распакуйте архив и следуйте инструкции выше."
        if callback.data == NavOpenFlux.DOWNLOAD_ANDROID:
            caption = "AFZ OpenFlux 0.1.1 · Android 8+. Установите APK поверх старой версии, затем нажмите «Добавить в AFZ OpenFlux» ниже. Профиль добавится автоматически."
        elif callback.data == NavOpenFlux.DOWNLOAD_SOURCE:
            caption = "Исходники Android/iPhone и инструкции сборки. Это архив проекта, не установочное приложение iPhone."
        await callback.message.answer_document(
            FSInputFile(artifact),
            caption=caption,
            reply_markup=(mobile_import_markup(config)
                          if callback.data == NavOpenFlux.DOWNLOAD_ANDROID else None),
            protect_content=True,
        )
        return

    await callback.answer()
    if callback.data == NavOpenFlux.MAIN:
        status = (
            "Документ настроен. Соединение с VPS и обход БС нужно проверить на вашем устройстве."
            if valid_doc_url(settings.DOC_URL)
            else "Подготовка: ожидается ссылка на отдельный документ Яндекса и запуск узла на VPS."
        )
        await callback.message.edit_text(
            "🧪 <b>Обход БС · OpenFlux</b>\n\n"
            "Тестовое подключение через Яндекс Документы и наш VPS. "
            "Доступно администратору для проверки.\n\n"
            f"<b>Состояние:</b> {status}\n\n"
            "Выберите устройство и установите отдельный клиент OpenFlux. "
            "Работа зависит от доступности транспорта у оператора.\n\n"
            "Основная подписка AFZVPN продолжает работать отдельно.",
            reply_markup=pilot_keyboard(), parse_mode="HTML",
        )
        return

    rows = []
    if callback.data == NavOpenFlux.MOBILE:
        text = (
            "<b>OpenFlux на телефоне</b>\n\n"
            "Выберите Android или iPhone. Для подключения нужен отдельный клиент AFZ OpenFlux."
        )
        rows.append([InlineKeyboardButton(text="Android", callback_data=NavOpenFlux.ANDROID),
                     InlineKeyboardButton(text="iPhone", callback_data=NavOpenFlux.IOS)])
    elif callback.data in (NavOpenFlux.ANDROID, NavOpenFlux.IOS):
        android = callback.data == NavOpenFlux.ANDROID
        if android:
            text = (
                "<b>AFZ OpenFlux · Android</b>\n\n"
                "Тестовое приложение для Android 8+ (arm64/x86_64), без root.\n\n"
                "1. Скачайте APK 0.1.1 и установите поверх старой версии. Если Android запросит разрешение, "
                "разрешите установку из используемого приложения.\n"
                "2. Нажмите «Добавить в AFZ OpenFlux» ниже. Приложение откроется с готовым профилем. "
                "Если появилась страница перехода, нажмите на ней «Открыть AFZ OpenFlux».\n"
                "3. В приложении нажмите «Подключить» и подтвердите системный запрос VPN.\n"
                "4. Нажмите «Проверить IP в браузере»: ожидается <code>185.239.141.75</code>.\n\n"
                "Если переход не сработал: «Скопировать настройки» здесь → «Вставить из буфера» в приложении. "
                "В старой версии можно вставить настройки в поле ссылки.\n\n"
                "Сборка проверена; проверка на реальном Android-устройстве ещё требуется. "
                "Включение OpenFlux заменит другой активный VPN на телефоне. "
                "Для возврата отключите OpenFlux и включите привычное приложение."
            )
            if (ARTIFACT_DIR / ARTIFACTS[NavOpenFlux.DOWNLOAD_ANDROID]).is_file():
                rows.append([InlineKeyboardButton(text="Скачать Android APK", callback_data=NavOpenFlux.DOWNLOAD_ANDROID)])
        else:
            text = (
                "<b>AFZ OpenFlux · iPhone</b>\n\n"
                "Подготовлен проект приложения с кнопкой подключения и импортом профиля. "
                "Готовой подписанной версии для установки пока нет: нужны сборка на Mac "
                "с Xcode и подпись Apple с поддержкой VPN-расширения.\n\n"
                "После установки новой сборки: нажмите «Добавить в AFZ OpenFlux» ниже, "
                "затем «Подключить» в приложении.\n\n"
                "Обычный VPN-профиль или импорт в Happ этот транспорт не запускает."
            )
        text += (
            "\n\nОдин тестовый клиент одновременно. Передаются TCP/IPv4; используйте HTTPS. "
            "Приложения, которым нужен UDP, могут не работать. Обход БС нужно проверить в сети оператора."
        )
        if valid_doc_url(settings.DOC_URL):
            rows.extend(mobile_import_buttons(config))
            rows.append([InlineKeyboardButton(text="Скачать профиль подключения", callback_data=NavOpenFlux.DOWNLOAD_PROFILE)])
        if (ARTIFACT_DIR / ARTIFACTS[NavOpenFlux.DOWNLOAD_SOURCE]).is_file():
            rows.append([InlineKeyboardButton(text="Исходники и сборка", callback_data=NavOpenFlux.DOWNLOAD_SOURCE)])
    else:
        windows = callback.data == NavOpenFlux.WINDOWS
        platform = "Windows (x64)" if windows else "Linux (x64)"
        download = NavOpenFlux.DOWNLOAD_WINDOWS if windows else NavOpenFlux.DOWNLOAD_LINUX
        text = f"<b>OpenFlux · {platform}</b>\n\n"
        if valid_doc_url(settings.DOC_URL):
            text += (
                "Распакуйте архив и откройте "
                + ("PowerShell" if windows else "терминал")
                + " в его папке. Запустите:\n\n<pre>"
                + ("" if windows else "chmod +x openflux\n")
                + escape(client_command(settings.DOC_URL, windows=windows))
                + "</pre>\n\n"
            )
        else:
            text += "Клиент можно скачать заранее. Команда подключения появится после настройки документа.\n\n"
        text += (
            "После запуска укажите в настройках прокси браузера SOCKS5 "
            "<code>127.0.0.1:1080</code>. Для проверки подойдёт Firefox. "
            "Проверяйте HTTPS-сайты; этот режим передаёт TCP/IPv4. "
            "DNS в текущем клиенте разрешается на устройстве и тоже зависит от сети.\n\n"
            "Для остановки нажмите Ctrl+C и отключите прокси в браузере. "
            "Для проверки именно обхода БС остановите основное VPN-подключение на устройстве."
        )
        if (ARTIFACT_DIR / ARTIFACTS[download]).is_file():
            rows.append([InlineKeyboardButton(text="Скачать тестовый клиент", callback_data=download)])
    rows.append([back_button(NavOpenFlux.MAIN)])
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
