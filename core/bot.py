#!/usr/bin/env python3
"""
Support Engine — Telegram-native support bot (single bot, many clients).

Flow:
  visitor -> t.me/<bot>?start=<client_id> -> picks project, writes message
  bot     -> forwards message to client's owner (chat_id), with visitor's nick
  owner   -> replies to the forwarded message
  bot     -> delivers the reply to the visitor (owner's account stays private)

Payment-API binding (CleanTrade pay, v0.3.5):
  site    -> t.me/<bot>?start=payauth_<code>   (code from POST /v1/tg/start)
  bot     -> POST {pay}/v1/tg/confirm          (X-Support-Secret) — code confirmed
  site    -> GET  /v1/tg/status?code=...       -> status=confirmed, tg_id
  bot     -> /key asks {pay}/v1/tg/mykey       -> sends the visitor's API key

Why this bot is the only listener: Telegram allows one getUpdates reader per
token. The payment API only sends (sendMessage) — otherwise 409 Conflict and
updates are lost between two processes.

State (clients/state.json):
  visitor_client : chat_id -> client_id   (remember which project a visitor chose)
  forward_map    : forwarded_msg_id -> {visitor, client}  (to route owner replies back)

Config: clients/clients.json + token in clients/secret.txt (or env SUPPORT_BOT_TOKEN).
        pay: env PAY_API_URL / SUPPORT_SHARED_SECRET, else clients/pay_secret.txt
        (line 1 = shared secret, line 2 = optional API url).
Run:  pip install requests && python core/bot.py
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENTS_FILE = os.path.join(BASE_DIR, "clients", "clients.json")
STATE_FILE = os.path.join(BASE_DIR, "clients", "state.json")
SECRET_FILE = os.path.join(BASE_DIR, "clients", "secret.txt")
PAY_SECRET_FILE = os.path.join(BASE_DIR, "clients", "pay_secret.txt")
TOKEN = os.environ.get("SUPPORT_BOT_TOKEN", "")

PAY_API_URL = os.environ.get("PAY_API_URL", "http://127.0.0.1:8080")
SUPPORT_SHARED_SECRET = os.environ.get("SUPPORT_SHARED_SECRET", "")

PAY_SITE = "https://pay.cleantrade.info"
SUBSCRIBE_URL = PAY_SITE + "/subscribe.html"

API = "https://api.telegram.org/bot{token}/{method}"

CMDS = [
    {"command": "start", "description": "start / pick a project"},
    {"command": "key", "description": "send me my API key"},
    {"command": "plans", "description": "plans and prices"},
    {"command": "support", "description": "talk to a human"},
]


def load_clients():
    with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"visitor_client": {}, "forward_map": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def api(method, **params):
    url = API.format(token=TOKEN, method=method)
    data = None
    if params:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def keyboard_rows(buttons):
    """Client's buttons -> Telegram reply keyboard, two buttons per row."""
    items = [str(b) for b in (buttons or []) if str(b).strip()]
    return [[{"text": b} for b in items[i:i + 2]]
            for i in range(0, len(items), 2)]


def button_action(text, buttons):
    """What a pressed reply-keyboard button means. '' = not a button."""
    t = (text or "").strip().lower()
    for b in buttons or []:
        if str(b).strip().lower() == t:
            low = str(b).lower()
            if "plan" in low or "price" in low:
                return "plans"
            if "key" in low or "api" in low:
                return "key"
            if "human" in low or "support" in low or "help" in low:
                return "human"
            return "message"          # custom button -> straight to the owner
    return ""


def send(chat_id, text, reply_to=None, keyboard=None):
    """Send a message. All our texts are HTML, so parse_mode is set here."""
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    rows = keyboard_rows(keyboard)
    if rows:
        params["reply_markup"] = json.dumps({"keyboard": rows,
                                             "resize_keyboard": True},
                                            ensure_ascii=False)
    try:
        return api("sendMessage", **params)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"[send err] {e} {body}", flush=True)
        return None
    except Exception as e:
        print(f"[send err] {e}", flush=True)
        return None


# ---------------- payment API (we only talk, never poll updates) ----------------

def pay_request(path, params=None, payload=None, method="GET"):
    """Call the payment API. Returns (ok, dict). Never raises."""
    if not SUPPORT_SHARED_SECRET:
        print("[pay] no shared secret — payment integration disabled", flush=True)
        return False, {"error": "no shared secret"}
    url = PAY_API_URL.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Support-Secret", SUPPORT_SHARED_SECRET)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return False, {"status": e.code, "detail": body}
    except Exception as e:
        return False, {"error": str(e)}


def pay_confirm(code, tg_id, username, first_name):
    return pay_request(
        "/v1/tg/confirm",
        payload={"code": code, "tg_id": tg_id, "username": username or "",
                 "first_name": first_name or ""},
        method="POST",
    )


def pay_mykey(tg_id):
    return pay_request("/v1/tg/mykey", params={"tg_id": tg_id})


def key_text(d):
    """'Here is your key' — English, HTML."""
    plan = html.escape(str(d.get("plan_name") or d.get("plan") or "Plan"))
    key = html.escape(str(d.get("api_key") or ""))
    exp = str(d.get("plan_expires_at") or "")[:10]
    lim = d.get("limit")
    used = d.get("orders_month")
    tail = ""
    if exp:
        tail += f"\nValid until: <b>{html.escape(exp)}</b>"
    if lim is not None:
        tail += f"\nOrders this month: <b>{used or 0}/{lim}</b>"
    return (
        f"🔑 <b>Your CleanTrade API key</b>\n\n"
        f"Plan: <b>{plan}</b>{tail}\n"
        f"Key:\n<code>{key}</code>\n\n"
        f"Keep it private — it is your merchant key.\n"
        f"Docs &amp; examples: {PAY_SITE}/integrate.html"
    )


def handle_binding(chat_id, payload, frm):
    """Visitor pressed the link from the site: bind this Telegram to the code."""
    code = payload[len("payauth_"):].strip()
    tg_id = frm.get("id")
    ok, res = pay_confirm(code, tg_id, frm.get("username", ""), frm.get("first_name", ""))
    if not ok:
        send(chat_id,
             "⚠️ <b>This link has expired or is unknown.</b>\n\n"
             "Please go back to the site and press the button again.")
        print(f"[pay] confirm failed: {res}", flush=True)
        return
    if res.get("has_key"):
        plan = html.escape(str(res.get("plan") or "your plan"))
        if res.get("key_sent"):
            # the API has just pushed the key into this chat itself
            send(chat_id,
                 "✅ <b>Telegram linked.</b>\n\n"
                 f"This Telegram already has a CleanTrade API key ({plan}).\n"
                 "I have just sent it to this chat — see the message above.\n\n"
                 "Any time later send /key here and I will send it again.")
        else:
            ok_key, key_res = pay_mykey(tg_id)
            if ok_key and key_res.get("api_key"):
                send(chat_id, "✅ <b>Telegram linked.</b>\n\n" + key_text(key_res))
            else:
                send(chat_id,
                     "✅ <b>Telegram linked.</b>\n\n"
                     f"This Telegram already has a key ({plan}).\n"
                     "Send /key here and I will send it to this chat.")
    else:
        send(chat_id,
             "✅ <b>Telegram linked.</b>\n\n"
             "Your Telegram is now connected to CleanTrade Payment API.\n"
             "Go back to the browser — the key will appear on the page.\n\n"
             "Any time later send /key here and I will send your API key to this chat.")
    print(f"[pay] confirmed code {code[:8]}… for tg_id {tg_id}", flush=True)


def handle_key(chat_id, tg_id, username):
    ok, res = pay_mykey(tg_id)
    if not ok:
        send(chat_id,
             "🔑 <b>No API key found</b> for your Telegram.\n\n"
             f"Get a free plan here: {SUBSCRIBE_URL}\n"
             f"Or write to support: /support")
        return
    send(chat_id, key_text(res))


def plans_text():
    return (
        "💰 <b>CleanTrade Payment API — plans</b>\n\n"
        "• <b>Free</b> — $0 · 5 orders/month\n"
        "• <b>Starter</b> — $9.99/mo · 100 orders/month\n"
        "• <b>Pro</b> — $29.99/mo · 3,000 orders/month\n"
        "• <b>Business</b> — $79.99/mo · unlimited\n\n"
        f"Yearly billing: 2 months free.\n"
        f"Subscribe: {SUBSCRIBE_URL}\n\n"
        "Pay in USDT (TRC20) — key is issued automatically.\n"
        "Press /key to get the key you already have."
    )


def help_text():
    return (
        "🛠 <b>CleanTrade support</b>\n\n"
        "• /key — send my API key here\n"
        "• /plans — plans and prices\n"
        "• /support — talk to a human\n"
        "• or just write a message — it goes straight to a real person.\n\n"
        f"Site: {PAY_SITE}"
    )


def main():
    global TOKEN, SUPPORT_SHARED_SECRET, PAY_API_URL

    if not TOKEN and os.path.exists(SECRET_FILE):
        TOKEN = open(SECRET_FILE, encoding="utf-8").read().strip()
    if not TOKEN:
        print("No token: set SUPPORT_BOT_TOKEN env or write clients/secret.txt")
        sys.exit(1)

    if os.path.exists(PAY_SECRET_FILE):
        lines = [x.strip() for x in
                 open(PAY_SECRET_FILE, encoding="utf-8").read().splitlines()]
        if lines and lines[0]:
            SUPPORT_SHARED_SECRET = SUPPORT_SHARED_SECRET or lines[0]
        if len(lines) > 1 and lines[1]:
            PAY_API_URL = lines[1]
    if SUPPORT_SHARED_SECRET:
        ok, health = pay_request("/healthz")
        print(f"[pay] api {PAY_API_URL} reachable={ok} {health if not ok else ''}",
              flush=True)
    else:
        print("[pay] no shared secret — /key and site binding disabled", flush=True)

    me = api("getMe")
    print(f"[+] bot @{me['result']['username']} running", flush=True)
    try:
        api("setMyCommands", commands=json.dumps(CMDS))
    except Exception as e:
        print(f"[commands err] {e}", flush=True)

    offset = 0
    while True:
        try:
            updates = api("getUpdates", timeout=30, offset=offset)["result"]
        except Exception as e:
            print(f"[updates err] {e}", flush=True)
            time.sleep(3)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue
            chat = msg["chat"]
            chat_id = chat["id"]
            text = msg.get("text", "") or ""
            is_private = chat["type"] == "private"
            state = load_state()
            clients = load_clients()
            owner_ids = {c["chat_id"] for c in clients.values() if c.get("chat_id")}
            is_owner = chat_id in owner_ids

            # ---------- /start [client_id | payauth_<code>] ----------
            if text.startswith("/start"):
                parts = text.split()
                param = parts[1] if len(parts) > 1 else ""

                # site sent the visitor here to prove his Telegram
                if param.startswith("payauth_"):
                    handle_binding(chat_id, param, msg.get("from", {}))
                    continue

                if param and param in clients:
                    state["visitor_client"][str(chat_id)] = param
                    save_state(state)
                    c = clients[param]
                    if is_owner:
                        send(chat_id, f"✅ You are the owner of <b>{html.escape(c['name'])}</b>.\n"
                                      f"Incoming visitor messages will appear here — reply to a "
                                      f"forwarded message to answer.")
                    else:
                        send(chat_id, f"👋 {html.escape(c['greeting'])}\n\n"
                                      f"Reply here — a real human will answer you.\n"
                                      f"/key — send me my API key · /plans — plans",
                             keyboard=c.get("buttons"))
                else:
                    menu = "\n".join(f"/start {cid} — {html.escape(c['name'])}"
                                     for cid, c in clients.items())
                    send(chat_id, f"Pick a project:\n{menu}\n\n"
                                  f"API key: /key · plans: /plans")
                continue

            # ---------- /key · /plans · /support · /help ----------
            cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
            if cmd == "/key":
                if is_owner:
                    send(chat_id, "This command is for clients — send it from a client's chat.")
                else:
                    handle_key(chat_id, msg.get("from", {}).get("id"), "")
                continue
            if cmd == "/plans":
                send(chat_id, plans_text())
                continue
            if cmd in ("/support", "/help"):
                send(chat_id, help_text())
                if not is_owner:
                    cid = state["visitor_client"].get(str(chat_id)) or \
                        next((k for k, v in clients.items() if v.get("chat_id")), None)
                    if cid and clients[cid].get("chat_id"):
                        send(clients[cid]["chat_id"],
                             f"🙋 Visitor asked for a human ({clients[cid]['name']}, "
                             f"chat_id <code>{chat_id}</code>).")
                continue

            # ---------- unknown command: answer, never silence ----------
            if cmd and is_private:
                send(chat_id, "🤔 <b>Unknown command.</b>\n\n" + help_text())
                continue

            # ---------- owner replies to a forwarded visitor message -> deliver back ----------
            rtm = msg.get("reply_to_message")
            if is_private and is_owner and rtm:
                fwd_id = rtm["message_id"]
                fm = state["forward_map"].get(str(fwd_id))
                if fm:
                    visitor_id = fm["visitor"]
                    cid = fm["client"]
                    label = clients.get(cid, {}).get("name", "Support")
                    ok = send(visitor_id, f"💬 <b>{html.escape(label)} · Support:</b>\n"
                                          f"{html.escape(text)}")
                    if ok:
                        send(chat_id, "✅ Delivered to visitor.")
                    else:
                        send(chat_id, "⚠️ Could not deliver (visitor blocked the bot?).")
                else:
                    send(chat_id, "Reply to a <b>forwarded</b> visitor message to answer it.")
                continue

            # ---------- visitor message ----------
            if is_private and not text.startswith("/"):
                cid = state["visitor_client"].get(str(chat_id))
                if not cid or cid not in clients:
                    cid = next((k for k, v in clients.items() if v.get("chat_id")), None)
                if cid and clients[cid].get("chat_id"):
                    owner = clients[cid]["chat_id"]
                    act = button_action(text, clients[cid].get("buttons"))
                    if act == "plans":
                        send(chat_id, plans_text(),
                             keyboard=clients[cid].get("buttons"))
                        continue
                    if act == "key":
                        handle_key(chat_id, msg.get("from", {}).get("id"), "")
                        continue
                    if act == "human":
                        send(chat_id,
                             "🙋 <b>Got it — a human will answer here.</b>\n\n"
                             "Write your question in one message and it goes "
                             "straight to a real person.")
                        send(owner, f"🙋 Visitor pressed <b>Human</b> "
                                    f"({html.escape(clients[cid]['name'])}, "
                                    f"chat_id <code>{chat_id}</code>).")
                        continue
                    frm = msg.get("from", {})
                    nick = frm.get("username") or frm.get("first_name") or "?"
                    link = f"https://t.me/{nick}" if frm.get("username") else ""
                    try:
                        fwd = api("forwardMessage", chat_id=owner, from_chat_id=chat_id,
                                  message_id=msg["message_id"])
                        fwd_id = fwd["result"]["message_id"]
                        state["forward_map"][str(fwd_id)] = {"visitor": chat_id, "client": cid}
                        save_state(state)
                    except Exception as e:
                        print(f"[forward err] {e}", flush=True)
                    send(owner, f"📩 <b>{html.escape(clients[cid]['name'])}</b> · from "
                                f"{html.escape(str(nick))} {html.escape(link)}\n"
                                f"(chat_id <code>{chat_id}</code>)")
                continue

        time.sleep(0.5)


if __name__ == "__main__":
    main()
