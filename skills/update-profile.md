# kitsdeals-river update-profile

Use when: the user gives feedback on a notification or a watch:

- *"stop showing me TVs"* / *"too many alerts on laptops"*
- *"I bought one of those, no need to keep watching"*
- *"more aggressive on Sony"* / *"only show me clear wins"*
- *"add Bose to my brand blacklist"*
- *"I switched from LG to Apple"*

The user's input is natural language; your job is to translate to a
structured CLI invocation that edits the profile.

## Process

### 1. Detect the intent

Common patterns and their CLI shape:

| User said | Translates to |
|---|---|
| "stop watching X" | `--remove-watch <label>` |
| "I already have Y" | `--add-owned '{"product_name": "Y"}'` |
| "be more strict on Z" | `--add-watch '<json>'` (replaces existing watch by label) |
| "show me anything from brand Q" | `--add-watch '<json with notify_threshold: anything>'` |
| "blacklist BadBrand" | `--blacklist-brand BadBrand` |
| "drop the laptop watch entirely" | `--remove-watch laptops` |

If the user's request doesn't fit any of these patterns, fall through
to the full-replacement path: build a complete updated profile and pass
it via `kitsdeals-river setup --json '<full profile>'` to overwrite.
That's the explicit no-clever-inference escape hatch.

### 2. Read current state via JSON, not YAML

```bash
kitsdeals-river profile show --json
```

Reason about it. **Never paste this output back to the user** — they
asked a natural-language question, give a natural-language answer.

### 3. Dry-run first

Use `--no-apply` to preview the diff without writing:

```bash
kitsdeals-river profile update \
  --remove-watch "TVs" \
  --no-apply
```

Output is a JSON `{ "before": ..., "after": ... }` — easy for you to
verify the change is what you intended.

### 4. Confirm proportionally

| Change | Confirm with user? |
|---|---|
| Tightening (raising threshold, narrowing brand list) | Just apply, report what you did |
| Adding a suppression (already_owned, blacklist) | Just apply, report |
| Removing a whole watch | **Confirm first** — destructive |
| Lowering a threshold (anything more permissive) | Just apply |
| Adding a new watch on a category they didn't request | **Confirm first** — risk of misread |

### 5. Apply + automatic reload

Drop `--no-apply`. The CLI signals `SIGHUP` to the running watcher
automatically — config changes pick up live, no restart needed:

```bash
kitsdeals-river profile update --remove-watch "TVs"
```

If the watcher isn't running (no PID file), the edit still applies; the
next `run` will pick it up.

### 6. Tell the user what you did, in their words

Good:

> "Dropped TVs from your watch list. You'll still see laptop deals."

Bad:

> "I removed the watch with `label = TVs` from `profile.watches[]` and
> sent SIGHUP to pid 4321."

The user is talking to their agent, not reading shell output.

## Worth doing proactively

When you notice patterns in `decisions.jsonl` feedback (e.g., user has
skipped 4 TV deals in a row with feedback like *"already have one"*),
consider proposing a profile update yourself:

> "I noticed you've skipped 4 TV deals this week, all with similar
> feedback. Want me to drop TVs from your watch list, or tighten the
> threshold to `clear_win` so only the very best slip through?"

If they say yes → run `update-profile`. If they say "tighten the
threshold" → `--add-watch` with the existing filter and updated
threshold.

This is the synthesis loop: agent watches the decisions log, proposes
edits, human approves, profile evolves. Don't apply unilaterally — the
user's `_meta.last_edited_by` should reflect their consent.

## Common pitfalls

- **Don't infer canonical_product_ids.** When the user says *"I bought
  the LG C4"*, store as `product_name`, not a guessed ID. The substring
  matcher does the right thing.
- **Don't blacklist brands the user merely complained about once.** If
  they said *"that wasn't a great deal"*, that's not a brand blacklist
  signal — it's a quality threshold signal. Different fix.
- **Don't merge multiple edits silently.** If the user says *"stop
  watching TVs and lower my budget on laptops"*, do those as two
  separate `update` calls so the diff is reviewable.
- **Don't forget the `feedback` field on the decisions log.** When the
  user replies to a notification, capture their reply and append it to
  the matching decision via `DecisionsLog.append(feedback=...)` (PR 38
  ships the message-handler that does this; for now it's optional).
