---
date: 2026-09-12
audience: Dispatchers
title: A copied job pastes clean into a partner's system
---

# A copied job pastes clean into a partner's system

## Send this to the team

> Hey team — when a partner company hit Copy whole job and pasted it into their
> own dispatch system, some of the punctuation came out as junk symbols. Quotes,
> dashes and the dot in the flight line were the culprits. Their system couldn't
> read them, so the job arrived peppered with code instead of words.
>
> That's fixed. The job now copies as plain, simple text from top to bottom, and
> it reads the same whether they paste it into their dispatch system, a text
> message or a notepad. Same for the copy-the-whole-day button.
>
> Nothing else about the job changed — same guest, same flight, same car seats,
> same everything, in the same order. Our own chauffeurs were never affected;
> their app is untouched. Nothing changed about how you book or assign.

---

## Behind the scenes

**Where it lives:** the partner (operator) portal — Copy whole job on a job
card, the per-field copy lines, and the copy-the-whole-day buttons.

**Why:** the copy text carried typographic punctuation — a middle dot in the
flight line, an em dash in the "car seats, count not confirmed" line, and
whatever curly quotes a dispatcher had pasted into the notes out of an email.
Anything on the far end that isn't UTF-8 clean renders those as percent codes,
which is what a partner was seeing. The text is normalised to plain ASCII where
the fields are built, so every copy button gets it.

**Expect to be asked:**
- "Did the notes get shortened?" No. Only the punctuation shape changed — curly
  quotes became straight ones, fancy dashes became hyphens. Every word is there.
- "Was our own driver app doing this too?" No. This was only the partner copy
  block; the chauffeur app never had it.
