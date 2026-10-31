# Alert Configuration (Discord / Telegram)

The agent sends notifications when the daily or per-task budget is exceeded.
The destination is configured via the `ALERT_WEBHOOK_URL` env var.

## Discord (native)

1. Go to **Server Settings → Integrations → Webhooks → Create Webhook**
2. Choose a channel, give it a name, copy the URL
3. Configure it on Fly:

```bash
fly secrets set ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/<id>/<token>"
```

The payload uses the [Discord Embed format](https://discord.com/developers/docs/resources/webhook#execute-webhook)
with `content` + `embeds[]` fields — works directly, no intermediaries needed.

## Telegram (via Bot API)

### 1. Create the bot

On Telegram, talk to [@BotFather](https://t.me/BotFather):

```
/newbot
→ Name: fly-swe Agent Alerts
→ Username: flyswe_alerts_bot
→ Token: 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

### 2. Find the chat_id

**For a group:**
1. Add the bot to the group as an administrator
2. Send any message in the group
3. Go to: `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Read the `message.chat.id` field (groups use negative IDs, e.g. `-1001234567890`)

**For a private message:**
1. Start a conversation with the bot
2. Send `/start`
3. Talk to [@userinfobot](https://t.me/userinfobot) to get your `id`

### 3. Build the URL

```bash
fly secrets set ALERT_WEBHOOK_URL="https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>"
```

Example:
```bash
fly secrets set ALERT_WEBHOOK_URL="https://api.telegram.org/bot123456:ABC-DEF/sendMessage?chat_id=-1001234567890"
```

### 4. Verify

Deploy or restart the machine and force a budget overflow to test.

## Payload Format (Reference)

The agent automatically detects the format based on the URL pattern:

| URL Pattern | Payload Sent |
|---|---|
| `discord.com/api/webhooks` | Discord Embed (`content` + `embeds[]` with fields, color, timestamp) |
| `api.telegram.org/bot` | Telegram API (`chat_id` + `text` + `parse_mode=Markdown`) |
| (any other) | Generic (`content`, `event`, `spent_usd`, `limit_usd`) |

## Testing

### Manual test on the container

```bash
fly ssh console

# Discord
curl -X POST "$ALERT_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "🧪 Manual test",
    "embeds": [{
      "title": "Test",
      "color": 15158332,
      "fields": [
        {"name": "Event", "value": "test", "inline": true},
        {"name": "Value", "value": "$10.00", "inline": true}
      ]
    }]
  }'

# Telegram
curl -X POST "$ALERT_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{"chat_id": "-1001234567890", "text": "🧪 *Manual test*", "parse_mode": "Markdown"}'
```

## Troubleshooting

| Problem | Likely Cause | Solution |
|---|---|---|
| No notification when budget is exceeded | `ALERT_WEBHOOK_URL` empty or not configured | Check with `fly secrets list` |
| Discord: "400 Bad Request" | Malformed payload | Test first with manual curl |
| Telegram: message not arriving | Incorrect chat_id or bot not in the group | Check with `getUpdates` |
| Worker logs `budget_alert_webhook_failed` | Invalid webhook URL or service down | Check URL and container connectivity |
