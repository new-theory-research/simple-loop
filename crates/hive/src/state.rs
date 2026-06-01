use chrono::{DateTime, Utc};
use serde::{Deserialize, Deserializer};
use std::{
    collections::HashSet,
    fs,
    io::{BufRead, BufReader},
    path::Path,
};

/// Deserialize a `Vec<T>` element-by-element, dropping any element that fails
/// to deserialize. Lets one stray entry not poison the whole list — important
/// for `running.json` which is occasionally hand-edited or written by paths
/// (the hand-merge recipe, etc.) that may emit unexpected types.
fn deserialize_lossy_vec<'de, D, T>(de: D) -> Result<Vec<T>, D::Error>
where
    D: Deserializer<'de>,
    T: serde::de::DeserializeOwned,
{
    let raw: Vec<serde_json::Value> = Vec::deserialize(de)?;
    Ok(raw
        .into_iter()
        .filter_map(|v| serde_json::from_value(v).ok())
        .collect())
}

// ── time helpers ──────────────────────────────────────────────────────────────

pub fn relative_time(ts: DateTime<Utc>) -> String {
    let secs = (Utc::now() - ts).num_seconds().max(0);
    if secs < 60 {
        format!("{}s ago", secs)
    } else if secs < 3600 {
        format!("{}m ago", secs / 60)
    } else if secs < 86400 {
        format!("{}h ago", secs / 3600)
    } else {
        format!("{}d ago", secs / 86400)
    }
}

/// Parse a log timestamp string defensively.
///
/// Known writer bugs produce in-future or inconsistent timestamps — the
/// conductor agent invents times that don't match wall clock, and the
/// daemon mislabels local-time as UTC. Both show up as future-dated `Z`
/// strings. Returning `None` on those rows left the time column as `?`
/// for long stretches of the Dance Floor, which was as unreadable as the
/// "0s ago" bug it was trying to replace (see incident
/// 2026-04-23-hive-parse-log-ts-break). Until the writers are fixed,
/// clamp untrustworthy future timestamps to `now` so relative-time
/// display stays numeric for trustworthy rows and degrades to "0s ago"
/// (not `?`) for buggy ones. Root fix still lives in the writers.
pub fn parse_log_ts(ts_str: &str) -> Option<DateTime<Utc>> {
    let parsed = ts_str.parse::<DateTime<Utc>>().ok()?;
    let now = Utc::now();
    if parsed > now + chrono::Duration::minutes(5) {
        Some(now)
    } else {
        Some(parsed)
    }
}

// ── daemon PID ────────────────────────────────────────────────────────────────

pub fn read_daemon_pid() -> Option<u32> {
    fs::read_to_string(".loop/state/daemon.pid")
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

/// mtime of `.loop/state/daemon.pid` as a UTC timestamp. The daemon writes
/// this file once on start, so its mtime is effectively the process start
/// time — a cheap proxy that doesn't require shelling out to `ps`. Used to
/// display daemon uptime so a `loop stop && loop start` is visible in the
/// Hive Status panel before the new daemon's first heartbeat lands.
pub fn read_daemon_started_at() -> Option<DateTime<Utc>> {
    fs::metadata(".loop/state/daemon.pid")
        .ok()
        .and_then(|m| m.modified().ok())
        .map(DateTime::<Utc>::from)
}

pub fn pid_alive(pid: u32) -> bool {
    std::process::Command::new("kill")
        .args(["-0", &pid.to_string()])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

// ── log.jsonl parsing ─────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct RawLogLine {
    pub timestamp: Option<String>,
    pub action: Option<String>,
    pub ts: Option<String>,
    pub actor: Option<String>,
    pub event: Option<String>,
    pub brief: Option<String>,
    pub session_id: Option<String>,
    /// Set on `daemon:scout_*` events; carries the specialist name so the
    /// dance floor and Cells Scouts subsection can attribute observations
    /// per-scout without re-parsing the action string.
    pub specialist: Option<String>,
}

impl RawLogLine {
    pub fn ts_str(&self) -> Option<&str> {
        self.ts.as_deref().or(self.timestamp.as_deref())
    }

    /// Return the explicit `actor` field, or fall back to the prefix of
    /// `action` before the first colon (e.g. `"daemon:merge"` → `"daemon"`).
    /// The daemon writes log.jsonl entries with `action` but no `actor` —
    /// without this they'd render as mystery `?` rows on the dance floor.
    ///
    /// Scouts are a special case: `daemon:scout_fire|scout_noop|scout_failed`
    /// is daemon-emitted plumbing, but the event is about the *scout*, not the
    /// daemon's orchestration. Relabel to "scout" so the dance floor gives
    /// scout events their own actor color + keeps brief-cycle events visually
    /// separate from observation pings.
    pub fn derived_actor(&self) -> Option<String> {
        if let Some(a) = &self.actor {
            return Some(a.clone());
        }
        if let Some(action) = &self.action {
            if action.starts_with("daemon:scout_") {
                return Some("scout".to_string());
            }
            if let Some((prefix, _)) = action.split_once(':') {
                return Some(prefix.to_string());
            }
        }
        None
    }

    pub fn is_heartbeat(&self) -> bool {
        if let Some(event) = &self.event {
            if event.starts_with("heartbeat") {
                return true;
            }
        }
        if let Some(action) = &self.action {
            if action.contains("heartbeat") {
                return true;
            }
        }
        false
    }

    /// True for per-brief startup_repair backfill entries. These are emitted
    /// with `datetime.now()` timestamps on every daemon restart, one row per
    /// historical brief, which flooded the Dance Floor with "Xm ago" entries
    /// for old briefs whenever the daemon bounced. Filtered from the Dance
    /// Floor; still visible inside the `startup_repair_complete` summary line
    /// for audit. The summary itself is NOT filtered — it's one row per
    /// restart and signals "daemon just came up."
    pub fn is_startup_repair(&self) -> bool {
        self.action.as_deref() == Some("daemon:startup_repair")
    }
}

pub fn parse_heartbeat_timestamps(log_path: &Path) -> Vec<DateTime<Utc>> {
    let file = match fs::File::open(log_path) {
        Ok(f) => f,
        Err(_) => return vec![],
    };
    let reader = BufReader::new(file);
    let mut timestamps = Vec::new();
    for line in reader.lines() {
        let Ok(line) = line else { continue };
        let Ok(entry) = serde_json::from_str::<RawLogLine>(&line) else { continue };
        if !entry.is_heartbeat() {
            continue;
        }
        if let Some(ts) = entry.ts_str().and_then(parse_log_ts) {
            timestamps.push(ts);
        }
    }
    timestamps
}

/// Returns the timestamp of the most recent non-heartbeat event in log.jsonl.
/// This is the "daemon is busy" signal — if a non-heartbeat event happened
/// recently, the daemon is active regardless of the heartbeat cadence.
pub fn parse_last_event_ts(log_path: &Path) -> Option<DateTime<Utc>> {
    let file = fs::File::open(log_path).ok()?;
    let reader = BufReader::new(file);
    let mut last: Option<DateTime<Utc>> = None;
    for line in reader.lines() {
        let Ok(line) = line else { continue };
        let Ok(entry) = serde_json::from_str::<RawLogLine>(&line) else { continue };
        if entry.is_heartbeat() {
            continue;
        }
        if let Some(ts) = entry.ts_str().and_then(parse_log_ts) {
            last = Some(ts);
        }
    }
    last
}

/// Most recent parseable timestamp in `.loop/logs/daemon.log`. This file
/// carries worker / validator / conductor live activity lines (e.g.
/// "WORKER: iteration complete"). Used alongside `log.jsonl`'s event
/// timestamps to detect whether the daemon is actively working — the
/// conductor may be quiet in `log.jsonl` while workers churn in
/// `daemon.log`.
pub fn latest_daemon_log_ts(path: &Path) -> Option<DateTime<Utc>> {
    let content = fs::read_to_string(path).ok()?;
    for line in content.lines().rev() {
        if let Some((ts, _, _)) = parse_daemon_log_line(line) {
            return Some(ts);
        }
    }
    None
}

/// Progress data read from a brief's `progress.json`. The daemon writes this
/// file deterministically on every iteration — it is script-written, not
/// LLM-written, so its fields are schema-bound and safe to trust as integers.
/// Used for the Active cell instead of the log.jsonl-counting heuristic, which
/// could render LLM-hallucinated values like "2026 cycles" (incident 2026-04-23,
/// same class as the parse_log_ts fix).
pub struct BriefProgress {
    pub iteration: usize,
    pub total: usize,
    pub last_task: String,
    pub tasks_remaining: usize,
    #[allow(dead_code)]
    pub status: String,
}

#[derive(Deserialize)]
struct BriefProgressRaw {
    #[serde(default)]
    iteration: serde_json::Value,
    #[serde(default)]
    tasks_completed: Vec<serde_json::Value>,
    #[serde(default)]
    tasks_remaining: Vec<serde_json::Value>,
    #[serde(default)]
    status: String,
}

/// Read and validate `<worktree_root>/.loop/state/progress.json`. Returns None
/// on missing file, malformed JSON, or values that fail sanity bounds
/// (iteration or total > 100 — well past MAX_ITERATIONS, indicating a
/// hallucinated field). Never renders raw garbage: callers receive Some with
/// valid data or None for the fail-safe `cycle ?/?` path.
pub fn read_brief_progress(worktree_root: &Path) -> Option<BriefProgress> {
    let path = worktree_root.join(".loop/state/progress.json");
    let body = fs::read_to_string(&path).ok()?;
    let raw: BriefProgressRaw = serde_json::from_str(&body).ok()?;

    let iteration: usize = match &raw.iteration {
        serde_json::Value::Null => 0,
        v => match v.as_u64() {
            Some(n) if n <= 100 => n as usize,
            Some(n) => {
                eprintln!("hive warn: progress.json iteration={n} exceeds sanity bound (>100), using fail-safe");
                return None;
            }
            None => {
                eprintln!("hive warn: progress.json iteration is not an integer ({v:?}), using fail-safe");
                return None;
            }
        },
    };

    let tasks_remaining_count = raw.tasks_remaining.len();
    let total = raw.tasks_completed.len() + tasks_remaining_count;
    if total > 100 {
        eprintln!("hive warn: progress.json total tasks={total} exceeds sanity bound (>100), using fail-safe");
        return None;
    }

    let last_task_val = raw
        .tasks_completed
        .last()
        .or_else(|| raw.tasks_remaining.first());
    let last_task_raw = match last_task_val {
        Some(v) => v.as_str().unwrap_or("—").to_string(),
        None => "—".to_string(),
    };
    let last_task = if last_task_raw.chars().count() > 40 {
        let s: String = last_task_raw.chars().take(39).collect();
        format!("{s}…")
    } else {
        last_task_raw
    };

    Some(BriefProgress {
        iteration,
        total,
        last_task,
        tasks_remaining: tasks_remaining_count,
        status: raw.status,
    })
}

// ── HiveState ─────────────────────────────────────────────────────────────────

pub enum IntervalMode {
    Active,
    Idle,
    Unknown,
}

impl IntervalMode {
    pub fn label(&self) -> &'static str {
        match self {
            IntervalMode::Active => "active ~120s",
            IntervalMode::Idle => "idle ~900s",
            IntervalMode::Unknown => "unknown",
        }
    }

    /// Nominal heartbeat interval in seconds for this mode. The daemon's
    /// bash loop uses 120s when actively cycling and 900s when idle, so
    /// these match the `.label()` strings exactly. Unknown returns None —
    /// we can't meaningfully predict the next heartbeat.
    pub fn interval_secs(&self) -> Option<i64> {
        match self {
            IntervalMode::Active => Some(120),
            IntervalMode::Idle => Some(900),
            IntervalMode::Unknown => None,
        }
    }
}

pub struct HiveState {
    pub pid: Option<u32>,
    pub pid_alive: bool,
    pub heartbeat_number: usize,
    pub last_heartbeat_ts: Option<DateTime<Utc>>,
    pub interval_mode: IntervalMode,
    /// When the current daemon process started, approximated via the
    /// mtime of `.loop/state/daemon.pid`. None if the file is absent.
    pub daemon_started_at: Option<DateTime<Utc>>,
    /// Briefs waiting to re-dispatch once a precondition clears.
    /// Parsed from goals.md `**Blocked-on:**` markers. Empty = section hidden.
    pub requeued_briefs: Vec<ReQueuedBrief>,
}

impl HiveState {
    /// Short human label for time until the next heartbeat.
    ///
    /// Heartbeats only fire when the conductor has nothing else to do — so
    /// when the daemon is actively cycling (Active mode), a heartbeat in
    /// the past isn't "overdue," it's contended with real work. Emit
    /// "busy cycling" in that case so the coral alarm doesn't mislead.
    /// In Idle mode, a past-due heartbeat is genuinely "overdue" and
    /// colored accordingly.
    pub fn heartbeat_countdown(&self) -> Option<String> {
        let last = self.last_heartbeat_ts?;
        let interval = self.interval_mode.interval_secs()?;
        let next = last + chrono::Duration::seconds(interval);
        let delta = (next - Utc::now()).num_seconds();
        Some(if delta <= 0 {
            if matches!(self.interval_mode, IntervalMode::Active) {
                "busy cycling".to_string()
            } else {
                let overdue = -delta;
                if overdue < 60 {
                    format!("overdue {}s", overdue)
                } else {
                    format!("overdue {}m", overdue / 60)
                }
            }
        } else if delta < 60 {
            format!("next ~{}s", delta)
        } else {
            format!("next ~{}m", delta / 60)
        })
    }
}

impl HiveState {
    pub fn load() -> Self {
        let log_path = Path::new(".loop/state/log.jsonl");
        let heartbeats = parse_heartbeat_timestamps(log_path);
        let heartbeat_number = heartbeats.len();
        let last_heartbeat_ts = heartbeats.last().copied();

        let last_event_ts = parse_last_event_ts(log_path);
        // Workers and validators write to daemon.log, not log.jsonl — so a
        // conductor that's been quiet in log.jsonl for 10 minutes can still
        // be actively orchestrating cycles (loud in daemon.log). Take the
        // max of both as the "daemon is doing something" signal.
        let daemon_log_path = Path::new(".loop/logs/daemon.log");
        let last_activity_ts = match (last_event_ts, latest_daemon_log_ts(daemon_log_path)) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        };
        let heartbeat_gap = if heartbeats.len() >= 2 {
            let last = heartbeats[heartbeats.len() - 1];
            let prev = heartbeats[heartbeats.len() - 2];
            Some((last - prev).num_seconds().abs())
        } else {
            None
        };
        let now = Utc::now();

        // Key insight: heartbeats only fire when the conductor has nothing to do.
        // Absence of recent heartbeats means the daemon is BUSY, not idle.
        // So: any non-heartbeat activity (log.jsonl OR daemon.log) in the
        // last 5 min → Active, regardless of heartbeat gap. Only fall back
        // to heartbeat-gap inference when the daemon has been quiet
        // everywhere for a while.
        let interval_mode = match (last_activity_ts, heartbeat_gap) {
            (Some(ts), _) if (now - ts).num_seconds() <= 300 => IntervalMode::Active,
            (_, Some(gap)) if gap <= 300 => IntervalMode::Active,
            (_, Some(_)) => IntervalMode::Idle,
            _ => IntervalMode::Unknown,
        };

        let pid = read_daemon_pid();
        let pid_alive = pid.map(self::pid_alive).unwrap_or(false);

        let daemon_started_at = read_daemon_started_at();

        // Re-queued briefs — scan cards for merged set, then parse goals.md.
        let cards_dir = Path::new("wiki/briefs/cards");
        let merged_briefs: std::collections::HashSet<String> = {
            let mut set = HashSet::new();
            if let Ok(entries) = fs::read_dir(cards_dir) {
                for entry in entries.flatten() {
                    let card_dir = entry.path();
                    if !card_dir.is_dir() { continue; }
                    let Some(brief_id) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else { continue; };
                    if brief_id.starts_with('.') { continue; }
                    let index_path = card_dir.join("index.md");
                    if parse_brief_status(&index_path).as_deref() == Some("merged") {
                        set.insert(brief_id);
                    }
                }
            }
            set
        };
        let goals_path = Path::new(".loop/state/goals.md");
        let requeued_briefs = parse_requeued_goals_md(goals_path, &merged_briefs);

        HiveState {
            pid,
            pid_alive,
            heartbeat_number,
            last_heartbeat_ts,
            interval_mode,
            daemon_started_at,
            requeued_briefs,
        }
    }
}

// ── running.json ──────────────────────────────────────────────────────────────

#[derive(Deserialize, Default)]
pub struct RunningJson {
    #[serde(default, deserialize_with = "deserialize_lossy_vec")]
    pub active: Vec<ActiveBriefRaw>,
    #[serde(default, deserialize_with = "deserialize_lossy_vec")]
    pub completed_pending_eval: Vec<PendingEvalRaw>,
    #[serde(default, deserialize_with = "deserialize_lossy_vec")]
    pub awaiting_review: Vec<PendingEvalRaw>,
    #[serde(default, deserialize_with = "deserialize_lossy_vec")]
    #[allow(dead_code)]
    pub history: Vec<HistoryEntryRaw>,
}

#[derive(Deserialize)]
pub struct ActiveBriefRaw {
    pub brief: String,
    pub branch: String,
    pub dispatched_at: Option<String>,
}

#[derive(Deserialize)]
pub struct PendingEvalRaw {
    pub brief: String,
    #[allow(dead_code)]
    pub branch: Option<String>,
    pub completed_at: Option<String>,
}

#[derive(Deserialize)]
pub struct HistoryEntryRaw {
    #[allow(dead_code)]
    pub brief: String,
    #[serde(default)]
    #[allow(dead_code)]
    pub merged_at: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    pub merge_sha: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    pub approved_at: Option<String>,
}

/// A brief that has been rejected + re-queued behind a blocking precondition.
///
/// Parsed from `## Queued next` entries in goals.md that carry a
/// `**Blocked-on:** brief-NNN` line. Parser is deterministic regex — no
/// inference at runtime.
#[derive(Debug, Clone)]
pub struct ReQueuedBrief {
    pub brief_id: String,
    pub blocked_on: String,
    #[allow(dead_code)]
    pub description: String,
    /// True when the blocking brief appears in running.json history[] with a
    /// merge_sha (precondition cleared; ready to re-dispatch).
    pub ready_to_dispatch: bool,
}

pub struct ActiveBrief {
    pub brief: String,
    pub branch: String,
    pub dispatched_at: Option<DateTime<Utc>>,
    /// Progress read from `progress.json` in the brief's worktree. None when
    /// the file is missing, malformed, or fails sanity checks.
    pub brief_progress: Option<BriefProgress>,
    /// Max cycle number N from `.loop/modules/validator/state/reviews/{brief}-cycle-N.md`.
    /// None means no validator review has landed yet for this brief.
    pub latest_validator_cycle: Option<usize>,
    /// Cycle budget declared in the brief's `## Budget` section. None if the
    /// brief file is missing or the section doesn't have a parseable integer.
    pub cycle_budget: Option<usize>,
    pub worktree_path: Option<String>,
}

/// Extract the cycle budget from a brief's markdown body.
///
/// Reads the `## Budget` section and returns the **max** integer found in
/// it. The first-integer heuristic read brief-011's "8 cycles. … cycles
/// 8-10 are polish + baseline + closeout." as cap=8, when the author
/// clearly meant cap=10. Max-integer matches author intent for that brief
/// and is unchanged for every other brief in the corpus (all phrase the
/// cap as the largest number in the section).
///
/// Scope is bounded by the next `## ` header or end-of-file. Budget
/// sections are short (1–2 sentences about cycle counts), so the
/// probability of picking up an unrelated big integer is low for now.
pub fn parse_cycle_budget(brief_path: &Path) -> Option<usize> {
    let content = fs::read_to_string(brief_path).ok()?;
    let mut lines = content.lines();
    for line in lines.by_ref() {
        if line.trim_start().starts_with("## Budget") {
            break;
        }
    }
    let mut max_seen: Option<usize> = None;
    let absorb_digits = |s: &str, out: &mut Option<usize>| {
        let mut digits = String::new();
        for c in s.chars() {
            if c.is_ascii_digit() {
                digits.push(c);
            } else if !digits.is_empty() {
                if let Ok(n) = digits.parse::<usize>() {
                    *out = Some(out.map_or(n, |m| m.max(n)));
                }
                digits.clear();
            }
        }
        if !digits.is_empty() {
            if let Ok(n) = digits.parse::<usize>() {
                *out = Some(out.map_or(n, |m| m.max(n)));
            }
        }
    };
    for line in lines {
        let trimmed = line.trim();
        // Stop at the next section header — keeps integers from adjacent
        // sections (Anti-patterns, Artifact) out of the running max.
        if trimmed.starts_with("## ") {
            break;
        }
        absorb_digits(trimmed, &mut max_seen);
    }
    max_seen
}

/// Scan a single directory for `{brief}-cycle-N.md` files; return the
/// highest N found. Returns None if the dir is missing or empty.
fn scan_reviews_dir(reviews_dir: &Path, brief_id: &str) -> Option<usize> {
    let entries = fs::read_dir(reviews_dir).ok()?;
    let prefix = format!("{}-cycle-", brief_id);
    let mut max_n: Option<usize> = None;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name_str = name.to_str().unwrap_or("");
        if !name_str.starts_with(&prefix) || !name_str.ends_with(".md") {
            continue;
        }
        let n_str = &name_str[prefix.len()..name_str.len() - 3];
        if let Ok(n) = n_str.parse::<usize>() {
            max_n = Some(max_n.map_or(n, |m| m.max(n)));
        }
    }
    max_n
}

/// Resolve cycle progress + budget for a pending brief (escalated,
/// awaiting-merge, awaiting-eval, etc). Used to render a progress bar on
/// Pending rows so "halted at 3/8" is distinguishable from "completed 8/8".
///
/// Branch name conventionally matches brief id in simple-loop; if that's
/// not the case the worktree lookup will miss and the main reviews dir
/// (post-merge state) takes over. Both misses → None, rendered bar-less.
fn pending_cycle_and_budget(
    brief_id: &str,
    main_reviews_dir: &Path,
    cards_dir: &Path,
) -> (Option<usize>, Option<usize>) {
    let worktree = {
        let p = format!(".loop/worktrees/{}", brief_id);
        if Path::new(&p).exists() {
            Some(p)
        } else {
            None
        }
    };
    let cycle = latest_validator_cycle(
        main_reviews_dir,
        worktree.as_deref().map(Path::new),
        brief_id,
    );
    let budget = parse_cycle_budget(&cards_dir.join(brief_id).join("index.md"));
    (cycle, budget)
}

/// Find the highest cycle number in any `.loop/modules/validator/state/reviews/`
/// directory that might carry reviews for `brief_id`. For in-progress briefs
/// the validator writes into the WORKTREE's reviews dir (on the brief's
/// branch); main only sees those files after merge. Checks the worktree
/// first, then main, returning the max across both.
///
/// Returns None if no reviews exist in either location — e.g. brief just
/// dispatched, validator hasn't run its first cycle yet.
pub fn latest_validator_cycle(
    main_reviews_dir: &Path,
    worktree_path: Option<&Path>,
    brief_id: &str,
) -> Option<usize> {
    let mut best = scan_reviews_dir(main_reviews_dir, brief_id);
    if let Some(wt) = worktree_path {
        let wt_reviews = wt.join(".loop/modules/validator/state/reviews");
        let wt_max = scan_reviews_dir(&wt_reviews, brief_id);
        best = match (best, wt_max) {
            (Some(a), Some(b)) => Some(a.max(b)),
            (a, b) => a.or(b),
        };
    }
    best
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PendingReason {
    Escalate,
    PendingMerge,
    PendingDispatch,
    AwaitingEval,
    AwaitingReview,
    Unknown,
}

impl PendingReason {
    pub fn label(&self) -> &'static str {
        match self {
            PendingReason::Escalate => "escalate",
            PendingReason::PendingMerge => "pending-merge",
            PendingReason::PendingDispatch => "pending-dispatch",
            PendingReason::AwaitingEval => "awaiting eval",
            PendingReason::AwaitingReview => "awaiting review",
            PendingReason::Unknown => "pending",
        }
    }

    /// True if the human needs to act. False when the daemon is doing work
    /// on its own and will advance state without Mattie's input. Used to
    /// partition the Pending panel into "Decide" vs "In flight" subsections
    /// so glance-level triage tells her if she's the bottleneck.
    ///
    /// - `Escalate` → Decide (daemon explicitly asked)
    /// - `PendingMerge` → In flight (approval already given, daemon queued)
    /// - `PendingDispatch` → In flight (conductor queued, daemon will pick up)
    /// - `AwaitingEval` → In flight (conductor hasn't evaluated yet)
    /// - `AwaitingReview` → Decide (worker completed, Mattie approves/rejects)
    pub fn needs_human(&self) -> bool {
        matches!(
            self,
            PendingReason::Escalate | PendingReason::AwaitingReview | PendingReason::Unknown
        )
    }
}

pub struct PendingBrief {
    pub brief: String,
    pub reason: PendingReason,
    pub age: Option<DateTime<Utc>>,
    /// Latest validator cycle N found for this brief — None when brief-less
    /// (e.g. decision escalates) or pre-dispatch.
    pub latest_validator_cycle: Option<usize>,
    /// Parsed `## Budget` integer from the brief file. None when the brief
    /// file can't be found (brief-less signals, deleted briefs).
    pub cycle_budget: Option<usize>,
    /// Time estimate for the recommended resolution option, pulled from
    /// the signal payload (e.g. "~60s"). None for signals without options
    /// or without estimates on the recommended option.
    pub estimated_time: Option<String>,
}

/// Whether a queued brief is dispatchable, derived from its `Depends-on:` field.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum QueuedReadiness {
    /// All deps are merged (or no deps).
    Ready,
    /// One or more deps are not yet merged.
    Blocked { first_unmet: String, more: usize },
    /// Direct dependency cycle detected (A depends on B, B depends on A).
    CycleDetected,
}

pub struct QueuedBrief {
    pub brief: String,
    /// Position in `.loop/state/goals.md` `## Queued next` (0 = top).
    /// `None` for cards not named in goals.md — these sort after ranked
    /// briefs in `brief_sort_key` order (i.e. current numeric behavior).
    pub priority_rank: Option<usize>,
    /// Env var names listed in `**Depends-on-secrets:**` frontmatter, if any.
    /// Non-empty means this brief is credential-gated and won't dispatch until
    /// all listed vars are set in the daemon's env. Rendered with a key marker.
    pub depends_on_secrets: Vec<String>,
    /// Derived from `Depends-on:` frontmatter — whether all deps are merged.
    pub readiness: QueuedReadiness,
}

pub struct DraftBrief {
    pub brief: String,
    /// True if the card has an `index.md`. False means only scratch files
    /// (e.g. `feedback.md`) exist — the number is reserved but no brief has
    /// been authored yet.
    pub has_index: bool,
}

pub struct RecentlyFinishedBrief {
    pub brief: String,
    /// When the brief landed on main. Prefers `merged_at`; falls back to
    /// `approved_at` for older entries that predate the merge-event record.
    pub finished_at: Option<DateTime<Utc>>,
}

/// A brief that was considered and explicitly declined (`Status: not-doing`).
/// Rendered in the Recent section below merged items with a ✗ glyph.
pub struct NotDoingBrief {
    pub brief: String,
    /// The `Not-doing-reason:` frontmatter field, if present.
    pub reason: Option<String>,
    /// When the decision was recorded; falls back to brief file mtime.
    pub declared_at: Option<DateTime<Utc>>,
}

/// Cap on how many finished briefs to surface. Enough to cover a meaningful
/// trailing window without bloating Cells.
pub const RECENTLY_FINISHED_LIMIT: usize = 5;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ScoutEventKind {
    Fire,
    Noop,
    Failed,
}

impl ScoutEventKind {
    fn from_action(action: &str) -> Option<Self> {
        match action {
            "daemon:scout_fire" => Some(ScoutEventKind::Fire),
            "daemon:scout_noop" => Some(ScoutEventKind::Noop),
            "daemon:scout_failed" => Some(ScoutEventKind::Failed),
            _ => None,
        }
    }

    pub fn label(&self) -> &'static str {
        match self {
            ScoutEventKind::Fire => "fired",
            ScoutEventKind::Noop => "noop",
            ScoutEventKind::Failed => "failed",
        }
    }
}

/// One enabled-or-declared scout specialist. Surfaced in the Cells Scouts
/// subsection so Mattie can see cadence + health at a glance.
pub struct Scout {
    pub name: String,
    /// Most recent `daemon:scout_*` event for this specialist, or None if
    /// the scout hasn't fired yet (file present but dormant — typically
    /// because `SCOUTS_ENABLED` doesn't include this name).
    pub last_event_at: Option<DateTime<Utc>>,
    pub last_event_kind: Option<ScoutEventKind>,
    pub fires_today: usize,
    pub noops_today: usize,
    pub failures_today: usize,
}

pub struct CellsState {
    pub active: Vec<ActiveBrief>,
    pub pending: Vec<PendingBrief>,
    pub queued: Vec<QueuedBrief>,
    pub drafts: Vec<DraftBrief>,
    pub recently_finished: Vec<RecentlyFinishedBrief>,
    pub not_doing: Vec<NotDoingBrief>,
    pub scouts: Vec<Scout>,
}

/// Sort key for any work-unit id: `brief-NNN-slug`, `audit-YYYY-MM-DD-N`,
/// `capture-YYYY-MM-DD-N`, etc. Primary sort is on the type prefix (so audits
/// cluster with audits, briefs with briefs); secondary is the leading integer
/// after the prefix (numeric briefs) with lex fallback for date-stamped
/// types that sort chronologically anyway.
fn brief_sort_key(id: &str) -> (String, u32, String) {
    let (prefix, rest) = match id.split_once('-') {
        Some((p, r)) => (p.to_string(), r),
        None => (String::new(), id),
    };
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    let n = digits.parse::<u32>().unwrap_or(u32::MAX);
    (prefix, n, id.to_string())
}

/// Parse the priority order from `.loop/state/goals.md`'s `## Queued next`
/// section. Returns work-unit ids in the order they appear.
///
/// Format is forgiving on purpose — Mattie writes goals.md freehand. We
/// only treat lines that START with a list marker as priority entries —
/// `1. `, `2. `, ..., `- `, or `* ` at the start (after indent). Continuation
/// prose lines and nested bullets are ignored. After the marker, the item
/// must lead with a work-unit id (optionally wrapped in `**...**` for
/// emphasis); items that lead with a prose label like `**Runway**` or
/// `**Future: …**` don't name a concrete priority and get skipped.
///
/// Graceful degradation:
/// - missing file → empty Vec
/// - missing heading → empty Vec
/// - malformed list (no ids) → empty Vec
///
/// Scanning stops at the next `## ` heading (any h2), not at blank lines —
/// Mattie's lists sometimes have gaps.
pub fn parse_goals_priority(goals_path: &Path) -> Vec<String> {
    let Ok(contents) = fs::read_to_string(goals_path) else {
        return vec![];
    };

    let mut in_section = false;
    let mut out: Vec<String> = Vec::new();
    for line in contents.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("## ") {
            if in_section {
                break; // next h2 ends the section
            }
            let header = trimmed.trim_start_matches("## ").trim();
            if header.eq_ignore_ascii_case("Queued next") {
                in_section = true;
            }
            continue;
        }
        if !in_section {
            continue;
        }
        // Only top-level list items count. Nested items (indented more than
        // ~3 spaces) are continuations and shouldn't introduce new priorities.
        let leading_ws = line.len() - trimmed.len();
        if leading_ws > 3 {
            continue;
        }
        if !is_list_marker(trimmed) {
            continue;
        }
        // After the list marker, require the item's leading token to be a
        // work-unit id (usually inside `**...**`). Items that lead with
        // prose labels like `**Runway**` or `**Future: …**` don't name a
        // concrete priority — skip them even though they share the list
        // marker with real priorities.
        let after_marker = strip_list_marker(trimmed);
        if let Some(id) = leading_work_unit_id(after_marker) {
            out.push(id);
        }
    }
    out
}

/// Parse goals.md for re-queued briefs with a `**Blocked-on:** brief-NNN` marker.
///
/// Returns a vec of [`ReQueuedBrief`] — one per goals.md numbered-list entry
/// that contains a `**Blocked-on:**` continuation line. Entries without the
/// marker are ignored (omit-when-empty contract: callers suppress the section).
///
/// Mirrors `parse_requeued_briefs()` in `lib/actions.py` — deterministic
/// regex, no inference at runtime.
///
/// `merged_briefs` is the set of brief IDs that appear in running.json
/// `history[]` with a non-empty `merge_sha` — used to set `ready_to_dispatch`.
pub fn parse_requeued_goals_md(goals_path: &Path, merged_briefs: &std::collections::HashSet<String>) -> Vec<ReQueuedBrief> {
    let Ok(contents) = fs::read_to_string(goals_path) else {
        return vec![];
    };

    // Numbered top-level list item whose first token is a brief-NNN id.
    // Mirrors Python: r"^\s{0,3}\d+\.\s+\*{0,2}(brief-\d+[\w-]*)"
    let is_brief_list_item = |line: &str| -> Option<(String, String)> {
        let trimmed = line.trim_start();
        let leading_ws = line.len() - trimmed.len();
        if leading_ws > 3 {
            return None;
        }
        // Must start with a digit + period list marker.
        let rest = {
            let mut chars = trimmed.chars();
            let mut saw_digit = false;
            let mut saw_dot = false;
            let mut idx = 0;
            for ch in chars.by_ref() {
                if ch.is_ascii_digit() {
                    saw_digit = true;
                    idx += ch.len_utf8();
                } else if ch == '.' && saw_digit {
                    saw_dot = true;
                    idx += ch.len_utf8();
                    break;
                } else {
                    break;
                }
            }
            if !saw_dot {
                return None;
            }
            &trimmed[idx..]
        };
        // Strip leading whitespace + optional ** emphasis wrapping.
        let rest = rest.trim_start();
        let rest = rest.trim_start_matches('*');
        // Must start with "brief-NNN".
        if !rest.starts_with("brief-") {
            return None;
        }
        // Extract the brief id — stop at whitespace or non-id chars.
        let id_end = rest.find(|c: char| !c.is_alphanumeric() && c != '-').unwrap_or(rest.len());
        let brief_id = &rest[..id_end];
        if brief_id.len() <= "brief-".len() {
            return None;
        }
        // Build a one-line description by stripping numbering + emphasis.
        let desc = {
            let s = trimmed.trim_start_matches(|c: char| c.is_ascii_digit() || c == '.' || c == ' ');
            let s = s.trim_start_matches('*').trim();
            if s.chars().count() > 80 {
                let truncated: String = s.chars().take(77).collect();
                format!("{}…", truncated)
            } else {
                s.to_string()
            }
        };
        Some((brief_id.to_string(), desc))
    };

    // `**Blocked-on:** brief-NNN(-slug)?` on a continuation line (any indent).
    // Mirrors Python: r"^\s*\*\*Blocked-on:\*\*\s+(brief-\d+(?:-[\w-]+)?)"
    let extract_blocked_on = |line: &str| -> Option<String> {
        let trimmed = line.trim();
        let marker = "**Blocked-on:**";
        let rest = trimmed.strip_prefix(marker)?.trim_start();
        if !rest.starts_with("brief-") {
            return None;
        }
        let id_end = rest.find(|c: char| !c.is_alphanumeric() && c != '-').unwrap_or(rest.len());
        let id = &rest[..id_end];
        if id.len() <= "brief-".len() {
            return None;
        }
        Some(id.to_string())
    };

    let mut results: Vec<ReQueuedBrief> = Vec::new();
    let mut current_entry: Option<(String, String)> = None; // (brief_id, desc)

    for line in contents.lines() {
        if let Some(entry) = is_brief_list_item(line) {
            current_entry = Some(entry);
            continue;
        }
        if let Some(blocked_on) = extract_blocked_on(line) {
            if let Some((brief_id, desc)) = current_entry.take() {
                let ready = merged_briefs.iter().any(|m| brief_id_matches(m, &blocked_on));
                results.push(ReQueuedBrief { brief_id, blocked_on, description: desc, ready_to_dispatch: ready });
            }
        }
    }

    results
}

/// Pull the first work-unit id token off a line.
///
/// Matches `brief-\d+(-[a-z0-9-]+)?`, `audit-\d{4}-\d{2}-\d{2}-\d+`, and
/// `capture-\d{4}-\d{2}-\d{2}-\d+`. Stops at the first character outside
/// the id alphabet (whitespace, `*`, `(`, etc.) so markdown emphasis and
/// paren-style suffixes don't leak into the returned id.
fn extract_work_unit_id(line: &str) -> Option<String> {
    let bytes = line.as_bytes();
    let prefixes: &[&str] = &["brief-", "audit-", "capture-"];
    let mut i = 0;
    while i < bytes.len() {
        for p in prefixes {
            if bytes[i..].starts_with(p.as_bytes()) {
                let start = i;
                let mut j = i + p.len();
                // Require at least one digit right after the prefix — rules
                // out `brief-` on its own, or `brief-foo` which isn't an id.
                if j >= bytes.len() || !bytes[j].is_ascii_digit() {
                    i += 1;
                    continue;
                }
                while j < bytes.len() {
                    let c = bytes[j];
                    let is_id_char = c.is_ascii_digit()
                        || c.is_ascii_lowercase()
                        || c == b'-';
                    if !is_id_char {
                        break;
                    }
                    j += 1;
                }
                // Don't return a trailing hyphen (e.g. `brief-020-`).
                let end = if j > start && bytes[j - 1] == b'-' { j - 1 } else { j };
                return Some(line[start..end].to_string());
            }
        }
        i += 1;
    }
    None
}

/// Strip the leading list marker (`1. `, `- `, `* `) so we're left with the
/// content of the item itself.
fn strip_list_marker(trimmed: &str) -> &str {
    let bytes = trimmed.as_bytes();
    if (bytes.first() == Some(&b'-') || bytes.first() == Some(&b'*'))
        && bytes.get(1) == Some(&b' ')
    {
        return &trimmed[2..];
    }
    let digit_run = bytes.iter().take_while(|c| c.is_ascii_digit()).count();
    if digit_run > 0
        && bytes.get(digit_run) == Some(&b'.')
        && bytes.get(digit_run + 1) == Some(&b' ')
    {
        return &trimmed[digit_run + 2..];
    }
    trimmed
}

/// Extract a work-unit id iff it's the first substantive token in the
/// line — optionally wrapped in `**...**` for emphasis. Anything else
/// (prose labels like `**Runway**`, `**Future: …**`, or text with a brief
/// mentioned later) returns `None`.
fn leading_work_unit_id(s: &str) -> Option<String> {
    let s = s.trim_start();
    let s = s.strip_prefix("**").unwrap_or(s);
    let s = s.strip_prefix("__").unwrap_or(s);
    let s = s.trim_start();
    let prefixes: &[&str] = &["brief-", "audit-", "capture-"];
    let matched = prefixes.iter().any(|p| s.starts_with(p));
    if !matched {
        return None;
    }
    extract_work_unit_id(s)
}

/// True if the line starts with a markdown list marker: `- `, `* `, or a
/// numbered marker like `1. ` / `12. `. Used by `parse_goals_priority` so
/// prose continuation lines don't get mistaken for new priority entries.
fn is_list_marker(trimmed: &str) -> bool {
    let bytes = trimmed.as_bytes();
    if bytes.is_empty() {
        return false;
    }
    if (bytes[0] == b'-' || bytes[0] == b'*') && bytes.get(1) == Some(&b' ') {
        return true;
    }
    let digit_run = bytes.iter().take_while(|c| c.is_ascii_digit()).count();
    if digit_run > 0
        && bytes.get(digit_run) == Some(&b'.')
        && bytes.get(digit_run + 1) == Some(&b' ')
    {
        return true;
    }
    false
}

/// True if a priority id parsed from goals.md matches a brief filename.
///
/// Goals.md often uses the short form `brief-017`; the file on disk is
/// `brief-017-pi0-real-integration.md`. Exact match wins; otherwise the
/// short-form matches iff the full id has the short as a `-`-bounded
/// prefix. Prevents `brief-01` from matching `brief-017-…`.
fn priority_matches(priority: &str, brief_id: &str) -> bool {
    if priority == brief_id {
        return true;
    }
    if let Some(rest) = brief_id.strip_prefix(priority) {
        return rest.starts_with('-');
    }
    false
}

/// Parse `**Depends-on-secrets:**` from a brief markdown file.
///
/// Returns a list of env var names. Same comma-split logic as the Python
/// `parse_depends_on_value` — strips whitespace, ignores empty tokens.
/// Returns empty Vec when the field is absent or the file can't be read.
pub fn parse_depends_on_secrets(brief_path: &Path) -> Vec<String> {
    let Ok(content) = fs::read_to_string(brief_path) else {
        return vec![];
    };
    for line in content.lines() {
        let lower = line.to_ascii_lowercase();
        if lower.contains("**depends-on-secrets:**") {
            // Extract the value after the marker
            if let Some(pos) = line.to_ascii_lowercase().find("**depends-on-secrets:**") {
                let after = &line[pos + "**depends-on-secrets:**".len()..];
                return after
                    .split(',')
                    .map(|s| s.trim().trim_matches(|c: char| ".,;".contains(c)).to_string())
                    .filter(|s| !s.is_empty())
                    .collect();
            }
        }
    }
    vec![]
}

/// Check whether a string looks like a brief ID (`brief-NNN` or `brief-NNN-slug`).
fn is_brief_id(s: &str) -> bool {
    let Some(rest) = s.strip_prefix("brief-") else { return false; };
    let digit_end = rest.find(|c: char| !c.is_ascii_digit()).unwrap_or(rest.len());
    if digit_end == 0 { return false; }
    let after = &rest[digit_end..];
    after.is_empty() || after.starts_with('-')
}

/// Extract all `brief-NNN-*` substrings from freeform prose (legacy cards).
fn extract_brief_ids_from_prose(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut remaining = s;
    while let Some(pos) = remaining.find("brief-") {
        let from_brief = &remaining[pos..];
        let end = from_brief
            .find(|c: char| c.is_whitespace() || ",;()[]".contains(c))
            .unwrap_or(from_brief.len());
        let candidate = from_brief[..end].trim_matches(|c: char| ".,;".contains(c));
        if is_brief_id(candidate) {
            out.push(candidate.to_string());
        }
        remaining = &remaining[pos + 6..];
    }
    out
}

/// Split a raw `Depends-on` value into a list of brief IDs.
///
/// Handles comma-separated IDs and freeform prose (extracts `brief-NNN-*`).
/// Returns `[]` for `_none_` or when no valid tokens survive.
fn split_depends_on_value(raw: &str) -> Vec<String> {
    let trimmed = raw.trim();
    // Sentinel detection ignores a trailing parenthetical rationale:
    // `_none_ (concurrent with Phase 1-3)` still means "no deps".
    let sentinel_head = trimmed.split('(').next().unwrap_or("").trim();
    if sentinel_head == "_none_" || sentinel_head.eq_ignore_ascii_case("none") {
        return vec![];
    }
    let mut out: Vec<String> = Vec::new();
    for tok in trimmed.split(',') {
        let cleaned = tok.trim().trim_matches(|c: char| ".,;".contains(c));
        if cleaned.is_empty() { continue; }
        // Strip trailing parenthetical annotation: "brief-078 (hard)" → "brief-078"
        let stripped = cleaned.split('(').next().unwrap_or("").trim().trim_matches(|c: char| ".,;".contains(c));
        if is_brief_id(stripped) {
            out.push(stripped.to_string());
        } else {
            let extracted = extract_brief_ids_from_prose(stripped);
            if extracted.is_empty() {
                eprintln!("parse_depends_on: dropping non-brief-id token: {cleaned:?}");
            } else {
                out.extend(extracted);
            }
        }
    }
    out
}

/// Parse `Depends-on:` from a brief markdown file.
///
/// Handles YAML frontmatter (`Depends-on: brief-X`) and bold-markdown legacy
/// form (`**Depends-on:** brief-X`). Comma-separated IDs are supported; freeform
/// prose falls back to extracting `brief-NNN-*` substrings. Returns empty Vec
/// when absent, unreadable, or value is `_none_`.
pub fn parse_depends_on(brief_path: &Path) -> Vec<String> {
    let Ok(content) = fs::read_to_string(brief_path) else {
        return vec![];
    };
    let lines: Vec<&str> = content.lines().collect();

    // YAML frontmatter: between opening and closing `---`
    if lines.first().map(|l| l.trim()) == Some("---") {
        for line in lines.iter().skip(1) {
            if line.trim() == "---" { break; }
            let lower = line.to_ascii_lowercase();
            if lower.starts_with("depends-on:") {
                let after = &line["depends-on:".len()..];
                return split_depends_on_value(after.trim());
            }
        }
    }

    // Bold markdown fallback: `**Depends-on:** value`
    for line in &lines {
        let lower = line.to_ascii_lowercase();
        if lower.contains("**depends-on:**") {
            if let Some(pos) = lower.find("**depends-on:**") {
                let after = &line[pos + "**depends-on:**".len()..];
                return split_depends_on_value(after.trim());
            }
        }
    }

    vec![]
}

/// Build a map of `brief_id → Status` for every card in `cards_dir`.
fn build_card_status_map(cards_dir: &Path) -> std::collections::HashMap<String, String> {
    let Ok(entries) = fs::read_dir(cards_dir) else {
        return std::collections::HashMap::new();
    };
    let mut map = std::collections::HashMap::new();
    for entry in entries.flatten() {
        let card_dir = entry.path();
        if !card_dir.is_dir() { continue; }
        let Some(brief_id) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else {
            continue;
        };
        if brief_id.starts_with('.') { continue; }
        let index_path = card_dir.join("index.md");
        if let Some(status) = parse_brief_status(&index_path) {
            map.insert(brief_id, status);
        }
    }
    map
}

/// Compute whether a queued brief is ready to dispatch or blocked.
///
/// Deps with status `merged` are met. Missing cards render as `[card not found]`.
/// Direct cycles (A→B and B→A) render as `CycleDetected`.
fn compute_readiness(
    cards_dir: &Path,
    deps: &[String],
    current_id: &str,
    status_map: &std::collections::HashMap<String, String>,
) -> QueuedReadiness {
    if deps.is_empty() {
        return QueuedReadiness::Ready;
    }
    let mut unmet: Vec<String> = Vec::new();
    for dep in deps {
        let card_entry = status_map.iter().find(|(k, _)| brief_id_matches(k, dep));
        match card_entry {
            None => {
                eprintln!("hive: Depends-on dep {dep:?} has no card — rendering as blocked");
                unmet.push(format!("{dep} [card not found]"));
            }
            Some((card_id, status)) => {
                if status == "merged" {
                    // dep is met
                } else {
                    // Check for direct cycle: does dep also depend on current_id?
                    let dep_path = cards_dir.join(card_id).join("index.md");
                    let dep_deps = parse_depends_on(&dep_path);
                    if dep_deps.iter().any(|d| brief_id_matches(d, current_id)) {
                        eprintln!("hive: dependency cycle between {current_id} and {card_id}");
                        return QueuedReadiness::CycleDetected;
                    }
                    unmet.push(dep.clone());
                }
            }
        }
    }
    if unmet.is_empty() {
        QueuedReadiness::Ready
    } else {
        QueuedReadiness::Blocked {
            first_unmet: unmet[0].clone(),
            more: unmet.len() - 1,
        }
    }
}

/// Normalize a Status field value to lowercase-hyphenated form for comparison.
/// Accepts variations like `not-doing`, `Not-Doing`, `not_doing`.
fn normalize_status(s: &str) -> String {
    s.trim().to_ascii_lowercase().replace('_', "-")
}

/// Parse `Status:` from a brief card file.
///
/// Handles two formats:
///   YAML frontmatter — `Status: queued` between `---` delimiters (new cards,
///     written by `_set_card_status.py`).
///   Bold markdown — `**Status:** queued` anywhere in the body (legacy cards).
///
/// YAML frontmatter is checked first; bold markdown is the fallback so old
/// cards continue to work without migration.
fn parse_brief_status(brief_path: &Path) -> Option<String> {
    let content = fs::read_to_string(brief_path).ok()?;
    let lines: Vec<&str> = content.lines().collect();

    // YAML frontmatter: between opening and closing `---`
    if lines.first().map(|l| l.trim()) == Some("---") {
        for line in lines.iter().skip(1) {
            if line.trim() == "---" {
                break;
            }
            let lower = line.to_ascii_lowercase();
            if lower.starts_with("status:") {
                let value = line["status:".len()..].trim().trim_matches(|c: char| ".,;".contains(c));
                if !value.is_empty() {
                    return Some(normalize_status(value));
                }
            }
        }
    }

    // Bold markdown fallback: `**Status:** value`
    for line in &lines {
        let lower = line.to_ascii_lowercase();
        if lower.contains("**status:**") {
            if let Some(pos) = lower.find("**status:**") {
                let after = &line[pos + "**status:**".len()..];
                let value = after.trim().trim_matches(|c: char| ".,;".contains(c));
                if !value.is_empty() {
                    return Some(normalize_status(value));
                }
            }
        }
    }

    None
}

/// Parse `**Not-doing-reason:**` from a brief markdown file. Returns the
/// trimmed value, or None if absent or unreadable.
fn parse_not_doing_reason(brief_path: &Path) -> Option<String> {
    let content = fs::read_to_string(brief_path).ok()?;
    for line in content.lines() {
        let lower = line.to_ascii_lowercase();
        if lower.contains("**not-doing-reason:**") {
            let pos = lower.find("**not-doing-reason:**")?;
            let after = &line[pos + "**not-doing-reason:**".len()..];
            let value = after.trim().trim_matches(|c: char| ".,;".contains(c));
            if !value.is_empty() {
                return Some(value.to_string());
            }
        }
    }
    None
}

/// Scan `wiki/briefs/cards/*/index.md` and return entries whose `Status:` is
/// `not-doing`. `declared_at` falls back to file mtime.
/// Results are sorted newest-first; ties broken by brief id descending.
pub fn discover_not_doing_briefs(cards_dir: &Path) -> Vec<NotDoingBrief> {
    let Ok(entries) = fs::read_dir(cards_dir) else {
        return vec![];
    };
    let mut out: Vec<NotDoingBrief> = Vec::new();
    for entry in entries.flatten() {
        let card_dir = entry.path();
        if !card_dir.is_dir() {
            continue;
        }
        let Some(brief_id) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else {
            continue;
        };
        if brief_id.starts_with('.') {
            continue;
        }
        let index_path = card_dir.join("index.md");
        if parse_brief_status(&index_path).as_deref() != Some("not-doing") {
            continue;
        }
        let reason = parse_not_doing_reason(&index_path);
        let declared_at = fs::metadata(&index_path)
            .ok()
            .and_then(|m| m.modified().ok())
            .map(DateTime::<Utc>::from);
        out.push(NotDoingBrief { brief: brief_id, reason, declared_at });
    }
    out.sort_by(|a, b| {
        match (a.declared_at, b.declared_at) {
            (Some(a_ts), Some(b_ts)) => b_ts.cmp(&a_ts).then_with(|| b.brief.cmp(&a.brief)),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => b.brief.cmp(&a.brief),
        }
    });
    out
}

/// Returns true when two brief IDs refer to the same brief.
///
/// Handles the common mismatch between truncated history IDs (e.g. "brief-102"
/// written by the backfill script) and full filesystem IDs (e.g.
/// "brief-102-loop-status-blocked-state-surface" from the symlink name).
/// Matching is symmetric: either side can be the truncated form.
pub fn brief_id_matches(a: &str, b: &str) -> bool {
    a == b
        || a.starts_with(&format!("{}-", b))
        || b.starts_with(&format!("{}-", a))
}

/// Scan `wiki/briefs/cards/*/index.md` and return entries whose `Status:` is
/// `queued`. Ordered by priority (from `goals_path` `## Queued next`) first,
/// then by `brief_sort_key` for unranked entries.
///
/// No exclude set — card `Status:` is the single source of truth.
pub fn discover_queued_from_cards(
    cards_dir: &Path,
    goals_path: &Path,
) -> Vec<QueuedBrief> {
    let Ok(entries) = fs::read_dir(cards_dir) else {
        return vec![];
    };
    let priority = parse_goals_priority(goals_path);
    let status_map = build_card_status_map(cards_dir);
    let mut out: Vec<QueuedBrief> = Vec::new();
    for entry in entries.flatten() {
        let card_dir = entry.path();
        if !card_dir.is_dir() {
            continue;
        }
        let Some(brief_id) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else {
            continue;
        };
        if brief_id.starts_with('.') {
            continue;
        }
        let index_path = card_dir.join("index.md");
        if parse_brief_status(&index_path).as_deref() != Some("queued") {
            continue;
        }
        let priority_rank = priority.iter().position(|p| priority_matches(p, &brief_id));
        let depends_on_secrets = parse_depends_on_secrets(&index_path);
        let deps = parse_depends_on(&index_path);
        let readiness = compute_readiness(cards_dir, &deps, &brief_id, &status_map);
        out.push(QueuedBrief {
            brief: brief_id,
            priority_rank,
            depends_on_secrets,
            readiness,
        });
    }
    // Sort: ready before blocked, then by priority_rank, then by brief_sort_key.
    out.sort_by(|a, b| {
        let rw_a: u8 = if matches!(a.readiness, QueuedReadiness::Ready) { 0 } else { 1 };
        let rw_b: u8 = if matches!(b.readiness, QueuedReadiness::Ready) { 0 } else { 1 };
        rw_a.cmp(&rw_b)
            .then_with(|| {
                let rank_a = a.priority_rank.unwrap_or(usize::MAX);
                let rank_b = b.priority_rank.unwrap_or(usize::MAX);
                rank_a.cmp(&rank_b)
            })
            .then_with(|| brief_sort_key(&a.brief).cmp(&brief_sort_key(&b.brief)))
    });
    out
}

/// Build the "Recently Finished" list from `running.json.history`, deduped
/// by brief id (the history often carries two entries per brief — a
/// dispatch-record with `approved_at` and a merge-record with `merged_at`).
/// Returns up to `RECENTLY_FINISHED_LIMIT` entries, sorted newest-first.
#[allow(dead_code)]
pub fn recent_finished(history: &[HistoryEntryRaw]) -> Vec<RecentlyFinishedBrief> {
    use std::collections::HashMap;
    // For each brief id, keep the entry with the latest merged_at (or
    // approved_at fallback). Iterating in order over history preserves
    // insertion order for brief ids that appear multiple times.
    let mut best: HashMap<String, Option<DateTime<Utc>>> = HashMap::new();
    for h in history {
        let ts = h
            .merged_at
            .as_deref()
            .and_then(parse_log_ts)
            .or_else(|| {
                h.approved_at
                    .as_deref()
                    .and_then(parse_log_ts)
            });
        let entry = best.entry(h.brief.clone()).or_insert(None);
        match (*entry, ts) {
            (None, Some(_)) => *entry = ts,
            (Some(cur), Some(new)) if new > cur => *entry = ts,
            _ => {}
        }
    }
    let mut out: Vec<RecentlyFinishedBrief> = best
        .into_iter()
        .map(|(brief, finished_at)| RecentlyFinishedBrief { brief, finished_at })
        .collect();
    // Most recent first. Entries with no timestamp sort to the bottom.
    out.sort_by(|a, b| match (a.finished_at, b.finished_at) {
        (Some(a_ts), Some(b_ts)) => b_ts.cmp(&a_ts),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => a.brief.cmp(&b.brief),
    });
    out.truncate(RECENTLY_FINISHED_LIMIT);
    out
}

/// Scan `wiki/briefs/cards/*/index.md` and return entries whose `Status:` is
/// `merged`. `finished_at` uses the index.md file mtime — the daemon writes
/// `Status: merged` at merge time, so mtime is a faithful proxy.
/// Returns up to `RECENTLY_FINISHED_LIMIT` entries, sorted newest-first.
pub fn discover_recently_finished_from_cards(cards_dir: &Path) -> Vec<RecentlyFinishedBrief> {
    let Ok(entries) = fs::read_dir(cards_dir) else {
        return vec![];
    };
    let mut out: Vec<RecentlyFinishedBrief> = Vec::new();
    for entry in entries.flatten() {
        let card_dir = entry.path();
        if !card_dir.is_dir() {
            continue;
        }
        let Some(brief_id) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else {
            continue;
        };
        if brief_id.starts_with('.') {
            continue;
        }
        let index_path = card_dir.join("index.md");
        if parse_brief_status(&index_path).as_deref() != Some("merged") {
            continue;
        }
        let finished_at = fs::metadata(&index_path)
            .ok()
            .and_then(|m| m.modified().ok())
            .map(DateTime::<Utc>::from);
        out.push(RecentlyFinishedBrief { brief: brief_id, finished_at });
    }
    out.sort_by(|a, b| match (a.finished_at, b.finished_at) {
        (Some(a_ts), Some(b_ts)) => b_ts.cmp(&a_ts),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => a.brief.cmp(&b.brief),
    });
    out.truncate(RECENTLY_FINISHED_LIMIT);
    out
}

/// Scan `wiki/briefs/cards/*/` for card dirs not already accounted for in
/// active/pending/queued/history. Accepts any work-unit prefix (`brief-`,
/// `audit-`, `capture-`, etc). These are drafts: the identifier is claimed
/// but the work isn't dispatch-ready (missing `index.md` or `Status: queued`).
pub fn discover_draft_briefs(cards_dir: &Path, exclude: &HashSet<String>) -> Vec<DraftBrief> {
    let Ok(entries) = fs::read_dir(cards_dir) else {
        return vec![];
    };
    let mut out: Vec<DraftBrief> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if name.starts_with('.') {
            continue;
        }
        let brief_id = name.to_string();
        if exclude.contains(&brief_id) {
            continue;
        }
        let has_index = path.join("index.md").exists();
        out.push(DraftBrief {
            brief: brief_id,
            has_index,
        });
    }
    out.sort_by_key(|d| brief_sort_key(&d.brief));
    out
}

/// Scan `.loop/specialists/*.md` for declared scouts and fold in today's
/// `daemon:scout_*` events from `log.jsonl`. One pass over the log per
/// render tick; the scout count is tiny (single-digit in practice) so we
/// hold a small HashMap keyed by specialist name.
///
/// Files starting with `_` (e.g. `_template.md`) and dotfiles are skipped.
/// Scouts with no events yet still render — file-on-disk is the
/// authoritative roster so Mattie sees dormant scouts, not just firing
/// ones. The Cells subsection is where "did queue-steward actually wake
/// up last night?" gets answered without opening the log.
pub fn discover_scouts(specialists_dir: &Path, log_path: &Path) -> Vec<Scout> {
    let Ok(entries) = fs::read_dir(specialists_dir) else {
        return vec![];
    };

    struct Acc {
        last_at: Option<DateTime<Utc>>,
        last_kind: Option<ScoutEventKind>,
        fires: usize,
        noops: usize,
        failures: usize,
    }
    use std::collections::HashMap;
    let mut by_name: HashMap<String, Acc> = HashMap::new();

    let mut roster: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !name.ends_with(".md") || name.starts_with('.') || name.starts_with('_') {
            continue;
        }
        let stem = name.trim_end_matches(".md").to_string();
        by_name.insert(
            stem.clone(),
            Acc {
                last_at: None,
                last_kind: None,
                fires: 0,
                noops: 0,
                failures: 0,
            },
        );
        roster.push(stem);
    }

    if !by_name.is_empty() {
        if let Ok(file) = fs::File::open(log_path) {
            let today = Utc::now().format("%Y-%m-%d").to_string();
            let reader = BufReader::new(file);
            for line in reader.lines() {
                let Ok(line) = line else { continue };
                if line.trim().is_empty() {
                    continue;
                }
                let Ok(entry) = serde_json::from_str::<RawLogLine>(&line) else {
                    continue;
                };
                let Some(action) = &entry.action else { continue };
                let Some(kind) = ScoutEventKind::from_action(action) else {
                    continue;
                };
                let Some(spec) = entry.specialist.clone() else { continue };
                let Some(acc) = by_name.get_mut(&spec) else { continue };
                let ts_str = entry.ts_str();
                let ts = ts_str.and_then(parse_log_ts);
                // Newest-wins for last_event — log.jsonl is append-only and
                // sorted, but be defensive about timestamp order anyway.
                match (acc.last_at, ts) {
                    (None, Some(_)) => {
                        acc.last_at = ts;
                        acc.last_kind = Some(kind.clone());
                    }
                    (Some(cur), Some(new)) if new >= cur => {
                        acc.last_at = Some(new);
                        acc.last_kind = Some(kind.clone());
                    }
                    _ => {}
                }
                // Daily counts keyed by UTC date prefix of the timestamp —
                // matches scouts.py fire_count_today which uses the same
                // YYYY-MM-DD prefix check against raw `ts`/`timestamp`.
                if let Some(ts_raw) = ts_str {
                    if ts_raw.starts_with(&today) {
                        match kind {
                            ScoutEventKind::Fire => acc.fires += 1,
                            ScoutEventKind::Noop => acc.noops += 1,
                            ScoutEventKind::Failed => acc.failures += 1,
                        }
                    }
                }
            }
        }
    }

    roster.sort();
    roster
        .into_iter()
        .map(|name| {
            let acc = by_name.remove(&name).unwrap();
            Scout {
                name,
                last_event_at: acc.last_at,
                last_event_kind: acc.last_kind,
                fires_today: acc.fires,
                noops_today: acc.noops,
                failures_today: acc.failures,
            }
        })
        .collect()
}

impl CellsState {
    pub fn load() -> Self {
        let running_path = Path::new(".loop/state/running.json");
        let log_path = Path::new(".loop/state/log.jsonl");
        let signals_dir = Path::new(".loop/state/signals");
        let goals_path = Path::new(".loop/state/goals.md");

        let raw: RunningJson = fs::read_to_string(running_path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default();

        let reviews_dir = Path::new(".loop/modules/validator/state/reviews");
        let cards_dir = Path::new("wiki/briefs/cards");
        let active: Vec<ActiveBrief> = raw
            .active
            .iter()
            .map(|r| {
                let dispatched_at = r
                    .dispatched_at
                    .as_deref()
                    .and_then(parse_log_ts);
                let cycle_budget = {
                    let brief_file = cards_dir.join(&r.brief).join("index.md");
                    parse_cycle_budget(&brief_file)
                };
                let worktree_path = {
                    let p = format!(".loop/worktrees/{}", r.branch);
                    if Path::new(&p).exists() {
                        Some(p)
                    } else {
                        None
                    }
                };
                let brief_progress = worktree_path
                    .as_deref()
                    .map(Path::new)
                    .and_then(read_brief_progress);
                let latest_validator_cycle = latest_validator_cycle(
                    reviews_dir,
                    worktree_path.as_deref().map(Path::new),
                    &r.brief,
                );
                ActiveBrief {
                    brief: r.brief.clone(),
                    branch: r.branch.clone(),
                    dispatched_at,
                    brief_progress,
                    latest_validator_cycle,
                    cycle_budget,
                    worktree_path,
                }
            })
            .collect();

        // Pending = union of signals + completed_pending_eval, deduped by brief id.
        // Signals carry richer "why it's stuck" context, so they win on conflict.
        let mut pending: Vec<PendingBrief> = Vec::new();
        let mut pending_seen: HashSet<String> = HashSet::new();

        if let Ok(entries) = fs::read_dir(signals_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map(|e| e != "json").unwrap_or(true) {
                    continue;
                }
                let filename = path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .unwrap_or("")
                    .to_string();
                let reason = match filename.as_str() {
                    "escalate.json" => PendingReason::Escalate,
                    "pending-merge.json" => PendingReason::PendingMerge,
                    "pending-dispatch.json" => PendingReason::PendingDispatch,
                    _ => PendingReason::Unknown,
                };
                let parsed: Option<RawSignal> = fs::read_to_string(&path)
                    .ok()
                    .and_then(|s| serde_json::from_str(&s).ok());
                let brief = parsed.as_ref().and_then(|r| r.brief.clone());
                let trigger = parsed.as_ref().and_then(|r| r.trigger.clone());
                let age = parsed
                    .as_ref()
                    .and_then(|r| r.ts.as_deref())
                    .and_then(parse_log_ts)
                    .or_else(|| {
                        fs::metadata(&path)
                            .ok()
                            .and_then(|m| m.modified().ok())
                            .map(DateTime::<Utc>::from)
                    });
                // A brief-less signal (e.g. a "what should I do next?" decision
                // escalate) still belongs in Pending — synthesize a label from
                // the trigger field so the row is meaningful.
                let has_real_brief = brief.is_some();
                let label = match brief {
                    Some(b) => b,
                    None => match trigger {
                        Some(t) => format!("[{}]", t),
                        None => format!("[{}]", filename.trim_end_matches(".json")),
                    },
                };
                if pending_seen.insert(label.clone()) {
                    // Progress-bar data: only attempt for real brief ids, not
                    // the synthesized `[trigger]` labels used for brief-less
                    // decision escalates.
                    let (cycle, budget) = if has_real_brief {
                        pending_cycle_and_budget(
                            &label,
                            reviews_dir,
                            cards_dir,
                        )
                    } else {
                        (None, None)
                    };
                    // Lift the recommended option's estimated_time into the
                    // Pending row so Mattie can triage time cost without
                    // opening the modal. Build a transient payload and ask it
                    // for the right field — same logic used by the modal.
                    let estimated_time = parsed.as_ref().and_then(|rs| {
                        let transient = SignalPayload {
                            scav_recommendation: rs.scav_recommendation.clone(),
                            options: rs
                                .options
                                .clone()
                                .unwrap_or_default()
                                .into_iter()
                                .map(EscalateOption::from)
                                .collect(),
                            ..Default::default()
                        };
                        transient.recommended_estimated_time().map(String::from)
                    });
                    pending.push(PendingBrief {
                        brief: label,
                        reason,
                        age,
                        latest_validator_cycle: cycle,
                        cycle_budget: budget,
                        estimated_time,
                    });
                }
            }
        }

        for pe in &raw.completed_pending_eval {
            if pending_seen.insert(pe.brief.clone()) {
                let age = pe
                    .completed_at
                    .as_deref()
                    .and_then(parse_log_ts);
                let (cycle, budget) = pending_cycle_and_budget(
                    &pe.brief,
                    reviews_dir,
                    cards_dir,
                );
                pending.push(PendingBrief {
                    brief: pe.brief.clone(),
                    reason: PendingReason::AwaitingEval,
                    age,
                    latest_validator_cycle: cycle,
                    cycle_budget: budget,
                    estimated_time: None,
                });
            }
        }

        for ar in &raw.awaiting_review {
            if pending_seen.insert(ar.brief.clone()) {
                let age = ar
                    .completed_at
                    .as_deref()
                    .and_then(|s| s.parse::<DateTime<Utc>>().ok());
                let (cycle, budget) = pending_cycle_and_budget(
                    &ar.brief,
                    reviews_dir,
                    cards_dir,
                );
                pending.push(PendingBrief {
                    brief: ar.brief.clone(),
                    reason: PendingReason::AwaitingReview,
                    age,
                    latest_validator_cycle: cycle,
                    cycle_budget: budget,
                    estimated_time: None,
                });
            }
        }

        pending.sort_by_key(|p| brief_sort_key(&p.brief));

        // Queued = cards with Status: queued, minus active/pending
        let mut exclude: HashSet<String> = HashSet::new();
        for a in &active {
            exclude.insert(a.brief.clone());
        }
        for p in &pending {
            exclude.insert(p.brief.clone());
        }
        let queued = discover_queued_from_cards(cards_dir, goals_path);
        for q in &queued {
            exclude.insert(q.brief.clone());
        }

        let recently_finished = discover_recently_finished_from_cards(cards_dir);
        let not_doing = discover_not_doing_briefs(cards_dir);

        // Build exclude set for drafts: active, pending, queued, recently_finished, not_doing, rejected
        for rf in &recently_finished {
            exclude.insert(rf.brief.clone());
        }
        for nd in &not_doing {
            exclude.insert(nd.brief.clone());
        }
        // Also exclude rejected cards
        if let Ok(entries) = fs::read_dir(cards_dir) {
            for entry in entries.flatten() {
                let card_dir = entry.path();
                if !card_dir.is_dir() { continue; }
                let Some(bid) = card_dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else { continue; };
                if bid.starts_with('.') { continue; }
                if parse_brief_status(&card_dir.join("index.md")).as_deref() == Some("rejected") {
                    exclude.insert(bid);
                }
            }
        }

        let drafts = discover_draft_briefs(cards_dir, &exclude);

        let specialists_dir = Path::new(".loop/specialists");
        let scouts = discover_scouts(specialists_dir, log_path);

        CellsState {
            active,
            pending,
            queued,
            drafts,
            recently_finished,
            not_doing,
            scouts,
        }
    }
}

// ── DanceFloorState ───────────────────────────────────────────────────────────

pub struct LogEvent {
    pub ts: Option<DateTime<Utc>>,
    pub actor: Option<String>,
    pub event: Option<String>,
    pub brief: Option<String>,
    pub malformed: bool,
    /// True if sourced from daemon.log (worker/validator live activity)
    #[allow(dead_code)]
    pub from_daemon_log: bool,
}

pub struct DanceFloorState {
    pub events: Vec<LogEvent>,
}

/// Parse a single line from daemon.log format: `[YYYY-MM-DD HH:MM:SS] ACTOR: message`
/// Returns (timestamp, actor, message) if parseable, else None.
pub fn parse_daemon_log_line(line: &str) -> Option<(DateTime<Utc>, String, String)> {
    // Must start with `[`
    if !line.starts_with('[') {
        return None;
    }
    let close = line.find(']')?;
    let ts_str = &line[1..close];
    // Parse as naive datetime. The daemon writes timestamps in local time via
    // shell `date`, with no TZ suffix — e.g. `[2026-04-21 11:45:09]`. Treating
    // that as UTC silently subtracts the local offset, pushing every event
    // out of the 30-minute cutoff and leaving the Dance Floor stuck on
    // log.jsonl events only. Parse as local, convert to UTC.
    use chrono::TimeZone;
    let naive = chrono::NaiveDateTime::parse_from_str(ts_str, "%Y-%m-%d %H:%M:%S").ok()?;
    let local = chrono::Local.from_local_datetime(&naive).single()?;
    let ts = local.with_timezone(&Utc);

    let rest = line[close + 1..].trim();
    // Split on first `: `
    let colon_pos = rest.find(": ")?;
    let actor_raw = rest[..colon_pos].trim().to_string();
    let message = rest[colon_pos + 2..].trim().to_string();

    // Normalize actor label to lowercase
    let actor = match actor_raw.to_uppercase().as_str() {
        "WORKER" => "worker",
        "VALIDATOR" => "validator",
        s if s == "CONDUCTOR" || s.starts_with("CONDUCTOR") => "conductor",
        "DAEMON ACTION" | "DAEMON" => "daemon",
        _ => return None, // skip unrecognized prefixes (git output, blank lines, etc.)
    }
    .to_string();

    Some((ts, actor, message))
}

/// Read daemon.log and extract worker/validator/conductor activity lines.
/// Only returns lines from the last `max_age_secs` seconds to avoid flooding.
/// Applies dampening: consecutive entries from the same actor within 30s are
/// collapsed to keep only the latest message.
pub fn load_daemon_log_events(log_path: &Path, max_age_secs: i64) -> Vec<LogEvent> {
    let file = match fs::File::open(log_path) {
        Ok(f) => f,
        Err(_) => return vec![],
    };
    let reader = BufReader::new(file);
    let cutoff = Utc::now() - chrono::Duration::seconds(max_age_secs);
    let mut events: Vec<LogEvent> = Vec::new();

    for line in reader.lines() {
        let Ok(line) = line else { continue };
        let Some((ts, actor, message)) = parse_daemon_log_line(&line) else { continue };
        if ts < cutoff {
            continue;
        }
        // Brief extraction: look for "brief-NNN-..." pattern in the message
        let brief = extract_brief_from_message(&message);

        // Dampening: if last event is same actor + same brief + within 30s, replace it
        if let Some(last) = events.last_mut() {
            if last.actor.as_deref() == Some(&actor) && last.brief == brief {
                if let (Some(last_ts), _) = (last.ts, ()) {
                    if (ts - last_ts).num_seconds().abs() <= 30 {
                        last.ts = Some(ts);
                        last.event = Some(message);
                        continue;
                    }
                }
            }
        }

        events.push(LogEvent {
            ts: Some(ts),
            actor: Some(actor),
            event: Some(message),
            brief,
            malformed: false,
            from_daemon_log: true,
        });
    }
    events
}

/// Extract a brief identifier (e.g. "brief-006-playground-render-fix") from
/// a daemon.log message string.
fn extract_brief_from_message(msg: &str) -> Option<String> {
    // Looks for "brief-NNN" pattern
    let start = msg.find("brief-")?;
    let tail = &msg[start..];
    // brief id ends at whitespace or end-of-string
    let end = tail
        .find(|c: char| c.is_whitespace() || c == ',' || c == '\'')
        .unwrap_or(tail.len());
    let candidate = &tail[..end];
    if candidate.len() > 6 {
        Some(candidate.to_string())
    } else {
        None
    }
}

impl DanceFloorState {
    pub fn load() -> Self {
        let log_path = Path::new(".loop/state/log.jsonl");
        let daemon_log_path = Path::new(".loop/logs/daemon.log");

        let mut events: Vec<LogEvent> = Vec::new();

        // Load structured events from log.jsonl
        if let Ok(file) = fs::File::open(log_path) {
            let reader = std::io::BufReader::new(file);
            for line in reader.lines() {
                let Ok(line) = line else { continue };
                if line.trim().is_empty() {
                    continue;
                }
                match serde_json::from_str::<RawLogLine>(&line) {
                    Ok(entry) => {
                        if entry.is_startup_repair() {
                            continue;
                        }
                        let ts = entry
                            .ts_str()
                            .and_then(parse_log_ts);
                        let actor = entry.derived_actor();
                        let event_msg = entry.event.or(entry.action);
                        events.push(LogEvent {
                            ts,
                            actor,
                            event: event_msg,
                            brief: entry.brief,
                            malformed: false,
                            from_daemon_log: false,
                        });
                    }
                    Err(_) => {
                        let preview = line.chars().take(60).collect::<String>();
                        events.push(LogEvent {
                            ts: None,
                            actor: None,
                            event: Some(format!("[malformed] {}", preview)),
                            brief: None,
                            malformed: true,
                            from_daemon_log: false,
                        });
                    }
                }
            }
        }

        // Load live worker/validator activity from daemon.log (last 30 min)
        let daemon_events = load_daemon_log_events(daemon_log_path, 1800);
        events.extend(daemon_events);

        // Sort by timestamp (None timestamps sort last), then cap
        events.sort_by(|a, b| match (a.ts, b.ts) {
            (Some(at), Some(bt)) => at.cmp(&bt),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        });

        if events.len() > 500 {
            events.drain(..events.len() - 500);
        }
        DanceFloorState { events }
    }
}

// ── SignalsState ──────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct RawSignal {
    pub brief: Option<String>,
    pub reason: Option<String>,
    pub note: Option<String>,
    pub ts: Option<String>,
    pub summary: Option<String>,
    pub trigger: Option<String>,
    #[serde(default)]
    pub key_facts: Option<Vec<String>>,
    pub evaluation: Option<String>,
    pub screenshot_to_review: Option<String>,
    #[serde(default)]
    pub options: Option<Vec<RawEscalateOption>>,
    pub scav_recommendation: Option<String>,
    pub scav_reasoning: Option<String>,
    /// Some escalate payloads use `rationale` for the same role as
    /// `scav_reasoning`. Accept both; display layer picks whichever is present.
    pub rationale: Option<String>,
    pub anti_pattern_guardrail: Option<String>,
    pub what_you_should_feel: Option<String>,
    /// Prescriptive: "reply with A/B/C" style ask. Distinct from
    /// `what_you_should_feel`, which is emotional framing.
    pub action_required_from_mattie: Option<String>,
}

/// Accepts all escalate option schemas seen in the wild:
/// - brief-008 shape: `{id, action, when_right, cost_if_wrong}`
/// - brief-009-followup shape: `{id, label, cost, pros, cons}`
/// - brief-011 shape: `{action, estimated_time, outcome}` or `{action, when_to_pick}`
///
/// Every field is Option, so the renderer lights up whichever are present.
#[derive(Deserialize, Clone)]
pub struct RawEscalateOption {
    pub id: Option<String>,
    pub action: Option<String>,
    pub when_right: Option<String>,
    pub cost_if_wrong: Option<String>,
    pub label: Option<String>,
    pub cost: Option<String>,
    #[serde(default)]
    pub pros: Option<Vec<String>>,
    #[serde(default)]
    pub cons: Option<Vec<String>>,
    pub estimated_time: Option<String>,
    pub outcome: Option<String>,
    pub when_to_pick: Option<String>,
}

/// Rich payload rendered inside the signal-detail modal. Every field is
/// optional because pending-merge / pending-dispatch signals don't carry the
/// same fields as escalate.json.
#[derive(Clone, Default)]
pub struct SignalPayload {
    pub summary: Option<String>,
    pub trigger: Option<String>,
    pub reason: Option<String>,
    pub note: Option<String>,
    pub key_facts: Vec<String>,
    pub evaluation: Option<String>,
    pub screenshot_to_review: Option<String>,
    pub options: Vec<EscalateOption>,
    pub scav_recommendation: Option<String>,
    pub scav_reasoning: Option<String>,
    pub anti_pattern_guardrail: Option<String>,
    pub what_you_should_feel: Option<String>,
    pub action_required_from_mattie: Option<String>,
}

#[derive(Clone)]
pub struct EscalateOption {
    pub id: Option<String>,
    /// Headline label for the option. Prefer `label` when present, falling
    /// back to `action` for the brief-008-era schema.
    pub label: Option<String>,
    pub action: Option<String>,
    pub when_right: Option<String>,
    pub cost_if_wrong: Option<String>,
    pub cost: Option<String>,
    pub pros: Vec<String>,
    pub cons: Vec<String>,
    /// Human-readable estimate of how long this option takes to execute
    /// (e.g. "~60s", "15-30 min"). Lifted into the Pending row for the
    /// recommended option so Mattie can triage by time cost.
    pub estimated_time: Option<String>,
    pub outcome: Option<String>,
    pub when_to_pick: Option<String>,
}

impl From<RawEscalateOption> for EscalateOption {
    fn from(r: RawEscalateOption) -> Self {
        EscalateOption {
            id: r.id,
            label: r.label,
            action: r.action,
            when_right: r.when_right,
            cost_if_wrong: r.cost_if_wrong,
            cost: r.cost,
            pros: r.pros.unwrap_or_default(),
            cons: r.cons.unwrap_or_default(),
            estimated_time: r.estimated_time,
            outcome: r.outcome,
            when_to_pick: r.when_to_pick,
        }
    }
}

impl EscalateOption {
    /// One-line headline for the option: prefer `label`, else `action`.
    pub fn headline(&self) -> Option<&str> {
        self.label.as_deref().or(self.action.as_deref())
    }
}

impl SignalPayload {
    /// True if the payload has enough content to warrant opening the modal.
    pub fn has_content(&self) -> bool {
        self.summary.is_some()
            || self.trigger.is_some()
            || self.reason.is_some()
            || self.note.is_some()
            || !self.key_facts.is_empty()
            || !self.options.is_empty()
            || self.scav_recommendation.is_some()
            || self.what_you_should_feel.is_some()
            || self.action_required_from_mattie.is_some()
            || self.evaluation.is_some()
    }

    /// Find the option matching the scav recommendation and return its
    /// `estimated_time` string. Used to surface the "if you follow the rec,
    /// this costs ~60s" signal inline in the Pending row without opening
    /// the modal. Falls back to the first option's estimated_time if no
    /// recommendation is set.
    pub fn recommended_estimated_time(&self) -> Option<&str> {
        if let Some(rec) = &self.scav_recommendation {
            for opt in &self.options {
                if let Some(id) = &opt.id {
                    if rec.contains(id.as_str()) {
                        return opt.estimated_time.as_deref();
                    }
                }
            }
        }
        self.options
            .first()
            .and_then(|o| o.estimated_time.as_deref())
    }
}

pub enum SignalType {
    Escalate,
    PendingMerge,
    PendingDispatch,
    Unknown(String),
}

impl SignalType {
    pub fn from_filename(name: &str) -> Self {
        match name {
            "escalate.json" => SignalType::Escalate,
            "pending-merge.json" => SignalType::PendingMerge,
            "pending-dispatch.json" => SignalType::PendingDispatch,
            other => SignalType::Unknown(other.to_string()),
        }
    }

    pub fn label(&self) -> &str {
        match self {
            SignalType::Escalate => "escalate",
            SignalType::PendingMerge => "pending-merge",
            SignalType::PendingDispatch => "pending-dispatch",
            SignalType::Unknown(s) => s.as_str(),
        }
    }
}

pub struct Signal {
    pub signal_type: SignalType,
    pub brief: Option<String>,
    pub reason: Option<String>,
    pub ts: Option<DateTime<Utc>>,
    pub filename: String,
    pub payload: SignalPayload,
}

impl Signal {
    /// One-line label for the list row. Prefers the brief id; falls back to
    /// a bracketed trigger (e.g. `[next_dispatch_decision…]`) for
    /// brief-less decision escalates; final fallback is the filename.
    pub fn display_label(&self) -> String {
        if let Some(brief) = &self.brief {
            return brief.clone();
        }
        if let Some(trigger) = &self.payload.trigger {
            // Cap trigger to keep the row width sane; caller may truncate further.
            let capped = if trigger.chars().count() > 32 {
                let truncated: String = trigger.chars().take(31).collect();
                format!("{}…", truncated)
            } else {
                trigger.clone()
            };
            return format!("[{}]", capped);
        }
        format!("[{}]", self.filename.trim_end_matches(".json"))
    }

    /// Second-column prose for the list row. Prefers `reason`/`note`; falls
    /// back to `summary` so brief-less escalates (which often carry only
    /// summary) aren't rendered as a bare `—`.
    pub fn display_reason(&self) -> Option<&str> {
        self.reason
            .as_deref()
            .or(self.payload.summary.as_deref())
    }
}

pub struct SignalsState {
    pub signals: Vec<Signal>,
}

// ── LearningsState ────────────────────────────────────────────────────────────

/// Cross-worktree collection of "learnings" — short notes workers and scav
/// leave in `progress.json` as they work. Rendered in the Signals panel
/// when calm (no active escalates) as a rotating quote surface. Falls
/// back gracefully to an empty collection when nothing's around.
pub struct LearningsState {
    pub items: Vec<String>,
}

#[derive(Deserialize)]
struct ProgressLearnings {
    #[serde(default)]
    learnings: Vec<String>,
}

impl LearningsState {
    /// Scan all worktrees' `progress.json` plus an optional curated
    /// `wiki/operating-docs/learnings.md` file. Dedupe by exact string.
    /// Shuffle deterministically per load so the rotation feels random
    /// but the same hive session doesn't flip order mid-tick.
    pub fn load() -> Self {
        let mut items: Vec<String> = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();

        // Source 1: every worktree's progress.json
        if let Ok(entries) = fs::read_dir(".loop/worktrees") {
            for entry in entries.flatten() {
                let path = entry.path().join(".loop/state/progress.json");
                if let Ok(body) = fs::read_to_string(&path) {
                    if let Ok(parsed) = serde_json::from_str::<ProgressLearnings>(&body) {
                        for learning in parsed.learnings {
                            let trimmed = learning.trim().to_string();
                            if trimmed.is_empty() {
                                continue;
                            }
                            if seen.insert(trimmed.clone()) {
                                items.push(trimmed);
                            }
                        }
                    }
                }
            }
        }

        // Source 2: curated wiki/operating-docs/learnings.md — bullet lines
        // (`- item` or `* item`). Optional, absent today.
        if let Ok(body) = fs::read_to_string("wiki/operating-docs/learnings.md") {
            for raw in body.lines() {
                let line = raw.trim_start();
                let bullet = line
                    .strip_prefix("- ")
                    .or_else(|| line.strip_prefix("* "));
                if let Some(text) = bullet {
                    let t = text.trim().to_string();
                    if !t.is_empty() && seen.insert(t.clone()) {
                        items.push(t);
                    }
                }
            }
        }

        // Deterministic shuffle per hive run so the rotation ordering
        // feels random without depending on an rng dep. Rotate the vec
        // by a process-stable pseudo-offset (bits of the PID's hash).
        if items.len() > 1 {
            let seed = std::process::id() as usize;
            let rot = seed % items.len();
            items.rotate_left(rot);
        }

        LearningsState { items }
    }

    /// Item at `index`, wrapping at len. None if there are no learnings.
    pub fn pick(&self, index: usize) -> Option<&str> {
        if self.items.is_empty() {
            return None;
        }
        Some(&self.items[index % self.items.len()])
    }
}

impl SignalsState {
    pub fn load() -> Self {
        let signals_dir = Path::new(".loop/state/signals");
        let mut signals = Vec::new();
        let Ok(entries) = fs::read_dir(signals_dir) else {
            return SignalsState { signals };
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().map(|e| e != "json").unwrap_or(true) {
                continue;
            }
            let filename = path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("")
                .to_string();
            let signal_type = SignalType::from_filename(&filename);
            let raw: Option<RawSignal> = fs::read_to_string(&path)
                .ok()
                .and_then(|s| serde_json::from_str(&s).ok());
            let ts: Option<DateTime<Utc>> = raw
                .as_ref()
                .and_then(|r| r.ts.as_deref())
                .and_then(parse_log_ts)
                .or_else(|| {
                    fs::metadata(&path)
                        .ok()
                        .and_then(|m| m.modified().ok())
                        .map(DateTime::<Utc>::from)
                });
            let brief = raw.as_ref().and_then(|r| r.brief.clone());
            let reason = raw
                .as_ref()
                .and_then(|r| r.reason.clone().or_else(|| r.note.clone()));
            let payload = match raw {
                Some(r) => SignalPayload {
                    summary: r.summary,
                    trigger: r.trigger,
                    reason: r.reason.clone(),
                    note: r.note.clone(),
                    key_facts: r.key_facts.unwrap_or_default(),
                    evaluation: r.evaluation,
                    screenshot_to_review: r.screenshot_to_review,
                    options: r
                        .options
                        .unwrap_or_default()
                        .into_iter()
                        .map(EscalateOption::from)
                        .collect(),
                    scav_recommendation: r.scav_recommendation,
                    // Prefer explicit scav_reasoning; fall back to rationale
                    // for the newer payload shape that uses that field name.
                    scav_reasoning: r.scav_reasoning.or(r.rationale),
                    anti_pattern_guardrail: r.anti_pattern_guardrail,
                    what_you_should_feel: r.what_you_should_feel,
                    action_required_from_mattie: r.action_required_from_mattie,
                },
                None => SignalPayload::default(),
            };
            signals.push(Signal {
                signal_type,
                brief,
                reason,
                ts,
                filename,
                payload,
            });
        }
        // escalate first (most urgent), then stable sort preserves file order otherwise
        signals.sort_by_key(|s| match s.signal_type {
            SignalType::Escalate => 0,
            SignalType::PendingMerge => 1,
            SignalType::PendingDispatch => 2,
            SignalType::Unknown(_) => 3,
        });
        SignalsState { signals }
    }
}

// ── run cards ─────────────────────────────────────────────────────────────────

/// How many tail heartbeat lines to load per run — enough for pace computation
/// (last 5 heartbeats) + loss trend display, with headroom.
#[allow(dead_code)]
const HEARTBEAT_TAIL: usize = 10;

#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunStatus {
    Pending,
    Running,
    Complete,
    Failed,
    Preempted,
    Stale,
    Unknown(String),
}

#[allow(dead_code)]
impl RunStatus {
    pub fn from_str(s: &str) -> Self {
        match s.trim().to_ascii_lowercase().as_str() {
            "pending" => RunStatus::Pending,
            "running" => RunStatus::Running,
            "complete" | "completed" => RunStatus::Complete,
            "failed" => RunStatus::Failed,
            "preempted" => RunStatus::Preempted,
            "stale" => RunStatus::Stale,
            other => RunStatus::Unknown(other.to_string()),
        }
    }

    pub fn label(&self) -> &str {
        match self {
            RunStatus::Pending => "pending",
            RunStatus::Running => "running",
            RunStatus::Complete => "complete",
            RunStatus::Failed => "failed",
            RunStatus::Preempted => "preempted",
            RunStatus::Stale => "stale",
            RunStatus::Unknown(s) => s.as_str(),
        }
    }
}

#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct RunHeartbeat {
    pub ts: DateTime<Utc>,
    pub last_step: Option<u64>,
    pub last_loss: Option<f64>,
    pub app_state: Option<String>,
    pub alert: Option<String>,
}

#[allow(dead_code)]
pub struct RunCard {
    pub run_id: String,
    pub policy: Option<String>,
    pub dataset: Option<String>,
    pub machine: Option<String>,
    pub status: RunStatus,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    /// Last `HEARTBEAT_TAIL` heartbeats from `wiki/runs/<run-id>/heartbeats.jsonl`.
    pub heartbeats: Vec<RunHeartbeat>,
    /// True when `heartbeats.jsonl` exists for this run (even if currently empty).
    /// False means the sidecar was never written — contract violation for running runs.
    pub heartbeat_sidecar_present: bool,
    /// Raw failure signal from `.loop/state/signals/training-{failed,preempted,stale}-<run-id>.json`.
    pub failure_signal: Option<serde_json::Value>,
}

#[allow(dead_code)]
impl RunCard {
    pub fn latest_heartbeat(&self) -> Option<&RunHeartbeat> {
        self.heartbeats.last()
    }
}

#[allow(dead_code)]
#[derive(Deserialize)]
struct HeartbeatRaw {
    ts: Option<String>,
    last_step: Option<serde_json::Value>,
    last_loss: Option<serde_json::Value>,
    app_state: Option<String>,
    alert: Option<String>,
}

/// Parse a timestamp from run card frontmatter. Handles RFC3339 and the
/// non-standard `"YYYY-MM-DDTHH:MM UTC"` form some scouts write.
/// Returns None for "TBD", null, empty, or unparseable strings.
#[allow(dead_code)]
fn parse_run_ts(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim().trim_matches('"');
    if s.is_empty() || s == "TBD" || s == "null" {
        return None;
    }
    if let Ok(ts) = s.parse::<DateTime<Utc>>() {
        return Some(ts);
    }
    // Normalize "YYYY-MM-DDTHH:MM UTC" → "YYYY-MM-DDTHH:MM:00Z"
    if let Ok(ts) = s.replace(" UTC", ":00Z").parse::<DateTime<Utc>>() {
        return Some(ts);
    }
    None
}

/// Extract a field value from YAML frontmatter. Key comparison is
/// case-insensitive; surrounding double-quotes are stripped from the value.
#[allow(dead_code)]
fn parse_yaml_front_field(lines: &[&str], key: &str) -> Option<String> {
    if lines.first().map(|l| l.trim()) != Some("---") {
        return None;
    }
    let key_prefix = format!("{}:", key.to_ascii_lowercase());
    for line in lines.iter().skip(1) {
        if line.trim() == "---" {
            break;
        }
        let lower = line.to_ascii_lowercase();
        if lower.starts_with(&key_prefix) {
            let after = &line[key_prefix.len()..];
            let val = after.trim().trim_matches('"');
            if !val.is_empty() && val != "null" {
                return Some(val.to_string());
            }
        }
    }
    None
}

/// Read last `HEARTBEAT_TAIL` heartbeats from `<runs_dir>/<run_id>/heartbeats.jsonl`.
/// Returns `(heartbeats, sidecar_present)`. `sidecar_present` is false only
/// when the file is absent (present-but-empty returns `([], true)`).
#[allow(dead_code)]
fn read_run_heartbeats(runs_dir: &Path, run_id: &str) -> (Vec<RunHeartbeat>, bool) {
    let path = runs_dir.join(run_id).join("heartbeats.jsonl");
    let Ok(file) = fs::File::open(&path) else {
        return (vec![], false);
    };
    let reader = BufReader::new(file);
    let mut raw_lines: Vec<String> = Vec::new();
    for line in reader.lines() {
        let Ok(l) = line else { continue };
        let trimmed = l.trim().to_string();
        if !trimmed.is_empty() {
            raw_lines.push(trimmed);
        }
    }
    let tail_start = raw_lines.len().saturating_sub(HEARTBEAT_TAIL);
    let heartbeats = raw_lines[tail_start..]
        .iter()
        .filter_map(|l| {
            let raw: HeartbeatRaw = serde_json::from_str(l).ok()?;
            let ts = raw.ts.as_deref().and_then(parse_run_ts)?;
            let last_step = raw.last_step.as_ref().and_then(|v| v.as_u64());
            let last_loss = raw.last_loss.as_ref().and_then(|v| v.as_f64());
            Some(RunHeartbeat {
                ts,
                last_step,
                last_loss,
                app_state: raw.app_state,
                alert: raw.alert,
            })
        })
        .collect();
    (heartbeats, true)
}

/// Check for a failure signal file for failed, preempted, or stale runs.
#[allow(dead_code)]
fn read_failure_signal(signals_dir: &Path, run_id: &str) -> Option<serde_json::Value> {
    for kind in &["failed", "preempted", "stale"] {
        let path = signals_dir.join(format!("training-{}-{}.json", kind, run_id));
        if let Ok(body) = fs::read_to_string(&path) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&body) {
                return Some(v);
            }
        }
    }
    None
}

/// Glob `<runs_dir>/*/index.md`, parse YAML frontmatter, load heartbeats and
/// failure signals. Returns all cards sorted by `started-at` desc (newest first).
#[allow(dead_code)]
pub fn load_run_cards(runs_dir: &Path, signals_dir: &Path) -> Vec<RunCard> {
    let Ok(entries) = fs::read_dir(runs_dir) else {
        return vec![];
    };
    let mut cards: Vec<RunCard> = Vec::new();
    for entry in entries.flatten() {
        let dir = entry.path();
        if !dir.is_dir() {
            continue;
        }
        let Some(name) = dir.file_name().and_then(|n| n.to_str()).map(|s| s.to_string()) else {
            continue;
        };
        if name.starts_with('.') || name == "_template" {
            continue;
        }
        let index = dir.join("index.md");
        let Ok(content) = fs::read_to_string(&index) else { continue };
        let lines: Vec<&str> = content.lines().collect();

        let run_id = parse_yaml_front_field(&lines, "run-id")
            .unwrap_or_else(|| name.clone());
        let policy = parse_yaml_front_field(&lines, "policy");
        let dataset = parse_yaml_front_field(&lines, "dataset");
        let machine = parse_yaml_front_field(&lines, "machine");
        let status = RunStatus::from_str(
            &parse_yaml_front_field(&lines, "status").unwrap_or_default(),
        );
        let started_at = parse_yaml_front_field(&lines, "started-at")
            .as_deref()
            .and_then(parse_run_ts);
        let completed_at = parse_yaml_front_field(&lines, "completed-at")
            .as_deref()
            .and_then(parse_run_ts);

        let (heartbeats, heartbeat_sidecar_present) = match &status {
            RunStatus::Running | RunStatus::Stale => read_run_heartbeats(runs_dir, &run_id),
            _ => (vec![], false),
        };

        let failure_signal = match &status {
            RunStatus::Failed | RunStatus::Preempted | RunStatus::Stale => {
                read_failure_signal(signals_dir, &run_id)
            }
            _ => None,
        };

        cards.push(RunCard {
            run_id,
            policy,
            dataset,
            machine,
            status,
            started_at,
            completed_at,
            heartbeats,
            heartbeat_sidecar_present,
            failure_signal,
        });
    }
    // Newest started_at first; missing dates trail; ties broken by run_id desc
    cards.sort_by(|a, b| match (b.started_at, a.started_at) {
        (Some(bt), Some(at)) => bt.cmp(&at),
        (Some(_), None) => std::cmp::Ordering::Less,
        (None, Some(_)) => std::cmp::Ordering::Greater,
        (None, None) => b.run_id.cmp(&a.run_id),
    });
    cards
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_log_ts_accepts_real_utc() {
        // Past UTC timestamp should parse cleanly.
        let ts = parse_log_ts("2026-04-21T17:00:00Z").unwrap();
        assert_eq!(ts.to_rfc3339(), "2026-04-21T17:00:00+00:00");
    }

    #[test]
    fn parse_log_ts_clamps_future_to_now() {
        // Writer bug: timestamps more than ~5 min in the future are
        // untrustworthy (conductor invents times, or daemon mislabels
        // local-as-UTC). Clamp to `now` so callers display "0s ago"
        // instead of `?` — incident 2026-04-23-hive-parse-log-ts-break.
        let future_utc = (Utc::now() + chrono::Duration::hours(6))
            .format("%Y-%m-%dT%H:%M:%SZ")
            .to_string();
        let parsed = parse_log_ts(&future_utc).expect("future ts clamped, not rejected");
        let delta = (Utc::now() - parsed).num_seconds().abs();
        assert!(delta < 5, "clamped ts should be within ~5s of now");
    }

    #[test]
    fn parse_log_ts_rejects_garbage() {
        assert!(parse_log_ts("not a date").is_none());
        assert!(parse_log_ts("").is_none());
    }

    #[test]
    fn heartbeat_detected_via_event_field() {
        let line = r#"{"ts":"2026-04-21T17:00:00Z","actor":"conductor","event":"heartbeat_noop","reason":"idle"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert!(entry.is_heartbeat());
        assert_eq!(entry.ts_str(), Some("2026-04-21T17:00:00Z"));
    }

    #[test]
    fn heartbeat_detected_via_action_field() {
        let line = r#"{"timestamp":"2026-04-21T17:00:00Z","action":"daemon:heartbeat"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert!(entry.is_heartbeat());
        assert_eq!(entry.ts_str(), Some("2026-04-21T17:00:00Z"));
    }

    #[test]
    fn derived_actor_uses_explicit_actor_when_present() {
        let line = r#"{"ts":"2026-04-21T17:00:00Z","actor":"conductor","event":"dispatch"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert_eq!(entry.derived_actor().as_deref(), Some("conductor"));
    }

    #[test]
    fn derived_actor_falls_back_to_action_prefix() {
        // Daemon's Python actions.py writes entries with `action` but no `actor`.
        let line = r#"{"timestamp":"2026-04-21T18:13:40Z","action":"daemon:merge","brief":"brief-005"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert_eq!(entry.derived_actor().as_deref(), Some("daemon"));
    }

    #[test]
    fn derived_actor_returns_none_when_neither_present() {
        let line = r#"{"ts":"2026-04-21T17:00:00Z","event":"something"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert!(entry.derived_actor().is_none());
    }

    #[test]
    fn derived_actor_relabels_daemon_scout_as_scout() {
        let line = r#"{"timestamp":"2026-04-24T02:00:00Z","action":"daemon:scout_fire","specialist":"queue-steward"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert_eq!(entry.derived_actor().as_deref(), Some("scout"));
        assert_eq!(entry.specialist.as_deref(), Some("queue-steward"));
    }

    #[test]
    fn derived_actor_daemon_non_scout_stays_daemon() {
        let line = r#"{"timestamp":"2026-04-24T02:00:00Z","action":"daemon:merge","brief":"brief-034"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert_eq!(entry.derived_actor().as_deref(), Some("daemon"));
    }

    #[test]
    fn scout_event_kind_from_action() {
        assert_eq!(
            ScoutEventKind::from_action("daemon:scout_fire"),
            Some(ScoutEventKind::Fire)
        );
        assert_eq!(
            ScoutEventKind::from_action("daemon:scout_noop"),
            Some(ScoutEventKind::Noop)
        );
        assert_eq!(
            ScoutEventKind::from_action("daemon:scout_failed"),
            Some(ScoutEventKind::Failed)
        );
        assert!(ScoutEventKind::from_action("daemon:merge").is_none());
    }

    #[test]
    fn discover_scouts_empty_when_dir_missing() {
        let missing = std::env::temp_dir().join("hive_scouts_missing_dir_xyz");
        let _ = std::fs::remove_dir_all(&missing);
        let log = tempfile_write(b"");
        let scouts = discover_scouts(&missing, &log);
        assert!(scouts.is_empty());
        std::fs::remove_file(&log).ok();
    }

    #[test]
    fn discover_scouts_reads_files_and_events() {
        // One dir with two specialist files, a template that should be
        // ignored, and a log.jsonl with two events for one of them.
        let dir = std::env::temp_dir().join(format!(
            "hive_scouts_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("queue-steward.md"), "---\nname: queue-steward\n---\n").unwrap();
        std::fs::write(dir.join("bug-spotter.md"), "---\nname: bug-spotter\n---\n").unwrap();
        std::fs::write(dir.join("_template.md"), "---\nname: _template\n---\n").unwrap();

        let today = Utc::now().format("%Y-%m-%d").to_string();
        let now_iso = format!("{}T12:00:00Z", today);
        let earlier = format!("{}T11:00:00Z", today);
        let log_data = format!(
            "{{\"timestamp\":\"{earlier}\",\"action\":\"daemon:scout_fire\",\"specialist\":\"queue-steward\"}}\n\
             {{\"timestamp\":\"{now_iso}\",\"action\":\"daemon:scout_noop\",\"specialist\":\"queue-steward\"}}\n\
             {{\"timestamp\":\"{now_iso}\",\"action\":\"daemon:merge\",\"brief\":\"brief-001\"}}\n",
            earlier = earlier,
            now_iso = now_iso,
        );
        let log = tempfile_write(log_data.as_bytes());

        let scouts = discover_scouts(&dir, &log);
        // `_template.md` skipped; two scouts in alphabetical order.
        assert_eq!(scouts.len(), 2);
        assert_eq!(scouts[0].name, "bug-spotter");
        assert_eq!(scouts[1].name, "queue-steward");

        // bug-spotter: never fired.
        assert!(scouts[0].last_event_at.is_none());
        assert!(scouts[0].last_event_kind.is_none());
        assert_eq!(scouts[0].fires_today, 0);

        // queue-steward: one fire + one noop today; last event is the noop.
        assert_eq!(scouts[1].fires_today, 1);
        assert_eq!(scouts[1].noops_today, 1);
        assert_eq!(scouts[1].failures_today, 0);
        assert_eq!(
            scouts[1].last_event_kind.as_ref(),
            Some(&ScoutEventKind::Noop)
        );
        assert!(scouts[1].last_event_at.is_some());

        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_file(&log).ok();
    }

    #[test]
    fn discover_scouts_failure_counted_and_flagged() {
        let dir = std::env::temp_dir().join(format!(
            "hive_scouts_fail_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("queue-steward.md"), "---\n---\n").unwrap();

        let today = Utc::now().format("%Y-%m-%d").to_string();
        let ts = format!("{}T12:00:00Z", today);
        let log_data = format!(
            "{{\"timestamp\":\"{ts}\",\"action\":\"daemon:scout_failed\",\"specialist\":\"queue-steward\"}}\n",
            ts = ts
        );
        let log = tempfile_write(log_data.as_bytes());

        let scouts = discover_scouts(&dir, &log);
        assert_eq!(scouts.len(), 1);
        assert_eq!(scouts[0].failures_today, 1);
        assert_eq!(
            scouts[0].last_event_kind.as_ref(),
            Some(&ScoutEventKind::Failed)
        );

        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_file(&log).ok();
    }

    #[test]
    fn non_heartbeat_event_not_detected() {
        let line = r#"{"ts":"2026-04-21T17:00:00Z","actor":"conductor","event":"dispatch","brief":"brief-005"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert!(!entry.is_heartbeat());
    }

    #[test]
    fn running_json_parses_active_briefs() {
        let json = r#"{
            "active": [
                {
                    "brief": "brief-005-beehive",
                    "branch": "brief-005-beehive",
                    "dispatched_at": "2026-04-21T17:36:06Z"
                }
            ],
            "completed_pending_eval": [],
            "history": []
        }"#;
        let parsed: RunningJson = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.active.len(), 1);
        assert_eq!(parsed.active[0].brief, "brief-005-beehive");
        assert_eq!(
            parsed.active[0].dispatched_at.as_deref(),
            Some("2026-04-21T17:36:06Z")
        );
    }

    #[test]
    fn running_json_bad_history_entry_does_not_blank_active() {
        // Regression: one bad history entry (here, merge_sha as a JSON
        // integer instead of a string — the 92329478 short-SHA bug from
        // the hand-merge recipe) was poisoning the whole RunningJson parse,
        // collapsing active[] to empty. With per-element lossy parsing,
        // the bad entry drops itself and active stays intact.
        let json = r#"{
            "active": [
                {
                    "brief": "brief-147",
                    "branch": "brief-147",
                    "dispatched_at": "2026-05-07T16:31:34Z"
                }
            ],
            "completed_pending_eval": [],
            "awaiting_review": [],
            "history": [
                {
                    "brief": "brief-good",
                    "merge_sha": "abc1234",
                    "merged_at": "2026-05-06T00:00:00Z"
                },
                {
                    "brief": "brief-bad",
                    "merge_sha": 92329478,
                    "merged_at": "2026-05-06T01:34:36Z"
                }
            ]
        }"#;
        let parsed: RunningJson = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.active.len(), 1, "active must survive a bad history entry");
        assert_eq!(parsed.active[0].brief, "brief-147");
        assert_eq!(parsed.history.len(), 1, "good history entry survives");
        assert_eq!(parsed.history[0].brief, "brief-good");
    }

    #[test]
    fn running_json_empty_active() {
        let json = r#"{"active":[],"completed_pending_eval":[],"history":[]}"#;
        let parsed: RunningJson = serde_json::from_str(json).unwrap();
        assert!(parsed.active.is_empty());
    }

    #[test]
    fn discover_not_doing_briefs_picks_up_status_field() {
        let dir = std::env::temp_dir().join(format!(
            "hive_not_doing_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        // not-doing brief with reason
        let b019 = dir.join("brief-019-daemon-state-repair");
        std::fs::create_dir_all(&b019).unwrap();
        std::fs::write(
            b019.join("index.md"),
            "---\nStatus: not-doing\n---\n**Not-doing-reason:** superseded by brief-061 worker-rebase\n",
        ).unwrap();
        // queued brief — should not appear
        let b020 = dir.join("brief-020-foo");
        std::fs::create_dir_all(&b020).unwrap();
        std::fs::write(b020.join("index.md"), "---\nStatus: queued\n---\n").unwrap();
        // not-doing with underscore variant (bold markdown format)
        let b021 = dir.join("brief-021-bar");
        std::fs::create_dir_all(&b021).unwrap();
        std::fs::write(b021.join("index.md"), "**Status:** not_doing\n").unwrap();
        // not-doing with mixed case (bold markdown format)
        let b022 = dir.join("brief-022-baz");
        std::fs::create_dir_all(&b022).unwrap();
        std::fs::write(b022.join("index.md"), "**Status:** Not-Doing\n").unwrap();

        let result = discover_not_doing_briefs(&dir);
        let ids: Vec<&str> = result.iter().map(|b| b.brief.as_str()).collect();
        assert!(ids.contains(&"brief-019-daemon-state-repair"), "should include not-doing brief");
        assert!(!ids.contains(&"brief-020-foo"), "queued brief must not appear");
        assert!(ids.contains(&"brief-021-bar"), "not_doing variant should match");
        assert!(ids.contains(&"brief-022-baz"), "Not-Doing variant should match");

        // reason parsed correctly
        let entry_019 = result.iter().find(|b| b.brief == "brief-019-daemon-state-repair").unwrap();
        assert_eq!(
            entry_019.reason.as_deref(),
            Some("superseded by brief-061 worker-rebase"),
        );

        std::fs::remove_dir_all(&dir).ok();
    }


    #[test]
    fn pid_alive_for_current_process() {
        let pid = std::process::id();
        assert!(pid_alive(pid));
    }

    #[test]
    fn pid_alive_bogus_pid() {
        assert!(!pid_alive(999_999_999));
    }

    #[test]
    fn relative_time_seconds() {
        let ts = Utc::now() - chrono::Duration::seconds(30);
        let r = relative_time(ts);
        assert!(r.ends_with("s ago"), "got: {}", r);
    }

    #[test]
    fn relative_time_minutes() {
        let ts = Utc::now() - chrono::Duration::seconds(90);
        let r = relative_time(ts);
        assert!(r.ends_with("m ago"), "got: {}", r);
    }

    #[test]
    fn log_event_parsed_from_conductor_line() {
        let line = r#"{"ts":"2026-04-21T17:00:00Z","actor":"conductor","event":"dispatch","brief":"brief-005-beehive"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert_eq!(entry.actor.as_deref(), Some("conductor"));
        assert_eq!(entry.event.as_deref(), Some("dispatch"));
        assert_eq!(entry.brief.as_deref(), Some("brief-005-beehive"));
        assert!(!entry.is_heartbeat());
    }

    #[test]
    fn log_event_parsed_from_daemon_line() {
        let line = r#"{"timestamp":"2026-04-21T02:06:40Z","action":"daemon:dispatch","brief":"brief-003-loop-revisions-v1"}"#;
        let entry: RawLogLine = serde_json::from_str(line).unwrap();
        assert!(entry.actor.is_none());
        assert_eq!(entry.action.as_deref(), Some("daemon:dispatch"));
        assert_eq!(entry.ts_str(), Some("2026-04-21T02:06:40Z"));
    }

    #[test]
    fn dance_floor_malformed_line_does_not_panic() {
        let tmp = tempfile_write(b"not json at all\n{\"ts\":\"2026-04-21T17:00:00Z\",\"actor\":\"conductor\",\"event\":\"dispatch\"}\n");
        let state = load_dance_floor_from_path(&tmp);
        assert_eq!(state.events.len(), 2);
        assert!(state.events[0].malformed);
        assert!(!state.events[1].malformed);
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn dance_floor_filters_startup_repair_per_brief_rows() {
        // Per-brief startup_repair entries get a fresh timestamp on every daemon
        // restart — they polluted the Dance Floor with bursts of "Xm ago" rows
        // for old briefs. The Dance Floor should skip them; the summary line
        // (startup_repair_complete) should still appear.
        let data = concat!(
            r#"{"timestamp":"2026-06-01T20:28:11Z","action":"daemon:startup_repair","reason":"backfilled_from_git","brief":"brief-018","merge_sha":"abc"}"#, "\n",
            r#"{"timestamp":"2026-06-01T20:28:11Z","action":"daemon:startup_repair","reason":"backfilled_from_git","brief":"brief-019","merge_sha":"def"}"#, "\n",
            r#"{"timestamp":"2026-06-01T20:28:11Z","action":"daemon:startup_repair_complete","duration_ms":97}"#, "\n",
            r#"{"timestamp":"2026-06-01T20:30:00Z","action":"daemon:dispatch","brief":"brief-200-real","actor":"daemon"}"#, "\n",
        );
        let tmp = tempfile_write(data.as_bytes());
        let state = load_dance_floor_from_path(&tmp);
        let briefs: Vec<&str> = state
            .events
            .iter()
            .filter_map(|e| e.brief.as_deref())
            .collect();
        assert!(!briefs.contains(&"brief-018"), "startup_repair brief-018 should be filtered");
        assert!(!briefs.contains(&"brief-019"), "startup_repair brief-019 should be filtered");
        assert!(briefs.contains(&"brief-200-real"), "real dispatch should be retained");
        // Summary line has no brief field but should still appear.
        let actions: Vec<&str> = state
            .events
            .iter()
            .filter_map(|e| e.event.as_deref())
            .collect();
        assert!(
            actions.iter().any(|a| a.contains("startup_repair_complete")),
            "startup_repair_complete summary should still be shown",
        );
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn dance_floor_caps_at_500_events() {
        use std::io::Write;
        let mut data = Vec::new();
        for i in 0..600u32 {
            writeln!(
                data,
                r#"{{"ts":"2026-04-21T17:00:00Z","actor":"conductor","event":"tick_{i}"}}"#
            )
            .unwrap();
        }
        let tmp = tempfile_write(&data);
        let state = load_dance_floor_from_path(&tmp);
        assert_eq!(state.events.len(), 500);
        // last event is tick_599
        assert!(state.events.last().unwrap().event.as_deref().unwrap_or("").contains("599"));
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn signal_type_from_filename() {
        assert!(matches!(SignalType::from_filename("escalate.json"), SignalType::Escalate));
        assert!(matches!(SignalType::from_filename("pending-merge.json"), SignalType::PendingMerge));
        assert!(matches!(SignalType::from_filename("pending-dispatch.json"), SignalType::PendingDispatch));
        assert!(matches!(SignalType::from_filename("other.json"), SignalType::Unknown(_)));
    }

    #[test]
    fn raw_signal_parses_brief_and_reason() {
        let json = r#"{"brief":"brief-004","reason":"ordering_block","ts":"2026-04-21T16:58:00Z"}"#;
        let raw: RawSignal = serde_json::from_str(json).unwrap();
        assert_eq!(raw.brief.as_deref(), Some("brief-004"));
        assert_eq!(raw.reason.as_deref(), Some("ordering_block"));
        assert_eq!(raw.ts.as_deref(), Some("2026-04-21T16:58:00Z"));
    }

    // ── test helpers ──────────────────────────────────────────────────────────

    fn tempfile_write(data: &[u8]) -> std::path::PathBuf {
        use std::io::Write;
        let path = std::env::temp_dir().join(format!(
            "hive_test_{}.jsonl",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let mut f = std::fs::File::create(&path).unwrap();
        f.write_all(data).unwrap();
        path
    }

    fn load_dance_floor_from_path(path: &std::path::Path) -> DanceFloorState {
        let file = match std::fs::File::open(path) {
            Ok(f) => f,
            Err(_) => return DanceFloorState { events: vec![] },
        };
        let reader = std::io::BufReader::new(file);
        let mut events: Vec<LogEvent> = Vec::new();
        for line in reader.lines() {
            let Ok(line) = line else { continue };
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<RawLogLine>(&line) {
                Ok(entry) => {
                    if entry.is_startup_repair() {
                        continue;
                    }
                    let ts = entry.ts_str().and_then(parse_log_ts);
                    let actor = entry.derived_actor();
                    let event_msg = entry.event.or(entry.action);
                    events.push(LogEvent { ts, actor, event: event_msg, brief: entry.brief, malformed: false, from_daemon_log: false });
                }
                Err(_) => {
                    let preview = line.chars().take(60).collect::<String>();
                    events.push(LogEvent { ts: None, actor: None, event: Some(format!("[malformed] {}", preview)), brief: None, malformed: true, from_daemon_log: false });
                }
            }
        }
        if events.len() > 500 {
            events.drain(..events.len() - 500);
        }
        DanceFloorState { events }
    }

    // ── interval_mode tests ───────────────────────────────────────────────────

    /// Build a log.jsonl temp file with given lines and return the path.
    fn make_log(lines: &[&str]) -> std::path::PathBuf {
        use std::io::Write;
        let path = std::env::temp_dir().join(format!(
            "hive_log_{}.jsonl",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let mut f = std::fs::File::create(&path).unwrap();
        for line in lines {
            writeln!(f, "{}", line).unwrap();
        }
        path
    }

    #[test]
    fn interval_mode_exposes_numeric_interval() {
        assert_eq!(IntervalMode::Active.interval_secs(), Some(120));
        assert_eq!(IntervalMode::Idle.interval_secs(), Some(900));
        assert_eq!(IntervalMode::Unknown.interval_secs(), None);
    }

    #[test]
    fn heartbeat_countdown_shows_time_until_next() {
        // Heartbeat 30s ago, idle mode (900s interval) → next in ~870s ≈ 14m.
        let hs = HiveState {
            pid: None,
            pid_alive: false,
            heartbeat_number: 3,
            last_heartbeat_ts: Some(Utc::now() - chrono::Duration::seconds(30)),
            interval_mode: IntervalMode::Idle,
            daemon_started_at: None,
            requeued_briefs: vec![],
        };
        let c = hs.heartbeat_countdown().expect("should produce countdown");
        assert!(c.starts_with("next ~"), "got: {}", c);
        assert!(c.ends_with('m'), "got: {}", c);
    }

    #[test]
    fn heartbeat_countdown_flags_overdue_in_idle_mode() {
        // Heartbeat 1000s ago in idle mode (900s interval) → overdue 100s.
        let hs = HiveState {
            pid: None,
            pid_alive: false,
            heartbeat_number: 3,
            last_heartbeat_ts: Some(Utc::now() - chrono::Duration::seconds(1000)),
            interval_mode: IntervalMode::Idle,
            daemon_started_at: None,
            requeued_briefs: vec![],
        };
        let c = hs.heartbeat_countdown().expect("should produce countdown");
        assert!(c.starts_with("overdue"), "got: {}", c);
    }

    #[test]
    fn heartbeat_countdown_says_busy_when_active_and_past_due() {
        // Same "past-due" delta, but daemon is Active (cycling). The
        // conductor is contending with non-heartbeat work; it's not stuck.
        let hs = HiveState {
            pid: None,
            pid_alive: true,
            heartbeat_number: 3,
            last_heartbeat_ts: Some(Utc::now() - chrono::Duration::seconds(300)),
            interval_mode: IntervalMode::Active,
            daemon_started_at: None,
            requeued_briefs: vec![],
        };
        let c = hs.heartbeat_countdown().expect("should produce countdown");
        assert_eq!(c, "busy cycling");
    }

    #[test]
    fn heartbeat_countdown_returns_none_when_unknown_mode() {
        let hs = HiveState {
            pid: None,
            pid_alive: false,
            heartbeat_number: 0,
            last_heartbeat_ts: Some(Utc::now()),
            interval_mode: IntervalMode::Unknown,
            daemon_started_at: None,
            requeued_briefs: vec![],
        };
        assert!(hs.heartbeat_countdown().is_none());
    }

    #[test]
    fn interval_mode_active_when_recent_non_heartbeat_event() {
        // Daemon busy: last non-heartbeat event 60s ago, heartbeat gap is huge (1800s)
        let recent = (Utc::now() - chrono::Duration::seconds(60))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let old_hb1 = (Utc::now() - chrono::Duration::seconds(1800))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let old_hb2 = (Utc::now() - chrono::Duration::seconds(900))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let path = make_log(&[
            &format!(r#"{{"ts":"{old_hb1}","actor":"conductor","event":"heartbeat_noop"}}"#),
            &format!(r#"{{"ts":"{old_hb2}","actor":"conductor","event":"heartbeat_noop"}}"#),
            &format!(r#"{{"ts":"{recent}","actor":"conductor","event":"dispatch","brief":"brief-006"}}"#),
        ]);
        let heartbeats = parse_heartbeat_timestamps(&path);
        let last_event_ts = parse_last_event_ts(&path);
        let heartbeat_gap = if heartbeats.len() >= 2 {
            let l = heartbeats[heartbeats.len() - 1];
            let p = heartbeats[heartbeats.len() - 2];
            Some((l - p).num_seconds().abs())
        } else { None };
        let now = Utc::now();
        let mode = match (last_event_ts, heartbeat_gap) {
            (Some(ts), _) if (now - ts).num_seconds() <= 300 => "Active",
            (_, Some(gap)) if gap <= 300 => "Active",
            (_, Some(_)) => "Idle",
            _ => "Unknown",
        };
        assert_eq!(mode, "Active", "expected Active: daemon has recent dispatch event");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn interval_mode_idle_when_old_event_and_small_heartbeat_gap() {
        // Daemon quiet: last non-heartbeat event 600s ago, heartbeat gap 120s
        let old_event = (Utc::now() - chrono::Duration::seconds(600))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let hb1 = (Utc::now() - chrono::Duration::seconds(240))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let hb2 = (Utc::now() - chrono::Duration::seconds(120))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let path = make_log(&[
            &format!(r#"{{"ts":"{old_event}","actor":"conductor","event":"dispatch","brief":"brief-001"}}"#),
            &format!(r#"{{"ts":"{hb1}","actor":"conductor","event":"heartbeat_noop"}}"#),
            &format!(r#"{{"ts":"{hb2}","actor":"conductor","event":"heartbeat_noop"}}"#),
        ]);
        let heartbeats = parse_heartbeat_timestamps(&path);
        let last_event_ts = parse_last_event_ts(&path);
        let heartbeat_gap = if heartbeats.len() >= 2 {
            let l = heartbeats[heartbeats.len() - 1];
            let p = heartbeats[heartbeats.len() - 2];
            Some((l - p).num_seconds().abs())
        } else { None };
        let now = Utc::now();
        let mode = match (last_event_ts, heartbeat_gap) {
            (Some(ts), _) if (now - ts).num_seconds() <= 300 => "Active",
            (_, Some(gap)) if gap <= 300 => "Active",
            (_, Some(_)) => "Idle",
            _ => "Unknown",
        };
        assert_eq!(mode, "Active", "small heartbeat gap → Active even without recent dispatch");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn interval_mode_idle_when_old_event_and_large_heartbeat_gap() {
        // Daemon quiet: last non-heartbeat event 600s ago, heartbeat gap 900s
        let old_event = (Utc::now() - chrono::Duration::seconds(600))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let hb1 = (Utc::now() - chrono::Duration::seconds(1800))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let hb2 = (Utc::now() - chrono::Duration::seconds(900))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let path = make_log(&[
            &format!(r#"{{"ts":"{old_event}","actor":"conductor","event":"dispatch","brief":"brief-001"}}"#),
            &format!(r#"{{"ts":"{hb1}","actor":"conductor","event":"heartbeat_noop"}}"#),
            &format!(r#"{{"ts":"{hb2}","actor":"conductor","event":"heartbeat_noop"}}"#),
        ]);
        let heartbeats = parse_heartbeat_timestamps(&path);
        let last_event_ts = parse_last_event_ts(&path);
        let heartbeat_gap = if heartbeats.len() >= 2 {
            let l = heartbeats[heartbeats.len() - 1];
            let p = heartbeats[heartbeats.len() - 2];
            Some((l - p).num_seconds().abs())
        } else { None };
        let now = Utc::now();
        let mode = match (last_event_ts, heartbeat_gap) {
            (Some(ts), _) if (now - ts).num_seconds() <= 300 => "Active",
            (_, Some(gap)) if gap <= 300 => "Active",
            (_, Some(_)) => "Idle",
            _ => "Unknown",
        };
        assert_eq!(mode, "Idle");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn interval_mode_unknown_when_no_data() {
        let path = make_log(&[]);
        let heartbeats = parse_heartbeat_timestamps(&path);
        let last_event_ts = parse_last_event_ts(&path);
        let heartbeat_gap: Option<i64> = if heartbeats.len() >= 2 { Some(0) } else { None };
        let now = Utc::now();
        let mode = match (last_event_ts, heartbeat_gap) {
            (Some(ts), _) if (now - ts).num_seconds() <= 300 => "Active",
            (_, Some(gap)) if gap <= 300 => "Active",
            (_, Some(_)) => "Idle",
            _ => "Unknown",
        };
        assert_eq!(mode, "Unknown");
        std::fs::remove_file(&path).ok();
    }

    // ── daemon.log parser tests ───────────────────────────────────────────────

    #[test]
    fn parse_daemon_log_worker_line() {
        // daemon.log timestamps are LOCAL time (written by shell `date` without TZ),
        // so the parser converts them through the host's local zone to UTC. We
        // can't assert an exact UTC string without pinning the test host's TZ.
        // Assert field-level correctness and that the wall-clock components
        // round-trip through a local-to-UTC-to-local conversion.
        let line = "[2026-04-21 11:26:52] WORKER: starting iteration for brief-006-playground-render-fix in worktree";
        let (ts, actor, msg) = parse_daemon_log_line(line).expect("should parse");
        assert_eq!(actor, "worker");
        assert!(msg.contains("brief-006"));
        use chrono::{Datelike, TimeZone, Timelike};
        let local = ts.with_timezone(&chrono::Local);
        assert_eq!(local.year(), 2026);
        assert_eq!(local.month(), 4);
        assert_eq!(local.day(), 21);
        assert_eq!(local.hour(), 11);
        assert_eq!(local.minute(), 26);
        assert_eq!(local.second(), 52);
        // Confirm the UTC offset equals the local offset at that instant — i.e.
        // we didn't silently skip the conversion.
        let reconstructed_naive = local.naive_local();
        let reconverted = chrono::Local.from_local_datetime(&reconstructed_naive).single().unwrap();
        assert_eq!(reconverted.with_timezone(&chrono::Utc), ts);
    }

    #[test]
    fn parse_daemon_log_validator_line() {
        let line = "[2026-04-21 11:33:17] VALIDATOR: reviewing brief-006-playground-render-fix cycle 4 (commit a3886394)";
        let (ts, actor, msg) = parse_daemon_log_line(line).expect("should parse");
        assert_eq!(actor, "validator");
        assert!(msg.contains("cycle 4"));
        use chrono::Datelike;
        assert_eq!(ts.year(), 2026);
    }

    #[test]
    fn parse_daemon_log_conductor_line() {
        let line = "[2026-04-21 11:00:00] CONDUCTOR #3: invoking (brief-006)";
        let result = parse_daemon_log_line(line);
        assert!(result.is_some());
        let (_, actor, _) = result.unwrap();
        assert_eq!(actor, "conductor");
    }

    #[test]
    fn parse_daemon_log_skips_git_output() {
        // Git push output has no structured prefix
        let line = "   01b8428..80d4617  brief-001-vla-spike -> brief-001-vla-spike";
        assert!(parse_daemon_log_line(line).is_none());
    }

    #[test]
    fn parse_daemon_log_skips_unrecognized_actor() {
        let line = "[2026-04-21 11:00:00] UNKNOWN_THING: some message";
        assert!(parse_daemon_log_line(line).is_none());
    }

    #[test]
    fn latest_daemon_log_ts_returns_newest_parseable_timestamp() {
        let path = std::env::temp_dir().join(format!(
            "hive_daemon_latest_{}.log",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let data = "\
[2026-04-22 10:00:00] WORKER: starting for brief-foo
[2026-04-22 10:15:00] VALIDATOR: reviewing cycle 2
some non-bracket junk line
[2026-04-22 10:30:00] WORKER: iteration complete
";
        std::fs::write(&path, data).unwrap();
        let ts = latest_daemon_log_ts(&path).expect("should find one");
        use chrono::{Datelike, Timelike};
        let local = ts.with_timezone(&chrono::Local);
        assert_eq!(local.hour(), 10);
        assert_eq!(local.minute(), 30);
        assert_eq!(local.day(), 22);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn latest_daemon_log_ts_returns_none_when_no_parseable_lines() {
        let path = std::env::temp_dir().join(format!(
            "hive_daemon_empty_{}.log",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::write(&path, "random junk\nno timestamps here\n").unwrap();
        assert!(latest_daemon_log_ts(&path).is_none());
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn daemon_log_dampening_collapses_same_actor_same_brief_within_30s() {
        // Two WORKER lines for same brief, 10s apart → should collapse to 1
        let _base = chrono::NaiveDateTime::parse_from_str("2026-04-21 11:00:00", "%Y-%m-%d %H:%M:%S").unwrap();
        // Put both in the far future so max_age_secs doesn't filter them out
        // Instead use a very large max_age
        let data = "[2026-04-21 11:00:00] WORKER: starting for brief-006-foo in worktree\n[2026-04-21 11:00:10] WORKER: model loaded for brief-006-foo\n";
        let path = std::env::temp_dir().join(format!(
            "hive_daemon_test_{}.log",
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().subsec_nanos()
        ));
        std::fs::write(&path, data).unwrap();
        // Use huge max_age so the 2026 timestamps aren't filtered out
        let events = load_daemon_log_events(&path, 99_999_999);
        // Two lines with same actor+brief within 30s should collapse
        assert_eq!(events.len(), 1, "expected 1 collapsed entry, got {}", events.len());
        assert!(events[0].event.as_deref().unwrap_or("").contains("model loaded"));
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn extract_brief_from_message_finds_brief_id() {
        let msg = "starting iteration for brief-006-playground-render-fix in worktree";
        assert_eq!(extract_brief_from_message(msg).as_deref(), Some("brief-006-playground-render-fix"));
    }

    #[test]
    fn extract_brief_from_message_returns_none_when_absent() {
        let msg = "daemon started, no briefs active";
        assert!(extract_brief_from_message(msg).is_none());
    }

    // ── escalate payload + queue discovery tests ──────────────────────────────

    #[test]
    fn escalate_payload_parses_full_shape() {
        let json = r#"{
            "ts": "2026-04-21T21:22:00Z",
            "brief": "brief-008",
            "trigger": "byte_identical_screenshots",
            "summary": "Worker claims the bug was already fixed.",
            "key_facts": ["fact one", "fact two"],
            "evaluation": ".loop/evaluations/brief-008.md",
            "screenshot_to_review": "wiki/briefs/cards/brief-008/cycle-4.png",
            "options": [
                {"id": "A_merge", "action": "merge it", "when_right": "arm visible", "cost_if_wrong": "ship no-op"},
                {"id": "B_reject", "action": "reject", "when_right": "arm not visible", "cost_if_wrong": "waste cycle"}
            ],
            "scav_recommendation": "C then A_or_B",
            "scav_reasoning": "cheapest way to collapse ambiguity",
            "anti_pattern_guardrail": "do not auto-resolve by diagnostic pattern",
            "what_you_should_feel": "vindication or sinking feeling"
        }"#;
        let raw: RawSignal = serde_json::from_str(json).unwrap();
        assert_eq!(raw.summary.as_deref(), Some("Worker claims the bug was already fixed."));
        let kf = raw.key_facts.clone().unwrap();
        assert_eq!(kf.len(), 2);
        let opts = raw.options.clone().unwrap();
        assert_eq!(opts.len(), 2);
        assert_eq!(opts[0].id.as_deref(), Some("A_merge"));
        assert_eq!(raw.scav_recommendation.as_deref(), Some("C then A_or_B"));
        assert_eq!(
            raw.what_you_should_feel.as_deref(),
            Some("vindication or sinking feeling")
        );
    }

    #[test]
    fn escalate_option_accepts_new_schema() {
        let json = r#"{
            "id": "A",
            "label": "Restart daemon, dispatch brief-011 now",
            "cost": "Mattie runs loop daemon restart",
            "pros": ["First real exercise of flow-v2", "brief-011 unblocks brief-012"],
            "cons": ["Daemon restart needed separately", "Another sonnet run"]
        }"#;
        let raw: RawEscalateOption = serde_json::from_str(json).unwrap();
        let opt = EscalateOption::from(raw);
        assert_eq!(opt.id.as_deref(), Some("A"));
        assert!(opt.label.is_some());
        assert!(opt.action.is_none());
        assert_eq!(opt.pros.len(), 2);
        assert_eq!(opt.cons.len(), 2);
        assert_eq!(opt.headline(), opt.label.as_deref());
    }

    #[test]
    fn escalate_option_headline_falls_back_to_action() {
        let opt = EscalateOption {
            id: Some("A".into()),
            label: None,
            action: Some("merge it".into()),
            when_right: None,
            cost_if_wrong: None,
            cost: None,
            pros: vec![],
            cons: vec![],
            estimated_time: None,
            outcome: None,
            when_to_pick: None,
        };
        assert_eq!(opt.headline(), Some("merge it"));
    }

    #[test]
    fn escalate_option_accepts_brief_011_schema() {
        // brief-011's options had estimated_time + outcome on some and
        // when_to_pick on others.
        let json = r#"{
            "action": "open http://localhost:3000/playground/get-started, verify…, then: loop approve brief-011",
            "estimated_time": "~60s",
            "outcome": "daemon merges next tick"
        }"#;
        let raw: RawEscalateOption = serde_json::from_str(json).unwrap();
        let opt = EscalateOption::from(raw);
        assert_eq!(opt.estimated_time.as_deref(), Some("~60s"));
        assert_eq!(opt.outcome.as_deref(), Some("daemon merges next tick"));
        assert!(opt.when_to_pick.is_none());
    }

    #[test]
    fn recommended_estimated_time_prefers_rec_match() {
        let payload = SignalPayload {
            scav_recommendation: Some("A_approve — every check green".into()),
            options: vec![
                EscalateOption {
                    id: Some("A_approve".into()),
                    label: None,
                    action: None,
                    when_right: None,
                    cost_if_wrong: None,
                    cost: None,
                    pros: vec![],
                    cons: vec![],
                    estimated_time: Some("~60s".into()),
                    outcome: None,
                    when_to_pick: None,
                },
                EscalateOption {
                    id: Some("B_fix".into()),
                    label: None,
                    action: None,
                    when_right: None,
                    cost_if_wrong: None,
                    cost: None,
                    pros: vec![],
                    cons: vec![],
                    estimated_time: Some("15-30 min".into()),
                    outcome: None,
                    when_to_pick: None,
                },
            ],
            ..Default::default()
        };
        assert_eq!(payload.recommended_estimated_time(), Some("~60s"));
    }

    #[test]
    fn recommended_estimated_time_falls_back_to_first() {
        let payload = SignalPayload {
            scav_recommendation: None,
            options: vec![
                EscalateOption {
                    id: Some("X".into()),
                    label: None,
                    action: None,
                    when_right: None,
                    cost_if_wrong: None,
                    cost: None,
                    pros: vec![],
                    cons: vec![],
                    estimated_time: Some("first!".into()),
                    outcome: None,
                    when_to_pick: None,
                },
            ],
            ..Default::default()
        };
        assert_eq!(payload.recommended_estimated_time(), Some("first!"));
    }

    #[test]
    fn learnings_state_pick_wraps_and_empty_returns_none() {
        let empty = LearningsState { items: vec![] };
        assert!(empty.pick(0).is_none());
        assert!(empty.pick(5).is_none());

        let three = LearningsState {
            items: vec!["a".into(), "b".into(), "c".into()],
        };
        assert_eq!(three.pick(0), Some("a"));
        assert_eq!(three.pick(1), Some("b"));
        assert_eq!(three.pick(2), Some("c"));
        assert_eq!(three.pick(3), Some("a"));
        assert_eq!(three.pick(7), Some("b"));
    }

    #[test]
    fn recent_finished_dedupes_and_sorts_newest_first() {
        // Simulates the real shape of running.json.history: each brief can
        // appear twice — once with approved_at, once with merged_at. Dedupe
        // by brief id and keep the latest timestamp.
        let history = vec![
            HistoryEntryRaw {
                brief: "brief-001".into(),
                merged_at: None,
                merge_sha: None,
                approved_at: Some("2026-04-20T22:23:00Z".into()),
            },
            HistoryEntryRaw {
                brief: "brief-001".into(),
                merged_at: Some("2026-04-20T22:26:54Z".into()),
                merge_sha: None,
                approved_at: None,
            },
            HistoryEntryRaw {
                brief: "brief-012".into(),
                merged_at: Some("2026-04-22T18:10:00Z".into()),
                merge_sha: None,
                approved_at: None,
            },
            HistoryEntryRaw {
                brief: "brief-013".into(),
                merged_at: Some("2026-04-22T14:23:30Z".into()),
                merge_sha: None,
                approved_at: None,
            },
        ];
        let finished = recent_finished(&history);
        let ids: Vec<&str> = finished.iter().map(|f| f.brief.as_str()).collect();
        assert_eq!(ids, vec!["brief-012", "brief-013", "brief-001"]);
        // brief-001's finished_at should be the merge timestamp, not the
        // earlier approval timestamp.
        let b001 = finished.iter().find(|f| f.brief == "brief-001").unwrap();
        assert_eq!(
            b001.finished_at.unwrap().to_rfc3339(),
            "2026-04-20T22:26:54+00:00"
        );
    }

    #[test]
    fn recent_finished_caps_at_limit() {
        let history: Vec<HistoryEntryRaw> = (0..20)
            .map(|i| HistoryEntryRaw {
                brief: format!("brief-{:03}", i),
                merged_at: Some(format!(
                    "2026-04-22T{:02}:00:00Z",
                    i.min(23)
                )),
                merge_sha: None,
                approved_at: None,
            })
            .collect();
        let finished = recent_finished(&history);
        assert_eq!(finished.len(), RECENTLY_FINISHED_LIMIT);
        // Newest first
        assert_eq!(finished[0].brief, "brief-019");
        assert_eq!(finished[4].brief, "brief-015");
    }

    #[test]
    fn pending_reason_needs_human_classifies_correctly() {
        assert!(PendingReason::Escalate.needs_human());
        assert!(PendingReason::Unknown.needs_human());
        assert!(PendingReason::AwaitingReview.needs_human());
        assert!(!PendingReason::PendingMerge.needs_human());
        assert!(!PendingReason::PendingDispatch.needs_human());
        assert!(!PendingReason::AwaitingEval.needs_human());
    }

    #[test]
    fn awaiting_review_parses_from_running_json() {
        let json = r#"{
            "active": [],
            "completed_pending_eval": [],
            "awaiting_review": [
                {"brief": "brief-016", "branch": "brief-016-wiki-cloudflare-deploy", "completed_at": "2026-04-22T06:45:00Z"}
            ],
            "history": []
        }"#;
        let parsed: RunningJson = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.awaiting_review.len(), 1);
        assert_eq!(parsed.awaiting_review[0].brief, "brief-016");
    }

    #[test]
    fn awaiting_review_label_is_correct() {
        assert_eq!(PendingReason::AwaitingReview.label(), "awaiting review");
    }

    #[test]
    fn signal_display_label_uses_trigger_when_brief_null() {
        let signal = Signal {
            signal_type: SignalType::Escalate,
            brief: None,
            reason: None,
            ts: None,
            filename: "escalate.json".into(),
            payload: SignalPayload {
                trigger: Some("next_dispatch_decision_post_brief009_merge".into()),
                ..Default::default()
            },
        };
        let label = signal.display_label();
        assert!(label.starts_with("[next_dispatch_decision"), "got: {}", label);
        assert!(label.ends_with(']'));
    }

    #[test]
    fn signal_display_label_falls_back_to_filename_when_both_missing() {
        let signal = Signal {
            signal_type: SignalType::Escalate,
            brief: None,
            reason: None,
            ts: None,
            filename: "escalate.json".into(),
            payload: SignalPayload::default(),
        };
        assert_eq!(signal.display_label(), "[escalate]");
    }

    #[test]
    fn signal_display_reason_falls_back_to_summary() {
        let signal = Signal {
            signal_type: SignalType::Escalate,
            brief: Some("brief-009".into()),
            reason: None,
            ts: None,
            filename: "escalate.json".into(),
            payload: SignalPayload {
                summary: Some("a long-form summary".into()),
                ..Default::default()
            },
        };
        assert_eq!(signal.display_reason(), Some("a long-form summary"));
    }

    #[test]
    fn signal_display_label_prefers_brief_when_both_present() {
        let signal = Signal {
            signal_type: SignalType::Escalate,
            brief: Some("brief-009-foo".into()),
            reason: None,
            ts: None,
            filename: "escalate.json".into(),
            payload: SignalPayload {
                trigger: Some("some_trigger".into()),
                ..Default::default()
            },
        };
        assert_eq!(signal.display_label(), "brief-009-foo");
    }

    #[test]
    fn signal_payload_has_content_reflects_emptiness() {
        let empty = SignalPayload::default();
        assert!(!empty.has_content());
        let with_summary = SignalPayload { summary: Some("a thing".to_string()), ..Default::default() };
        assert!(with_summary.has_content());
    }

    #[test]
    fn pending_eval_parses_from_running_json() {
        let json = r#"{
            "active": [],
            "completed_pending_eval": [
                {"brief": "brief-008", "branch": "brief-008", "completed_at": "2026-04-21T21:44:57Z"}
            ],
            "history": [{"brief": "brief-001"}]
        }"#;
        let parsed: RunningJson = serde_json::from_str(json).unwrap();
        assert_eq!(parsed.completed_pending_eval.len(), 1);
        assert_eq!(parsed.completed_pending_eval[0].brief, "brief-008");
        assert_eq!(parsed.history.len(), 1);
    }

    #[test]
    fn brief_sort_key_orders_numerically_within_type() {
        let mut briefs = vec![
            "brief-010-nav",
            "brief-002-playground",
            "brief-009-flow",
            "brief-100-future",
        ];
        briefs.sort_by_key(|b| brief_sort_key(b));
        assert_eq!(
            briefs,
            vec![
                "brief-002-playground",
                "brief-009-flow",
                "brief-010-nav",
                "brief-100-future"
            ]
        );
    }

    #[test]
    fn brief_sort_key_groups_by_type_prefix() {
        // Mixed work-unit types cluster by prefix (audits together, briefs
        // together, etc). Within a prefix, the numeric suffix orders briefs;
        // date-stamped ids fall through to lex order (which sorts
        // chronologically because YYYY-MM-DD-N is already well-formed).
        let mut items = vec![
            "brief-017-pi0-integration",
            "audit-2026-04-22-01",
            "brief-016-wiki-deploy",
            "capture-2026-04-22-01",
            "audit-2026-04-21-02",
        ];
        items.sort_by_key(|b| brief_sort_key(b));
        assert_eq!(
            items,
            vec![
                "audit-2026-04-21-02",
                "audit-2026-04-22-01",
                "brief-016-wiki-deploy",
                "brief-017-pi0-integration",
                "capture-2026-04-22-01",
            ]
        );
    }

    #[test]
    fn parse_cycle_budget_uses_max_integer_for_dual_bound_briefs() {
        // brief-011 in the wild: first integer is 8 (soft cap), but the
        // section names 8-10 as the real upper range. Expect 10.
        let content = "## Budget\n\n8 cycles. Cycle 7 is the latest that a fix should be shipping; cycles 8-10 are polish + baseline + closeout. If cycle 7 doesn't have the nav visible end-to-end in playground, escalate.\n\n## Anti-patterns\n\nDon't.\n";
        let tmp = std::env::temp_dir().join(format!(
            "hive_budget_dualbound_{}.md",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::write(&tmp, content).unwrap();
        assert_eq!(parse_cycle_budget(&tmp), Some(10));
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn parse_cycle_budget_stops_at_next_section() {
        // Integers in the next section should NOT influence the max.
        let content = "## Budget\n\n6 cycles cap.\n\n## Anti-patterns\n\nDon't push past 99 files.\n";
        let tmp = std::env::temp_dir().join(format!(
            "hive_budget_scoped_{}.md",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::write(&tmp, content).unwrap();
        assert_eq!(parse_cycle_budget(&tmp), Some(6));
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn parse_cycle_budget_extracts_various_prose_forms() {
        let cases = [
            ("## Budget\n\n6 cycles cap. 5 expected.\n", Some(6)),
            ("## Budget\n\n10 cycles cap. Mattie not worried.\n", Some(10)),
            ("## Budget\n\n12 cycles max. If cycle 10…\n", Some(12)),
            ("## Budget\n\n6 cycles.\n", Some(6)),
            ("## Budget\n\n5 cycles soft cap.\n", Some(5)),
            ("## Budget\n\n7 cycles. If cycle 6 still shows…\n", Some(7)),
            ("## Budget\n\n12 cycles cap. 10 expected.\n", Some(12)),
            // blank lines before the content
            ("## Budget\n\n\n\n8 cycles max.\n", Some(8)),
            // no budget section at all
            ("## Something Else\n\n10 things.\n", None),
            // budget section but no parseable integer
            ("## Budget\n\nAs long as it takes.\n", None),
        ];
        for (content, expected) in cases {
            let tmp = std::env::temp_dir().join(format!(
                "hive_budget_{}.md",
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .subsec_nanos()
            ));
            std::fs::write(&tmp, content).unwrap();
            let got = parse_cycle_budget(&tmp);
            std::fs::remove_file(&tmp).ok();
            assert_eq!(got, expected, "for content: {}", content);
        }
    }

    #[test]
    fn latest_validator_cycle_finds_max_in_main_dir() {
        let dir = std::env::temp_dir().join(format!(
            "hive_reviews_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        for name in &[
            "brief-009-foo-cycle-1.md",
            "brief-009-foo-cycle-3.md",
            "brief-009-foo-cycle-7.md",
            "brief-009-foo-cycle-2.md",
            "brief-010-bar-cycle-5.md",
            "README.md",
        ] {
            std::fs::write(dir.join(name), "").unwrap();
        }
        assert_eq!(
            latest_validator_cycle(&dir, None, "brief-009-foo"),
            Some(7)
        );
        assert_eq!(
            latest_validator_cycle(&dir, None, "brief-011-missing"),
            None
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn latest_validator_cycle_prefers_max_across_main_and_worktree() {
        // Main has stale cycles 1-3 from merged briefs; worktree has in-progress
        // cycles 4-6. Expected: max across both = 6.
        let base = std::env::temp_dir().join(format!(
            "hive_reviews_split_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let main_dir = base.join("main");
        let wt = base.join("worktree");
        let wt_reviews = wt.join(".loop/modules/validator/state/reviews");
        std::fs::create_dir_all(&main_dir).unwrap();
        std::fs::create_dir_all(&wt_reviews).unwrap();
        for n in 1..=3 {
            std::fs::write(
                main_dir.join(format!("brief-011-foo-cycle-{}.md", n)),
                "",
            )
            .unwrap();
        }
        for n in 4..=6 {
            std::fs::write(
                wt_reviews.join(format!("brief-011-foo-cycle-{}.md", n)),
                "",
            )
            .unwrap();
        }
        assert_eq!(
            latest_validator_cycle(&main_dir, Some(&wt), "brief-011-foo"),
            Some(6)
        );

        // If only worktree has reviews (fresh in-progress brief with no merged history)
        let fresh = base.join("fresh_main");
        std::fs::create_dir_all(&fresh).unwrap();
        assert_eq!(
            latest_validator_cycle(&fresh, Some(&wt), "brief-011-foo"),
            Some(6)
        );
        std::fs::remove_dir_all(&base).ok();
    }

    #[test]
    fn discover_draft_briefs_surfaces_cards_not_queued() {
        let cards = std::env::temp_dir().join(format!(
            "hive_cards_test_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&cards).unwrap();

        // brief-005 has full index.md (simulates a complete, queued card)
        let b5 = cards.join("brief-005-beehive");
        std::fs::create_dir_all(&b5).unwrap();
        std::fs::write(b5.join("index.md"), "brief body").unwrap();

        // brief-010 has only feedback.md (scratch, no brief yet)
        let b10 = cards.join("brief-010-api-v0-1");
        std::fs::create_dir_all(&b10).unwrap();
        std::fs::write(b10.join("feedback.md"), "feedback notes").unwrap();

        // brief-012 has index.md but (in caller's view) no symlink (simulates
        // a drafted-but-not-queued brief).
        let b12 = cards.join("brief-012-proposed");
        std::fs::create_dir_all(&b12).unwrap();
        std::fs::write(b12.join("index.md"), "brief body").unwrap();

        let mut exclude = HashSet::new();
        exclude.insert("brief-005-beehive".to_string()); // already queued/merged

        let drafts = discover_draft_briefs(&cards, &exclude);
        let ids: Vec<_> = drafts.iter().map(|d| d.brief.as_str()).collect();
        assert_eq!(ids, vec!["brief-010-api-v0-1", "brief-012-proposed"]);
        // has_index reflects the structural truth
        assert!(!drafts[0].has_index, "brief-010 has no index.md");
        assert!(drafts[1].has_index, "brief-012 has index.md");

        std::fs::remove_dir_all(&cards).ok();
    }

    // ── parse_goals_priority ──────────────────────────────────────────────

    fn tmp_goals_path(tag: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "hive_goals_{}_{}.md",
            tag,
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ))
    }

    #[test]
    fn parse_goals_priority_extracts_ordered_ids() {
        let p = tmp_goals_path("ordered");
        std::fs::write(
            &p,
            "# Goals\n\
             \n\
             ## Done\n\
             - **brief-014-simple-loop-hardening** — merged.\n\
             \n\
             ## Queued next\n\
             \n\
             1. **brief-017-pi0-real-integration** — next up.\n\
             2. **brief-020-text-to-action-playground** — Jon feedback.\n\
             3. **brief-016-wiki-cloudflare-deploy** — ops work.\n\
             \n\
             ## Carry-overs\n\
             - **brief-099-ignore-me** — should not appear.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(
            ids,
            vec![
                "brief-017-pi0-real-integration",
                "brief-020-text-to-action-playground",
                "brief-016-wiki-cloudflare-deploy",
            ]
        );
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_handles_paren_style() {
        let p = tmp_goals_path("paren");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             1. **brief-017 (pi0 real integration)** — next up.\n\
             2. **brief-020 (text-to-action)** — later.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(ids, vec!["brief-017", "brief-020"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_returns_empty_on_missing_heading() {
        let p = tmp_goals_path("no-heading");
        std::fs::write(
            &p,
            "# Goals\n\n## Done\n- **brief-001-a** — merged.\n\n## Something Else\n- **brief-002-b** — prose only.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert!(ids.is_empty());
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_returns_empty_on_missing_file() {
        let p = tmp_goals_path("missing");
        // Don't write the file.
        let ids = parse_goals_priority(&p);
        assert!(ids.is_empty());
    }

    #[test]
    fn parse_goals_priority_stops_at_next_section() {
        let p = tmp_goals_path("stop");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             1. **brief-017-a** — first.\n\
             \n\
             ## Carry-overs\n\
             \n\
             - **brief-099-nope** — after section boundary, ignored.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(ids, vec!["brief-017-a"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_ignores_prose_without_ids() {
        let p = tmp_goals_path("prose");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             Note: the order below is a priority list set by Mattie.\n\
             \n\
             1. **brief-017-a** — real item.\n\
             Some prose follows, and a sub-bullet.\n\
             2. **brief-020-b** — second real item.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(ids, vec!["brief-017-a", "brief-020-b"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_skips_non_id_leaders() {
        // Items that start with a prose label (`**Runway**`, `**Future: …**`)
        // share the list-marker shape with real priorities, but they don't
        // name one. Regression guard for the live goals.md bug where items
        // 6–12 leaked into the priority list.
        let p = tmp_goals_path("non-id-leaders");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             1. **brief-017 (pi0)** — real priority.\n\
             2. **Runway** (`wiki/briefs/runway.md`) — pre-filed scope for brief-020. Not priorities.\n\
             3. **Future: tech-debt framework** — flagged. Likely brief-021-ish.\n\
             4. **brief-019-x** — second real priority.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(ids, vec!["brief-017", "brief-019-x"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_ignores_prose_mentions_on_continuation_lines() {
        // List items often span multiple lines. Brief mentions on
        // continuation lines shouldn't create phantom priorities.
        let p = tmp_goals_path("continuation");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             1. **brief-017-a** — first.\n\
                Depends-on brief-015 (merged). Notes continue.\n\
             2. **brief-020-b** — second.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(ids, vec!["brief-017-a", "brief-020-b"]);
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn parse_goals_priority_accepts_audit_and_capture_prefixes() {
        let p = tmp_goals_path("prefixes");
        std::fs::write(
            &p,
            "## Queued next\n\
             \n\
             1. **audit-2026-04-22-01** — audit item.\n\
             2. **capture-2026-04-22-01** — capture item.\n\
             3. **brief-017-a** — regular brief.\n",
        )
        .unwrap();
        let ids = parse_goals_priority(&p);
        assert_eq!(
            ids,
            vec![
                "audit-2026-04-22-01",
                "capture-2026-04-22-01",
                "brief-017-a",
            ]
        );
        std::fs::remove_file(&p).ok();
    }

    #[test]
    fn priority_matches_short_form_against_full_slug() {
        assert!(priority_matches(
            "brief-017",
            "brief-017-pi0-real-integration"
        ));
        assert!(priority_matches(
            "brief-017-pi0-real-integration",
            "brief-017-pi0-real-integration"
        ));
        // Must be hyphen-bounded — `brief-01` isn't a match for `brief-017-…`.
        assert!(!priority_matches("brief-01", "brief-017-foo"));
        // Different numbers don't match.
        assert!(!priority_matches("brief-018", "brief-017-foo"));
    }

    // ── parse_requeued_goals_md tests (brief-102) ─────────────────────────────

    fn write_goals(content: &str) -> std::path::PathBuf {
        use std::io::Write;
        let path = std::env::temp_dir().join(format!(
            "goals_{}_{}.md",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let mut f = std::fs::File::create(&path).unwrap();
        write!(f, "{}", content).unwrap();
        path
    }

    #[test]
    fn requeued_parse_returns_empty_when_no_marker() {
        let path = write_goals(
            "## Queued next\n\
             1. brief-010-foo — normal queued brief.\n\
             2. brief-011-bar — another one.\n",
        );
        let merged = HashSet::new();
        let result = parse_requeued_goals_md(&path, &merged);
        assert!(result.is_empty(), "expected empty, got: {:?}", result);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_finds_single_blocked_entry() {
        let path = write_goals(
            "## Queued next\n\
             1. brief-099-some-brief — re-queued after reject.\n\
               **Blocked-on:** brief-100\n\
             2. brief-010-other — normal.\n",
        );
        let merged = HashSet::new();
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 1, "expected 1 entry");
        assert_eq!(result[0].brief_id, "brief-099-some-brief");
        assert_eq!(result[0].blocked_on, "brief-100");
        assert!(!result[0].ready_to_dispatch);
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_finds_multiple_blocked_entries() {
        let path = write_goals(
            "## Queued next\n\
             1. brief-099-foo — blocked entry one.\n\
               **Blocked-on:** brief-100\n\
             2. brief-088-bar — blocked entry two.\n\
               **Blocked-on:** brief-095\n\
             3. brief-010-other — normal.\n",
        );
        let merged = HashSet::new();
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 2);
        assert_eq!(result[0].brief_id, "brief-099-foo");
        assert_eq!(result[1].brief_id, "brief-088-bar");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_flags_ready_when_blocker_merged() {
        let path = write_goals(
            "## Queued next\n\
             1. brief-099-foo — waiting for brief-100.\n\
               **Blocked-on:** brief-100\n",
        );
        let mut merged = HashSet::new();
        merged.insert("brief-100".to_string());
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 1);
        assert!(result[0].ready_to_dispatch, "blocker merged → should be ready");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_returns_empty_on_missing_file() {
        let path = std::path::PathBuf::from("/nonexistent/goals.md");
        let merged = HashSet::new();
        let result = parse_requeued_goals_md(&path, &merged);
        assert!(result.is_empty());
    }

    #[test]
    fn requeued_parse_ready_when_blocker_is_full_slug_of_truncated_goals_id() {
        // goals.md says **Blocked-on:** brief-101 (short)
        // running.json history has "brief-101-code-change-review-shape" (full slug)
        // brief_id_matches must bridge this: truncated goals ID matches full history ID.
        let path = write_goals(
            "## Queued next\n\
             1. brief-103-agent-metrics — waiting for brief-101.\n\
               **Blocked-on:** brief-101\n",
        );
        let mut merged = HashSet::new();
        merged.insert("brief-101-code-change-review-shape".to_string()); // full slug in history
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 1);
        assert!(result[0].ready_to_dispatch, "full-slug history entry must clear short blocked-on id");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_ready_when_blocker_truncated_history_matches_full_goals_id() {
        // goals.md says **Blocked-on:** brief-102-loop-status-blocked-state-surface (full)
        // running.json history has "brief-102" (truncated, as written by backfill)
        let path = write_goals(
            "## Queued next\n\
             1. brief-103-agent-metrics — waiting for brief-102.\n\
               **Blocked-on:** brief-102-loop-status-blocked-state-surface\n",
        );
        let mut merged = HashSet::new();
        merged.insert("brief-102".to_string()); // truncated in history
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 1);
        assert!(result[0].ready_to_dispatch, "truncated history entry must clear full-slug blocked-on id");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn requeued_parse_truncates_at_char_boundary_with_em_dash() {
        // Regression: byte-indexed `&s[..77]` panicked when the boundary landed
        // inside a multi-byte UTF-8 char. Mattie's standard brief-description
        // shape `**brief-NNN (subject)** — **status:** ...` reliably puts the
        // second em-dash near byte 77 for subjects ~50–65 chars long.
        // The fix: char-counted truncation (`.chars().take(77).collect()`).
        let path = write_goals(
            "## Queued next\n\
             1. brief-107-daemon-producer-state-cleanup-on-merge — dispatchable now — harness brief touching daemon contract.\n\
               **Blocked-on:** brief-100\n",
        );
        let merged = HashSet::new();
        let result = parse_requeued_goals_md(&path, &merged);
        assert_eq!(result.len(), 1, "must parse without panic");
        assert!(
            result[0].description.ends_with('…'),
            "long description must be truncated with ellipsis; got: {:?}",
            result[0].description
        );
        // Truncated string must itself be valid UTF-8.
        let _ = result[0].description.chars().count();
        std::fs::remove_file(&path).ok();
    }

    // ── brief_id_matches ──────────────────────────────────────────────────

    #[test]
    fn brief_id_matches_exact() {
        assert!(brief_id_matches("brief-101", "brief-101"));
        assert!(brief_id_matches("brief-101-slug", "brief-101-slug"));
    }

    #[test]
    fn brief_id_matches_truncated_vs_full() {
        assert!(brief_id_matches("brief-101-code-change-review-shape", "brief-101"));
        assert!(brief_id_matches("brief-101", "brief-101-code-change-review-shape"));
    }

    #[test]
    fn brief_id_matches_no_false_positives() {
        // "brief-1010" must NOT match "brief-101"
        assert!(!brief_id_matches("brief-1010", "brief-101"));
        assert!(!brief_id_matches("brief-101", "brief-1010"));
        // Completely different numbers
        assert!(!brief_id_matches("brief-102", "brief-101"));
    }

    // ── read_brief_progress tests ─────────────────────────────────────────────

    fn make_progress_worktree(content: &str) -> std::path::PathBuf {
        use std::io::Write;
        let dir = std::env::temp_dir().join(format!(
            "hive_progress_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        let state_dir = dir.join(".loop/state");
        std::fs::create_dir_all(&state_dir).unwrap();
        let mut f = std::fs::File::create(state_dir.join("progress.json")).unwrap();
        write!(f, "{}", content).unwrap();
        dir
    }

    #[test]
    fn read_brief_progress_well_formed() {
        // iteration=2, tasks_completed=[a,b], tasks_remaining=[c,d,e,f]
        // → cycle 2/6, last_task "b", 4 remaining
        let root = make_progress_worktree(
            r#"{"iteration":2,"tasks_completed":["a","b"],"tasks_remaining":["c","d","e","f"],"status":"running"}"#,
        );
        let p = read_brief_progress(&root).expect("should parse well-formed progress.json");
        assert_eq!(p.iteration, 2);
        assert_eq!(p.total, 6);
        assert_eq!(p.last_task, "b");
        assert_eq!(p.tasks_remaining, 4);
        assert_eq!(p.status, "running");
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn read_brief_progress_missing_file() {
        // No progress.json present → None, no panic.
        let dir = std::env::temp_dir().join(format!(
            "hive_noprogress_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let result = read_brief_progress(&dir);
        assert!(result.is_none(), "missing progress.json must yield None");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn read_brief_progress_iteration_2026_failsafe() {
        // iteration=2026 exceeds sanity bound → None.
        // This test directly proves the symptom-2 class (year-as-cycle-count)
        // cannot reach the display layer.
        let root = make_progress_worktree(
            r#"{"iteration":2026,"tasks_completed":[],"tasks_remaining":[],"status":"running"}"#,
        );
        let result = read_brief_progress(&root);
        assert!(
            result.is_none(),
            "iteration=2026 must trigger fail-safe (None), not render '2026 cycles'"
        );
        std::fs::remove_dir_all(&root).ok();
    }

    #[test]
    fn discover_queued_from_cards_returns_only_queued_status() {
        let dir = std::env::temp_dir().join(format!(
            "hive_cards_queued_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        for (id, status) in &[
            ("brief-010-queued-a", "queued"),
            ("brief-011-queued-b", "queued"),
            ("brief-012-active", "active"),
            ("brief-013-merged", "merged"),
            ("brief-014-rejected", "rejected"),
            ("brief-015-not-doing", "not-doing"),
        ] {
            let card_dir = dir.join(id);
            std::fs::create_dir_all(&card_dir).unwrap();
            std::fs::write(
                card_dir.join("index.md"),
                format!("---\nStatus: {status}\n---\n# {id}\n"),
            ).unwrap();
        }
        let missing_goals = dir.join("nonexistent.md");
        let result = discover_queued_from_cards(&dir, &missing_goals);
        let ids: Vec<&str> = result.iter().map(|q| q.brief.as_str()).collect();
        assert_eq!(ids, vec!["brief-010-queued-a", "brief-011-queued-b"]);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn discover_queued_from_cards_orders_by_goals_priority() {
        let dir = std::env::temp_dir().join(format!(
            "hive_cards_priority_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        for id in &["brief-108-z", "brief-109-a", "brief-075-b"] {
            let card_dir = dir.join(id);
            std::fs::create_dir_all(&card_dir).unwrap();
            std::fs::write(card_dir.join("index.md"), "---\nStatus: queued\n---\n").unwrap();
        }
        // goals: 109 first, 075 second, 108 third
        let goals = dir.join("goals.md");
        std::fs::write(
            &goals,
            "## Queued next\n\n1. brief-109-a\n2. brief-075-b\n3. brief-108-z\n",
        ).unwrap();
        let result = discover_queued_from_cards(&dir, &goals);
        let ids: Vec<&str> = result.iter().map(|q| q.brief.as_str()).collect();
        assert_eq!(ids, vec!["brief-109-a", "brief-075-b", "brief-108-z"]);
        assert_eq!(result[0].priority_rank, Some(0));
        assert_eq!(result[1].priority_rank, Some(1));
        assert_eq!(result[2].priority_rank, Some(2));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn discover_recently_finished_from_cards_returns_only_merged() {
        let dir = std::env::temp_dir().join(format!(
            "hive_cards_merged_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        for (id, status) in &[
            ("brief-020-merged-a", "merged"),
            ("brief-021-merged-b", "merged"),
            ("brief-022-queued", "queued"),
            ("brief-023-rejected", "rejected"),
        ] {
            let card_dir = dir.join(id);
            std::fs::create_dir_all(&card_dir).unwrap();
            std::fs::write(
                card_dir.join("index.md"),
                format!("---\nStatus: {status}\n---\n"),
            ).unwrap();
        }
        let result = discover_recently_finished_from_cards(&dir);
        let ids: Vec<&str> = result.iter().map(|r| r.brief.as_str()).collect();
        assert!(ids.contains(&"brief-020-merged-a"), "merged brief must appear");
        assert!(ids.contains(&"brief-021-merged-b"), "merged brief must appear");
        assert!(!ids.contains(&"brief-022-queued"), "queued brief must not appear");
        assert!(!ids.contains(&"brief-023-rejected"), "rejected brief must not appear");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn discover_recently_finished_from_cards_caps_at_limit() {
        let dir = std::env::temp_dir().join(format!(
            "hive_cards_merged_cap_{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .subsec_nanos()
        ));
        for i in 0..10 {
            let id = format!("brief-0{:02}-merged", i);
            let card_dir = dir.join(&id);
            std::fs::create_dir_all(&card_dir).unwrap();
            std::fs::write(card_dir.join("index.md"), "---\nStatus: merged\n---\n").unwrap();
        }
        let result = discover_recently_finished_from_cards(&dir);
        assert!(result.len() <= RECENTLY_FINISHED_LIMIT, "must cap at RECENTLY_FINISHED_LIMIT");
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── parse_depends_on tests (brief-117) ────────────────────────────────────

    #[test]
    fn parse_depends_on_none_value_returns_empty() {
        let dir = std::env::temp_dir().join(format!("hive_dep_none_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(&path, "---\nStatus: queued\nDepends-on: _none_\n---\n").unwrap();
        assert_eq!(parse_depends_on(&path), Vec::<String>::new());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_depends_on_none_with_parenthetical_rationale_returns_empty() {
        let dir = std::env::temp_dir().join(format!("hive_dep_none_paren_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(
            &path,
            "---\nStatus: queued\nDepends-on: _none_ (concurrent with Phase 1-3; gates Phase 5 demo)\n---\n",
        )
        .unwrap();
        assert_eq!(parse_depends_on(&path), Vec::<String>::new());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_depends_on_missing_field_returns_empty() {
        let dir = std::env::temp_dir().join(format!("hive_dep_missing_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(&path, "---\nStatus: queued\n---\n# no depends-on field\n").unwrap();
        assert_eq!(parse_depends_on(&path), Vec::<String>::new());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_depends_on_single_dep_yaml() {
        let dir = std::env::temp_dir().join(format!("hive_dep_single_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(&path, "---\nStatus: queued\nDepends-on: brief-091-modal-training\n---\n").unwrap();
        assert_eq!(parse_depends_on(&path), vec!["brief-091-modal-training"]);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_depends_on_multiple_deps_yaml() {
        let dir = std::env::temp_dir().join(format!("hive_dep_multi_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(&path, "---\nStatus: queued\nDepends-on: brief-010-foo, brief-011-bar, brief-012-baz\n---\n").unwrap();
        assert_eq!(
            parse_depends_on(&path),
            vec!["brief-010-foo", "brief-011-bar", "brief-012-baz"]
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parse_depends_on_bold_markdown_fallback() {
        let dir = std::env::temp_dir().join(format!("hive_dep_bold_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("index.md");
        std::fs::write(&path, "# legacy card\n**Depends-on:** brief-018-smolvla-adapter\n").unwrap();
        assert_eq!(parse_depends_on(&path), vec!["brief-018-smolvla-adapter"]);
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── discover_queued_from_cards readiness tests (brief-117) ───────────────

    fn nanos() -> u32 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .subsec_nanos()
    }

    fn make_card(dir: &Path, id: &str, status: &str, depends_on: Option<&str>) {
        let card_dir = dir.join(id);
        std::fs::create_dir_all(&card_dir).unwrap();
        let dep_line = match depends_on {
            Some(d) => format!("Depends-on: {d}\n"),
            None => String::new(),
        };
        std::fs::write(
            card_dir.join("index.md"),
            format!("---\nStatus: {status}\n{dep_line}---\n"),
        ).unwrap();
    }

    #[test]
    fn queued_brief_with_all_deps_merged_is_ready() {
        let dir = std::env::temp_dir().join(format!("hive_ready_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        make_card(&dir, "brief-001-upstream", "merged", None);
        make_card(&dir, "brief-002-queued", "queued", Some("brief-001-upstream"));
        let goals = dir.join("goals.md");
        let result = discover_queued_from_cards(&dir, &goals);
        assert_eq!(result.len(), 1);
        assert_eq!(result[0].readiness, QueuedReadiness::Ready);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn queued_brief_with_unmet_dep_is_blocked() {
        let dir = std::env::temp_dir().join(format!("hive_blocked_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        make_card(&dir, "brief-001-upstream", "active", None);
        make_card(&dir, "brief-002-downstream", "queued", Some("brief-001-upstream"));
        let goals = dir.join("goals.md");
        let result = discover_queued_from_cards(&dir, &goals);
        assert_eq!(result.len(), 1);
        assert!(
            matches!(&result[0].readiness, QueuedReadiness::Blocked { first_unmet, more: 0 }
                if first_unmet == "brief-001-upstream"),
            "expected blocked on brief-001-upstream, got {:?}", result[0].readiness
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn queued_brief_with_multiple_unmet_deps_shows_first_plus_count() {
        let dir = std::env::temp_dir().join(format!("hive_multi_blocked_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        make_card(&dir, "brief-010-a", "active", None);
        make_card(&dir, "brief-011-b", "active", None);
        make_card(&dir, "brief-012-c", "active", None);
        make_card(&dir, "brief-020-downstream", "queued", Some("brief-010-a, brief-011-b, brief-012-c"));
        let goals = dir.join("goals.md");
        let result = discover_queued_from_cards(&dir, &goals);
        assert_eq!(result.len(), 1);
        assert!(
            matches!(&result[0].readiness, QueuedReadiness::Blocked { more, .. } if *more == 2),
            "expected 2 more blocked deps, got {:?}", result[0].readiness
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn queued_brief_with_orphan_dep_renders_card_not_found() {
        let dir = std::env::temp_dir().join(format!("hive_orphan_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        make_card(&dir, "brief-050-queued", "queued", Some("brief-9999-nonexistent"));
        let goals = dir.join("goals.md");
        let result = discover_queued_from_cards(&dir, &goals);
        assert_eq!(result.len(), 1);
        assert!(
            matches!(&result[0].readiness, QueuedReadiness::Blocked { first_unmet, .. }
                if first_unmet.contains("[card not found]")),
            "expected card-not-found marker, got {:?}", result[0].readiness
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn queued_briefs_sort_ready_before_blocked() {
        let dir = std::env::temp_dir().join(format!("hive_sort_ready_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        make_card(&dir, "brief-001-upstream", "active", None);
        // brief-010: blocked (dep active)
        make_card(&dir, "brief-010-blocked", "queued", Some("brief-001-upstream"));
        // brief-020: ready (no deps)
        make_card(&dir, "brief-020-ready", "queued", None);
        // brief-030: blocked (dep active)
        make_card(&dir, "brief-030-also-blocked", "queued", Some("brief-001-upstream"));
        // brief-040: ready (dep merged)
        make_card(&dir, "brief-040-also-ready", "queued", None);
        let goals = dir.join("goals.md");
        let result = discover_queued_from_cards(&dir, &goals);
        assert_eq!(result.len(), 4);
        // First two must be ready
        assert!(matches!(result[0].readiness, QueuedReadiness::Ready), "index 0 must be ready");
        assert!(matches!(result[1].readiness, QueuedReadiness::Ready), "index 1 must be ready");
        // Last two must be blocked
        assert!(matches!(result[2].readiness, QueuedReadiness::Blocked { .. }), "index 2 must be blocked");
        assert!(matches!(result[3].readiness, QueuedReadiness::Blocked { .. }), "index 3 must be blocked");
        std::fs::remove_dir_all(&dir).ok();
    }

    // ── run cards tests (brief-125) ───────────────────────────────────────────

    #[test]
    fn parse_run_ts_rfc3339() {
        assert!(parse_run_ts("2026-05-02T16:30:17Z").is_some());
    }

    #[test]
    fn parse_run_ts_utc_space_format() {
        let ts1 = parse_run_ts("2026-05-02T18:44 UTC").unwrap();
        let ts2 = parse_run_ts("2026-05-02T18:44:00Z").unwrap();
        assert_eq!(ts1.timestamp(), ts2.timestamp());
    }

    #[test]
    fn parse_run_ts_tbd_and_empty_are_none() {
        assert!(parse_run_ts("TBD").is_none());
        assert!(parse_run_ts("").is_none());
        assert!(parse_run_ts("null").is_none());
    }

    #[test]
    fn parse_yaml_front_field_basic() {
        let content = "---\nrun-id: my-run\npolicy: act\nstatus: running\n---\n";
        let lines: Vec<&str> = content.lines().collect();
        assert_eq!(parse_yaml_front_field(&lines, "run-id").as_deref(), Some("my-run"));
        assert_eq!(parse_yaml_front_field(&lines, "policy").as_deref(), Some("act"));
        assert_eq!(parse_yaml_front_field(&lines, "status").as_deref(), Some("running"));
        assert!(parse_yaml_front_field(&lines, "missing").is_none());
    }

    #[test]
    fn parse_yaml_front_field_strips_quotes() {
        let content = "---\nstarted-at: \"2026-05-02T18:44 UTC\"\n---\n";
        let lines: Vec<&str> = content.lines().collect();
        let val = parse_yaml_front_field(&lines, "started-at").unwrap();
        assert!(!val.starts_with('"'), "quotes should be stripped, got: {val:?}");
        assert_eq!(val, "2026-05-02T18:44 UTC");
    }

    #[test]
    fn load_run_cards_returns_running_cards() {
        let dir = std::env::temp_dir().join(format!("hive_runcards_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_rcsig_{}", nanos()));
        std::fs::create_dir_all(&sig_dir).unwrap();

        let run1 = dir.join("2026-05-02-act-r1");
        std::fs::create_dir_all(&run1).unwrap();
        std::fs::write(run1.join("index.md"),
            "---\nrun-id: 2026-05-02-act-r1\npolicy: act\ndataset: ds1\nmachine: modal:a10g\nstatus: running\nstarted-at: 2026-05-02T16:30:17Z\ncompleted-at: TBD\n---\n",
        ).unwrap();

        let run2 = dir.join("2026-05-02-smolvla-r1");
        std::fs::create_dir_all(&run2).unwrap();
        std::fs::write(run2.join("index.md"),
            "---\nrun-id: 2026-05-02-smolvla-r1\npolicy: smolvla\ndataset: ds1\nmachine: modal:a10g\nstatus: running\nstarted-at: \"2026-05-02T18:44 UTC\"\ncompleted-at: TBD\n---\n",
        ).unwrap();

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 2);
        let ids: Vec<&str> = cards.iter().map(|c| c.run_id.as_str()).collect();
        assert!(ids.contains(&"2026-05-02-act-r1"), "missing act-r1");
        assert!(ids.contains(&"2026-05-02-smolvla-r1"), "missing smolvla-r1");
        for card in &cards {
            assert_eq!(card.status, RunStatus::Running, "{} should be Running", card.run_id);
            assert_eq!(card.policy.as_deref(), Some(if card.run_id.contains("act") { "act" } else { "smolvla" }));
        }
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }

    #[test]
    fn load_run_cards_reads_heartbeats_from_sidecar() {
        let dir = std::env::temp_dir().join(format!("hive_hb_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_hbsig_{}", nanos()));
        std::fs::create_dir_all(&sig_dir).unwrap();

        let run_dir = dir.join("test-run-r1");
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(run_dir.join("index.md"),
            "---\nrun-id: test-run-r1\npolicy: act\nstatus: running\nstarted-at: 2026-05-02T10:00:00Z\ncompleted-at: TBD\n---\n",
        ).unwrap();
        std::fs::write(run_dir.join("heartbeats.jsonl"),
            "{\"ts\":\"2026-05-02T10:30:00Z\",\"status\":\"running\",\"last_step\":500,\"last_loss\":1.23,\"log_mtime\":\"2026-05-02T10:30:00Z\",\"app_state\":\"running\"}\n\
             {\"ts\":\"2026-05-02T11:00:00Z\",\"status\":\"running\",\"last_step\":1000,\"last_loss\":0.87,\"log_mtime\":\"2026-05-02T11:00:00Z\",\"app_state\":\"running\"}\n",
        ).unwrap();

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 1);
        let card = &cards[0];
        assert!(card.heartbeat_sidecar_present, "sidecar should be present");
        assert_eq!(card.heartbeats.len(), 2);
        let latest = card.latest_heartbeat().unwrap();
        assert_eq!(latest.last_step, Some(1000));
        assert!((latest.last_loss.unwrap() - 0.87).abs() < 0.001);
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }

    #[test]
    fn load_run_cards_no_heartbeat_sidecar() {
        let dir = std::env::temp_dir().join(format!("hive_nohb_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_nohbsig_{}", nanos()));
        std::fs::create_dir_all(&sig_dir).unwrap();

        let run_dir = dir.join("run-no-sidecar");
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(run_dir.join("index.md"),
            "---\nrun-id: run-no-sidecar\npolicy: act\nstatus: running\nstarted-at: 2026-05-02T10:00:00Z\ncompleted-at: TBD\n---\n",
        ).unwrap();

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 1);
        assert!(!cards[0].heartbeat_sidecar_present, "sidecar should be absent");
        assert!(cards[0].heartbeats.is_empty());
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }

    #[test]
    fn load_run_cards_failed_reads_failure_signal() {
        let dir = std::env::temp_dir().join(format!("hive_fail_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_failsig_{}", nanos()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::create_dir_all(&sig_dir).unwrap();

        let run_dir = dir.join("2026-04-28-failed-r1");
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(run_dir.join("index.md"),
            "---\nrun-id: 2026-04-28-failed-r1\npolicy: act\nstatus: failed\nstarted-at: 2026-04-28T10:00:00Z\ncompleted-at: 2026-04-28T12:00:00Z\n---\n",
        ).unwrap();
        std::fs::write(
            sig_dir.join("training-failed-2026-04-28-failed-r1.json"),
            r#"{"run_id":"2026-04-28-failed-r1","reason":"OOM at step 2000"}"#,
        ).unwrap();

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 1);
        assert_eq!(cards[0].status, RunStatus::Failed);
        let sig = cards[0].failure_signal.as_ref().expect("failure_signal should be set");
        assert_eq!(sig["reason"].as_str(), Some("OOM at step 2000"));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }

    #[test]
    fn load_run_cards_sorted_newest_first() {
        let dir = std::env::temp_dir().join(format!("hive_sort_rc_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_sort_rcsig_{}", nanos()));
        std::fs::create_dir_all(&sig_dir).unwrap();

        for (name, started) in &[
            ("run-a", "2026-04-01T10:00:00Z"),
            ("run-b", "2026-05-01T10:00:00Z"),
            ("run-c", "2026-03-01T10:00:00Z"),
        ] {
            let run_dir = dir.join(name);
            std::fs::create_dir_all(&run_dir).unwrap();
            std::fs::write(run_dir.join("index.md"),
                format!("---\nrun-id: {}\nstatus: complete\nstarted-at: {}\ncompleted-at: {}\n---\n", name, started, started),
            ).unwrap();
        }

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 3);
        assert_eq!(cards[0].run_id, "run-b");
        assert_eq!(cards[1].run_id, "run-a");
        assert_eq!(cards[2].run_id, "run-c");
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }

    #[test]
    fn load_run_cards_skips_template_dir() {
        let dir = std::env::temp_dir().join(format!("hive_tmpl_{}", nanos()));
        let sig_dir = std::env::temp_dir().join(format!("hive_tmplsig_{}", nanos()));
        std::fs::create_dir_all(&sig_dir).unwrap();

        // Real run
        let run_dir = dir.join("2026-05-02-real-r1");
        std::fs::create_dir_all(&run_dir).unwrap();
        std::fs::write(run_dir.join("index.md"),
            "---\nrun-id: 2026-05-02-real-r1\nstatus: complete\nstarted-at: 2026-05-02T10:00:00Z\ncompleted-at: 2026-05-02T12:00:00Z\n---\n",
        ).unwrap();

        // _template dir — should be skipped
        let tmpl_dir = dir.join("_template");
        std::fs::create_dir_all(&tmpl_dir).unwrap();
        std::fs::write(tmpl_dir.join("index.md"),
            "---\nrun-id: _template\nstatus: pending\nstarted-at: TBD\ncompleted-at: TBD\n---\n",
        ).unwrap();

        let cards = load_run_cards(&dir, &sig_dir);
        assert_eq!(cards.len(), 1);
        assert_eq!(cards[0].run_id, "2026-05-02-real-r1");
        std::fs::remove_dir_all(&dir).ok();
        std::fs::remove_dir_all(&sig_dir).ok();
    }
}
