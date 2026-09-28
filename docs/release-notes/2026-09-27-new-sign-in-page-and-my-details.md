---
date: 2026-09-27
audience: Chauffeurs
title: A new sign-in page, sign in with your email, and a My Details tab in the driver app
---

# A new sign-in page and a My Details tab

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
-->

## Send this to the team

> Hey team — the sign-in page has a new look, and a couple of things that were broken on it now work.
>
> - You can sign in with your username **or** your email address, and capital letters don't matter.
> - "Reset password" next to the password box actually sends you a reset email now, and after you reset you land back on your own trips — not on the travel agent page.
> - There's a Show button on the password box so you can see what you typed.
>
> In the driver app there's a new **My Details** tab. Check your mobile number, email and home address are right, and hit save. If we don't have a photo of your license yet, the tab asks for one before it saves — take it right there and we read the details off the card. That's the number dispatch texts when a trip changes, so please keep it current. The same tab has a Change password button and a Sign out link.
>
> Nothing else changed. Your username and password are the same, and your trips, schedule, documents and time off are where they always were.

---

## Behind the scenes

**Where it lives:** The sign-in page (staff and chauffeurs share it; travel advisors have their own with the same look), the four password-reset screens, and "My Details" in the driver app menu.

**Why:** The old sign-in page was a leftover template: the "Forgot Password?" link went nowhere, there was a Google button that did nothing, a public "Sign up" link, and the reset flow dropped everyone on the travel agent dashboard. The new design matches the rest of the site. My Details is the driver's half of onboarding — they type their own phone and address instead of the office doing it.

**Expect to be asked:**
- *"Forgot password says it emailed me but nothing came."* — It only works for accounts with an email on file, and only for accounts that already have a password. For anyone else, send them a welcome link from their profile — same result.
- *"I typed my number with dashes and it saved differently."* — Numbers are stored one way and shown as (407) 555-0134 everywhere. Any US number in any format is accepted; international numbers need the + in front.
- *"How do I change my password if I'm already signed in?"* — My Details → Change password. You stay signed in on that phone.
- *"Where do they add the license?"* — On the welcome page itself, optionally, or on My Details, where it's required until one is on file.
- *"It won't let me save My Details."* — Almost always the license photo. It's required until one is on file; after that the field disappears.
- *"Can I change my username?"* — Not from My Details. New drivers choose it when they open their welcome link; after that, ask the office.
