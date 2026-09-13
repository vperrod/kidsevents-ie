# Lessons (Small Days)

- 2026-09-13 — Victor: "the same methodology as WanderTold must be applied" was said on 09-12 and answered with a partial copy (auto-approve + a two-hop social crew) rather than the whole method (facet vocabulary + hard validation, QA gate with grounding, free-model fallback chain local-first, photo gate, merge lock only at merge). When a reference project is named, replicate its full stage map, not the two stages closest to the symptom.
- 2026-09-13 — "hundreds waiting for me" was a stuck process (self-deadlocking flock), not a review load. Before reporting a backlog as "needs Victor", check the process that should be draining it is alive and progressing (`/proc/<pid>/wchan`, log mtime).
- 2026-09-13 — flock re-entrancy: `fcntl.flock` on a second `open()` of the same file in the same process blocks. Any lock helper used by nested callers must track depth per thread. Test the nested case before shipping.
- 2026-09-13 — systemd user units have no `~/.local/bin` on PATH; any CLI the pipeline shells out to must be resolved to an absolute path in code, and a timer run must be checked in `journalctl` after every change that touches it.
- 2026-09-12 — ship UI claims only after a real-browser click-through at desktop and phone sizes; curl proves the bytes, not the usability (Approve buttons clipped off-screen for every row went unnoticed).
