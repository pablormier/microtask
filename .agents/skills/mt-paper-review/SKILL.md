---
name: mt-paper-review
description: Read a research paper, build a concise paper map, and discuss its methods, evidence, assumptions, and limitations through journal-club Q&A. Save the review as a microtask Note when working in a microtask workspace.
---

# Paper review and journal club

Act as a collaborative, curious, constructively skeptical journal-club peer.
Read [journal-club.md](references/journal-club.md) for the user's complete
review prompt and follow it throughout the discussion.

## Source and initial map

In a microtask workspace, create a run directory only if the review needs
downloads, extracted text, rendered pages, or draft files. Follow the scratch
convention in the [microtask skill](../microtask/SKILL.md):

```sh
paper_review_dir="$(./mt scratch)"
```

Place those working files and tool outputs in that directory. If the paper and
review can be handled without intermediate files, skip the directory. Keep
verified source references in the saved Note.

Accept an uploaded paper, local file, or source URL, plus any question or review
focus. Obtain the paper before reviewing it; an abstract alone is not a full
paper. If access is incomplete, identify what you could read and limit claims
accordingly. Ask for missing source material when needed.

Begin each new paper with the concise map specified in the prompt. Include
paper type, research question, contribution, datasets and their roles, data
format/scale/source, applications, evaluation, findings, and uncertainties.
Use tables when they make datasets, applications, or evaluations easier to
compare. Mark missing details as not reported rather than guessing.

Identify the most consequential assumption and a simple baseline or test that
could challenge it. Label these as your assessment and proposed test. If no
single assumption supports most of the paper, say so rather than inventing one.
Distinguish an evaluation not found in the available material from one proven
to be absent; do not claim the authors missed a test without checking the paper
and available supplementary material.

## Discussion and evidence

Base answers primarily on the paper. Distinguish paper statements, code
evidence, external background, your inference, and missing information. Cite
sections, pages, figures, tables, or equations only when actually verified.
Keep simple answers direct and concise; use sections for complex questions.

For methods papers, cover inputs, formats, dimensions, preprocessing, steps,
parameters, defaults, outputs, assumptions, failure modes, and minimum usage
as relevant to the user's question. When an implementation is available,
inspect its documentation and relevant code, then compare it with the paper.
Record the inspected repository version when known. Do not claim to have run
code based on inspection; installation and experiments are separate work.

## Save a microtask Note

In a microtask workspace, prepare the initial paper map as a Note unless the
user requests discussion only. Show the proposed Note and save it after
agreement through the sibling microtask skill's creation review workflow.
Keep the conversational overview concise; the Note
can retain the supporting details needed to revisit the paper. Outside a
microtask workspace, provide the review in the conversation and save only to
a destination supplied by the user.

Read the sibling [microtask skill](../microtask/SKILL.md) for all card
operations. Use [note-outline.md](assets/note-outline.md) as an adaptable
outline for the review content. Omit irrelevant or empty subsections and do
not fabricate bibliographic details.

Save approved review content through `./mt note --title "Paper review: TITLE"
--file -`, supplying the body on stdin. Append context with `./mt edit REF
--body --append --file -` when needed. Use UTF-8 files in the run
directory and pass their paths with `--file` only when a draft needs inspection,
revision, or reuse.

Keep the original user request and supplied context verbatim.
Follow the operating skill's input rules; the CLI supplies the card structure.
Validate and return the Note's reference or path.

For subsequent Q&A, answer directly. Append substantive discussion to the
saved Note when the user asks to retain it. For a substantive authorized revision, retain the previous review under an
ordinary body heading, then use a hash-guarded body edit or exact replacement.
Small corrections need no duplicate review. Tasks and notes use the same body
interface; no application-specific heading names or levels are required.
Do not create a new Note for each question or replace a review automatically.
