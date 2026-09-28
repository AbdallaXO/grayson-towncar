---
date: 2026-09-27
audience: Dispatchers
title: Add a new driver and text them a welcome link — no more reading out passwords
---

# Add a new driver and text them a welcome link

<!--
  Everything inside the block below gets pasted into the group chat as-is.
  Read docs/release-notes/README.md before writing it. Under ~150 words.
-->

## Send this to the team

> Hey team — new drivers can now set up their own driver app login from a link, so nobody has to make a password for them or have them come in.
>
> 1. On the driver directory, click the gold "Add a driver" button (admins only).
> 2. Type their name and mobile, pick in-house or affiliate, and choose "Text it".
> 3. They open the link on their phone, choose a username and password, and can snap a photo of their license right there. Then they check their details. If they skipped the license, My Details asks for it before it saves.
>
> Every driver's profile now has a "Driver App Login" card. It shows whether they can sign in, and lets you text or email a welcome link again — that also works as a password reset for anyone who forgot theirs. Below it, an Onboarding checklist ticks off what's on file: login, mobile, email, license, permit, home address, start date.
>
> Phone numbers now show the same way everywhere, like (407) 555-0134, and the directory search still finds them however you type the digits.
>
> Nothing else changed. Pay, schedules, the board and existing logins all work exactly as before.

---

## Behind the scenes

**Where it lives:** Driver directory → "Add a driver" (top right, admins only), and the "Driver App Login" + "Onboarding" cards at the top of every driver profile. Chauffeurs get a new "My Details" tab in the driver app.

**Why:** Creating a driver meant making the account in /admin, typing a password, and reading it out over the phone or having the driver sit next to you. As the roster grows that doesn't scale, and it left phone numbers in five different formats and no place for a home address or start date.

**Expect to be asked:**
- *"Can a dispatcher add a driver?"* — No. Same rule as editing a profile: admins only. Dispatchers see the login state on the directory ("No login" / "Link sent") but can't send links.
- *"The text didn't go out."* — The profile shows a red message if texting isn't set up on the server or the number is bad. Use "Make a link to copy" and send it from your own phone instead.
- *"The link says expired."* — Links work once and for seven days. Open the profile and hit "Text it again"; that makes a fresh one and kills the old one.
- *"A driver forgot their password and has no email."* — Same button: "Text a welcome link". They open it, pick a new password, and are signed straight in. The old "Forgot password" page only works for people with an email on file.
- *"Why does the directory search find '555-0134' but the number shows as (407) 555-0134?"* — Numbers are stored one way now; search matches on the digits you type.
