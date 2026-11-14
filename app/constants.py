HISTORY_COUNT = 50
POLL_INTERVAL_SEC = 60
DEDUP_TTL_SEC = 604800  # 7 суток
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
ALBUM_LIMIT = 10
PHOTO_MAX_BYTES = 10 * 1024 * 1024
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SEC = (1, 2, 4)
MSG_LINK_ID_FIELD = "conversation_message_id"

SESSION_EXPIRED_TEXT = (
    "Сессия ВК недействительна. Выполните вход (vkover-login) и перезапустите сервис."
)
