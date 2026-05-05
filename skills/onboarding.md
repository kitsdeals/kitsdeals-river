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

### 6. Apply via CLI

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

### 7. Confirm running

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

## Common adaptations

- **User has no Telegram bot yet:** walk them through `@BotFather` → new
  bot → token. Or fall back to `channel: stdout` for now and tell them
  they can rerun setup later when they have credentials.
- **User declines `notify_threshold` question:** default to `clear_win`.
  They can loosen later via `update-profile`.
- **User asks for things outside our taxonomy** (e.g., real estate
  listings): explain Kit's Deals is product/retailer-focused. Don't try
  to fit a square peg.
