# PINNED_FITCHECK_MESSAGE.md

Canonical reference text for ops to paste + pin in #fitcheck. Surfaces
the /roast mechanics so members can self-serve before flooding mods
with questions. Length-aware — fits Discord's pinned-message body.

Paste literally; the `@Stitch` mention auto-resolves; emoji literals
render natively.

---

```
# /roast — the rules

drop a fit, get a take. here's how it works.

## the basics
- post any image in this channel → bot reacts 🔥, starts a thread, counts your streak
- chat in the THREAD, not in the main channel — text posts here get deleted

## the burn
- `/burn-me` opts you in to be roasted on your next fit
- `/stop-pls` permanently opts you out (sticky — survives bot restarts)
- mods can right-click any fit → Apps → "Roast this fit" to manually roast

## peer roasting (@Stitch role)
- if you hold @Stitch, you get 1 free peer-roast per calendar month
- right-click someone's fit → Apps → "Roast this fit"
- run `/my-roasts` any time to see your tokens, streak progress, and cooldowns
- hit a 7-day streak → bonus restoration token (1/month max — break + re-streak in the next calendar month for another)

## consent + safety
- targets get a silent DM "{actor} roasted your fit in #fitcheck" with a 🚩 react option
- react 🚩 on the bot's roast → mods see it in `/peer-roast-report`
- `/stop-pls` is permanent, global to this server, and bypasses ALL paths (mod + peer)
- inner-circle members can be peer-roasted past the normal caps, but @stop-pls still wins

## caps + cooldowns
- 30s cooldown between any /burn-me, mod /roast, or peer /roast invocation
- target hits a 20-roast/day cap → mod & peer roasts both stop
- per-target peer cap: 3 roasts/month/target (bypassed for inner-circle)
- per-actor-target cooldown: 90 days (bypassed for inner-circle)

## privacy
- personalize-mode (admin toggle) lets the bot use your recent vibe in roasts
- if you /stop-pls, ALL your observation + vibe data is purged immediately
- the vibe inference rejects anything that looks like a prompt-injection attempt
```

---

## Authoring notes (for the operator pasting this)

- Pin via right-click → Pin Message (need Manage Messages)
- Edit the @Stitch handle if you've named the role differently in this server
- This text is locked-in for V1 launch; only the operator pasting it should
  trim/expand based on server culture
- Re-paste verbatim after any role-rename so the surface stays consistent
