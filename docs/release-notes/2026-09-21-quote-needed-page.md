---
date: 2026-09-21
audience: Dispatchers
title: A "quote needed" task opens on the text to send, and sending it marks the lead contacted
---

# A "quote needed" task opens on the text to send, and sending it marks the lead contacted

## Send this to the team

> Hey team — the "QUOTE NEEDED" tasks in Ops Control have a proper page now, and
> the first thing on it is the text to the guest.
>
> 1. The price is already there. It is worked out the moment the request comes
>    in, the same way the quote calculator does it, so the task shows up in
>    Ops Control with the number in its title and the message already priced:
>    their name, the route, the date, the car they picked, the price. Next to
>    it you can see where the number came from and open "Show the math". Type
>    over it if you want a different price; edit the message if you like.
> 2. Hit **Copy text** and paste it into RingCentral.
> 3. Complete the task once the price is out.
>
> If the addresses can't be priced automatically, it says so and you type the
> price or open the calculator, which comes up with the route already filled in.
>
> Copying the text (or hitting Call or Email) marks the lead as contacted and
> puts the attempt on their record, so nobody has to do that by hand any more.
> If the guest opted out of texts, the text box is off and it tells you to call
> or email.
>
> Under the message you'll see the trip laid out with the car's picture and
> capacity, then "Where this lead stands": a few plain sentences, then every
> automatic text that has already reached the guest with its full wording, any
> reply they sent, and every text the automation still plans to send with the
> exact words and the day and time it will go. So you can see what they've
> been told before you write anything.
>
> Once you've sent the price, hit **Stop the automatic texts — I'm handling
> this** so the guest isn't asked "still looking?" the next morning. It shows
> right under the message box while the follow-ups are running.
>
> What did not change: how these tasks get created, the quote calculator's
> prices, and the automatic follow-ups.

---

## Behind the scenes

**Where it lives:** Ops Control, any manual task filed from the website's quote
form when the rate card had no price for the route (titled "QUOTE NEEDED"). The
quote calculator also accepts the route in the address bar and prices it on load.

**How the price gets there:** when the quote form files the task, a background
job asks the quote calculator's engine for the lead's route, car and trip type
and writes the answer onto the task: the price (also appended to the title, so
the queue row reads "… · $185"), its source (published rate card, local custom,
or custom estimate), the distance and drive time, and the internal breakdown
behind "Show the math". Only the price itself belongs in the text. A task filed
before this shipped, or one the engine could not price at the time, asks the
engine live when the page opens; that answer is remembered for six hours per
route. The website's own "estimated price" on the lead stays empty on purpose:
it means the site quoted it, and the reason the task exists is that it did not.

**Why:** the page showed five lines of raw text and a name. The vehicle, the trip
type and the date were buried in that text, there was no link to price it, the
message had to be written from scratch every time, and marking the lead
contacted was a separate step people skipped. These are the custom, long,
high-ticket routes most worth answering fast.

**Where the automation context comes from:** the first text is read from the
GoHighLevel send log (the step-1 row only holds a placeholder); texts 2 to 6
from the follow-up rows; a reply from its activity entry; the quote email is
described, not quoted, since it is a template. Upcoming texts are rendered from
the live templates with the lead's details, exactly as the sender would render
them. Stopping uses the sequence's own cancel path with the reason "manual"
and a line on the lead naming who stopped it.

**What "marks contacted" writes:** the lead moves from New to Contacted (other
statuses are left alone), its contact count and last-contact time update, an
entry lands in the lead's activity log naming who did it, and a text or email is
logged on the task as a sent attempt. A call is recorded on the lead but its
outcome is left for the person to log below, since the page cannot know how it
went. An unclaimed task becomes yours the moment you reach out from it.

**Expect to be asked:**
- *"Copy text says paste into RingCentral — can it just send?"* No. Nothing here
  sends a message; RingCentral is not connected. Copy, paste, send. "Open in
  texting app" is there for a phone.
- *"I copied but changed my mind — is the lead still marked contacted?"* Yes.
  Copying counts as reaching out. If you did not send anything, leave a note on
  the task.
- *"Why can't I text this one?"* The guest opted out of texts. Call or email.
- *"The car has no picture."* That vehicle type has no image on file. The name
  and capacity still show.
