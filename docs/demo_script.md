# Demo video script (about 75 seconds)

Record the phone (WhatsApp) on the left and the staff console on the right. Before recording, reset the database (`data/app.db`) so the counters start at zero.

| Time | Screen | Action | Voice-over |
|---|---|---|---|
| 0:00–0:08 | Title card / README diagram | — | "This is an AI support agent for a dental clinic on WhatsApp. It answers from the clinic's documents, takes actions, and hands off to a person when unsure." |
| 0:08–0:20 | WhatsApp | "What's the late cancellation fee?" | "Answers come only from the clinic's policies. Each reply must cite the passage it came from, or it isn't sent." Flash the web demo with *Show reasoning trail* to show the source id. |
| 0:20–0:40 | WhatsApp | "Book a cleaning tomorrow at 11" → name → "yes" | "It checks real availability, confirms the details, and books. The reference number comes from the booking system; the agent can't invent one." |
| 0:40–0:48 | WhatsApp | "Status of ORD-1001" → "Sharma" | "Order lookups verify the patient first." |
| 0:48–0:58 | WhatsApp | "Can you recommend a laptop?" | "Out of scope: low confidence, so it hands off with a ticket instead of guessing." |
| 0:58–1:10 | Staff console | Open the handoff, reply "Hi, this is Priya from reception", reply arrives on the phone, click *Return to bot* | "Staff see the full conversation and reply from the console, straight to WhatsApp." |
| 1:10–1:15 | Staff console stats | — | "Every message, tool call, handoff and failure is logged." |
