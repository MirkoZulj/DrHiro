# drHiro tool request templates — for OpenClaw tool generation.

Each tool maps to a POST/GET against the drHiro Core API. The gateway
injects X-Service-Token and X-Telegram-Id automatically.

## create_meal_from_telegram_photo

POST /api/v1/tools/create_meal_from_telegram_photo

Body:
```json
{
  "file_id": "AgACAgUAAx...",
  "caption": "chicken, rice, salad",
  "eaten_at": "2026-08-10T12:30:00Z"
}
```

Behavior: creates a needs_review meal draft. The vision worker analyzes
the photo and populates items. The assistant MUST ask the user to confirm
before treating the draft as logged.

## create_meal_from_text

POST /api/v1/tools/create_meal_from_text

Body:
```json
{
  "text": "Breakfast: 2 eggs, 2 slices rye bread, 10g butter",
  "meal_type": "breakfast",
  "eaten_at": "2026-08-10T08:00:00Z"
}
```

Behavior: the meal is created as a draft with the raw text in notes.
Structured item extraction happens in the pipeline; the assistant reports
the draft back and asks for any high-impact corrections.

## update_meal_item

POST /api/v1/tools/update_meal_item

Body:
```json
{
  "meal_id": "...",
  "item_id": "...",
  "patch": {"grams": 200, "display_name": "Grilled chicken breast"}
}
```

## confirm_meal

POST /api/v1/tools/confirm_meal

Body:
```json
{"meal_id": "..."}
```

Only call after the user explicitly confirms the draft.
