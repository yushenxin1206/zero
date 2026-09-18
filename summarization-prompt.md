Compress this conversation history into a structured summary — the ONLY context for future turns. Output the summary alone, in the conversation's primary language.

Rules:
- A <previous_summary> block, if present, is the sole record of earlier history: merge all its facts, prune what is stale or superseded.
- Record current state and outcomes, not step-by-step transcripts. Keep exact wording only for paths, commands, error text, and names/titles.
- Each fact appears in exactly one section. Empty sections get "(none)"; never omit a heading.
- If space is tight, keep errors/fixes, paths/identifiers, decisions/rationale, pending work — drop tool-call narration first.

Follow this exact format:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
- [Hard constraints and stated preferences; "(none)" if none]

## Progress
### Done
- [x] [Completed tasks/changes — for content-production tasks, include each item's title/topic]
### In Progress
- [ ] [Unfinished work]
### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of remaining work]

## Critical Context
- [Technical facts, exact error text, and details that must not be lost; "(none)" if none]

## Files
[Every file path read or modified, absolute or relative to the working directory, one per line; "(none)" if none]
