from enum import Enum


class NavMain(str, Enum):
    START = "start"
    MENU = "menu"
    MAIN_MENU = "main_menu"
    CLOSE_NOTIFICATION = "close_notification"
    REDIRECT_TO_DOWNLOAD = "redirect_to_download"


class NavProfile(str, Enum):
    MAIN = "profile"
    SHOW_KEY = "show_key"
    SHOW_ADDITIONAL_KEY = "show_additional_key"
    SELECT_SERVER = "select_server"


class NavOpenFlux(str, Enum):
    MAIN = "openflux"
    WINDOWS = "openflux_windows"
    LINUX = "openflux_linux"
    MOBILE = "openflux_mobile"
    ANDROID = "openflux_android"
    IOS = "openflux_ios"
    DOWNLOAD_ANDROID = "openflux_download_android"
    DOWNLOAD_SOURCE = "openflux_download_source"
    DOWNLOAD_PROFILE = "openflux_download_profile"
    DOWNLOAD_WINDOWS = "openflux_download_windows"
    DOWNLOAD_LINUX = "openflux_download_linux"


class NavReferral(str, Enum):
    MAIN = "referral"
    GET_REFERRED_TRIAL = "get_referral_trial"


class NavSupport(str, Enum):
    MAIN = "support"
    HOW_TO_CONNECT = "how_to_connect"
    VPN_NOT_WORKING = "vpn_not_working"


class NavDownload(str, Enum):
    MAIN = "download"
    PLATFORM = "platform"
    PLATFORM_IOS = f"{PLATFORM}_ios"
    PLATFORM_ANDROID = f"{PLATFORM}_android"
    PLATFORM_WINDOWS = f"{PLATFORM}_windows"
    CLIENT = "client"
    CLIENT_HAPP = f"{CLIENT}_happ"
    CLIENT_V2RAYNG = f"{CLIENT}_v2rayng"
    CLIENT_HIDDIFY = f"{CLIENT}_hiddify"
    CLIENT_NEKOBOX = f"{CLIENT}_nekobox"


class NavSubscription(str, Enum):
    MAIN = "subscription"
    ADDITIONAL_PROFILE = "additional_profile"
    CHANGE_MODE = "change_mode"
    CHANGE = "change"
    CHANGE_CONFIRM = "change_confirm"
    CHANGE_APPLY = "change_apply"
    SCHEDULE = "schedule"
    SCHEDULE_CONFIRM = "schedule_confirm"
    SCHEDULE_APPLY = "schedule_apply"
    EXTEND = "extend"
    UPGRADE = "upgrade"
    UPGRADE_PAYMENT = "upgrade_payment"
    PROCESS = "process"
    DEVICES = "devices"
    DURATION = "duration"
    PROMOCODE = "promocode"
    GET_TRIAL = "get_trial"

    PAY = "pay"
    PAY_YOOKASSA = f"{PAY}_yookassa"
    PAY_TELEGRAM_STARS = f"{PAY}_telegram_stars"
    PAY_CRYPTOMUS = f"{PAY}_cryptomus"
    PAY_HELEKET = f"{PAY}_heleket"
    PAY_YOOMONEY = f"{PAY}_yoomoney"
    BACK_TO_DURATION = "back_to_duration"
    BACK_TO_PAYMENT = "back_to_payment"


class NavAdminTools(str, Enum):
    MAIN = "admin_tools"
    TEST = "test"
    SERVER_MANAGEMENT = "server_management"
    SHOW_SERVER = "show_server"
    PING_SERVER = "ping_server"
    ADD_SERVER = "add_server"
    ADD_SERVER_BACK = "add_server_back"
    СONFIRM_ADD_SERVER = "сonfirm_add_server"
    DELETE_SERVER = "delete_server"
    EDIT_SERVER = "edit_server"
    SYNC_SERVERS = "sync_servers"
    STATISTICS = "statistics"
    STATISTICS_PERIOD = "statistics_period"
    HEALTH = "health"
    HEALTH_CHECK_NODES = "health_check_nodes"

    USER_EDITOR = "user_editor"
    USER_LIST = "user_list"
    USER_LIST_PAGE = "user_list_page"
    USER_SEARCH = "user_search"
    USER_DETAILS = "user_details"
    USER_BACK = "user_back"
    USER_SEND_MESSAGE = "user_send_message"
    USER_EDIT_SUBSCRIPTION = "user_edit_subscription"
    USER_SET_PLAN = "user_set_plan"
    USER_SET_PLAN_DURATION = "user_set_plan_duration"
    USER_CONFIRM_PLAN = "user_confirm_plan"
    USER_TOGGLE_BLOCK = "user_toggle_block"
    USER_SET_DISCOUNT = "user_set_discount"
    USER_ACTIVE_FILTER = "user_filter_active"
    USER_PAID_FILTER = "user_filter_paid"
    USER_TRIAL_FILTER = "user_filter_trial"
    USER_INACTIVE_FILTER = "user_filter_inactive"
    USER_ALL_FILTER = "user_filter_all"

    PLAN_EDITOR = "plan_editor"
    PLAN_LIST = "plan_list"
    PLAN_SHOW = "plan_show"
    PLAN_ADD = "plan_add"
    PLAN_EDIT_JSON = "plan_edit_json"
    PLAN_DELETE = "plan_delete"
    PLAN_CONFIRM_DELETE = "plan_confirm_delete"
    PAYMENT_ORDER = "payment_order"
    PAYMENT_MOVE_UP = "payment_move_up"
    PAYMENT_MOVE_DOWN = "payment_move_down"

    INVITE_EDITOR = "invite_editor"
    CREATE_INVITE = "create_invite"
    DELETE_INVITE = "delete_invite"
    LIST_INVITES = "list_invites"
    SHOW_INVITE_PAGE = "show_invite_page"
    SHOW_INVITE_DETAILS = "show_invite_details"
    TOGGLE_INVITE_STATUS = "toggle_invite_status"
    CONFIRM_DELETE_INVITE = "confirm_delete_invite"

    PROMOCODE_EDITOR = "promocode_editor"
    CREATE_PROMOCODE = "create_promocode"
    DELETE_PROMOCODE = "delete_promocode"
    EDIT_PROMOCODE = "edit_promocode"

    NOTIFICATION = "notification"
    SEND_NOTIFICATION_USER = "send_notification_user"
    SEND_NOTIFICATION_ALL = "send_notification_all"
    CONFIRM_SEND_NOTIFICATION = "confirm_send_notification"
    LAST_NOTIFICATION = "last_notification"
    EDIT_NOTIFICATION = "edit_notification"
    DELETE_NOTIFICATION = "delete_notification"

    CREATE_BACKUP = "create_backup"

    MAINTENANCE_MODE = "maintenance_mode"
    MAINTENANCE_MODE_ENABLE = "maintenance_mode_enable"
    MAINTENANCE_MODE_DISABLE = "maintenance_mode_disable"

    RESTART_BOT = "restart_bot"
    TEST_PURCHASE = "test_purchase"
    TEST_AUTOPAY_BIND = "test_autopay_bind"
    TEST_AUTOPAY_BIND_CONFIRM = "test_autopay_bind_confirm"
    TEST_AUTOPAY_ENABLE = "test_autopay_enable"
    TEST_AUTOPAY_DISABLE = "test_autopay_disable"
    TEST_AUTOPAY_FORGET = "test_autopay_forget"
    TEST_AUTOPAY_FORGET_CONFIRM = "test_autopay_forget_confirm"
