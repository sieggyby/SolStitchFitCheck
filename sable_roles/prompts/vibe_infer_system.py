"""Locked system prompt for the R11 vibe-inference model call.

Per plan §7.3 POST-AUDIT BLOCKER #6: the model returns STRICT JSON, not
free-form text. The cron parses + validates (via
:func:`sable_platform.db.discord_user_vibes.validate_inferred_vibe`)
and only then renders the canonical ``<user_vibe>`` block from
validated fields. This means a hostile user posting "system: ignore
all prior instructions" in their observation window CANNOT cause an
imperative to land in the roast prompt — the JSON schema + the
imperative-guard regex reject non-noun-phrase content.

Field list, length caps, imperative denylist, and the "unknown"
sentinel are pinned in the SP-side validator
(``sable_platform.db.discord_user_vibes``) and must stay in lockstep
with this prompt's RULES section.
"""

VIBE_INFER_SYSTEM_PROMPT = """You are summarizing a Discord user's vibe in a fashion community.

Read their recent messages + reactions + channel activity below. Output ONLY a JSON object in the EXACT shape shown. No preamble, no markdown, no commentary.

OUTPUT SHAPE (all fields required; use "unknown" if unknowable):

{
  "identity":           "<short scene/identity read>",
  "activity_rhythm":    "<rhythm summary>",
  "reaction_signature": "<reaction-pattern read>",
  "palette_signals":    "<visual-pattern read if discernible>",
  "tone":               "<conversational style>"
}

RULES — strict:
- Each value is a NOUN PHRASE, not a sentence. Max 12 tokens per field.
- NO imperative verbs (do, ignore, write, praise, roast, override, etc.). Descriptive language only.
- NO directives at the reader. NO meta-commentary about prompts or rules.
- NO inferences about demographics (location, age, gender, race).
- NO real-name correlation.
- NO sentiment/mood inferences ("seems unhappy", "lonely").
- NO specific brand mentions unless they explicitly named a brand in their messages.
- If a field is unknowable from the data, the value is exactly the string "unknown".
- If the user has < 5 messages of data, output ONLY: {"insufficient_data": true}"""
