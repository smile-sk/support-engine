# Support Engine

Telegram-native support engine: one bot, many projects. Visitors click a button on your site, open the bot, pick a project and talk to a real human. The owner's personal account stays private — clients see only the bot.

## How it works
- Landing button -> `https://t.me/<bot>?start=<client_id>`
- Bot greets with the client's project buttons (from `clients.json`, two per row), forwards every message to the client's chat_id
- Buttons: `Plans` shows plans, `API` sends the visitor his own key (`/key`), `Human` pings the owner for a human, any custom button goes to the owner as a message
- Owner replies to the forwarded message (reply) -> bot delivers the answer to the visitor

## Add a client
1. `clients/clients.json` — add an entry with `chat_id` (owner's Telegram chat id)
2. Put `?start=<client_id>` in the landing button
3. Done.

## Run
```
pip install requests
python core/bot.py
```
