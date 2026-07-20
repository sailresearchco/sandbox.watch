Read AGENTS.md in this directory first and follow it, including the writing
style section.

A web monitor found changes relevant to the sandbox products this site
tracks. The full event payload, including researched content and citation
URLs, is in {event_path}.

Do the following, changing only what the event gives evidence for:

1. Update the matching files under data/providers/. Touch only fields the
   event supports, set last_verified to today, and add the event's citation
   URLs to sources (deduplicated).
2. If the event describes a sandbox product this site doesn't track yet,
   create data/providers/<slug>.json for it. Fill what the event cites and
   leave every other field null.
3. Write state/turn_summary.md. Its first line is a commit-style summary
   under 72 characters. Below that, one short paragraph saying what changed
   and which sources support it.

Never invent numbers or facts. If the event contains no real product change,
edit nothing and say so in state/turn_summary.md.
