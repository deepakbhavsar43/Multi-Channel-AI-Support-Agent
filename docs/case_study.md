# Case study: a WhatsApp support agent that knows when to stop

*Draft. Fill in the bracketed results after deploying and running the eval set.*

## Problem

Small clinics answer the same questions all day — hours, prices, cancellation rules, insurance — and lose time on booking back-and-forth. A chatbot helps only if patients can trust it: one invented price or a fake booking confirmation costs more goodwill than the bot saves. The goal was a WhatsApp agent for a (fictional) dental clinic that:

- answers only from the clinic's own documents,
- completes real tasks: booking, support tickets, lab-order status,
- hands off to a person when unsure, and does so visibly and predictably.

## Approach

**Grounding is enforced in code, not requested in the prompt.** The model acts only through tools and ends every turn by calling `send_reply` with a message, a reply type, cited passage ids, and a confidence level. A deterministic `verify` step checks the reply before it is sent:

- an answer must cite passages actually retrieved this turn, above a relevance threshold;
- a claimed action must match a tool call that succeeded;
- any booking or ticket reference must appear in a tool result.

A failed check is fed back to the model once to self-correct; a second failure hands off to a human.

**Safety-critical routing never depends on the model.** Requests for a person and emergency symptoms are matched before the LLM runs. Card numbers and OTPs are redacted before storage and never reach the model.

**Handoff is a state, not just a message.** It creates a ticket with a conversation summary, the bot goes quiet while staff own the thread, and staff return the conversation to the bot from a console.

**Failures are expected.** Tool errors return to the model as data. LLM outages produce a fallback reply and, on repeat, a handoff. Webhook retries are deduplicated. Replies are sent asynchronously so slow model calls never hit Twilio's timeout.

## Stack

LangGraph (state graph, SQLite checkpointer for session memory) · LangChain `init_chat_model` (provider-agnostic; tested with [model]) · fastembed `bge-small-en-v1.5` local embeddings with BM25 fallback · FastAPI · Twilio WhatsApp · SQLite · Docker on Render.

## Result

- Live on WhatsApp sandbox and web demo: [links]
- 43 automated tests covering grounding rejection, hallucinated references, handoff triggers, memory across turns, LLM outage, webhook signature and idempotency.
- On a [N]-question eval set: [x]% answered correctly with valid citations, [y]% correctly handed off, [z] unsupported claims sent.
- Median reply latency: [n] s; tool-call failure rate: [n]%.

## What I'd do next

Self-service reschedule and cancel, Postgres for multiple instances, a voice channel reusing the same service layer, and an eval run in CI on every prompt or model change.
