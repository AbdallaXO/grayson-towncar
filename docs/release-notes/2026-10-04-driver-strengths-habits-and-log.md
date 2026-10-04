---
date: 2026-10-04
audience: Dispatchers
title: Driver profiles hold what we know about each driver
---

# Driver profiles hold what we know about each driver

## Send this to the team

> Hey team — every driver's profile now holds what we know about them.
>
> 1. **Strengths & habits:** tag a driver as an airport pro, great with car seats, always early or slow with luggage, plus the languages they speak and the areas they know. Add a short note if it helps.
> 2. **The log:** write down compliments, complaints, incidents and notes, with the date and the trip. Managers can mark a complaint or incident as a strike, and the profile shows the strikes from the last 12 months.
>
> Anyone on the desk can add tags and log entries. Only managers can make new tags, remove tags, mark strikes, or edit and delete entries.
>
> Drivers never see any of this, and nothing about how trips get assigned changes.

---

## Behind the scenes

**Where it lives:** a driver's profile, left column: the Strengths & habits card
and the Log card, both marked "Office only".

**Why:** what the desk knows about a driver lived in people's heads and the group
chat. Now it sits on the driver, so a new dispatcher can see at a glance who is
great at the airport, who speaks Spanish, and who needs a nudge to tap statuses.
Auto-assign doesn't read any of it yet.

**Expect to be asked:**
- *Can the driver see their tags or the log?* No. They only show on the office's
  driver profile, never in the driver app.
- *The tag I want isn't in the list.* A manager can make it from the same card
  (New tag); it is then offered for every driver.
- *I tagged the wrong thing.* Only a manager can take a tag off, so ask one.
  To change a note, a manager takes the tag off, then anyone adds it back
  with the new note.
- *A tag is misspelt, or we don't want it any more.* Fix the name or switch it
  off in the Django admin, under Driver tags. Drivers who have it keep it; it
  just stops being offered.
- *I logged something wrong.* Only a manager can edit or delete a log entry,
  so ask one. Editing shows who changed it.
- *The trip I want isn't in the list.* The log offers that driver's trips from
  the last 60 days. For an older one, leave the trip out and say which trip in
  the details.
- *Where do strikes show?* Next to the driver's name at the top of the profile
  and on the Log card: amber for one or two in the last 12 months, red from
  three. A strike older than a year stops counting on its own; it stays in
  the log.
