---
date: 2026-09-21
audience: Dispatchers
title: The automatic texts no longer quote a blank price to guests who never got one
---

# The automatic texts no longer quote a blank price to guests who never got one

## Send this to the team

> Hey team — heads up on the automatic follow-up texts to website leads. The
> fourth one says "your quoted rate of $125 is still available". For a guest
> whose route had no online price (the ones that come to you as a QUOTE NEEDED
> task), it was going out with the number missing: "your quoted rate of  for
> the October 27 trip". That text is now skipped for those guests, and so is
> the "your trip is coming up" text that quotes the same price.
>
> If a guest mentions getting a text with a blank price, that is what happened.
> Send them the real price and carry on.
>
> Nothing else about the follow-ups changed: same texts, same timing, same
> stop-when-they-reply behaviour. Guests who did get a website price still get
> all five.

---

## Behind the scenes

**Where it lives:** the automatic lead follow-up sequence (steps 2 to 5 after
the first "still need transportation?" text) and the pre-pickup nudge.

**Why:** the templates use a price placeholder. The renderer fills it with an
empty string when the lead has no website price, and nothing checked. Sixty
step-4 texts went out that way between August and mid-September 2026. Both
senders now check whether the template quotes a price and the lead has none,
and skip the step with the reason "no_price" on the row. The QUOTE NEEDED task
page shows these as "will be skipped" in its list of upcoming texts.

**Expect to be asked:**
- *"Why did this guest only get four automatic texts?"* The fourth was the
  price one and they had no website price. The rest went as normal.
- *"Can the price you sent by hand go into that text?"* Not today. The text
  quotes the website's own price, which is what the booking link honours. The
  price you send from the task page is yours to send by hand.
