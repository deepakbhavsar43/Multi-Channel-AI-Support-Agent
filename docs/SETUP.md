# Setup Guide — from zero to a working WhatsApp agent

This guide takes you through every step: accounts and API keys, installing, running locally, connecting WhatsApp, and deploying a live demo. Follow it top to bottom the first time.

**Time needed:** about 30 minutes for local + WhatsApp, plus 15 minutes to deploy.

---

## Overview

| Part | You get | Required? |
|---|---|---|
| [1. Accounts and keys](#1-accounts-and-api-keys) | An LLM key, an admin token, a Meta developer app + ngrok | Yes |
| [2. Install](#2-install-the-project) | Project running on your PC | Yes |
| [3. Configure `.env`](#3-configure-env) | All keys in one file | Yes |
| [4. Run and test locally](#4-run-and-test-locally) | Web demo and staff console working | Yes |
| [5. Connect WhatsApp](#5-connect-whatsapp) | Chat with the bot from your phone | For the WhatsApp demo |
| [6. Calibrate retrieval](#6-calibrate-the-relevance-threshold) | Better "I'm not sure" decisions | Recommended |
| [7. Deploy](#7-deploy-a-live-demo-render) | Always-on public URL | For the portfolio |
| [8. Troubleshooting](#8-troubleshooting) | Fixes for common problems | — |

---

## 1. Accounts and API keys

You need **one LLM** (pick A, B or C), an **admin token** you make up yourself, and — for WhatsApp — a **Meta developer app** (free) and **ngrok**. Everything below has a free option; no OpenAI key is needed.

> **Which WhatsApp provider?** Use **Meta WhatsApp Cloud API** (free test number, normal replies work). Twilio also works, but new Twilio *trial* accounts can only send pre-approved templates, so the bot's replies are rejected (`ContentSid Required`, error 21654) until you upgrade Twilio. Both are supported; the app picks Meta automatically when its keys are set.

### Keys at a glance

| `.env` variable | What it is | Where to get it | Cost | Needed for |
|---|---|---|---|---|
| `LLM_MODEL` | Which LLM to use | You choose (see below) | — | Everything |
| `GOOGLE_API_KEY` | Google Gemini key (option A) | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier | Option A |
| `GROQ_API_KEY` | Groq key (option B) | [console.groq.com/keys](https://console.groq.com/keys) | Free tier | Option B |
| *(none)* | Ollama runs locally (option C) | [ollama.com](https://ollama.com) | Free | Option C |
| `ADMIN_TOKEN` | Password for the staff console | You make it up | Free | `/admin` |
| `META_ACCESS_TOKEN` | Lets the app send WhatsApp messages | Meta App Dashboard → WhatsApp → API Setup | Free | WhatsApp (Meta) |
| `META_PHONE_NUMBER_ID` | ID of the test sender number (not the number itself) | Same API Setup page | Free | WhatsApp (Meta) |
| `META_APP_SECRET` | Verifies that webhooks really come from Meta | App settings → Basic → App secret | Free | WhatsApp (Meta) |
| `META_VERIFY_TOKEN` | Any string you invent; pasted in Meta's webhook settings too | You make it up | Free | WhatsApp (Meta) |
| `TWILIO_ACCOUNT_SID` | Twilio account ID (starts with `AC`) | Twilio Console home page | Free trial* | WhatsApp (Twilio) |
| `TWILIO_AUTH_TOKEN` | Twilio secret | Twilio Console home page | Free trial* | WhatsApp (Twilio) |
| `TWILIO_WHATSAPP_FROM` | The Twilio WhatsApp number | Twilio → Try WhatsApp page | Free trial* | WhatsApp (Twilio) |
| `PUBLIC_BASE_URL` | Public https address of your app | Your ngrok domain or Render URL | Free | WhatsApp (Twilio) |
| ngrok authtoken | Lets ngrok run on your PC (not in `.env`) | [dashboard.ngrok.com](https://dashboard.ngrok.com) | Free | WhatsApp locally |

\* Twilio trial accounts receive messages but can't send the bot's free-form replies; upgrade Twilio or use Meta.

### A. Google Gemini (recommended)

1. Go to [Google AI Studio](https://aistudio.google.com/apikey) and sign in with a Google account.
2. Click **Create API key** and copy it.
3. You'll use `LLM_MODEL=google_genai:gemini-3.5-flash-lite`.

The free tier has per-minute and per-day request limits (shown in AI Studio); they're enough for development and a demo. Google may use free-tier prompts to improve its products — fine here because all clinic data is fictional.

### B. Groq (free alternative)

1. Sign up at [console.groq.com](https://console.groq.com) and open **API Keys** → **Create API Key**.
2. You'll use `LLM_MODEL=groq:llama-3.3-70b-versatile`.

### C. Ollama (local, no key, no internet for the LLM)

1. Install from [ollama.com](https://ollama.com), then run `ollama pull qwen2.5:7b`.
2. You'll use `LLM_MODEL=ollama:qwen2.5:7b` and `LLM_TIMEOUT_S=120`.

Needs about 8 GB free RAM. Local models are slower and follow tool-calling rules less reliably, so expect more handoffs.

> Model names change over time. If you get a "model not found" error, pick a current model from the provider's model list and update `LLM_MODEL`.

### Admin token

A password you invent for the staff console. Generate a random one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### Meta developer app (for WhatsApp — recommended)

1. Go to [developers.facebook.com](https://developers.facebook.com), log in with Facebook and register as a developer.
2. **My Apps → Create App**, choose the WhatsApp / "Connect with customers through WhatsApp" use case, and select or create a **business portfolio** when asked.
3. The full WhatsApp setup (test number, token, webhook) is in [step 5A](#5a-meta-whatsapp-cloud-api-recommended).

### Twilio (alternative)

1. Sign up at [twilio.com](https://www.twilio.com) (free trial, phone verification required).
2. On the Console home page, copy the **Account SID** and **Auth Token**.
3. Setup is in [step 5B](#5b-twilio-alternative). Remember: trial accounts can't send the bot's replies.

### ngrok (for WhatsApp while running on your PC)

1. Sign up at [ngrok.com](https://ngrok.com) and install it ([Windows download](https://ngrok.com/download/windows)).
2. Run the one-time command from the ngrok dashboard: `ngrok config add-authtoken <your-token>`.
3. In the dashboard, open **Gateway → Domains** and copy your free fixed domain (ends in `ngrok-free.dev`).

---

## 2. Install the project

**Prerequisites:** Python 3.11+ (`python --version`), Git (optional), and internet access on first run (the ~70 MB embedding model downloads once).

From the project folder:

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

If PowerShell blocks the activate script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt with `.venv\Scripts\activate.bat`.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Activate the virtual environment in every new terminal before running project commands.

---

## 3. Configure `.env`

Copy the template: `copy .env.example .env` (Windows) or `cp .env.example .env` (macOS/Linux). Then edit `.env`.

### Minimum to run locally (web demo + staff console)

```ini
LLM_MODEL=google_genai:gemini-3.5-flash-lite
GOOGLE_API_KEY=your-gemini-key
ADMIN_TOKEN=your-random-admin-token
```

### Add for WhatsApp — Meta (recommended)

```ini
META_ACCESS_TOKEN=EAAG...
META_PHONE_NUMBER_ID=123456789012345
META_APP_SECRET=your-app-secret
META_VERIFY_TOKEN=any-string-you-invent
```

### Or add for WhatsApp — Twilio

```ini
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your-twilio-auth-token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_VALIDATE_SIGNATURE=true
PUBLIC_BASE_URL=https://your-name.ngrok-free.dev
```

`TWILIO_WHATSAPP_FROM` must be the number shown on **your** Try WhatsApp page, prefixed with `whatsapp:` (new trial accounts get a different number, e.g. +1 737 250 8034). `PUBLIC_BASE_URL` must start with `https://`.

If both are configured, Meta is used. Force one with `WHATSAPP_PROVIDER=meta` or `WHATSAPP_PROVIDER=twilio`.

### Optional settings

| Variable | Default | What it does |
|---|---|---|
| `RETRIEVER` | `dense` | `dense` = local embedding model; `bm25` = keyword search, no download |
| `MIN_SCORE_DENSE` / `MIN_SCORE_BM25` | `0.55` / `2.0` | Relevance cut-off below which the bot won't answer from the docs (see step 6) |
| `LLM_TIMEOUT_S` | `25` | Seconds to wait for the LLM; raise for Ollama |
| `SESSION_TIMEOUT_MIN` | `30` | Minutes of inactivity before conversation memory starts fresh |
| `HANDOFF_IDLE_TIMEOUT_MIN` | `30` | Bot takes back a handed-off chat once staff go quiet this long |
| `CLINIC_TIMEZONE` | `Asia/Kolkata` | Timezone for opening hours and bookings |
| `DB_PATH` / `CHECKPOINT_DB_PATH` | `data/…db` | Where conversations, bookings and memory are stored |
| `WHATSAPP_PROVIDER` | `auto` | `auto` (Meta if configured, else Twilio), `meta` or `twilio` |
| `META_GRAPH_VERSION` | `v23.0` | Graph API version used to send Meta messages |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

**Never commit `.env`** — it holds your secrets and is already in `.gitignore`.

---

## 4. Run and test locally

1. **Run the tests** (optional, no keys needed):
   ```bash
   pytest
   ```
2. **Start the server:**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```
   Wait for `app started` in the log.
3. **Check health:** open http://localhost:8000/health and confirm `"llm_ready": true`.
4. **Web demo:** open http://localhost:8000/demo and try:
   - "What are your opening hours?" — answer with sources
   - "Book a cleaning tomorrow at 11am" — booking flow
   - "Status of ORD-1001" then last name "Sharma" — order lookup
   - "Talk to a human" — handoff with a ticket
5. **Staff console:** open http://localhost:8000/admin, sign in with your name and `ADMIN_TOKEN`, open the handoff, send a reply (it appears in the demo chat), then return it to the bot.

Stop the server with `Ctrl+C`. To start with empty data, delete `data/app.db` and `data/checkpoints.db`.

---

## 5. Connect WhatsApp

Both providers call your app through a public https address, so first expose your PC with ngrok.

### 5.0 Start ngrok

Keep the app running (step 4), and in a **second terminal** run:

```bash
ngrok http 8000 --url https://your-name.ngrok-free.dev
```

Use the domain listed under **Gateway → Domains** in the ngrok dashboard (don't click "New Domain" — that needs a paid plan). Or run `ngrok http 8000` and use the random `https://…` address it prints (it changes on every restart).

While ngrok runs, watch every incoming request (and your app's response) in the inspector: **[ngrok – Inspect](http://127.0.0.1:4040/inspect/http)**.

### 5A. Meta WhatsApp Cloud API (recommended)

> A more detailed, screen-by-screen walkthrough with fixes for every common error is in **[WHATSAPP_META_SETUP.md](WHATSAPP_META_SETUP.md)**.

**1. Get the test number, token and IDs**

1. In your Meta app (App Dashboard), open **WhatsApp → API Setup** (or **Use cases → Customize**, depending on how the app was created).
2. Meta gives you a free **test phone number**. Copy the **Phone number ID** shown under it → `META_PHONE_NUMBER_ID`. (It's a long ID, not the phone number.)
3. Click **Generate access token** → `META_ACCESS_TOKEN`. This temporary token expires within hours; for longer testing create a system user token (see below).
4. Under **To**, add your own WhatsApp number as a recipient and confirm it with the code WhatsApp sends you. The test number can only message numbers on this list.
5. Open **App settings → Basic**, click **Show** next to **App secret** → `META_APP_SECRET`.
6. Invent a verify token (any random string) → `META_VERIFY_TOKEN`.

Put these four values in `.env` and **restart the app**. http://localhost:8000/health should show `"whatsapp_provider": "meta"`.

**2. Connect the webhook**

1. In the App Dashboard open **WhatsApp → Configuration** (or **Use cases → Customize → Configuration**).
2. **Callback URL:** `https://your-name.ngrok-free.dev/webhooks/meta/whatsapp`
3. **Verify token:** the same string as `META_VERIFY_TOKEN`.
4. Click **Verify and save**. Meta calls your app once; it only saves if the app answers correctly (your app log shows `GET /webhooks/meta/whatsapp … 200`).
5. Under **Webhook fields**, **Subscribe** to `messages`.

**3. Test**

From your phone, send "What are your opening hours?" to the test number (it appears in WhatsApp as a business chat). Within a few seconds you should get the answer. Try "talk to a human", reply from `/admin`, and the staff reply arrives on your phone.

**Longer-lived token (optional).** The API Setup token expires quickly. In **Business settings → Users → System users**, add a system user, assign your app (and WhatsApp account) with full control, click **Generate token**, pick an expiry, and grant `whatsapp_business_messaging`, `whatsapp_business_management` and `business_management`. Put that token in `META_ACCESS_TOKEN`.

**Meta test number limits**

- It only messages numbers on the **To** list (add each person you demo to).
- Free-form replies are allowed within 24 hours of the user's last message; after that WhatsApp requires templates, so late staff replies won't be delivered.
- Your PC and ngrok must stay running, or deploy (step 7).

### 5B. Twilio (alternative)

> New Twilio **trial** accounts can receive messages but **can't send the bot's replies** (error 21654 "ContentSid Required"); you'll need to upgrade Twilio for replies to work.

1. In the Twilio Console open **Messaging → Try it out → Try WhatsApp**.
2. From your phone, send the shown `join <code>` message to the number shown (new trial accounts: `join twilio-trial` to +1 737 250 8034; upgraded accounts: the sandbox +1 415 523 8886). Put that number in `TWILIO_WHATSAPP_FROM` as `whatsapp:+…`.
3. Set the webhook to `https://your-name.ngrok-free.dev/webhooks/twilio/whatsapp` (POST):
   - **New trial screen:** **Receive a message** tab → **Auto-reply settings** → **Custom** → paste the URL → **Save**.
   - **Upgraded / sandbox screen:** **Sandbox settings** → **When a message comes in**.
4. Set `PUBLIC_BASE_URL=https://your-name.ngrok-free.dev` (with `https://`, no path), restart the app, and message the number.

---

## 6. Calibrate the relevance threshold

Run once after the first start (the embedding model must be downloaded):

```bash
python -m scripts.calibrate
```

It prints scores for in-scope and out-of-scope questions. Set `MIN_SCORE_DENSE` in `.env` between the lowest **IN** score and the highest **OUT** score, then restart. Repeat whenever you change the clinic documents or the embedding model.

---

## 7. Deploy a live demo (Render)

1. Push the project to a GitHub repository (make sure `.env` is **not** committed).
2. In [Render](https://render.com), choose **New → Blueprint** and select the repo. It reads `render.yaml`.
3. Fill in the secret environment variables when prompted: `GOOGLE_API_KEY` (or change `LLM_MODEL` and add your provider's key), the four `META_*` values (or the `TWILIO_*` values), and `PUBLIC_BASE_URL` = your Render URL (e.g. `https://lotus-lane-support-agent.onrender.com`). `ADMIN_TOKEN` is generated for you — find it under the service's **Environment** tab. Use a system user token for `META_ACCESS_TOKEN`, since the temporary one expires.
4. Wait for the build (it downloads the embedding model) and open `https://<your-app>.onrender.com/health`.
5. Point WhatsApp at the live URL: Meta **Configuration → Callback URL** = `https://<your-app>.onrender.com/webhooks/meta/whatsapp` (or the Twilio webhook = `…/webhooks/twilio/whatsapp`).
6. Put the live links in the README: `/demo` and how to message the WhatsApp number.

`render.yaml` uses the paid Starter plan so the database survives restarts on a persistent disk. For a quick free demo, switch to the free plan and remove the `disk` block — data then resets on each restart, and the free service sleeps when idle (the first message after sleep is slow).

**Other Docker hosts:**

```bash
docker build -t support-agent .
docker run -p 8000:8000 --env-file .env support-agent
```

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Every reply is "Sorry, I'm having a technical problem" | `/health` shows `llm_ready: false` → check `LLM_MODEL` and the matching API key in `.env`, then restart. |
| Log shows `429` / `RESOURCE_EXHAUSTED` | Free-tier rate limit reached. Wait a minute (or for the daily reset), or switch to Groq / Ollama. |
| Log shows model `404` / not found | Model name changed; pick a current one from the provider's list. |
| `dense retriever unavailable, falling back to bm25` | Embedding model couldn't download. Check internet/proxy, or set `RETRIEVER=bm25`. |
| `/admin` returns 403 | `ADMIN_TOKEN` is empty in `.env`. |
| Meta: "Verify and save" fails | App or ngrok not running, Callback URL wrong (must end in `/webhooks/meta/whatsapp`), or Verify token ≠ `META_VERIFY_TOKEN` (restart the app after editing `.env`). |
| Meta: log says `invalid meta signature` (403) | `META_APP_SECRET` is wrong — copy it again from App settings → Basic. |
| Meta: log `whatsapp send failed`, code `131030` | Your number isn't on the test number's **To** list (API Setup). |
| Meta: code `190` | Access token expired — generate a new one, or use a system user token. |
| Meta: code `100` | `META_PHONE_NUMBER_ID` is wrong — use the Phone number **ID**, not the phone number. |
| Meta: no request reaches the app | Webhook field `messages` not subscribed, or the message was sent to a different number. |
| Twilio: `ContentSid Required` (21654) | Twilio trial limitation — upgrade Twilio, or switch to Meta. |
| Twilio: ngrok shows `403` | Twilio webhook URL doesn't exactly equal `PUBLIC_BASE_URL` + `/webhooks/twilio/whatsapp`, or wrong `TWILIO_AUTH_TOKEN`. |
| Twilio: nothing appears in ngrok | ngrok not running, or webhook URL not saved in Twilio. |
| Twilio: worked, now silent | Sandbox membership expired; send the `join` message again. |
| Ollama times out | Raise `LLM_TIMEOUT_S` or use a smaller model (`qwen2.5:3b`). |
| Port 8000 in use | Start with `--port 8001` (and use that port in ngrok). |
| PowerShell won't activate `.venv` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `activate.bat` in Command Prompt. |

---

## Checklist

- [ ] LLM key created and set in `.env`
- [ ] `ADMIN_TOKEN` set
- [ ] `pip install -r requirements-dev.txt` done, `pytest` passes
- [ ] `/health` shows `llm_ready: true`
- [ ] Web demo answers, books, looks up orders, hands off
- [ ] ngrok running
- [ ] Meta app: four `META_*` values in `.env`, your number on the **To** list
- [ ] Meta webhook verified and subscribed to `messages`
- [ ] WhatsApp message gets a reply; staff reply reaches the phone
- [ ] Relevance threshold calibrated
- [ ] Deployed; WhatsApp webhook points at the live URL
