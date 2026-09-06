# Operator export of the a13–a15 documentation set (input for reconciliation)

These files are the operator's published documentation as of 2026-09-01/03
(manuals at 5.0.0a13, architecture at 5.0.0a15, memory-layers pages, and the
a8–a14 what's-new page). They carry sections that the a21 rewrite of
`docs-local/manual-*.html` and `docs-local/architecture-th.html` dropped.
They are reference input for the documentation reconciliation tickets, not the
published set: the published set is `docs-local/*.html`. Machine-specific paths
were replaced with `/PATH/TO/...` placeholders before import.

## Scope guard for documentation work

Pursers documentation covers the Pursers work track only. Anything from the
operator's personal/hobby tracks (role-play tooling, personal assistants, local
notes or wikis outside this repository) is out of scope: do not import, cite, or
link it. Machine-specific and home-relative paths (`/Users/...`, `~/...`) are
never allowed in this repository; use `/PATH/TO/...` placeholders.
