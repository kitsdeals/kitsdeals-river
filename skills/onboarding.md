# kitsdeals-river onboarding

Use when: the user has linked you to this repo or installed the package
and asked you to *"set it up"* / *"configure it for me"* / *"I'm looking
for X."*

## Goal

Produce a complete, valid `~/.kitsdeals/profile.yaml` and start the
watcher daemon. The user should never see the YAML — your interview
becomes the file.

## Process

### 1. Read their stated intent first

If they already said *"I'm looking for X, Y"* — those are watch starting
points. Don't re-ask what they already told you.

### 2. Ask only what you need (4–6 questions max)

Required:

- **Their name / how they want to be addressed.**
- **Notification channel.** Telegram, email, Slack, Desktop notifications.
  If Telegram: ask for bot token + chat ID env var names (or accept the
  defaults: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
- **For each declared interest:**
  - Brands they prefer or want to avoid (1 quick question per category)
  - Rough budget (or "no budget if it's a great deal")
  - OK with refurb / open-box, or only new?
  - Threshold preference: *"every match"* (`anything`),
    *"clearly above noise"* (`good`), or *"only clear wins"* (`clear_win`)
- **Anything they already own** that should suppress notifications.

### 3. Mirror their tone

If they write to you in short sentences, set `tone: terse, factual`. If
they're conversational, mirror that. Don't ask them about tone explicitly
— observe.

### 4. Resolve product IDs (best-effort)

For `already_owned`, ask what they have. In v0.1 there's no
`resolve-products` server endpoint yet (PR 38), so pass the product names
as `OwnedProduct.product_name` for substring-based dedup. Ask for an
exact-ish match — the substring matcher is case-insensitive but does need
the brand + model in the name. Example user input: *"LG C4 65 OLED"* →
`product_name: "LG C4 65 OLED"`. When PR 38 ships `resolve-products`, the
agent looks up canonical IDs at this step instead.

### 5. Show a summary in plain English, not YAML

> "I'll notify you about LG / Sony / Samsung TVs under $2000 with at least
> 25% off, and any Apple or LG laptops with at least 20% off. Notifications
> go to Telegram. You won't get pinged about anything you already own.
> Sound right?"

Their answer is conceptual, not syntactic. Adjust based on the response.

### 6. Decide whether to enable the agent-evaluator step

By default the watcher just notifies on every match. If the user wants
**personalized evaluation per deal** (you, the agent, decide notify/skip/
defer based on the user's profile + recent decisions), set the
`agent` block:

```json
{
  "agent": {
    "command": "claude",
    "timeout_seconds": 60
  }
}
```

**The `command` value is YOUR own invocation.** You're the agent that
will be spawned per deal. If you're Claude Code → `"claude"`. If you're
a different CLI agent → use that. If unsure, leave the `agent` block
out entirely; the user gets simple notifications without the eval step,
and they can ask you to enable it later.

Some agents take the prompt differently. The default appends
`--no-input --task <prompt>` after the command. Override via
`prompt_args` with the literal token `{prompt}` substituted at spawn
time:

```json
"agent": {
  "command": "myagent",
  "prompt_args": ["run", "--input", "{prompt}"]
}
```

### 7. Apply via CLI

Build the profile spec as a JSON object matching the
`kitsdeals_river.profile.Profile` schema, then run:

```bash
kitsdeals-river setup --json '<the full profile JSON>'
kitsdeals-river run &
```

Or pipe the spec through a temp file via `--from-file` if the JSON is
long.

The shape of the JSON spec:

```json
{
  "name": "Tom",
  "tone": "Terse, factual.",
  "notify": {
    "channel": "telegram",
    "telegram": { "bot_token_env": "TELEGRAM_BOT_TOKEN", "chat_id_env": "TELEGRAM_CHAT_ID" }
  },
  "watches": [
    {
      "label": "TVs",
      "filter": {
        "category": "tv",
        "brand_in": ["LG", "Sony", "Samsung"],
        "max_price_cents": 200000,
        "min_discount_pct": 25,
        "condition_in": ["new", "open_box"]
      },
      "notify_threshold": "clear_win"
    }
  ],
  "suppress": {
    "already_owned": [
      { "product_name": "LG C4 65 OLED" }
    ],
    "brands_blacklist": [],
    "keywords_blacklist": []
  }
}
```

### 8. Confirm running

After `run &`:

```bash
kitsdeals-river status
```

Should show `running: true`. Tell the user it's running and that they'll
get a notification the next time a deal matches.

## Don't

- **Don't show them the YAML.** Show plain-language summaries. The agent-
  mediated interaction model is the whole point.
- **Don't ask >6 questions.** Overwhelming on first install. Refinement
  happens conversationally later via `skills/update-profile.md`.
- **Don't proceed if a critical answer is ambiguous.** One follow-up
  question is much better than a profile they'll be irritated by for a
  week.
- **Don't invent canonical product IDs.** When the user says *"LG C4 65
  OLED"*, store that as `product_name`. If you guess `prod_lg_c4_65` and
  it doesn't match anything in the river events, dedup silently fails.

## Setting up Telegram delivery (the from-scratch case)

If the user already has a Telegram bot they use for personal automation
and just needs to plug in the token + chat id, skip this section.

If the user is starting from zero, walk them through this — there are
five steps and one critical gotcha. **You should drive the steps for
them; don't just hand them a list and disappear.** Ask for the outputs
of each step inline so you can detect mistakes early.

### Step 1 — Create the bot via BotFather

Tell the user to open Telegram (mobile or desktop), search for
`@BotFather` (the official Telegram bot for managing bots — the
verified one with a blue checkmark), open a chat with it, and send:

```
/newbot
```

BotFather will prompt for:
- A **display name** for the bot (any string, e.g. "Tom's Deals")
- A **username** ending in `bot` (must be globally unique, e.g.
  `toms_deals_v1_bot`)

When successful, BotFather replies with a token that looks like:

```
1234567890:AAH9Xq...rest-of-token
```

**Ask the user to paste that token to you.** Treat it as a secret —
don't echo it back in plain text in subsequent messages, don't log it.

### Step 2 — CRITICAL: the user must DM `/start` to their new bot

This is the most-skipped step and the failure mode is silent. Telegram
bots **cannot send the first message** to a user — the user has to
initiate the conversation. If you skip this, the watcher will start
fine, the `kitsdeals-river status` check will say `running: true`, no
notification will ever arrive, and the only signal in the logs is a
single line:

```
{"description": "Forbidden: bot can't initiate conversation with a user"}
```

So: tell the user to open Telegram, search for the username they just
made (e.g. `@toms_deals_v1_bot`), open the chat, and either tap the
"Start" button or send `/start` as a message. **Wait for them to
confirm they did this.** Don't proceed until they do.

### Step 3 — Get the chat_id

The chat_id is a numeric Telegram identifier for the conversation the
bot will deliver to. Two paths to retrieve it; pick whichever fits the
user's setup:

**Path A (fastest, agent-driven)**: have the user send any message to
the bot (e.g. "hello"). Then make this call yourself, substituting the
token they gave you in step 1:

```
GET https://api.telegram.org/bot<TOKEN>/getUpdates
```

The response is JSON. Find `result[*].message.chat.id` — that's the
number you want, e.g. `123456789`. Direct chats are positive integers;
group chats are large negative integers (we'll skip groups in v1 —
direct chat is enough for most setups).

**Path B (user-driven)**: if you can't make outbound HTTP calls from
your runtime, tell the user to open Telegram and DM `@userinfobot` —
it replies with their numeric user id, which IS their direct chat_id
with any bot.

Either way, **read back the chat_id to the user** so they can sanity-
check ("That's a 9-digit number starting with 12 — does that look
right?"). Wrong chat_id is the second-most-common Telegram setup
failure.

### Step 4 — Set the env vars

The default profile expects two env-var names: `TELEGRAM_BOT_TOKEN`
and `TELEGRAM_CHAT_ID`. Set them so the watcher process can read them.
For systemd, this typically means an EnvironmentFile with mode 0600:

```ini
# /etc/systemd/system/kitsdeals.service
[Service]
EnvironmentFile=/home/<user>/.config/kitsdeals-river/telegram.env
ExecStart=/path/to/kitsdeals-river run
```

```bash
# /home/<user>/.config/kitsdeals-river/telegram.env
TELEGRAM_BOT_TOKEN=1234567890:AAH9Xq...
TELEGRAM_CHAT_ID=123456789
```

Permissions: `chmod 600` the env file. The token is a secret.

### Step 5 — Verify before declaring done

Don't trust `kitsdeals-river status` as a verification of delivery —
it reports the watcher's own health, not whether Telegram is reachable.
Send a one-shot test by hitting Telegram's API directly:

```
POST https://api.telegram.org/bot<TOKEN>/sendMessage
{"chat_id": <CHAT_ID>, "text": "kitsdeals-river setup test"}
```

The response should include `"ok": true`. If it returns
`"description": "Forbidden: bot can't initiate conversation with a
user"`, step 2 wasn't done — go back and have the user send `/start`
to the bot. If it returns `"description": "Bad Request: chat not
found"`, step 3's chat_id is wrong — try Path B from step 3 to get
the right one.

Once the test message lands in the user's Telegram, you're done.

### Forum-topic delivery (optional, advanced)

If the user wants notifications routed to a specific topic in a
Telegram supergroup (vs. their direct chat), they need to:
1. Create the supergroup with topics enabled
2. Add the bot to the group
3. Get the topic_id (visible in the URL when you tap into a topic)
4. Set `TELEGRAM_TOPIC_ID=<id>` as an additional env var
5. In the profile, set `notify.telegram.topic_id_env: "TELEGRAM_TOPIC_ID"`

This is rare for v1 setups (most users want direct chat). Skip unless
the user specifically asks for group/topic routing.

## Common adaptations

- **User has no Telegram bot yet:** see "Setting up Telegram delivery
  (the from-scratch case)" above. If they don't want to set up a bot
  at all (e.g., privacy-conscious), fall back to `channel: stdout` and
  tell them they can rerun setup later when they have credentials.
- **User declines `notify_threshold` question:** default to `clear_win`.
  They can loosen later via `update-profile`.
- **User asks for things outside our taxonomy** (e.g., real estate
  listings): explain Kit's Deals is product/retailer-focused. Don't try
  to fit a square peg.
