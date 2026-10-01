# APP RUBRIC (generic UX checklist)

The Planner converts this checklist into observable checks for every item that produces something a person
looks at or interacts with. A rubric line that cannot be observed in the served output is not a check.
This file names no domain; apply each line to whatever the task describes.

## How to convert a line into a check

1. Pick the lines that apply to the item (at least two per such item).
2. Write them into `stage-N/acceptance/check_<item>.py`, using `factory/lib/hammer.py` to fetch the page
   and `factory/lib/htmlcheck.py` (`check_page(html)`) for the static lines.
3. Each check names the line it comes from, so a failure points to the rubric.

## Checklist

### Structure and accessibility (static, `htmlcheck.check_page`)

- The page declares a viewport meta tag, a `<title>`, and `<html lang>`.
- Every input has a `<label>` or an `aria-label`.
- At least one landmark element (`main` or `nav`) is present.
- No body-level container uses a fixed pixel width above 480.

### Responsive layout

- The layout reflows at a narrow width: no horizontal scrolling caused by fixed widths, images that
  scale, text that wraps.
- Controls are large enough to tap and are not placed so close together that they overlap.

### States

- Empty state: with no data, the page says what is missing and what to do next, rather than showing a blank area.
- Error state: a rejected input or a failed request shows a plain-language message near the cause and keeps
  what the person typed.
- Loading or progress: any action that takes noticeable time shows that something is happening.
- Success: a completed action is confirmed visibly and the result is where the person expects it.

### Consistency and clarity

- The same action uses the same wording, placement and style everywhere it appears.
- Headings describe the content below them; labels use plain words rather than internal names.
- Primary actions are visually distinct from secondary ones; destructive actions are not the default.
- Text has readable size and contrast, and meaning is never carried by colour alone.

### Behavior

- Repeating or abandoning an action (double submit, reload, back button) does not duplicate or lose work.
- Keyboard use works: focus order follows reading order and every control can be reached and activated.
- Bad input is rejected with a specific message; nothing crashes or shows a raw error or stack trace.

### Maintainability (reviewed by the Steward)

- Styles and scripts are organised rather than repeated; no unused code or assets.
- The README states how to build, run and exercise the deliverable in a few lines.
