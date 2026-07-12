# .loop/config.sh — project configuration
PROJECT_NAME="my-project"
HEARTBEAT_INTERVAL=300        # Idle interval (seconds)
WORKER_COOLDOWN=30            # Between worker iterations
MAX_ITERATIONS=20             # Safety limit per brief
MAX_CYCLE_WALL_TIME_SECS=5400 # Per-cycle wall-time budget (seconds). Default 90 min.
                              # Override per-brief with **Cycle-wall-time-secs:** frontmatter.
WORKER_KILL_GRACE_SECS=10     # Grace period between SIGTERM and SIGKILL on timeout
CONDUCTOR_DEDUP_TTL_SECS=1800 # Conductor dedup cache TTL (seconds). After this, a repeated
                              # trigger is re-evaluated fresh — prevents indefinite idle when
                              # a stuck condition persists but the dedup cache holds the action.
NTFY_TOPIC=""                 # ntfy.sh topic (empty = no push notifications)
# Which notification CLASSES actually push (comma-separated allowlist). Every
# notify() call carries a class; a class not listed here is silently dropped —
# NTFY_TOPIC empty still means zero pushes regardless. Classes:
#   brief_started    — a brief was dispatched (worker spawned), once per dispatch
#   brief_escalated  — a brief was escalated/parked (repeat-failure, over-budget,
#                      sync/rebase/staleness, capability-gap, 3x worker failures)
#   brief_completed  — a brief finished (auto-merge queued OR awaiting review)
#   queue_stuck      — alive daemon, queued work, but no dispatch for
#                      QUEUE_STUCK_TICKS ticks with nothing active (one alarm)
#   ops              — operational chatter (queen kill/fail, circuit breaker,
#                      pause/resume, per-iteration + validator noise, merges).
#                      Absent from the default list → silenced. Add "ops" to hear it.
NTFY_EVENTS="brief_started,brief_escalated,brief_completed,queue_stuck"
QUEUE_STUCK_TICKS=6           # Consecutive stuck ticks before one queue_stuck push
VERIFY_CMD=""                 # Command to run after each task (e.g. npm test, cargo build)
GIT_REMOTE="origin"
GIT_MAIN_BRANCH="main"       # "main" or "master"

# ── Concurrency (all off by default → serial, single-flight behavior) ─────────
THROTTLE=1                    # Max concurrent in-flight briefs. 1 = serial.
WORKER_PARALLEL=false         # true = run up to THROTTLE worker iterations
                              # concurrently (non-blocking tick + reaper).
                              # Requires THROTTLE>1 to overlap anything.
SOLO_DRAIN_AFTER_SECS=0       # 0 = off. When >0 and a parallel-safe:false brief
                              # has sat at the queue head longer than this many
                              # seconds, hold other dispatches so the board
                              # drains and the solo brief runs next.
