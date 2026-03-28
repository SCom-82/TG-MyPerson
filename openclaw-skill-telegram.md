# Skill: Telegram — Полное управление личным аккаунтом

Ты умеешь читать, отправлять сообщения, вступать в каналы, управлять чатами и контактами в Telegram через сервис TG-MyPerson, который подключён к личному аккаунту пользователя через MTProto.

## Подключение

- **URL**: `${TG_MYPERSON_URL}/api/v1` (env var `TG_MYPERSON_URL`)
- **Авторизация**: заголовок `X-API-Key: ${TG_MYPERSON_API_KEY}` (env var `TG_MYPERSON_API_KEY`)

Все запросы делай через Python `urllib.request` (надёжнее curl для JSON):
```python
import json, os, urllib.request

BASE = os.environ["TG_MYPERSON_URL"] + "/api/v1"
KEY = os.environ["TG_MYPERSON_API_KEY"]

def tg_api(path, method="GET", data=None):
    """Универсальный вызов TG-MyPerson API."""
    url = f"{BASE}{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"X-API-Key": KEY}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())
```

Для GET-запросов с query-параметрами:
```python
tg_api("/chats?search=ТЕКСТ&limit=50")
```

Для POST-запросов с JSON-телом:
```python
tg_api("/chats/join", method="POST", data={"target": "@channel_username"})
```

> **Примечание**: curl тоже работает, но при сложных JSON (вложенные кавычки, Unicode) shell может ломать тело запроса. Python `urllib.request` работает надёжнее.

## Доступные команды

### Статус подключения
```python
tg_api("/auth/status")
```

### Список чатов
```python
tg_api("/chats?limit=50")
tg_api("/chats?chat_type=channel&is_monitored=true")
tg_api("/chats?search=ТЕКСТ")
```
Фильтры: `chat_type` (private/group/supergroup/channel), `search`, `is_monitored` (true/false), `limit`, `offset`

### Сообщения
```python
# Последние сообщения из чата
tg_api("/messages?chat_id=CHAT_ID&limit=20")

# Поиск по тексту
tg_api("/messages?search=ТЕКСТ&limit=20")

# За период
tg_api("/messages?chat_id=CHAT_ID&date_from=2026-03-01T00:00:00Z&date_to=2026-03-28T23:59:59Z")

# По типу: text, photo, video, document, voice, audio, sticker, animation, video_note, location, contact, poll
tg_api("/messages?chat_id=CHAT_ID&message_type=photo")

# Только исходящие/входящие
tg_api("/messages?chat_id=CHAT_ID&is_outgoing=true")

# Конкретное сообщение
tg_api("/messages/CHAT_ID/MESSAGE_ID")
```

### Отправить сообщение
```python
tg_api("/messages", "POST", {"chat_id": CHAT_ID, "text": "Текст сообщения", "reply_to_message_id": None})
```

### Переслать сообщение
```python
tg_api("/messages/forward", "POST", {"from_chat_id": CHAT_ID, "message_id": MSG_ID, "to_chat_id": TARGET_CHAT_ID})
```

### Удалить сообщение
```python
tg_api("/messages/CHAT_ID/MESSAGE_ID", method="DELETE")
```

### Редактировать сообщение
```python
tg_api("/messages/edit", "POST", {"chat_id": CHAT_ID, "message_id": MSG_ID, "text": "Новый текст"})
```

### Список пользователей
```python
tg_api("/users?search=ТЕКСТ")
```

---

### Посмотреть инфу о канале/пользователе (без вступления)
```python
tg_api("/chats/resolve", "POST", {"target": "@channel_username"})
```
`target` принимает: `@username`, `https://t.me/channel`, `https://t.me/+inviteHash`

### Вступить в канал/группу
```python
tg_api("/chats/join", "POST", {"target": "@channel_username"})
```

### Выйти из канала/группы
```python
tg_api("/chats/leave", "POST", {"chat_id": CHAT_ID})
```

### Включить/выключить мониторинг чата
```python
tg_api(f"/chats/{CHAT_ID}", method="PATCH", data={"is_monitored": True})
```

### Участники чата
```python
tg_api(f"/chats/{CHAT_ID}/members?limit=200")
```
Фильтры: `search` — поиск по имени, `limit` — до 1000

### Пометить чат прочитанным
```python
tg_api(f"/chats/{CHAT_ID}/read", "POST")
```

### Архивировать/разархивировать чат
```python
tg_api(f"/chats/{CHAT_ID}/archive", "POST", {"archived": True})
```

### Отправить файл
```python
# Для файлов используй multipart/form-data через curl:
# curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" \
#   -F "chat_id=CHAT_ID" -F "file=@/path/to/file.pdf" -F "caption=Описание" \
#   "$TG_MYPERSON_URL/api/v1/messages/send-file"
```

### Скачать медиафайл из сообщения
```python
# Возвращает бинарные данные файла
# tg_api("/messages/CHAT_ID/MESSAGE_ID/media")  # → bytes
```

### Закрепить / открепить сообщение
```python
tg_api(f"/messages/{CHAT_ID}/{MESSAGE_ID}/pin", "POST")
tg_api(f"/messages/{CHAT_ID}/{MESSAGE_ID}/unpin", "POST")
```

### Поставить реакцию
```python
tg_api(f"/messages/{CHAT_ID}/{MESSAGE_ID}/react", "POST", {"emoticon": "👍"})
# Удалить реакцию:
tg_api(f"/messages/{CHAT_ID}/{MESSAGE_ID}/react", "POST", {"emoticon": None})
```

### Найти пользователя по username
```python
tg_api("/users/resolve", "POST", {"username": "durov"})
```

### Заблокировать / разблокировать пользователя
```python
tg_api(f"/users/{USER_ID}/block", "POST")
tg_api(f"/users/{USER_ID}/unblock", "POST")
```

### Список контактов
```python
tg_api("/contacts?limit=200")
```

### Глобальный поиск по Telegram
```python
tg_api("/search/global?q=ТЕКСТ&limit=20")
```

### Синхронизация
```python
# Обновить список чатов из Telegram
tg_api("/sync/chats", "POST")

# Загрузить историю чата
tg_api("/sync/backfill", "POST", {"chat_id": CHAT_ID, "limit": 1000})

# Статус загрузки
tg_api("/sync/status")
```

## Типичные флоу

### Подключение нового источника для мониторинга

Пошаговый процесс — **всегда следуй этому порядку**:

```
1. resolve  → Посмотреть инфу (без вступления)
2. confirm  → Спросить у пользователя подтверждение
3. join     → Вступить
4. sync     → POST /sync/chats (чтобы чат появился в БД)
5. monitor  → PATCH /chats/{id} {"is_monitored": true}
6. backfill → POST /sync/backfill {"chat_id": id} (если нужна история)
```

Пример на Python:
```python
# 1. Resolve
info = tg_api("/chats/resolve", "POST", {"target": "@montazhnikiokon"})
print(f"{info['title']} — {info['members_count']} участников")

# 2. Confirm → спросить пользователя

# 3. Join
chat = tg_api("/chats/join", "POST", {"target": "@montazhnikiokon"})

# 4. Sync
tg_api("/sync/chats", "POST")

# 5. Monitor
tg_api(f"/chats/{chat['id']}", method="PATCH", data={"is_monitored": True})
# Примечание: для PATCH передавай method="PATCH" в tg_api

# 6. Backfill (опционально)
tg_api("/sync/backfill", "POST", {"chat_id": chat["id"], "limit": 1000})
```

### Отправка сообщения

```
1. Найти чат → GET /chats?search=...
2. Если не найден → POST /sync/chats → повторить поиск
3. Показать пользователю: "Отправить в [название]: [текст]?"
4. POST /messages {"chat_id": id, "text": "..."}
```

### Поиск информации

```
1. GET /messages?search=ТЕКСТ  → поиск в уже загруженных
2. Если мало результатов → GET /search/global?q=ТЕКСТ → поиск по всему Telegram
3. Если нужна история конкретного чата → POST /sync/backfill
```

## Правила поведения

1. **Перед отправкой** — всегда сначала найди нужный чат через GET /chats?search=. Если чат не найден в БД, запусти POST /sync/chats и повтори поиск.
2. **При показе сообщений** — форматируй читаемо: имя отправителя, дата, текст. Не показывай raw_data и технические ID. Показывай тип сообщения, если это не text (📷 фото, 📄 документ, 🎵 голосовое и т.д.).
3. **Поиск** — если пользователь спрашивает "что писали про X", используй search-параметр. Если нужна история — сначала проверь, есть ли backfill (GET /sync/status), если нет — предложи запустить.
4. **Отправка сообщений** — всегда спрашивай подтверждение перед отправкой: "Отправить в чат [название]: [текст]?". Это критически важно, т.к. сообщения идут от имени реального пользователя.
5. **Удаление** — всегда спрашивай подтверждение. Удаление необратимо.
6. **Rate limiting** — не делай более 3 запросов подряд. Если нужно много данных, используй пагинацию (limit/offset).
7. **Ответы на русском** — всегда отвечай на русском языке.
8. **Приватность** — не показывай номера телефонов пользователей, только имена и username.
9. **Вступление в каналы** — перед join всегда делай resolve, чтобы показать пользователю инфу о канале (название, кол-во участников). Спрашивай подтверждение: "Вступить в [название] ([участников] участников)?".
10. **Выход из чатов** — всегда спрашивай подтверждение перед leave. Показывай название чата.
11. **Блокировка** — спрашивай подтверждение перед block/unblock.
12. **Редактирование** — спрашивай подтверждение: "Изменить сообщение на: [новый текст]?".
13. **Новые источники** — если нужно мониторить новый канал: resolve → подтверждение → join → sync/chats → включить мониторинг (PATCH is_monitored=true) → backfill если нужна история.
