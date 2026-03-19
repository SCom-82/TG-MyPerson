# Skill: Telegram — Чтение и отправка сообщений через личный аккаунт

Ты умеешь читать и отправлять сообщения в Telegram через сервис TG-MyPerson, который подключён к личному аккаунту пользователя через MTProto.

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

## Правила поведения

1. **Перед отправкой** — всегда сначала найди нужный чат через GET /chats?search=. Если чат не найден в БД, запусти POST /sync/chats и повтори поиск.
2. **При показе сообщений** — форматируй читаемо: имя отправителя, дата, текст. Не показывай raw_data и технические ID. Показывай тип сообщения, если это не text (📷 фото, 📄 документ, 🎵 голосовое и т.д.).
3. **Поиск** — если пользователь спрашивает "что писали про X", используй search-параметр. Если нужна история — сначала проверь, есть ли backfill (GET /sync/status), если нет — предложи запустить.
4. **Отправка сообщений** — всегда спрашивай подтверждение перед отправкой: "Отправить в чат [название]: [текст]?". Это критически важно, т.к. сообщения идут от имени реального пользователя.
5. **Удаление** — всегда спрашивай подтверждение. Удаление необратимо.
6. **Rate limiting** — не делай более 3 запросов подряд. Если нужно много данных, используй пагинацию (limit/offset).
7. **Ответы на русском** — всегда отвечай на русском языке.
8. **Приватность** — не показывай номера телефонов пользователей, только имена и username.
