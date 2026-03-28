# Skill: Telegram — Полное управление личным аккаунтом

Ты умеешь читать, отправлять сообщения, вступать в каналы, управлять чатами и контактами в Telegram через сервис TG-MyPerson, который подключён к личному аккаунту пользователя через MTProto.

## Подключение

- **URL**: `${TG_MYPERSON_URL}/api/v1` (env var `TG_MYPERSON_URL`)
- **Авторизация**: заголовок `X-API-Key: ${TG_MYPERSON_API_KEY}` (env var `TG_MYPERSON_API_KEY`)

Все запросы делай через `curl` с этими параметрами:
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/..."
```

## Доступные команды

### Статус подключения к Telegram
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/auth/status"
```

### Список чатов
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/chats?limit=50"
```
Фильтры:
- `chat_type` — `private`, `group`, `supergroup`, `channel`
- `search` — поиск по названию/username
- `is_monitored` — `true`/`false`

### Поиск чата по имени
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/chats?search=ТЕКСТ"
```

### Последние сообщения из чата
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages?chat_id=CHAT_ID&limit=20"
```

### Поиск сообщений по тексту
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages?search=ТЕКСТ&limit=20"
```

### Сообщения за период
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages?chat_id=CHAT_ID&date_from=2026-03-01T00:00:00Z&date_to=2026-03-18T23:59:59Z"
```

### Фильтр по типу сообщений
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages?chat_id=CHAT_ID&message_type=photo"
```
Типы: `text`, `photo`, `video`, `document`, `voice`, `audio`, `sticker`, `animation`, `video_note`, `location`, `contact`, `poll`

### Только исходящие/входящие сообщения
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages?chat_id=CHAT_ID&is_outgoing=true"
```

### Конкретное сообщение
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID"
```

### Отправить сообщение
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/messages" \
  -d '{"chat_id": CHAT_ID, "text": "Текст сообщения", "reply_to_message_id": null}'
```

### Переслать сообщение
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/messages/forward" \
  -d '{"from_chat_id": CHAT_ID, "message_id": MSG_ID, "to_chat_id": TARGET_CHAT_ID}'
```

### Удалить сообщение
```bash
curl -s -X DELETE -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID"
```

### Список пользователей
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/users?search=ТЕКСТ"
```

### Обновить список чатов из Telegram
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/sync/chats"
```

### Запустить загрузку истории чата (backfill)
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/sync/backfill" \
  -d '{"chat_id": CHAT_ID, "limit": 1000}'
```

### Статус загрузки истории
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/sync/status"
```

### Включить/выключить мониторинг чата
```bash
curl -s -X PATCH -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/chats/CHAT_ID" \
  -d '{"is_monitored": true}'
```

---

### Посмотреть инфу о канале/пользователе (без вступления)
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/chats/resolve" \
  -d '{"target": "@channel_username"}'
```
`target` принимает: `@username`, `https://t.me/channel`, `https://t.me/+inviteHash`

### Вступить в канал/группу
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/chats/join" \
  -d '{"target": "@channel_username"}'
```
`target` принимает: `@username`, `https://t.me/channel`, `https://t.me/+inviteHash`

### Выйти из канала/группы
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/chats/leave" \
  -d '{"chat_id": CHAT_ID}'
```

### Участники чата
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/chats/CHAT_ID/members?limit=200"
```
Фильтры: `search` — поиск по имени, `limit` — до 1000

### Пометить чат прочитанным
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/chats/CHAT_ID/read"
```

### Архивировать/разархивировать чат
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/chats/CHAT_ID/archive" \
  -d '{"archived": true}'
```

### Редактировать сообщение
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/messages/edit" \
  -d '{"chat_id": CHAT_ID, "message_id": MSG_ID, "text": "Новый текст"}'
```

### Отправить файл
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" \
  -F "chat_id=CHAT_ID" -F "file=@/path/to/file.pdf" -F "caption=Описание" \
  "$TG_MYPERSON_URL/api/v1/messages/send-file"
```

### Скачать медиафайл из сообщения
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" \
  "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID/media" -o file.bin
```

### Закрепить сообщение
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID/pin"
```

### Открепить сообщение
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID/unpin"
```

### Поставить реакцию
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/messages/CHAT_ID/MESSAGE_ID/react" \
  -d '{"emoticon": "👍"}'
```
Для удаления реакции: `{"emoticon": null}`

### Найти пользователя по username
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" -H "Content-Type: application/json" \
  "$TG_MYPERSON_URL/api/v1/users/resolve" \
  -d '{"username": "durov"}'
```

### Заблокировать пользователя
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/users/USER_ID/block"
```

### Разблокировать пользователя
```bash
curl -s -X POST -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/users/USER_ID/unblock"
```

### Список контактов
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/contacts?limit=200"
```

### Глобальный поиск по Telegram
```bash
curl -s -H "X-API-Key: $TG_MYPERSON_API_KEY" "$TG_MYPERSON_URL/api/v1/search/global?q=ТЕКСТ&limit=20"
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
