---
name: skill-jev
description: Verify a candidate food/drink name against the user's raw input with the Jev (TypeSafe System One) decision model, and decide whether to accept it or ask the user.
version: 0.1.0
author: drHiro team
metadata:
  openclaw:
    requires: []
---

# Jev Verification Skill

Jev is a **bounded-decision model**, not a chat model. You give it a state and
one typed question; it returns a probability instead of prose. This skill uses
exactly one question:

> Does the candidate food name refer to the same food product as the raw input?

The answer is `noul`: a probability from 0 to 1. Higher means the two names
denote the same product.

## Usage

```
exec {baseDir}/scripts/jev_verify.sh "<raw input>" "<candidate name>"
```

Optional third argument overrides the threshold.

Output (one JSON line):

```json
{"score": 0.98, "accept": true, "threshold": 0.88, "model": "jev-1.13.0"}
```

On a network/API problem it prints `{"error":"jev_unavailable","accept":null}`
and exits 0 — treat `accept: null` as **unverified**, not as a rejection.

## Threshold

Accept at **>= 0.88**. Calibrated on drHiro's own data: correct matches score
0.88-0.99, wrong matches 0.01-0.32. Do not lower it without re-measuring.

## Language rule — translate first

Jev is English-first. It scores Croatian `kava` against `Coffee` at **0.01**,
i.e. it does not know the Croatian word. **Always translate the input to
English before verifying.** The drHiro API already does this inside its
resolution cascade; when you call this skill directly, translate first.

## When to use it

- The API returned a candidate for a food or drink and you are unsure it is right.
- Before telling the user something was logged, when the resolved name looks wrong.
- A regional or brand product the catalogue may not know (gemišt, čajna pašteta).

## When NOT to use it

- **Never** use it to invent nutrition values. It judges *names*, not numbers.
- A low score is not proof the food is absent — only that *this* candidate is
  the wrong match. Move to the next candidate or source.
- Do not use it for open-ended questions; it cannot explain or generate.

## Cascade this skill belongs to

1. Local catalogue -> verify -> accept if >= 0.88
2. USDA FoodData Central -> verify -> accept if >= 0.88
3. DuckDuckGo, queried in the **user's own language** -> translate the result
   to English -> verify -> accept if >= 0.88
4. Nothing verified -> **ask the user**. Never guess, never fabricate a value.