---
name: mt-focus
description: Help choose what to work on next in microtask, explain the task in plain language, and give a small starting step. Use for focus, refocus, or choosing work that fits the user's time and energy.
---

# Choose what to do next

Follow the [microtask skill](../microtask/SKILL.md) for task operations.
Focus chooses work; [triage](../mt-triage/SKILL.md) organizes captures.

## Find a good choice

Run `mt focus --json`. Start from its saved choice, ranked candidates, and
deadline alerts; the ranking is a starting point, not the decision. Use `--by`
only with the user's own identity. Read the relevant cards in full, and search
with `--all` when related or archived cards matter. Look at alternatives
without changing the saved focus.

Fit the choice to the user's time, energy, and tools, and prefer continuing
current work when it makes sense. Check whether a deadline needs preparation
or help from someone else, including beyond the seven-day window. Ask at most
one short question, and only if the answer would change the choice. Respect
claims and deferred work. If nothing fits, say so and suggest one step that
unblocks or clarifies something.

## Make it easy to start

Recommend one task with its `@ref`. Explain what needs doing, why now, and any
important constraints so the user can start without rereading the card.

Use complete, natural sentences and familiar words. Leave out irrelevant detail
rather than compressing sentences into shorthand. Use a few bullets when helpful;
include related card references and explain why they matter. Avoid backlog lists.

Give one small starting step. Avoid extra choices, compulsory timers, or guilt.
If the user gets stuck, shrink the step. After a distraction, remind them where
to resume and check for meaningful changes.

For example, when supported by the cards:

> **Suggested: finish the proposal (@a12bc345).**
>
> Marta needs the draft tomorrow so she can review it. You have already agreed
> on what the project will include. You still need to update how much it will cost.
>
> **Start:** Open the proposal. Use the figures in **Budget notes (@b23cd456)**
> to update the cost section.
>
> **Focus on this?**

## Save the choice when agreed

Ask "Focus on this?" and run `mt focus REF` only after the user agrees. If they
already said "focus on this task", or it is already saved, don't ask again.
Cycle or clear focus only when asked.

Saving focus does not start the task, claim it, change its priority, or start a
timer. Do those only when separately asked, following the microtask skill.
