# WhatsApp setup: Meta Cloud API + ngrok (step by step)

This guide connects the support agent to WhatsApp using **Meta's free test number** and **ngrok** (so Meta can reach the app running on your PC). It follows the exact screens you'll see, including the places where people usually get stuck.

**Time:** about 30 minutes. **Cost:** free.

**Before you start:** the app runs locally (`uvicorn app.main:app --port 8000`) and http://localhost:8000/health shows `"llm_ready": true`. See [SETUP.md](SETUP.md) steps 1–4 if not.

---

## How it fits together

```
Patient's phone ──WhatsApp──▶ Meta Cloud API ──webhook POST──▶ ngrok URL ──tunnel──▶ your PC :8000 (the bot)
Patient's phone ◀──WhatsApp── Meta Cloud API ◀──── Graph API "send message" ────────────────┘
```

- **Meta** receives the patient's message on the test number and forwards it to your **Callback URL**.
- **ngrok** gives your PC that public `https://` address.
- The **app** answers and sends the reply back through Meta's Graph API using your **access token**.

## What you'll end up with in `.env`

| Variable | What it is | Where it comes from |
|---|---|---|
| `META_PHONE_NUMBER_ID` | ID of the test sender number (not the number itself) | Step 3 |
| `META_ACCESS_TOKEN` | Lets the app send messages | Step 3 (temporary) → Step 9 (long-lived) |
| `META_APP_SECRET` | Lets the app check messages really come from Meta | Step 4 |
| `META_VERIFY_TOKEN` | A password you invent for the one-time webhook check | Step 5 |
| `META_GRAPH_VERSION` | Graph API version, e.g. `v25.0` | Step 3 (shown in the sample request) |
| `WHATSAPP_PROVIDER` | Set to `meta` | — |

---

## Part 1 — Meta

### Step 1. Create a Meta developer app

1. Go to **https://developers.facebook.com/apps** (Meta *for Developers*) and log in with your Facebook account. Register as a developer if asked.
2. Click **Create app** and choose the WhatsApp use case (**"Connect with customers through WhatsApp"**).
3. When asked for a **business portfolio**, select an existing one or create one (e.g. *Lotus Lane Dental*).

> ⚠️ **Meta Business Suite is a different site.** If you land on a page with Home / Ads Manager / Inbox, or "Unable to access Meta Business Suite with this account", you're in the wrong place for this step — go back to developers.facebook.com/apps. You don't need a Facebook Page.

### Step 2. Claim the free test number

1. In your app, open **Connect on WhatsApp → Overview** (or **WhatsApp → API Setup** in older layouts).
2. Keep **Integrate with API** selected. On the "WhatsApp Business Platform" card your portfolio is preselected — wait for the button to stop spinning, then click **Continue**.
3. Open **Step 1. Try it out**. Meta has created a test number such as `+1 555 xxx xxxx`.

> ⚠️ **Only Step 1 is needed.** Skip *Step 2. Production setup* and *Step 3. Business verification*. Production setup registers a phone number as the **business** number — if you enter your personal number there, Meta will ask you to disconnect it from your own WhatsApp. **Don't.** Your personal number is the *patient* in this setup.

### Step 3. Copy the Phone number ID, generate a token, add yourself as a recipient

On the **Step 1. Try it out** page:

1. **Phone Number ID** — shown next to the test number (a long number like `1338867805982104`). Copy it → `META_PHONE_NUMBER_ID`. You don't need the *WhatsApp Business account ID* here (it's used in Step 8).
2. **Access token** — click **Generate token**, approve the pop-up, and copy the `EAA…` value → `META_ACCESS_TOKEN`.
   - The token is shown **only once**; after a page reload the box says "Not generated yet" — that's normal.
   - This token **expires within hours**. Step 9 replaces it with a long-lived one.
3. **Graph API version** — the sample request on the right starts with `https://graph.facebook.com/v25.0/…`. Copy that version → `META_GRAPH_VERSION`.
4. **Recipient** — open the dropdown → **Manage phone number list** → add your WhatsApp number → enter the code WhatsApp sends you. The test number can only **reply** to numbers on this list (max 5).
5. Select your number as the recipient and click **Send message**. A template message (e.g. "hello world" / order confirmation) arrives on your phone from the test number. ✅ Token and recipient list work — and that chat is where you'll talk to the bot.

### Step 4. Copy the App secret

In the app's left menu: **App settings → Basic → App secret → Show** (enter your Facebook password). Copy it → `META_APP_SECRET`.

### Step 5. Choose a verify token and fill in `.env`

Invent any string (no spaces), e.g. `lotus-lane-verify-2026`, or generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(16))"
```

Your `.env` should now contain:

```ini
META_ACCESS_TOKEN=EAA...
META_PHONE_NUMBER_ID=1338867805982104
META_APP_SECRET=...
META_VERIFY_TOKEN=lotus-lane-verify-2026
META_GRAPH_VERSION=v25.0
WHATSAPP_PROVIDER=meta
```

**Restart the app** (it reads `.env` only at startup) and open http://localhost:8000/health → `"whatsapp_provider": "meta"`.

---

## Part 2 — ngrok

### Step 6. Install ngrok and start the tunnel

1. **Install** — on Windows, the **Microsoft Store** (search "ngrok") is ngrok's recommended method; or `winget install ngrok -s msstore`; or download from https://ngrok.com/download. Check with `ngrok version` (reopen PowerShell if it isn't found).
2. **Sign up** at https://ngrok.com and run the one-time command from the dashboard:
   ```powershell
   ngrok config add-authtoken "YOUR_NGROK_AUTHTOKEN"
   ```
3. **Find your free domain** — in the ngrok dashboard open **Gateway → Domains**. One domain ending in `.ngrok-free.dev` is already assigned to you.

   > ⚠️ Don't click **New Domain** — that asks you to upgrade. Use the one already listed.

4. With the app running, open a **second terminal** and run:
   ```powershell
   ngrok http 8000 --url https://your-name.ngrok-free.dev
   ```
   You should see `Forwarding https://your-name.ngrok-free.dev -> http://localhost:8000`.

   *No domain?* `ngrok http 8000` also works, but its random address changes every restart, so you'd have to update the Callback URL each time.

5. Keep this window open. Open the request inspector in your browser: **[ngrok – Inspect](http://127.0.0.1:4040/inspect/http)** (http://127.0.0.1:4040/inspect/http). Every request Meta sends shows up there with its headers, body and your app's response — click a request to see the details, or **Replay** to resend it after a fix. It only works while ngrok is running.

---

## Part 3 — Connect Meta to your app

### Step 7. Set the webhook

1. In the Meta app: **WhatsApp → Configuration** (or **Use cases → Customize → Configuration**).
2. **Callback URL:**
   ```
   https://your-name.ngrok-free.dev/webhooks/meta/whatsapp
   ```
3. **Verify token:** exactly the same text as `META_VERIFY_TOKEN`.
4. Leave **"Attach a client certificate to Webhook requests"** **off** (that's mutual TLS; the app already verifies every message with your App secret).
5. Click **Verify and save**. In the ngrok inspector you'll see `GET /webhooks/meta/whatsapp … 200 OK` — that's Meta's one-time check.
6. Scroll to **Webhook fields**, find **messages**, and click **Subscribe**. *(Verifying the URL does not subscribe you — this step is easy to miss.)*
7. Optional: click **Test** on the `messages` row. A `POST /webhooks/meta/whatsapp 200` should appear in the inspector. (It uses a fake sender, so the reply fails with `131030` — expected.)

### Step 8. Link your WhatsApp account to the app

If a real message from your phone produces **no POST** in the inspector, your WhatsApp Business account isn't subscribed to the app yet. Run (PowerShell), with your **WhatsApp Business account ID** from Step 3 and your token:

```powershell
curl.exe -X POST "https://graph.facebook.com/v25.0/YOUR_WABA_ID/subscribed_apps" -H "Authorization: Bearer YOUR_TOKEN"
```

Expected: `{"success":true}`. Check it with:

```powershell
curl.exe "https://graph.facebook.com/v25.0/YOUR_WABA_ID/subscribed_apps" -H "Authorization: Bearer YOUR_TOKEN"
```

### Step 9. Replace the temporary token with a long-lived one

The token from Step 3 expires within hours (you'll see `HTTP 401 code 190` in the log). Create a **system user token** once:

1. Open **https://business.facebook.com/settings/system-users** (Business **Settings**, not Business Suite home). If asked, choose your business portfolio. If you get "Unable to access Meta Business Suite", try `https://business.facebook.com/latest/settings/system_users`.
2. **Add** a system user (any name, e.g. `lotus-bot`), role **Admin**.
3. Click it → **Assign assets**:
   - **Apps** → your app → **Full control**.
   - **WhatsApp accounts** → your account → **Full control**. If several appear (e.g. two with the same name), tick **Select all**, or search by your WhatsApp Business account ID.
4. **Generate token** → choose your app → expiry **Never** (or the longest option) → permissions **`whatsapp_business_messaging`** and **`whatsapp_business_management`** (that's all the bot needs).
5. Copy the token (shown once) into `META_ACCESS_TOKEN` and **restart the app**.

You won't need to replace it again. Keep it secret — it can message as your business. It belongs only in `.env` (already in `.gitignore`).

---

## Part 4 — Test

1. App running, ngrok running, inspector open at [ngrok – Inspect](http://127.0.0.1:4040/inspect/http).
2. From your phone, reply in the chat with the test number: **"What are your opening hours?"**
3. You should see `POST /webhooks/meta/whatsapp 200` in the inspector and an answer on your phone within a few seconds.
4. Try: "Book a cleaning tomorrow at 11am", "Status of ORD-1001" (last name Sharma), and "Talk to a human" — then reply from http://localhost:8000/admin; the staff message arrives on your phone.

### Where to look when something fails

| You see | Meaning | Fix |
|---|---|---|
| "Verify and save" fails | App/ngrok not running, wrong URL, or token mismatch | URL must end in `/webhooks/meta/whatsapp`; Verify token = `META_VERIFY_TOKEN` exactly; restart the app after editing `.env` |
| [Inspector](http://127.0.0.1:4040/inspect/http) shows only the `GET` (verification), no `POST` for your messages | Meta isn't forwarding messages | Subscribe the **messages** field (Step 7.6); run the `subscribed_apps` command (Step 8) |
| `POST … 403`, log `invalid meta signature` | Wrong App secret | Copy it again (Step 4), restart |
| `POST … 403`, log `META_APP_SECRET is not set` | Secret missing or app not restarted | Add it, restart |
| `POST … 200` but no reply; log `code 131030` | Recipient not on the allowed list | Add the number (Step 3.4). If the log's `to` is a fake US number, it was the **Test** button — ignore |
| Log `HTTP 401 code 190` | Access token expired/invalid | New token, or the long-lived one (Step 9); restart |
| Log `code 100` | Wrong Phone number ID | Use the **ID** from Step 3, not the phone number |
| Log `code 131047` | More than 24 h since the patient's last message | Patient must message first; only templates can be sent after 24 h |
| Bot replies with a ticket reference | Normal: it used the **create ticket** tool (billing, cancellation, complaints…) or handed off to staff | Check `/admin` — handoffs appear in the queue, plain tickets don't |
| ngrok asks to upgrade | You tried to create a new domain | Use the free domain already listed under Gateway → Domains |

---

## Limits of the free test number

- Replies only go to **up to 5 numbers** on the recipient list — anyone can message it, but only listed numbers get answers.
- Free-form replies are allowed within **24 hours** of the patient's last message (fine for a support bot; late staff replies won't deliver).
- Your PC and ngrok must stay running — or deploy (SETUP.md § 7) and change the Callback URL to the deployed address.

## Going live later (real patients, no recipient list)

Use **Step 2. Production setup** with a phone number that is **not** on WhatsApp (a spare SIM, a landline or a virtual number — never your personal number). Meta reviews the display name and may require a payment method and business verification; check Meta's current WhatsApp pricing. In this project you then only change `META_PHONE_NUMBER_ID` (and use the system user token) — no code changes.
