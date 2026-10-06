# SPEC v1.3.2 — dual-expiry options books (2026-10-05 CT)

**Source:** Christian via Trading / Team Manager — options overlay scaffolding must cover **both daily and weekly** expiries from the start; side-by-side books; same exits/risk scenarios; not daily-only.

**O1 (binding scaffold):**
- Every overlay scenario runs `book=daily` and `book=weekly` in parallel from the first `options.py` commit.
- Same signals / structure / stop-TP / time exit / sizing; only listed expiry differs.
- Daily: DTE ≤ 1 when listed; weekly: Friday weekly, DTE 2–10 [P].
- Each (scenario, book) is its own d2+w5 portfolio; readouts always side-by-side.
- Baseline sleeve: 3 structures × 2 books = 6 (replaces DTE-bucket axis).
- A2 high-risk: 9 stop×TP × 2 books = 18.
- Options rows 24; program **N = 456** (was 450).
- Design lock only — Dev 1 does not start overlay until Architect clears parallel overlay work. F2/cache/CP4 equity path stays first.
