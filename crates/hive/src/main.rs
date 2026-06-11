mod buzz;
mod config;
mod state;

use anyhow::Result;
use crossterm::{
    event::{self, Event, KeyCode, KeyModifiers},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use notify::{RecursiveMode, Watcher};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{Block, BorderType, Borders, Clear, Paragraph},
    Terminal,
};
use std::{
    io,
    panic,
    path::Path,
    sync::mpsc,
    sync::atomic::{AtomicI32, Ordering},
    time::{Duration, Instant},
};

const SPINNER_FRAMES: &[&str] = &["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// Saved fd 2 from before we redirected stderr to a log file.
/// Used to restore stderr on graceful shutdown / panic so messages reach
/// the user's terminal again.
#[cfg(unix)]
static SAVED_STDERR_FD: AtomicI32 = AtomicI32::new(-1);

/// Redirect stderr (fd 2) into `.loop/state/hive.stderr.log`.
///
/// ratatui owns the screen via the alternate buffer; any `eprintln!` from
/// render-path code (state.rs::parse_depends_on, config warnings, etc.)
/// would otherwise smear text across panels. Call after the `.loop/` check
/// and before `enable_raw_mode`.
#[cfg(unix)]
fn redirect_stderr_to_log() {
    let path = Path::new(".loop/state/hive.stderr.log");
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let Ok(f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    else {
        return;
    };
    use std::os::fd::AsRawFd;
    unsafe {
        let saved = libc::dup(libc::STDERR_FILENO);
        if saved >= 0 {
            SAVED_STDERR_FD.store(saved, Ordering::SeqCst);
        }
        libc::dup2(f.as_raw_fd(), libc::STDERR_FILENO);
    }
}

#[cfg(not(unix))]
fn redirect_stderr_to_log() {}

#[cfg(unix)]
fn restore_stderr() {
    let saved = SAVED_STDERR_FD.swap(-1, Ordering::SeqCst);
    if saved >= 0 {
        unsafe {
            libc::dup2(saved, libc::STDERR_FILENO);
            libc::close(saved);
        }
    }
}

#[cfg(not(unix))]
fn restore_stderr() {}

fn restore_terminal() {
    let _ = disable_raw_mode();
    let _ = execute!(io::stdout(), LeaveAlternateScreen);
    restore_stderr();
}

fn setup_panic_hook() {
    let default_hook = panic::take_hook();
    panic::set_hook(Box::new(move |info| {
        restore_terminal();
        default_hook(info);
    }));
}

// ── panel identity ────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Panel {
    Hive,
    Signals,
    Cells,
    DanceFloor,
    Buzz,
}

impl Panel {
    fn next(self) -> Panel {
        match self {
            Panel::Hive => Panel::Signals,
            Panel::Signals => Panel::Cells,
            Panel::Cells => Panel::DanceFloor,
            Panel::DanceFloor => Panel::Buzz,
            Panel::Buzz => Panel::Hive,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Panel::Hive => " Hive Status ",
            Panel::Signals => " Signals ",
            Panel::Cells => " Cells ",
            Panel::DanceFloor => " Dance Floor ",
            Panel::Buzz => " Buzz ",
        }
    }
}

// ── colors ────────────────────────────────────────────────────────────────────

const AMBER: Color = Color::from_u32(0x00F5A623);
const GOLD: Color = Color::from_u32(0x00FFCE5C);
const STAMP_GREEN: Color = Color::from_u32(0x005EC488);
const CORAL: Color = Color::from_u32(0x00FF6B6B);
const MUTED: Color = Color::from_u32(0x006A6A6A);
const DIM_BORDER: Color = Color::from_u32(0x004A4A4A);
const INDIGO: Color = Color::from_u32(0x007B8FD4);
const LAVENDER: Color = Color::from_u32(0x00B894E6);
const BLUE: Color = Color::from_u32(0x005B9BD5);
// Worker = pop-green (bees collecting nectar); Validator = orange (quality gate)
const POP_GREEN: Color = Color::from_u32(0x0039FF80);
const ORANGE: Color = Color::from_u32(0x00FF8C00);
// Scouts = teal (observation pings, not throughput). Deliberately chosen to
// read as calmer than conductor/worker colors so the dance floor separates
// brief-cycle noise from scout observation at a glance.
const TEAL: Color = Color::from_u32(0x005DADE2);

// ── app state ─────────────────────────────────────────────────────────────────

struct App {
    focused: Panel,
    scroll: [u16; 5],
    spinner_frame: usize,
    hive: state::HiveState,
    cells: state::CellsState,
    dance_floor: state::DanceFloorState,
    buzz: buzz::BuzzState,
    signals: state::SignalsState,
    learnings: state::LearningsState,
    dance_floor_auto_scroll: bool,
    show_help: bool,
    /// Row index of the selected signal in the Signals panel (clamped to
    /// `0..signals.len()`). Visible when the Signals panel has focus.
    signal_cursor: usize,
    /// True when the signal-detail modal is open.
    show_signal_detail: bool,
    /// Cursor position in the Buzz grid (0 = newest event, rendered top-left).
    buzz_cursor: usize,
    /// Seconds to offset the Buzz time window end backward from now.
    /// 0 = window ends at "now"; positive = window is in the past.
    buzz_window_offset_secs: i64,
    /// Vertical scroll offset inside the signal-detail modal.
    signal_modal_scroll: u16,
    /// Index into learnings, cycled every LEARNING_ROTATION_SECS while
    /// the Signals panel is in "From the Hive" (no-signals) mode.
    learning_index: usize,
    last_learning_rotation: Instant,
    /// Per-project palette + layout config from `.loop/config.json`.
    config: config::HiveConfig,
    /// True when the Signals slot should show the Buzz hex grid instead of
    /// "From the Hive" learnings. Toggled by `b`. Alert signals override both.
    signals_show_buzz: bool,
    /// True when the Signals slot should show the Run Cards view. Toggled by
    /// `r`. Auto-promoted to true on startup when ≥1 run has status: running.
    /// Alert signals and buzz (when active) both override runs.
    signals_show_runs: bool,
    /// Loaded run cards from wiki/runs/*/index.md + heartbeats.jsonl.
    run_cards: Vec<state::RunCard>,
}

const LEARNING_ROTATION_SECS: u64 = 60;

impl App {
    fn new() -> Self {
        App {
            focused: Panel::DanceFloor,
            scroll: [0; 5],
            spinner_frame: 0,
            hive: state::HiveState::load(),
            cells: state::CellsState::load(),
            dance_floor: state::DanceFloorState::load(),
            buzz: buzz::load_buzz_state(std::time::Duration::from_secs(3 * 3600), 0),
            signals: state::SignalsState::load(),
            learnings: state::LearningsState::load(),
            dance_floor_auto_scroll: true,
            show_help: false,
            signal_cursor: 0,
            show_signal_detail: false,
            buzz_cursor: 0,
            buzz_window_offset_secs: 0,
            signal_modal_scroll: 0,
            learning_index: 0,
            last_learning_rotation: Instant::now(),
            config: config::HiveConfig::load(),
            signals_show_buzz: false,
            signals_show_runs: {
                let cards = state::load_run_cards(
                    std::path::Path::new("wiki/runs"),
                    std::path::Path::new(".loop/state/signals"),
                );
                cards.iter().any(|c| matches!(c.status, state::RunStatus::Running))
            },
            run_cards: state::load_run_cards(
                std::path::Path::new("wiki/runs"),
                std::path::Path::new(".loop/state/signals"),
            ),
        }
    }

    // ── palette helpers ───────────────────────────────────────────────────────
    fn col_primary(&self) -> Color { Color::from_u32(self.config.palette.primary) }
    fn col_muted(&self) -> Color { Color::from_u32(self.config.palette.muted) }

    fn refresh_state(&mut self) {
        self.hive = state::HiveState::load();
        self.cells = state::CellsState::load();
        self.dance_floor = state::DanceFloorState::load();
        self.reload_buzz();
        self.signals = state::SignalsState::load();
        self.learnings = state::LearningsState::load();
        // Clamp cursor to current signals count — signals come and go as the
        // conductor files / clears them.
        let max = self.signals.signals.len().saturating_sub(1);
        if self.signal_cursor > max {
            self.signal_cursor = max;
        }
        self.run_cards = state::load_run_cards(
            std::path::Path::new("wiki/runs"),
            std::path::Path::new(".loop/state/signals"),
        );
    }

    /// Advance the learning quote if it's been long enough since the last
    /// rotation. Cheap — called every render tick, no-ops until the interval
    /// elapses.
    fn maybe_rotate_learning(&mut self) {
        if self.last_learning_rotation.elapsed().as_secs() >= LEARNING_ROTATION_SECS {
            self.learning_index = self.learning_index.wrapping_add(1);
            self.last_learning_rotation = Instant::now();
        }
    }

    fn reload_buzz(&mut self) {
        self.buzz = buzz::load_buzz_state(
            std::time::Duration::from_secs(3 * 3600),
            self.buzz_window_offset_secs,
        );
        if !self.buzz.events.is_empty() {
            self.buzz_cursor = self.buzz_cursor.min(self.buzz.events.len() - 1);
        } else {
            self.buzz_cursor = 0;
        }
    }

    fn signal_cursor_down(&mut self) {
        let max = self.signals.signals.len().saturating_sub(1);
        if self.signal_cursor < max {
            self.signal_cursor += 1;
        }
    }

    fn signal_cursor_up(&mut self) {
        self.signal_cursor = self.signal_cursor.saturating_sub(1);
    }

    fn open_signal_detail(&mut self) {
        if !self.signals.signals.is_empty() {
            self.show_signal_detail = true;
            self.signal_modal_scroll = 0;
        }
    }

    fn close_signal_detail(&mut self) {
        self.show_signal_detail = false;
    }

    #[cfg(test)]
    fn selected_signal(&self) -> Option<&state::Signal> {
        self.signals.signals.get(self.signal_cursor)
    }

    fn tick_spinner(&mut self) {
        self.spinner_frame = (self.spinner_frame + 1) % SPINNER_FRAMES.len();
    }

    fn focus_next(&mut self) {
        self.focused = self.focused.next();
    }

    fn scroll_down(&mut self) {
        let idx = self.focused as usize;
        self.scroll[idx] = self.scroll[idx].saturating_add(1);
    }

    fn scroll_up(&mut self) {
        let idx = self.focused as usize;
        self.scroll[idx] = self.scroll[idx].saturating_sub(1);
    }

    fn toggle_help(&mut self) {
        self.show_help = !self.show_help;
    }

    fn panel_block<'a>(&self, panel: Panel) -> Block<'a> {
        self.panel_block_titled(panel, panel.label())
    }

    /// Like `panel_block` but with a caller-provided title. Used by the
    /// Signals slot when it flips to "From the Hive" mode and needs to
    /// relabel without gaining a second title segment.
    fn panel_block_titled<'a>(&self, panel: Panel, title: &'static str) -> Block<'a> {
        let focused = self.focused == panel;
        let border_type = if focused {
            BorderType::Double
        } else {
            BorderType::Rounded
        };
        let primary = self.col_primary();
        let muted = self.col_muted();
        let title_style = if focused {
            Style::default().fg(primary).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(muted)
        };
        let border_style = if focused {
            Style::default().fg(primary)
        } else {
            Style::default().fg(DIM_BORDER)
        };
        Block::default()
            .borders(Borders::ALL)
            .border_type(border_type)
            .border_style(border_style)
            .title(Span::styled(title, title_style))
    }
}

// ── panel renderers ───────────────────────────────────────────────────────────

fn render_hive<'a>(app: &App) -> Text<'a> {
    let h = &app.hive;
    let mut lines = Vec::new();

    // Line 1: PID + alive status + uptime (so `loop stop && loop start` is
    // visible in the panel immediately, before the new daemon's first
    // heartbeat lands).
    let uptime_suffix = if h.pid_alive {
        h.daemon_started_at
            .map(|start| format!(" · up {}", state::relative_time(start).trim_end_matches(" ago")))
    } else {
        None
    };
    let pid_line = if let Some(pid) = h.pid {
        if h.pid_alive {
            let mut spans = vec![
                Span::styled("PID: ", Style::default().fg(MUTED)),
                Span::styled(pid.to_string(), Style::default().fg(GOLD)),
                Span::styled("  ●  alive", Style::default().fg(STAMP_GREEN)),
            ];
            if let Some(up) = uptime_suffix {
                spans.push(Span::styled(up, Style::default().fg(MUTED)));
            }
            Line::from(spans)
        } else {
            Line::from(vec![
                Span::styled("PID: ", Style::default().fg(MUTED)),
                Span::styled(format!("{} (dead)", pid), Style::default().fg(CORAL)),
            ])
        }
    } else {
        Line::from(Span::styled("PID: — stopped", Style::default().fg(CORAL)))
    };
    lines.push(pid_line);

    // Line 2: spinner + heartbeat number + age
    let spinner = if h.pid_alive {
        SPINNER_FRAMES[app.spinner_frame]
    } else {
        "·"
    };
    let beat_line = if h.heartbeat_number > 0 {
        let age = h
            .last_heartbeat_ts
            .map(state::relative_time)
            .unwrap_or_else(|| "unknown".to_string());
        let countdown = h.heartbeat_countdown();
        // Color tiers: "overdue" means the daemon might be stuck (coral
        // alarm); "busy cycling" means the daemon is working on non-
        // heartbeat events and heartbeats are contended (amber "alive");
        // anything else is the quiet-count-to-next-beat case (muted).
        let countdown_color = match countdown.as_deref() {
            Some(s) if s.starts_with("overdue") => CORAL,
            Some(s) if s.starts_with("busy") => AMBER,
            Some(_) => MUTED,
            None => MUTED,
        };
        let mut spans = vec![
            Span::styled(
                format!("{} Heartbeat ", spinner),
                Style::default().fg(if h.pid_alive { AMBER } else { MUTED }),
            ),
            Span::styled(
                format!("#{}", h.heartbeat_number),
                Style::default().fg(GOLD).add_modifier(Modifier::BOLD),
            ),
            Span::styled(format!(" · {}", age), Style::default().fg(MUTED)),
        ];
        if let Some(c) = countdown {
            spans.push(Span::styled(" · ", Style::default().fg(MUTED)));
            spans.push(Span::styled(c, Style::default().fg(countdown_color)));
        }
        Line::from(spans)
    } else {
        Line::from(vec![
            Span::styled(format!("{} ", spinner), Style::default().fg(MUTED)),
            Span::styled("no heartbeats yet", Style::default().fg(MUTED)),
        ])
    };
    lines.push(beat_line);

    // Line 3: interval mode
    let mode_line = Line::from(vec![
        Span::styled("Mode: ", Style::default().fg(MUTED)),
        Span::styled(
            h.interval_mode.label(),
            Style::default().fg(match h.interval_mode {
                state::IntervalMode::Active => STAMP_GREEN,
                state::IntervalMode::Idle => MUTED,
                state::IntervalMode::Unknown => MUTED,
            }),
        ),
    ]);
    lines.push(mode_line);

    // Re-queued (pending precondition) — brief-102.
    // Omitted entirely when empty (no noisy empty-state line).
    if !h.requeued_briefs.is_empty() {
        lines.push(Line::from(vec![
            Span::styled(
                format!("Re-queued ({}):", h.requeued_briefs.len()),
                Style::default().fg(AMBER),
            ),
        ]));
        for rb in &h.requeued_briefs {
            let (tag, tag_color) = if rb.ready_to_dispatch {
                ("★ ready to re-dispatch", STAMP_GREEN)
            } else {
                ("waiting", MUTED)
            };
            lines.push(Line::from(vec![
                Span::styled("  ", Style::default()),
                Span::styled(rb.brief_id.clone(), Style::default().fg(GOLD)),
                Span::styled("  blocked-on: ", Style::default().fg(MUTED)),
                Span::styled(rb.blocked_on.clone(), Style::default().fg(INDIGO)),
                Span::styled("  ", Style::default()),
                Span::styled(tag, Style::default().fg(tag_color)),
            ]));
        }
    }

    // External commits on main row.
    {
        let em = &h.external_main;
        let ext_line = if em.error {
            Line::from(vec![
                Span::styled("External on main: ", Style::default().fg(MUTED)),
                Span::styled("?", Style::default().fg(MUTED)),
            ])
        } else if em.count_external == 0 {
            let mut spans = vec![
                Span::styled("External on main: ", Style::default().fg(MUTED)),
                Span::styled("0", Style::default().fg(MUTED)),
            ];
            if em.allowlist_defaults_only {
                spans.push(Span::styled(
                    " (allowlist=defaults-only)",
                    Style::default().fg(MUTED),
                ));
            }
            Line::from(spans)
        } else {
            let mut spans = vec![
                Span::styled("External on main: ", Style::default().fg(MUTED)),
                Span::styled(
                    em.count_external.to_string(),
                    Style::default().fg(CORAL).add_modifier(Modifier::BOLD),
                ),
            ];
            if let Some(last) = &em.last_external {
                spans.push(Span::styled(" (last: ", Style::default().fg(MUTED)));
                spans.push(Span::styled(
                    last.sha_short.clone(),
                    Style::default().fg(GOLD),
                ));
                spans.push(Span::styled(" ", Style::default().fg(MUTED)));
                spans.push(Span::styled(
                    last.author.clone(),
                    Style::default().fg(CORAL),
                ));
                spans.push(Span::styled(" \u{2014} ", Style::default().fg(MUTED)));
                spans.push(Span::styled(last.subject.clone(), Style::default().fg(MUTED)));
                spans.push(Span::styled(")", Style::default().fg(MUTED)));
            }
            if em.allowlist_defaults_only {
                spans.push(Span::styled(
                    " (allowlist=defaults-only)",
                    Style::default().fg(MUTED),
                ));
            }
            Line::from(spans)
        };
        lines.push(ext_line);
    }

    Text::from(lines)
}

/// Build a unicode progress bar with partial-block resolution.
/// `ratio` is clamped to 0.0..=1.0 for the fill; `width` is the bar length
/// in cells. Returns the bar string (not bracketed) — bracket at the call site.
fn progress_bar_str(ratio: f32, width: usize) -> String {
    // 8 subdivisions per cell using the partial-block set.
    // ordering: empty → full = ' ' ▏ ▎ ▍ ▌ ▋ ▊ ▉ █
    let partials = [' ', '▏', '▎', '▍', '▌', '▋', '▊', '▉'];
    let clamped = ratio.clamp(0.0, 1.0);
    let total = (clamped * (width as f32) * 8.0).round() as usize;
    let full_cells = (total / 8).min(width);
    let partial_idx = total % 8;
    let mut s = String::with_capacity(width);
    for _ in 0..full_cells {
        s.push('█');
    }
    if full_cells < width {
        s.push(partials[partial_idx]);
        for _ in (full_cells + 1)..width {
            s.push(' ');
        }
    }
    s
}

fn budget_color(current: usize, budget: usize) -> Color {
    if budget == 0 {
        return MUTED;
    }
    let ratio = current as f32 / budget as f32;
    if ratio > 1.0 {
        CORAL
    } else if ratio >= 0.75 {
        AMBER
    } else {
        STAMP_GREEN
    }
}

fn render_cells<'a>(cells: &state::CellsState, active_section_height: u16) -> Text<'a> {
    let mut lines: Vec<Line<'a>> = Vec::new();
    let section_header = |label: &'static str, color: Color| {
        Line::from(vec![
            Span::styled(
                label,
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
        ])
    };
    let empty_row = |msg: &'static str| {
        Line::from(vec![
            Span::styled("  ", Style::default()),
            Span::styled(msg, Style::default().fg(MUTED)),
        ])
    };

    // ── Active ────────────────────────────────────────────────────────────────
    lines.push(section_header("Active", AMBER));
    if cells.active.is_empty() {
        lines.push(empty_row("— hive idle"));
    } else {
        let total_active = cells.active.len();
        let show_active = total_active.min(active_section_height as usize);
        for brief in cells.active.iter().take(show_active) {
            lines.push(Line::from(vec![
                Span::styled("  ⬢ ", Style::default().fg(AMBER)),
                Span::styled(
                    brief.brief.clone(),
                    Style::default().fg(GOLD).add_modifier(Modifier::BOLD),
                ),
            ]));
            let age = brief
                .dispatched_at
                .map(state::relative_time)
                .unwrap_or_else(|| "unknown".to_string());

            // Row 2: branch + cycle progress + age.
            // Cycle number is the latest validator cycle if one has landed,
            // else 0 (brief dispatched but no review yet). Budget may be None
            // for briefs without a parseable `## Budget` section.
            //
            // When budget is unknown: read from progress.json (daemon-written,
            // schema-bound) instead of counting log.jsonl entries (LLM-written,
            // can hallucinate values like "2026"). Fail-safe is `cycle ?/?`.
            let current_cycle = brief.latest_validator_cycle.unwrap_or(0);
            let cycle_label = match brief.cycle_budget {
                Some(budget) => {
                    format!("cycle {}/{}", current_cycle, budget)
                }
                None => match &brief.brief_progress {
                    Some(p) if p.total > 0 => format!("cycle {}/{}", p.iteration, p.total),
                    _ => "cycle ?/?".to_string(),
                },
            };
            // Branch name is almost always identical to the brief id in
            // simple-loop (both follow `brief-NNN-slug`), so it's duplicate
            // text eating the column. Only show it when it actually differs
            // — rare, but useful (manual rename, hotfix branch, etc).
            let mut row_spans = vec![Span::styled("    ", Style::default())];
            if brief.branch != brief.brief {
                row_spans.push(Span::styled(
                    brief.branch.clone(),
                    Style::default().fg(MUTED),
                ));
                row_spans.push(Span::styled("  ·  ", Style::default().fg(MUTED)));
            }
            row_spans.push(Span::styled(
                cycle_label,
                Style::default().fg(match brief.cycle_budget {
                    Some(b) => budget_color(current_cycle, b),
                    None => MUTED,
                }),
            ));
            // When no cycle budget, append last_task + remaining from progress.json.
            if brief.cycle_budget.is_none() {
                if let Some(p) = &brief.brief_progress {
                    if p.total > 0 {
                        row_spans.push(Span::styled(
                            format!(" · last_task: {}", p.last_task),
                            Style::default().fg(MUTED),
                        ));
                        row_spans.push(Span::styled(
                            format!(" · {} tasks remaining", p.tasks_remaining),
                            Style::default().fg(MUTED),
                        ));
                    }
                }
            }
            row_spans.push(Span::styled(
                format!("  ·  {}", age),
                Style::default().fg(MUTED),
            ));
            lines.push(Line::from(row_spans));

            // Row 3: progress bar — only when a budget is known.
            if let Some(budget) = brief.cycle_budget {
                let ratio = if budget == 0 {
                    0.0
                } else {
                    current_cycle as f32 / budget as f32
                };
                let color = budget_color(current_cycle, budget);
                let bar = progress_bar_str(ratio, 12);
                let pct = (ratio * 100.0).round() as i32;
                // When past budget, prefix with ⚠ instead of the leading bracket.
                let (lead, tail) = if ratio > 1.0 {
                    ("    ⚠ [", "]")
                } else {
                    ("    [", "]")
                };
                lines.push(Line::from(vec![
                    Span::styled(lead, Style::default().fg(MUTED)),
                    Span::styled(bar, Style::default().fg(color)),
                    Span::styled(tail, Style::default().fg(MUTED)),
                    Span::styled(format!("  {}%", pct), Style::default().fg(color)),
                ]));
            }

            if let Some(path) = &brief.worktree_path {
                let display = if path.len() > 40 {
                    format!("…{}", &path[path.len() - 39..])
                } else {
                    path.clone()
                };
                lines.push(Line::from(vec![
                    Span::styled("    ", Style::default()),
                    Span::styled(display, Style::default().fg(MUTED)),
                ]));
            }
        }
        if total_active > show_active {
            lines.push(Line::from(Span::styled(
                format!("  + {} more…", total_active - show_active),
                Style::default().fg(MUTED),
            )));
        }
    }
    lines.push(Line::from(""));

    // ── Pending ───────────────────────────────────────────────────────────────
    // Partitioned into "Decide" (needs Mattie) and "In flight" (daemon
    // is working; zero action on her). Glance-level triage: if Decide is
    // empty, she's not the bottleneck.
    lines.push(section_header("Pending", CORAL));
    if cells.pending.is_empty() {
        lines.push(empty_row("— no briefs awaiting you"));
    } else {
        let (decide, in_flight): (Vec<_>, Vec<_>) = cells
            .pending
            .iter()
            .partition(|pb| pb.reason.needs_human());

        let render_pending_row = |lines: &mut Vec<Line<'a>>, pb: &state::PendingBrief| {
            let (glyph, color) = match pb.reason {
                state::PendingReason::Escalate => ("!", CORAL),
                state::PendingReason::PendingMerge => ("✓", STAMP_GREEN),
                state::PendingReason::PendingDispatch => ("→", BLUE),
                state::PendingReason::AwaitingEval => ("…", LAVENDER),
                state::PendingReason::AwaitingReview => ("~", AMBER),
                state::PendingReason::Unknown => ("?", MUTED),
            };
            let age = pb
                .age
                .map(state::relative_time)
                .unwrap_or_else(|| "?".to_string());
            let mut row = vec![
                Span::styled(format!("    {} ", glyph), Style::default().fg(color)),
                Span::styled(
                    pb.brief.clone(),
                    Style::default().fg(GOLD).add_modifier(Modifier::BOLD),
                ),
                Span::styled("  ·  ", Style::default().fg(MUTED)),
                Span::styled(
                    pb.reason.label().to_string(),
                    Style::default().fg(color),
                ),
            ];
            if let Some(eta) = &pb.estimated_time {
                row.push(Span::styled("  ·  ", Style::default().fg(MUTED)));
                row.push(Span::styled(
                    eta.clone(),
                    Style::default().fg(AMBER).add_modifier(Modifier::BOLD),
                ));
            }
            row.push(Span::styled(
                format!("  ·  {}", age),
                Style::default().fg(MUTED),
            ));
            lines.push(Line::from(row));

            if let Some(budget) = pb.cycle_budget {
                let current = pb.latest_validator_cycle.unwrap_or(0);
                let ratio = if budget == 0 {
                    0.0
                } else {
                    current as f32 / budget as f32
                };
                let bar_color = budget_color(current, budget);
                let bar = progress_bar_str(ratio, 12);
                let pct = (ratio * 100.0).round() as i32;
                let (lead, tail) = if ratio > 1.0 {
                    ("      ⚠ [", "]")
                } else {
                    ("      [", "]")
                };
                lines.push(Line::from(vec![
                    Span::styled(lead, Style::default().fg(MUTED)),
                    Span::styled(bar, Style::default().fg(bar_color)),
                    Span::styled(tail, Style::default().fg(MUTED)),
                    Span::styled(
                        format!("  cycle {}/{}  ·  {}%", current, budget, pct),
                        Style::default().fg(bar_color),
                    ),
                ]));
            }
        };

        // Decide — show subheader only when non-empty; muted "(clear)"
        // placeholder when empty so the section still reads as deliberate.
        lines.push(Line::from(vec![
            Span::styled(
                "  Decide",
                Style::default().fg(CORAL).add_modifier(Modifier::BOLD),
            ),
        ]));
        if decide.is_empty() {
            lines.push(Line::from(Span::styled(
                "    — clear",
                Style::default().fg(MUTED),
            )));
        } else {
            for pb in &decide {
                render_pending_row(&mut lines, pb);
            }
        }

        // In flight — only render the subheader when there's content,
        // since an empty flight-deck isn't actionable signal either way.
        if !in_flight.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(vec![
                Span::styled(
                    "  In flight",
                    Style::default().fg(LAVENDER).add_modifier(Modifier::BOLD),
                ),
            ]));
            for pb in &in_flight {
                render_pending_row(&mut lines, pb);
            }
        }
    }
    lines.push(Line::from(""));

    // ── Queued ────────────────────────────────────────────────────────────────
    // Ranked briefs (from goals.md `## Queued next`) lead in priority order
    // with a muted `N.` indicator. Unranked briefs trail in numeric order,
    // rendered as before. See `state::discover_queued_briefs`.
    lines.push(section_header("Queued", BLUE));
    if cells.queued.is_empty() {
        lines.push(empty_row("— nothing queued"));
    } else {
        // When any brief is ranked, reserve a two-column slot in every row
        // so ranked/unranked ids stay aligned in a single column. Single
        // digits get "N." (2 chars); blank for unranked. If every brief is
        // unranked, skip the slot entirely — panel looks exactly as before.
        for (idx, qb) in cells.queued.iter().enumerate() {
            let mut spans: Vec<Span> = Vec::with_capacity(4);
            spans.push(Span::styled("  · ", Style::default().fg(MUTED)));
            let tag = format!("{:>2}. ", idx + 1);
            spans.push(Span::styled(tag, Style::default().fg(MUTED)));
            spans.push(Span::styled(
                qb.brief.clone(),
                Style::default().fg(Color::White),
            ));
            match &qb.readiness {
                state::QueuedReadiness::Ready => {
                    spans.push(Span::styled("  (ready)", Style::default().fg(STAMP_GREEN)));
                }
                state::QueuedReadiness::Blocked { first_unmet, more: 0 } => {
                    spans.push(Span::styled(
                        format!("  (blocked: waiting on {first_unmet})"),
                        Style::default().fg(AMBER),
                    ));
                }
                state::QueuedReadiness::Blocked { first_unmet, more } => {
                    spans.push(Span::styled(
                        format!("  (blocked: waiting on {first_unmet} +{more} more)"),
                        Style::default().fg(AMBER),
                    ));
                }
                state::QueuedReadiness::CycleDetected => {
                    spans.push(Span::styled(
                        "  (blocked: cycle detected)",
                        Style::default().fg(CORAL),
                    ));
                }
            }
            if !qb.depends_on_secrets.is_empty() {
                let missing: Vec<&str> = qb.depends_on_secrets.iter().map(|s| s.as_str()).collect();
                spans.push(Span::styled(
                    format!("  [key: {}]", missing.join(",")),
                    Style::default().fg(AMBER),
                ));
            }
            lines.push(Line::from(spans));
        }
    }

    // ── Drafts ────────────────────────────────────────────────────────────────
    // Only surface the section when there's something to show — Drafts should
    // be background signal, not visual noise in the common case.
    if !cells.drafts.is_empty() {
        lines.push(Line::from(""));
        lines.push(section_header("Drafts", MUTED));
        for db in &cells.drafts {
            let (glyph, tail) = if db.has_index {
                ("∘", "no symlink")
            } else {
                ("∅", "no index.md")
            };
            lines.push(Line::from(vec![
                Span::styled(format!("  {} ", glyph), Style::default().fg(MUTED)),
                Span::styled(
                    db.brief.clone(),
                    Style::default().fg(MUTED).add_modifier(Modifier::DIM),
                ),
                Span::styled(
                    format!("  ·  {}", tail),
                    Style::default().fg(MUTED).add_modifier(Modifier::DIM),
                ),
            ]));
        }
    }

    // ── Recent ────────────────────────────────────────────────────────────────
    // Last N merged briefs, dimmed. Historical context — "wait, did brief-013
    // actually land?" without opening git log. Count-based (no time filter)
    // so you always see the most recent merges even on quiet days.
    // Not-doing items render below merged items in the same section, visibly
    // darker (DIM_BORDER, 0x4A4A4A) to distinguish "shipped" from "declined".
    if !cells.recently_finished.is_empty() || !cells.not_doing.is_empty() {
        lines.push(Line::from(""));
        lines.push(section_header("Recent", MUTED));
        for rf in &cells.recently_finished {
            let age = rf
                .finished_at
                .map(state::relative_time)
                .unwrap_or_else(|| "?".to_string());
            lines.push(Line::from(vec![
                Span::styled("  ⬡ ", Style::default().fg(MUTED)),
                Span::styled(
                    rf.brief.clone(),
                    Style::default().fg(MUTED),
                ),
                Span::styled(
                    format!("  ·  merged {}", age),
                    Style::default().fg(MUTED).add_modifier(Modifier::DIM),
                ),
            ]));
        }
        for nd in &cells.not_doing {
            let tail = match &nd.reason {
                Some(r) => format!("  ·  not doing — {}", r),
                None => "  ·  not doing".to_string(),
            };
            lines.push(Line::from(vec![
                Span::styled("  ✗ ", Style::default().fg(DIM_BORDER)),
                Span::styled(nd.brief.clone(), Style::default().fg(DIM_BORDER)),
                Span::styled(tail, Style::default().fg(DIM_BORDER).add_modifier(Modifier::DIM)),
            ]));
        }
    }

    Text::from(lines)
}

fn truncate_chars(s: &str, max: usize) -> String {
    let mut chars = s.chars();
    let out: String = chars.by_ref().take(max).collect();
    if chars.next().is_some() {
        format!("{}…", out)
    } else {
        out
    }
}

fn actor_color(actor: Option<&str>) -> Color {
    match actor {
        Some("queen") | Some("conductor") => LAVENDER,
        Some("daemon") => AMBER,
        Some("worker") => POP_GREEN,
        Some("validator") => ORANGE,
        Some("reviewer") => INDIGO,
        Some("scout") => TEAL,
        Some("builder") | Some("coder") | Some("researcher") => GOLD,
        _ => MUTED,
    }
}

/// Canonicalize legacy actor names to current apiary terminology for display.
/// Log events on disk preserve the original name (forensic record), but the
/// dance floor renders the current term so it stays consistent with the
/// glossary at wiki/operating-docs/apiary-glossary.md.
fn display_actor(actor: Option<&str>) -> &str {
    match actor {
        Some("conductor") => "queen",
        Some(other) => other,
        None => "?",
    }
}

fn event_color(event: Option<&str>) -> Color {
    match event {
        Some(e) if e.contains("error") || e.contains("escalate") || e.contains("fail") => CORAL,
        Some(e) if e.contains("merge") || e.contains("approve") || e.contains("stamp") => STAMP_GREEN,
        Some(e) if e.contains("dispatch") || e.contains("evaluate") => LAVENDER,
        Some(e) if e.starts_with("heartbeat") || e.contains("noop") => MUTED,
        _ => Color::White,
    }
}

/// Strip the brief id from a dance-floor event message. The brief is already
/// rendered in its own column, so embedding it in the message column is
/// redundant — and it was triggering false-positive coloring in event_color
/// for briefs whose id contained words like "error", "fail", "merge", etc.
/// Falls back to the original message if the brief id isn't a substring (or
/// stripping leaves an empty string).
fn clean_event_message(raw: &str, brief: Option<&str>) -> String {
    let Some(b) = brief.filter(|s| !s.is_empty()) else {
        return raw.to_string();
    };
    let stripped = raw.replace(b, "");
    // Collapse the double-space left by removing an inline brief id.
    let collapsed = stripped.replace("  ", " ");
    let trimmed = collapsed.trim();
    if trimmed.is_empty() {
        raw.to_string()
    } else {
        trimmed.to_string()
    }
}

fn signal_glyph_color(signal_type: &state::SignalType) -> (&'static str, Color) {
    match signal_type {
        state::SignalType::Escalate => ("◆", CORAL),
        state::SignalType::PendingMerge => ("◆", STAMP_GREEN),
        state::SignalType::PendingDispatch => ("◆", BLUE),
        state::SignalType::Unknown(_) => ("◆", MUTED),
    }
}

fn render_dance_floor<'a>(df: &'a state::DanceFloorState) -> (Text<'a>, u16) {
    if df.events.is_empty() {
        let t = Text::from(Line::from(Span::styled(
            "Waiting for the first dance…",
            Style::default().fg(MUTED),
        )));
        return (t, 1);
    }

    let mut lines: Vec<Line<'a>> = Vec::with_capacity(df.events.len());
    for ev in &df.events {
        if ev.malformed {
            lines.push(Line::from(vec![
                Span::styled("⚠ ", Style::default().fg(MUTED)),
                Span::styled(
                    ev.event.as_deref().unwrap_or("[malformed]").to_string(),
                    Style::default().fg(MUTED).add_modifier(Modifier::DIM),
                ),
            ]));
            continue;
        }

        let time_str = ev
            .ts
            .map(state::relative_time)
            .unwrap_or_else(|| "?".to_string());
        let actor_str = display_actor(ev.actor.as_deref());
        let ac = actor_color(ev.actor.as_deref());
        // Strip the brief id from the message — it's already in its own
        // column, and the redundancy triggered false positives in event_color
        // for briefs whose id contains words like "error", "fail", "merge",
        // etc. (e.g. brief-217-error-catalog turned every worker row coral).
        let raw_event = ev.event.as_deref().unwrap_or("?");
        let cleaned_event = clean_event_message(raw_event, ev.brief.as_deref());
        let msg = truncate_chars(&cleaned_event, 55);
        let ec = event_color(Some(&cleaned_event));

        // Scout events get a leading diamond glyph so they're distinct from
        // brief-cycle rows even in monochrome terminals / colorblind palettes.
        // Color alone isn't enough signal for "observation vs throughput".
        let is_scout = ev.actor.as_deref() == Some("scout");
        let leading = if is_scout { "◇ " } else { "  " };

        let mut spans: Vec<Span<'a>> = vec![
            Span::styled(format!("{:>7}", time_str), Style::default().fg(MUTED)),
            Span::styled(leading, Style::default().fg(TEAL)),
            Span::styled(format!("{:<11}", actor_str), Style::default().fg(ac)),
            Span::styled("  ", Style::default()),
        ];

        if let Some(brief) = &ev.brief {
            let brief_short = truncate_chars(brief, 20);
            spans.push(Span::styled(
                format!("{:<21}", brief_short),
                Style::default().fg(MUTED),
            ));
            spans.push(Span::styled("  ", Style::default()));
        }

        spans.push(Span::styled(msg, Style::default().fg(ec)));
        lines.push(Line::from(spans));
    }

    let count = lines.len() as u16;
    (Text::from(lines), count)
}

fn render_hive_learning<'a>(learning: Option<&'a str>) -> Text<'a> {
    match learning {
        Some(text) => Text::from(vec![
            Line::from(Span::styled(
                text.to_string(),
                Style::default().fg(Color::White),
            )),
        ]),
        None => Text::from(Line::from(Span::styled(
            "The hive is quiet. No learnings yet — run a brief.",
            Style::default().fg(MUTED),
        ))),
    }
}

// ── run cards render ─────────────────────────────────────────────────────────

/// Compute step/s pace from the last `n` heartbeats (up to 5).
/// Returns None when fewer than 2 usable data points exist.
fn compute_pace(heartbeats: &[state::RunHeartbeat]) -> Option<f64> {
    let useful: Vec<(chrono::DateTime<chrono::Utc>, u64)> = heartbeats
        .iter()
        .rev()
        .take(5)
        .filter_map(|hb| hb.last_step.map(|s| (hb.ts, s)))
        .collect();
    if useful.len() < 2 {
        return None;
    }
    let (ts_newest, step_newest) = useful[0];
    let (ts_oldest, step_oldest) = *useful.last().unwrap();
    let delta_steps = step_newest.saturating_sub(step_oldest) as f64;
    let delta_secs = (ts_newest - ts_oldest).num_seconds() as f64;
    if delta_secs <= 0.0 || delta_steps <= 0.0 {
        return None;
    }
    Some(delta_steps / delta_secs)
}

/// Returns 4 slots (2x2) for the active-run grid. Each slot is either a card
/// index into `cards` or `None` (placeholder). Only Running and Stale cards
/// appear in the grid; others live in the Recent list.
fn run_card_slots(cards: &[state::RunCard]) -> [Option<usize>; 4] {
    let indices: Vec<usize> = cards
        .iter()
        .enumerate()
        .filter(|(_, c)| matches!(c.status, state::RunStatus::Running | state::RunStatus::Stale))
        .take(4)
        .map(|(i, _)| i)
        .collect();
    [
        indices.first().copied(),
        indices.get(1).copied(),
        indices.get(2).copied(),
        indices.get(3).copied(),
    ]
}

fn run_status_chrome(status: &state::RunStatus) -> (&'static str, &'static str, Color) {
    match status {
        state::RunStatus::Running    => ("🟢", "RUNNING",   STAMP_GREEN),
        state::RunStatus::Stale      => ("⚠",  "STALE",     AMBER),
        state::RunStatus::Preempted  => ("🟡", "PREEMPTED", AMBER),
        state::RunStatus::Failed     => ("✗",  "FAILED",    CORAL),
        state::RunStatus::Complete   => ("✓",  "COMPLETE",  MUTED),
        state::RunStatus::Pending    => ("🔘", "PENDING",   MUTED),
        state::RunStatus::Unknown(_) => ("?",  "UNKNOWN",   MUTED),
    }
}

fn render_run_card(f: &mut ratatui::Frame, area: Rect, card: &state::RunCard) {
    let (icon, label, border_color) = run_status_chrome(&card.status);
    let title = Line::from(vec![
        Span::styled(
            format!(" {} ", card.run_id),
            Style::default().fg(GOLD).add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            format!("{} {} ", icon, label),
            Style::default().fg(border_color),
        ),
    ]);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(Style::default().fg(border_color))
        .title(title);
    let inner = block.inner(area);
    f.render_widget(block, area);

    let mut lines: Vec<Line> = Vec::new();

    // Meta row: policy · machine · dataset
    let meta: Vec<&str> = [
        card.policy.as_deref(),
        card.machine.as_deref(),
        card.dataset.as_deref(),
    ]
    .iter()
    .flatten()
    .copied()
    .collect();
    if !meta.is_empty() {
        lines.push(Line::from(Span::styled(
            meta.join(" · "),
            Style::default().fg(MUTED),
        )));
        lines.push(Line::from(""));
    }

    if let Some(hb) = card.latest_heartbeat() {
        if let Some(step) = hb.last_step {
            lines.push(Line::from(vec![
                Span::styled("Step  ", Style::default().fg(MUTED)),
                Span::styled(step.to_string(), Style::default().fg(Color::White)),
            ]));
        }

        if let Some(loss) = hb.last_loss {
            let mut loss_spans = vec![
                Span::styled("Loss  ", Style::default().fg(MUTED)),
                Span::styled(format!("{:.4}", loss), Style::default().fg(Color::White)),
            ];
            // Trend: compare against 2 heartbeats back (index len-3 from tail)
            if card.heartbeats.len() >= 3 {
                let prev_idx = card.heartbeats.len().saturating_sub(3);
                if let Some(prev) = card.heartbeats[prev_idx].last_loss {
                    let trend = if loss < prev { "↓" } else if loss > prev { "↑" } else { "→" };
                    loss_spans.push(Span::styled(
                        format!("  {} from {:.4}", trend, prev),
                        Style::default().fg(MUTED),
                    ));
                }
            }
            lines.push(Line::from(loss_spans));
        }

        if let Some(pace) = compute_pace(&card.heartbeats) {
            lines.push(Line::from(vec![
                Span::styled("Pace  ", Style::default().fg(MUTED)),
                Span::styled(
                    format!("{:.1} step/s", pace),
                    Style::default().fg(Color::White),
                ),
            ]));
        }

        let hb_age = state::relative_time(hb.ts);
        let app_state_str = hb
            .app_state
            .as_deref()
            .map(|s| truncate_chars(s, 12))
            .unwrap_or_else(|| "—".to_string());
        lines.push(Line::from(vec![
            Span::styled("♥ ", Style::default().fg(CORAL)),
            Span::styled(hb_age, Style::default().fg(MUTED)),
            Span::styled(format!("  {}", app_state_str), Style::default().fg(MUTED)),
        ]));
    } else {
        lines.push(Line::from(Span::styled(
            "No heartbeats yet",
            Style::default().fg(MUTED),
        )));
    }

    f.render_widget(Paragraph::new(Text::from(lines)), inner);
}

/// Build one-liner rows for the Recent list (non-active runs sorted by completed_at desc).
/// Returns (lines, overflow_count). Capped at 6 rows.
fn recent_run_lines(cards: &[state::RunCard]) -> (Vec<Line<'static>>, usize) {
    let mut historical: Vec<&state::RunCard> = cards
        .iter()
        .filter(|c| !matches!(c.status, state::RunStatus::Running | state::RunStatus::Stale))
        .collect();
    historical.sort_by(|a, b| match (b.completed_at, a.completed_at) {
        (Some(bt), Some(at)) => bt.cmp(&at),
        // b has date, a doesn't → b should come first → a > b → a after b
        (Some(_), None) => std::cmp::Ordering::Greater,
        // b has no date, a does → a should come first → a < b → a before b
        (None, Some(_)) => std::cmp::Ordering::Less,
        (None, None) => b.run_id.cmp(&a.run_id),
    });
    let overflow = historical.len().saturating_sub(6);
    historical.truncate(6);

    let lines = historical
        .iter()
        .map(|card| {
            let (icon, _, color) = run_status_chrome(&card.status);
            let age = card
                .completed_at
                .or(card.started_at)
                .map(state::relative_time)
                .unwrap_or_else(|| "?".to_string());
            let policy_str = card.policy.as_deref().unwrap_or("—").to_string();
            let metric = match &card.status {
                state::RunStatus::Complete => {
                    match (card.started_at, card.completed_at) {
                        (Some(s), Some(e)) => {
                            let secs = (e - s).num_seconds().max(0);
                            let dur = if secs < 3600 {
                                format!("{}m", secs / 60)
                            } else {
                                format!("{}h{}m", secs / 3600, (secs % 3600) / 60)
                            };
                            format!("done ({})", dur)
                        }
                        _ => "done".to_string(),
                    }
                }
                state::RunStatus::Failed => "failed".to_string(),
                state::RunStatus::Preempted => "preempted".to_string(),
                state::RunStatus::Pending => "pending".to_string(),
                state::RunStatus::Unknown(s) => s.clone(),
                _ => String::new(),
            };
            Line::from(vec![
                Span::styled(format!("{} ", icon), Style::default().fg(color)),
                Span::styled(card.run_id.clone(), Style::default().fg(GOLD)),
                Span::styled(
                    format!(" · {} · {} · {}", policy_str, metric, age),
                    Style::default().fg(MUTED),
                ),
            ])
        })
        .collect();

    (lines, overflow)
}

/// Render the 2x2 active-run grid + Recent list into `area`.
/// Called from the Signals slot when `signals_show_runs` is active.
fn render_run_cards(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let (recent_lines, recent_overflow) = recent_run_lines(&app.run_cards);

    // Split area: grid (top) + optional recent section (bottom)
    let recent_section_h = if recent_lines.is_empty() {
        0u16
    } else {
        (1 + recent_lines.len() + if recent_overflow > 0 { 1 } else { 0 }) as u16
    };
    let (grid_area, recent_area_opt): (Rect, Option<Rect>) = if recent_section_h == 0 {
        (area, None)
    } else {
        let sections = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Min(4), Constraint::Length(recent_section_h)])
            .split(area);
        (sections[0], Some(sections[1]))
    };

    // ── 2x2 active-run grid ──────────────────────────────────────────────────
    let active_count = app
        .run_cards
        .iter()
        .filter(|c| matches!(c.status, state::RunStatus::Running | state::RunStatus::Stale))
        .count();
    let overflow = active_count.saturating_sub(4);

    let row_constraints: Vec<Constraint> = if overflow > 0 {
        vec![Constraint::Min(1), Constraint::Min(1), Constraint::Length(1)]
    } else {
        vec![Constraint::Percentage(50), Constraint::Percentage(50)]
    };
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints(row_constraints)
        .split(grid_area);

    let slots = run_card_slots(&app.run_cards);
    let row0 = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(rows[0]);
    let row1 = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(rows[1]);
    let cell_rects = [row0[0], row0[1], row1[0], row1[1]];

    for (i, cell_rect) in cell_rects.iter().enumerate() {
        if let Some(card_idx) = slots[i] {
            if let Some(card) = app.run_cards.get(card_idx) {
                render_run_card(f, *cell_rect, card);
            }
        } else {
            let block = Block::default()
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(Style::default().fg(DIM_BORDER));
            let inner = block.inner(*cell_rect);
            f.render_widget(block, *cell_rect);
            f.render_widget(
                Paragraph::new(Span::styled(
                    "(no active run)",
                    Style::default().fg(MUTED).add_modifier(Modifier::DIM),
                )),
                inner,
            );
        }
    }

    if overflow > 0 {
        f.render_widget(
            Paragraph::new(Span::styled(
                format!("+{} more — see wiki/runs/", overflow),
                Style::default().fg(MUTED),
            )),
            rows[2],
        );
    }

    // ── Recent list ──────────────────────────────────────────────────────────
    if let Some(recent_area) = recent_area_opt {
        let mut all_lines: Vec<Line<'static>> = vec![Line::from(Span::styled(
            "Recent",
            Style::default().fg(MUTED).add_modifier(Modifier::BOLD),
        ))];
        all_lines.extend(recent_lines);
        if recent_overflow > 0 {
            all_lines.push(Line::from(Span::styled(
                format!("[+{} older — wiki/runs/index]", recent_overflow),
                Style::default().fg(MUTED).add_modifier(Modifier::DIM),
            )));
        }
        f.render_widget(Paragraph::new(Text::from(all_lines)), recent_area);
    }
}

fn render_signals<'a>(
    sig: &'a state::SignalsState,
    cursor: usize,
    show_cursor: bool,
) -> Text<'a> {
    if sig.signals.is_empty() {
        return Text::from(Line::from(Span::styled(
            "All calm · no distress signals",
            Style::default().fg(MUTED),
        )));
    }

    let mut lines: Vec<Line<'a>> = Vec::new();
    for (i, signal) in sig.signals.iter().enumerate() {
        let (glyph, color) = signal_glyph_color(&signal.signal_type);
        let age = signal
            .ts
            .map(state::relative_time)
            .unwrap_or_else(|| "?".to_string());
        // Prefer brief id; fall back to trigger-based label for brief-less
        // decision escalates so the row isn't "— —".
        let brief_str = signal.display_label();
        let reason_raw = signal.display_reason().unwrap_or("—");
        let reason_str = truncate_chars(reason_raw, 45);

        let is_cursor = show_cursor && i == cursor;
        let cursor_prefix: Span<'a> = if is_cursor {
            Span::styled("› ", Style::default().fg(AMBER).add_modifier(Modifier::BOLD))
        } else {
            Span::styled("  ", Style::default())
        };
        let brief_style = if is_cursor {
            Style::default().fg(AMBER).add_modifier(Modifier::BOLD)
        } else {
            Style::default().fg(GOLD)
        };

        lines.push(Line::from(vec![
            cursor_prefix,
            Span::styled(format!("{} ", glyph), Style::default().fg(color)),
            Span::styled(
                signal.signal_type.label().to_string(),
                Style::default().fg(color).add_modifier(Modifier::BOLD),
            ),
            Span::styled("  ", Style::default()),
            Span::styled(brief_str, brief_style),
            Span::styled("  ·  ", Style::default().fg(MUTED)),
            Span::styled(reason_str, Style::default().fg(MUTED)),
            Span::styled(format!("  ({})", age), Style::default().fg(MUTED)),
        ]));
    }
    Text::from(lines)
}

fn render_signal_modal<'a>(signal: &'a state::Signal) -> Text<'a> {
    let p = &signal.payload;
    let (glyph, glyph_color) = signal_glyph_color(&signal.signal_type);

    let section = |s: &'static str| {
        Line::from(Span::styled(
            s,
            Style::default().fg(LAVENDER).add_modifier(Modifier::BOLD),
        ))
    };
    let blank = || Line::from("");
    let body_span = |s: String| Span::styled(s, Style::default().fg(Color::White));

    let mut lines: Vec<Line<'a>> = Vec::new();

    // Header — use display_label() so brief-less decisions get their
    // trigger-based label instead of a bare "—".
    let age = signal
        .ts
        .map(state::relative_time)
        .unwrap_or_else(|| "?".to_string());
    lines.push(Line::from(vec![
        Span::styled(format!("{} ", glyph), Style::default().fg(glyph_color)),
        Span::styled(
            signal.signal_type.label().to_string(),
            Style::default()
                .fg(glyph_color)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled("  ·  ", Style::default().fg(MUTED)),
        Span::styled(
            signal.display_label(),
            Style::default().fg(GOLD).add_modifier(Modifier::BOLD),
        ),
        Span::styled("  ·  ", Style::default().fg(MUTED)),
        Span::styled(age, Style::default().fg(MUTED)),
    ]));
    blank();

    // Action required — put this up top for decision escalates so the ask
    // is the first thing you see.
    if let Some(ask) = &p.action_required_from_mattie {
        lines.push(blank());
        lines.push(section("Action required"));
        lines.push(Line::from(vec![
            Span::styled("  → ", Style::default().fg(AMBER)),
            Span::styled(
                ask.clone(),
                Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
            ),
        ]));
    }

    // Summary / trigger
    if let Some(summary) = &p.summary {
        lines.push(blank());
        lines.push(section("Summary"));
        lines.push(Line::from(body_span(summary.clone())));
    }
    if let Some(trigger) = &p.trigger {
        lines.push(blank());
        lines.push(section("Trigger"));
        lines.push(Line::from(body_span(trigger.clone())));
    }

    // Key facts
    if !p.key_facts.is_empty() {
        lines.push(blank());
        lines.push(section("Key facts"));
        for fact in &p.key_facts {
            lines.push(Line::from(vec![
                Span::styled("  · ", Style::default().fg(MUTED)),
                body_span(fact.clone()),
            ]));
        }
    }

    // Options
    if !p.options.is_empty() {
        lines.push(blank());
        lines.push(section("Options"));
        for opt in &p.options {
            let is_rec = match (&opt.id, &p.scav_recommendation) {
                (Some(id), Some(rec)) => rec.contains(id.as_str()),
                _ => false,
            };
            let id_str = opt.id.clone().unwrap_or_else(|| "—".to_string());
            let id_style = if is_rec {
                Style::default()
                    .fg(STAMP_GREEN)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(GOLD).add_modifier(Modifier::BOLD)
            };
            let marker = if is_rec { "  ★ " } else { "  ◇ " };

            // Headline row: id + label (prefer `label`, fall back to `action`)
            let mut headline_spans = vec![
                Span::styled(
                    marker,
                    Style::default().fg(if is_rec { STAMP_GREEN } else { MUTED }),
                ),
                Span::styled(id_str, id_style),
            ];
            if let Some(headline) = opt.headline() {
                headline_spans.push(Span::styled("  ", Style::default()));
                headline_spans.push(Span::styled(
                    headline.to_string(),
                    Style::default().fg(Color::White),
                ));
            }
            lines.push(Line::from(headline_spans));

            // brief-008 schema: separate action line already covered by headline fallback.
            // when_right / cost_if_wrong (brief-008 shape)
            if let Some(w) = &opt.when_right {
                lines.push(Line::from(vec![
                    Span::styled("      when right:     ", Style::default().fg(MUTED)),
                    Span::styled(w.clone(), Style::default().fg(Color::White)),
                ]));
            }
            if let Some(c) = &opt.cost_if_wrong {
                lines.push(Line::from(vec![
                    Span::styled("      cost if wrong: ", Style::default().fg(MUTED)),
                    Span::styled(c.clone(), Style::default().fg(Color::White)),
                ]));
            }

            // cost / pros / cons (brief-009-followup shape)
            if let Some(cost) = &opt.cost {
                lines.push(Line::from(vec![
                    Span::styled("      cost: ", Style::default().fg(MUTED)),
                    Span::styled(cost.clone(), Style::default().fg(Color::White)),
                ]));
            }
            if let Some(eta) = &opt.estimated_time {
                lines.push(Line::from(vec![
                    Span::styled("      time: ", Style::default().fg(MUTED)),
                    Span::styled(
                        eta.clone(),
                        Style::default().fg(AMBER).add_modifier(Modifier::BOLD),
                    ),
                ]));
            }
            if let Some(outcome) = &opt.outcome {
                lines.push(Line::from(vec![
                    Span::styled("      outcome: ", Style::default().fg(MUTED)),
                    Span::styled(outcome.clone(), Style::default().fg(Color::White)),
                ]));
            }
            if let Some(wtp) = &opt.when_to_pick {
                lines.push(Line::from(vec![
                    Span::styled("      when to pick: ", Style::default().fg(MUTED)),
                    Span::styled(wtp.clone(), Style::default().fg(Color::White)),
                ]));
            }
            if !opt.pros.is_empty() {
                lines.push(Line::from(Span::styled(
                    "      pros:",
                    Style::default().fg(STAMP_GREEN),
                )));
                for p_item in &opt.pros {
                    lines.push(Line::from(vec![
                        Span::styled("        + ", Style::default().fg(STAMP_GREEN)),
                        body_span(p_item.clone()),
                    ]));
                }
            }
            if !opt.cons.is_empty() {
                lines.push(Line::from(Span::styled(
                    "      cons:",
                    Style::default().fg(CORAL),
                )));
                for c_item in &opt.cons {
                    lines.push(Line::from(vec![
                        Span::styled("        − ", Style::default().fg(CORAL)),
                        body_span(c_item.clone()),
                    ]));
                }
            }
        }
    }

    // Recommendation
    if let Some(rec) = &p.scav_recommendation {
        lines.push(blank());
        lines.push(section("Recommendation"));
        lines.push(Line::from(vec![
            Span::styled("  ★ ", Style::default().fg(STAMP_GREEN)),
            Span::styled(
                rec.clone(),
                Style::default()
                    .fg(STAMP_GREEN)
                    .add_modifier(Modifier::BOLD),
            ),
        ]));
        if let Some(reasoning) = &p.scav_reasoning {
            lines.push(Line::from(vec![
                Span::styled("    ", Style::default()),
                body_span(reasoning.clone()),
            ]));
        }
    }

    if let Some(guard) = &p.anti_pattern_guardrail {
        lines.push(blank());
        lines.push(section("Guardrail"));
        lines.push(Line::from(vec![
            Span::styled("  ⚠ ", Style::default().fg(CORAL)),
            Span::styled(guard.clone(), Style::default().fg(Color::White)),
        ]));
    }

    if let Some(feel) = &p.what_you_should_feel {
        lines.push(blank());
        lines.push(section("What you should feel"));
        lines.push(Line::from(body_span(feel.clone())));
    }

    // Artifacts
    let has_artifacts = p.evaluation.is_some() || p.screenshot_to_review.is_some();
    if has_artifacts {
        lines.push(blank());
        lines.push(section("Artifacts"));
        if let Some(e) = &p.evaluation {
            lines.push(Line::from(vec![
                Span::styled("  eval:  ", Style::default().fg(MUTED)),
                Span::styled(e.clone(), Style::default().fg(Color::White)),
            ]));
        }
        if let Some(s) = &p.screenshot_to_review {
            lines.push(Line::from(vec![
                Span::styled("  shot:  ", Style::default().fg(MUTED)),
                Span::styled(s.clone(), Style::default().fg(Color::White)),
            ]));
        }
    }

    // Fallback: if payload is empty, surface the bare reason/note so the modal
    // isn't just a blank box (pending-merge / pending-dispatch signals land
    // here).
    if !p.has_content() {
        if let Some(reason) = &signal.reason {
            lines.push(blank());
            lines.push(section("Reason"));
            lines.push(Line::from(body_span(reason.clone())));
        } else {
            lines.push(blank());
            lines.push(Line::from(Span::styled(
                "  (no payload detail — signal file is minimal)",
                Style::default().fg(MUTED),
            )));
        }
    }

    lines.push(blank());
    lines.push(Line::from(Span::styled(
        "  j/k scroll  ·  esc / enter close",
        Style::default().fg(MUTED),
    )));

    Text::from(lines)
}

// ── help modal ────────────────────────────────────────────────────────────────

fn centered_rect(width: u16, height: u16, area: Rect) -> Rect {
    let x = area.x + area.width.saturating_sub(width) / 2;
    let y = area.y + area.height.saturating_sub(height) / 2;
    Rect {
        x,
        y,
        width: width.min(area.width),
        height: height.min(area.height),
    }
}

fn render_help_modal<'a>() -> Text<'a> {
    let key = |k: &'static str| Span::styled(k, Style::default().fg(GOLD).add_modifier(Modifier::BOLD));
    let desc = |d: &'static str| Span::styled(d, Style::default().fg(Color::White));
    let sep = || Span::styled("  ", Style::default());
    let section = |s: &'static str| {
        Line::from(Span::styled(s, Style::default().fg(LAVENDER).add_modifier(Modifier::BOLD)))
    };
    let blank = || Line::from("");

    let color_chip = |label: &'static str, color: Color| {
        Line::from(vec![
            Span::styled("  ◆ ", Style::default().fg(color)),
            Span::styled(label, Style::default().fg(MUTED)),
        ])
    };

    Text::from(vec![
        section("  Key Bindings"),
        blank(),
        Line::from(vec![key("  q"), sep(), desc("quit")]),
        Line::from(vec![key("  ctrl-c"), sep(), desc("quit")]),
        Line::from(vec![key("  tab"), sep(), desc("cycle focus between panels")]),
        Line::from(vec![key("  j / k"), sep(), desc("scroll panel, or move cursor in Signals")]),
        Line::from(vec![key("  enter"), sep(), desc("open signal detail (when Signals focused)")]),
        Line::from(vec![key("  esc"), sep(), desc("close modal")]),
        Line::from(vec![key("  r"), sep(), desc("toggle Run Cards view in Signals slot (auto-promotes when runs are active)")]),
        Line::from(vec![key("  ?"), sep(), desc("toggle this help modal")]),
        blank(),
        section("  Color Legend"),
        blank(),
        color_chip("queen (dispatch/evaluate)", LAVENDER),
        color_chip("daemon (heartbeat)", AMBER),
        color_chip("worker (nectar collector)", POP_GREEN),
        color_chip("validator (quality gate)", ORANGE),
        color_chip("builder / coder / researcher", GOLD),
        color_chip("reviewer", INDIGO),
        color_chip("errors / escalate", CORAL),
        color_chip("approve / merge / stamp", STAMP_GREEN),
        color_chip("pending-dispatch signal", BLUE),
        color_chip("noop / idle / heartbeat", MUTED),
        blank(),
        Line::from(Span::styled("  press ? or esc to close", Style::default().fg(MUTED))),
    ])
}

// ── entry point ───────────────────────────────────────────────────────────────

pub fn detect_loop_root() -> bool {
    Path::new(".loop").exists()
}

fn main() -> Result<()> {
    if !Path::new(".loop").exists() {
        eprintln!("No .loop/ directory found — hive must be run from a project root (run 'loop init' first).");
        std::process::exit(1);
    }

    setup_panic_hook();
    redirect_stderr_to_log();

    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;

    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let cwd = std::env::current_dir()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "unknown".to_string());

    let result = run_app(&mut terminal, &cwd);

    restore_terminal();
    result
}

// ── render loop ───────────────────────────────────────────────────────────────

fn run_app<B: ratatui::backend::Backend>(terminal: &mut Terminal<B>, cwd: &str) -> Result<()> {
    let mut app = App::new();

    // File-change notifications from .loop/state/
    let (watch_tx, watch_rx) = mpsc::channel::<()>();
    let mut watcher = notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
        if res.is_ok() {
            let _ = watch_tx.send(());
        }
    })?;
    if Path::new(".loop/state").exists() {
        watcher.watch(Path::new(".loop/state"), RecursiveMode::Recursive)?;
    }
    if Path::new(".loop/logs").exists() {
        watcher.watch(Path::new(".loop/logs"), RecursiveMode::Recursive)?;
    }

    let mut last_state_refresh = Instant::now();

    loop {
        app.maybe_rotate_learning();

        // Compute Buzz row width for Up/Down cursor navigation (approximation from terminal size).
        let buzz_hexes_per_row = terminal
            .size()
            .map(|sz| {
                let right_w = (sz.width as usize * 67 / 100).saturating_sub(2);
                (right_w / 2).max(1)
            })
            .unwrap_or(10);

        let hive_text = render_hive(&app);
        let cells_text = render_cells(&app.cells, app.config.layout.active_section_height);
        let (dance_floor_text, dance_floor_line_count) = render_dance_floor(&app.dance_floor);
        // Mode switch: Signals-slot priority (high→low):
        //   1. Alert signals (signals present) — overrides both buzz and learnings
        //   2. Buzz hex grid — when signals_show_buzz toggled ON and no alerts
        //   3. "From the Hive" learnings — calm default
        let signals_slot_calm = app.signals.signals.is_empty();
        let signals_slot_buzz = app.signals_show_buzz && signals_slot_calm;
        let signals_slot_runs = app.signals_show_runs && signals_slot_calm && !signals_slot_buzz;
        let signals_text = if !signals_slot_calm {
            render_signals(
                &app.signals,
                app.signal_cursor,
                app.focused == Panel::Signals,
            )
        } else if signals_slot_buzz || signals_slot_runs {
            // Buzz and Runs are rendered directly inside the draw closure using
            // the actual slot rect — Text::default() here is never displayed.
            Text::default()
        } else {
            render_hive_learning(app.learnings.pick(app.learning_index))
        };
        let signals_title = if !signals_slot_calm {
            Panel::Signals.label()
        } else if signals_slot_buzz {
            " Buzz "
        } else if signals_slot_runs {
            " Runs "
        } else {
            " From the Hive "
        };
        let df_auto = app.dance_floor_auto_scroll;
        let df_manual_scroll = app.scroll[Panel::DanceFloor as usize];
        let show_buzz = app.focused == Panel::Buzz;
        let show_help = app.show_help;
        let show_signal_detail = app.show_signal_detail;
        let buzz_cursor_idx = app.buzz_cursor;
        let signal_modal_scroll = app.signal_modal_scroll;
        let selected_signal_idx = if show_signal_detail {
            Some(app.signal_cursor)
        } else {
            None
        };

        terminal.draw(|f| {
            let area = f.area();

            let outer = Layout::default()
                .direction(Direction::Vertical)
                .constraints([
                    Constraint::Length(1), // header
                    Constraint::Length(7), // top row: hive + signals
                    Constraint::Min(0),    // main row: cells + dance floor
                    Constraint::Length(1), // footer
                ])
                .split(area);

            // ── header ────────────────────────────────────────────────────────
            f.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(
                        "🐝 Beehive",
                        Style::default().fg(app.col_primary()).add_modifier(Modifier::BOLD),
                    ),
                    Span::styled("  ·  ", Style::default().fg(app.col_muted())),
                    Span::styled(cwd, Style::default().fg(app.col_muted())),
                ])),
                outer[0],
            );

            // ── top row ───────────────────────────────────────────────────────
            let top_row = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(30), Constraint::Percentage(70)])
                .split(outer[1]);

            f.render_widget(
                Paragraph::new(hive_text).block(app.panel_block(Panel::Hive)),
                top_row[0],
            );

            {
                let signals_block = app.panel_block_titled(Panel::Signals, signals_title);
                if signals_slot_buzz {
                    // Render buzz hex grid directly inside the signals slot.
                    // Split inner area: grid rows + legend (1 line at bottom).
                    let inner = signals_block.inner(top_row[1]);
                    f.render_widget(signals_block, top_row[1]);
                    let inner_h = inner.height;
                    let (grid_area, legend_area) = if inner_h > 1 {
                        let parts = Layout::default()
                            .direction(Direction::Vertical)
                            .constraints([
                                Constraint::Min(1),
                                Constraint::Length(1),
                            ])
                            .split(inner);
                        (parts[0], parts[1])
                    } else {
                        (inner, inner)
                    };
                    let buzz_text =
                        buzz::render_buzz(&app.buzz, grid_area, buzz_cursor_idx);
                    f.render_widget(
                        Paragraph::new(buzz_text)
                            .scroll((app.scroll[Panel::Signals as usize], 0)),
                        grid_area,
                    );
                    if inner_h > 1 {
                        f.render_widget(
                            Paragraph::new(buzz::render_buzz_legend()),
                            legend_area,
                        );
                    }
                } else if signals_slot_runs {
                    // Render run cards panel (2x2 grid + Recent list) directly
                    // inside the signals slot.
                    let inner = signals_block.inner(top_row[1]);
                    f.render_widget(signals_block, top_row[1]);
                    render_run_cards(f, inner, &app);
                } else {
                    f.render_widget(
                        Paragraph::new(signals_text)
                            .scroll((app.scroll[Panel::Signals as usize], 0))
                            .wrap(ratatui::widgets::Wrap { trim: false })
                            .block(signals_block),
                        top_row[1],
                    );
                }
            }

            // ── main row ──────────────────────────────────────────────────────
            let main_row = Layout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Percentage(33), Constraint::Percentage(67)])
                .split(outer[2]);

            f.render_widget(
                Paragraph::new(cells_text)
                    .scroll((app.scroll[Panel::Cells as usize], 0))
                    .block(app.panel_block(Panel::Cells)),
                main_row[0],
            );

            // Right main slot: Buzz when focused, otherwise Dance Floor
            if show_buzz {
                let buzz_block = app.panel_block(Panel::Buzz);
                let inner = buzz_block.inner(main_row[1]);
                f.render_widget(buzz_block, main_row[1]);

                // Split inner area: grid (most), detail (2 lines), legend (1 line)
                let buzz_layout = Layout::default()
                    .direction(Direction::Vertical)
                    .constraints([
                        Constraint::Min(2),
                        Constraint::Length(2),
                        Constraint::Length(1),
                    ])
                    .split(inner);

                let buzz_text = buzz::render_buzz(&app.buzz, buzz_layout[0], buzz_cursor_idx);
                let buzz_scroll = app.scroll[Panel::Buzz as usize];
                f.render_widget(
                    Paragraph::new(buzz_text).scroll((buzz_scroll, 0)),
                    buzz_layout[0],
                );
                f.render_widget(
                    Paragraph::new(buzz::render_buzz_detail(&app.buzz, buzz_cursor_idx)),
                    buzz_layout[1],
                );
                f.render_widget(
                    Paragraph::new(buzz::render_buzz_legend()),
                    buzz_layout[2],
                );
            } else {
                // Auto-scroll: pin dance floor to bottom unless user manually scrolled up
                let df_inner_h = main_row[1].height.saturating_sub(2);
                let df_scroll = if df_auto {
                    dance_floor_line_count.saturating_sub(df_inner_h)
                } else {
                    df_manual_scroll
                };
                f.render_widget(
                    Paragraph::new(dance_floor_text)
                        .scroll((df_scroll, 0))
                        .block(app.panel_block(Panel::DanceFloor)),
                    main_row[1],
                );
            }

            // ── footer ────────────────────────────────────────────────────────
            f.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled("  q ", Style::default().fg(GOLD)),
                    Span::styled("quit  ", Style::default().fg(MUTED)),
                    Span::styled("·  j/k ", Style::default().fg(GOLD)),
                    Span::styled("scroll  ", Style::default().fg(MUTED)),
                    Span::styled("·  tab ", Style::default().fg(GOLD)),
                    Span::styled("cycle  ", Style::default().fg(MUTED)),
                    Span::styled("·  b ", Style::default().fg(GOLD)),
                    Span::styled("buzz  ", Style::default().fg(MUTED)),
                    Span::styled("·  r ", Style::default().fg(GOLD)),
                    Span::styled("runs toggle  ", Style::default().fg(MUTED)),
                    Span::styled("·  c ", Style::default().fg(GOLD)),
                    Span::styled("cells  ", Style::default().fg(MUTED)),
                    Span::styled("·  ? ", Style::default().fg(GOLD)),
                    Span::styled("help", Style::default().fg(MUTED)),
                ])),
                outer[3],
            );

            // ── help modal overlay ────────────────────────────────────────────
            if show_help {
                let modal_rect = centered_rect(62, 28, area);
                f.render_widget(Clear, modal_rect);
                f.render_widget(
                    Paragraph::new(render_help_modal())
                        .block(
                            Block::default()
                                .borders(Borders::ALL)
                                .border_type(BorderType::Double)
                                .border_style(Style::default().fg(LAVENDER))
                                .title(Span::styled(
                                    " Beehive — Help ",
                                    Style::default()
                                        .fg(LAVENDER)
                                        .add_modifier(Modifier::BOLD),
                                )),
                        ),
                    modal_rect,
                );
            }

            // ── signal detail modal overlay ───────────────────────────────────
            if let Some(idx) = selected_signal_idx {
                if let Some(signal) = app.signals.signals.get(idx) {
                    // Consume most of the screen — payloads are verbose.
                    let w = area.width.saturating_sub(6).min(110);
                    let h = area.height.saturating_sub(4);
                    let modal_rect = centered_rect(w, h, area);
                    f.render_widget(Clear, modal_rect);
                    let border_color = match signal.signal_type {
                        state::SignalType::Escalate => CORAL,
                        state::SignalType::PendingMerge => STAMP_GREEN,
                        state::SignalType::PendingDispatch => BLUE,
                        state::SignalType::Unknown(_) => MUTED,
                    };
                    f.render_widget(
                        Paragraph::new(render_signal_modal(signal))
                            .scroll((signal_modal_scroll, 0))
                            .wrap(ratatui::widgets::Wrap { trim: false })
                            .block(
                                Block::default()
                                    .borders(Borders::ALL)
                                    .border_type(BorderType::Double)
                                    .border_style(Style::default().fg(border_color))
                                    .title(Span::styled(
                                        " Signal detail ",
                                        Style::default()
                                            .fg(border_color)
                                            .add_modifier(Modifier::BOLD),
                                    )),
                            ),
                        modal_rect,
                    );
                }
            }
        })?;

        // Drain file-change events; flag if any came in
        let file_changed = watch_rx.try_recv().is_ok();
        while watch_rx.try_recv().is_ok() {}

        // Refresh state on file change or every 1s (for age strings)
        if file_changed || last_state_refresh.elapsed() >= Duration::from_secs(1) {
            app.refresh_state();
            last_state_refresh = Instant::now();
        }

        // Advance spinner every poll cycle (~200ms)
        app.tick_spinner();

        if event::poll(Duration::from_millis(200))? {
            match event::read()? {
                Event::Key(key) => {
                    // ── signal detail modal has highest priority ───────────────
                    if app.show_signal_detail {
                        match key.code {
                            KeyCode::Esc | KeyCode::Enter => app.close_signal_detail(),
                            KeyCode::Char('q') => break,
                            KeyCode::Char('c')
                                if key.modifiers.contains(KeyModifiers::CONTROL) =>
                            {
                                break
                            }
                            KeyCode::Char('j') => {
                                app.signal_modal_scroll =
                                    app.signal_modal_scroll.saturating_add(1);
                            }
                            KeyCode::Char('k') => {
                                app.signal_modal_scroll =
                                    app.signal_modal_scroll.saturating_sub(1);
                            }
                            _ => {}
                        }
                    } else if app.show_help {
                        match key.code {
                            KeyCode::Char('q') => break,
                            KeyCode::Char('c')
                                if key.modifiers.contains(KeyModifiers::CONTROL) =>
                            {
                                break
                            }
                            KeyCode::Char('?') | KeyCode::Esc => app.toggle_help(),
                            _ => {}
                        }
                    } else {
                        match key.code {
                            KeyCode::Char('q') => break,
                            KeyCode::Char('c')
                                if key.modifiers.contains(KeyModifiers::CONTROL) =>
                            {
                                break
                            }
                            KeyCode::Char('?') => app.toggle_help(),
                            KeyCode::Tab => app.focus_next(),
                            // b → toggle Buzz hex grid in Signals slot (focus Signals on activation)
                            KeyCode::Char('b') => {
                                app.signals_show_buzz = !app.signals_show_buzz;
                                if app.signals_show_buzz {
                                    app.focused = Panel::Signals;
                                }
                            }
                            KeyCode::Char('c') => app.focused = Panel::Cells,
                            KeyCode::Enter if app.focused == Panel::Signals => {
                                app.open_signal_detail();
                            }
                            KeyCode::Char('j') => {
                                if app.focused == Panel::Signals
                                    && !app.signals_show_buzz
                                    && !app.signals_show_runs
                                {
                                    app.signal_cursor_down();
                                } else {
                                    app.scroll_down();
                                }
                            }
                            KeyCode::Char('k') => {
                                if app.focused == Panel::Signals
                                    && !app.signals_show_buzz
                                    && !app.signals_show_runs
                                {
                                    app.signal_cursor_up();
                                } else {
                                    if app.focused == Panel::DanceFloor {
                                        app.dance_floor_auto_scroll = false;
                                    }
                                    app.scroll_up();
                                }
                            }
                            // r → toggle Run Cards view in Signals slot
                            KeyCode::Char('r') => {
                                app.signals_show_runs = !app.signals_show_runs;
                                if app.signals_show_runs {
                                    app.focused = Panel::Signals;
                                }
                                app.dance_floor_auto_scroll = true;
                            }
                            KeyCode::Left
                                if app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz) =>
                            {
                                app.buzz_cursor = app.buzz_cursor.saturating_sub(1);
                            }
                            KeyCode::Right
                                if (app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz))
                                    && !app.buzz.events.is_empty() =>
                            {
                                let max = app.buzz.events.len() - 1;
                                app.buzz_cursor = (app.buzz_cursor + 1).min(max);
                            }
                            KeyCode::Up
                                if app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz) =>
                            {
                                app.buzz_cursor =
                                    app.buzz_cursor.saturating_sub(buzz_hexes_per_row);
                            }
                            KeyCode::Down
                                if (app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz))
                                    && !app.buzz.events.is_empty() =>
                            {
                                let max = app.buzz.events.len() - 1;
                                app.buzz_cursor =
                                    (app.buzz_cursor + buzz_hexes_per_row).min(max);
                            }
                            KeyCode::Char('[')
                                if app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz) =>
                            {
                                app.buzz_window_offset_secs += 3 * 3600 / 2;
                                app.reload_buzz();
                            }
                            KeyCode::Char(']')
                                if app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz) =>
                            {
                                app.buzz_window_offset_secs =
                                    (app.buzz_window_offset_secs - 3 * 3600 / 2).max(0);
                                app.reload_buzz();
                            }
                            KeyCode::Char('=')
                                if app.focused == Panel::Buzz
                                    || (app.focused == Panel::Signals
                                        && app.signals_show_buzz) =>
                            {
                                app.buzz_window_offset_secs = 0;
                                app.reload_buzz();
                            }
                            _ => {}
                        }
                    }
                }
                Event::Resize(_, _) => {}
                _ => {}
            }
        }
    }

    Ok(())
}

// ── tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detect_loop_root_in_tmpdir_no_panic() {
        let original = std::env::current_dir().unwrap();
        let tmp = std::env::temp_dir();
        std::env::set_current_dir(&tmp).unwrap();
        let _ = detect_loop_root();
        std::env::set_current_dir(&original).unwrap();
    }

    #[test]
    fn clean_event_strips_brief_id_so_error_in_slug_does_not_color_row_coral() {
        // brief-217-error-catalog-and-envelope-retrofit appearing in a worker
        // message used to flag the whole row as coral because event_color
        // substring-matches "error". Stripping the brief id (already shown in
        // its own column) defuses the false positive.
        let raw = "rebased brief-217-error-catalog-and-envelope-retrofit onto origin/main (2 commits)";
        let cleaned = clean_event_message(raw, Some("brief-217-error-catalog-and-envelope-retrofit"));
        assert!(!cleaned.contains("error"), "stripped msg should not contain 'error': {cleaned}");
        assert!(!matches!(event_color(Some(&cleaned)), CORAL));
    }

    #[test]
    fn clean_event_preserves_real_error_keyword_outside_brief_id() {
        // If the message has a real "error" keyword elsewhere, stripping the
        // brief id must NOT scrub it — actual errors should still color red.
        let raw = "WORKER iteration FAILED for brief-300-foo — exit 1";
        let cleaned = clean_event_message(raw, Some("brief-300-foo"));
        assert!(cleaned.contains("FAILED"));
        assert!(!cleaned.contains("brief-300-foo"));
    }

    #[test]
    fn clean_event_no_brief_returns_raw() {
        let raw = "QUEEN #1: complete (33s)";
        assert_eq!(clean_event_message(raw, None), raw);
        assert_eq!(clean_event_message(raw, Some("")), raw);
    }

    #[test]
    fn clean_event_brief_not_substring_returns_raw_unchanged() {
        let raw = "daemon:dispatch";
        let cleaned = clean_event_message(raw, Some("brief-218"));
        assert_eq!(cleaned, raw);
    }

    #[test]
    fn clean_event_fallback_when_strip_leaves_empty() {
        // If the message *is* just the brief id, the stripped string would be
        // empty — fall back to the raw value so the row doesn't render blank.
        let raw = "brief-217-error-catalog";
        let cleaned = clean_event_message(raw, Some("brief-217-error-catalog"));
        assert_eq!(cleaned, raw);
    }

    #[test]
    fn display_actor_canonicalizes_legacy_conductor_to_queen() {
        // Legacy log events written before brief-065 (conductor → queen rename)
        // still live in log.jsonl as forensic record. Dance Floor renders the
        // current term so it stays consistent with apiary-glossary.md.
        assert_eq!(display_actor(Some("conductor")), "queen");
        assert_eq!(display_actor(Some("queen")), "queen");
        assert_eq!(display_actor(Some("worker")), "worker");
        assert_eq!(display_actor(Some("scout")), "scout");
        assert_eq!(display_actor(None), "?");
    }

    #[test]
    fn panel_tab_cycles_all_five() {
        let mut app = App::new();
        app.focused = Panel::Hive;
        app.focus_next();
        assert_eq!(app.focused, Panel::Signals);
        app.focus_next();
        assert_eq!(app.focused, Panel::Cells);
        app.focus_next();
        assert_eq!(app.focused, Panel::DanceFloor);
        app.focus_next();
        assert_eq!(app.focused, Panel::Buzz);
        app.focus_next();
        assert_eq!(app.focused, Panel::Hive);
    }

    #[test]
    fn scroll_is_per_panel_and_saturates_at_zero() {
        let mut app = App::new();
        app.focused = Panel::Cells;
        app.scroll_down();
        app.scroll_down();
        assert_eq!(app.scroll[Panel::Cells as usize], 2);
        assert_eq!(app.scroll[Panel::DanceFloor as usize], 0);
        app.focused = Panel::DanceFloor;
        app.scroll_up();
        assert_eq!(app.scroll[Panel::DanceFloor as usize], 0);
    }

    #[test]
    fn spinner_cycles_through_all_frames() {
        let mut app = App::new();
        for _ in 0..SPINNER_FRAMES.len() {
            app.tick_spinner();
        }
        // Back to frame 0 after full cycle
        assert_eq!(app.spinner_frame, 0);
    }

    fn app_with_buzz_events(n: usize) -> App {
        let mut app = App::new();
        let events = (0..n)
            .map(|_| buzz::BuzzEvent {
                ts: None,
                actor: Some("worker".to_string()),
                action: None,
                brief: None,
                cost_usd: 0.10,
                intensity_bucket: 2,
                duration_ms: None,
            })
            .collect();
        app.buzz = buzz::BuzzState { events };
        app
    }

    #[test]
    fn buzz_cursor_clamps_at_zero_going_left() {
        let mut app = app_with_buzz_events(3);
        app.buzz_cursor = 0;
        app.buzz_cursor = app.buzz_cursor.saturating_sub(1);
        assert_eq!(app.buzz_cursor, 0);
    }

    #[test]
    fn buzz_cursor_clamps_at_max_going_right() {
        let mut app = app_with_buzz_events(3);
        for _ in 0..10 {
            let max = app.buzz.events.len().saturating_sub(1);
            app.buzz_cursor = (app.buzz_cursor + 1).min(max);
        }
        assert_eq!(app.buzz_cursor, 2); // 3 events, max index = 2
    }

    #[test]
    fn buzz_window_offset_increments_on_pan_left() {
        let mut app = App::new();
        assert_eq!(app.buzz_window_offset_secs, 0);
        app.buzz_window_offset_secs += 3 * 3600 / 2;
        assert_eq!(app.buzz_window_offset_secs, 5400);
    }

    #[test]
    fn buzz_window_offset_does_not_go_below_zero_on_pan_right() {
        let mut app = App::new();
        // Simulate pan-right from zero: offset cannot go negative
        let new_offset = app.buzz_window_offset_secs - 5400;
        app.buzz_window_offset_secs = new_offset.max(0);
        assert_eq!(app.buzz_window_offset_secs, 0);
    }

    #[test]
    fn buzz_window_offset_resets_to_zero_on_equal() {
        let mut app = App::new();
        app.buzz_window_offset_secs = 10800;
        app.buzz_window_offset_secs = 0;
        assert_eq!(app.buzz_window_offset_secs, 0);
    }

    /// Build an App with a synthetic signals list for cursor-behavior tests.
    fn app_with_signals(n: usize) -> App {
        let mut app = App::new();
        // Replace signals with n dummies.
        let signals: Vec<state::Signal> = (0..n)
            .map(|i| state::Signal {
                signal_type: state::SignalType::Escalate,
                brief: Some(format!("brief-{:03}", i)),
                reason: None,
                ts: None,
                filename: format!("escalate-{}.json", i),
                payload: state::SignalPayload::default(),
            })
            .collect();
        app.signals = state::SignalsState { signals };
        app
    }

    #[test]
    fn signal_cursor_clamps_at_zero_going_up() {
        let mut app = app_with_signals(3);
        app.signal_cursor_up();
        assert_eq!(app.signal_cursor, 0);
    }

    #[test]
    fn signal_cursor_clamps_at_end_going_down() {
        let mut app = app_with_signals(3);
        for _ in 0..10 {
            app.signal_cursor_down();
        }
        assert_eq!(app.signal_cursor, 2);
    }

    #[test]
    fn open_signal_detail_noop_when_no_signals() {
        let mut app = app_with_signals(0);
        app.open_signal_detail();
        assert!(!app.show_signal_detail);
    }

    #[test]
    fn open_signal_detail_opens_when_signals_present() {
        let mut app = app_with_signals(2);
        app.open_signal_detail();
        assert!(app.show_signal_detail);
        assert_eq!(app.signal_modal_scroll, 0);
        app.close_signal_detail();
        assert!(!app.show_signal_detail);
    }

    #[test]
    fn progress_bar_boundary_cases() {
        // Empty bar
        assert_eq!(progress_bar_str(0.0, 10), "          ");
        // Full bar
        assert_eq!(progress_bar_str(1.0, 10), "██████████");
        // Overflow clamps to full
        assert_eq!(progress_bar_str(1.5, 10), "██████████");
        // 50% of 10 cells = 5 full, 5 empty
        assert_eq!(progress_bar_str(0.5, 10), "█████     ");
        // Partial block rendered for fractional fill (25% of 8 = 2 full + one ▎)
        let bar = progress_bar_str(0.25, 8);
        assert!(bar.starts_with("██"), "got: {:?}", bar);
        assert_eq!(bar.chars().count(), 8, "width should be preserved");
    }

    #[test]
    fn budget_color_matches_thresholds() {
        // 0% → green
        assert_eq!(budget_color(0, 10), STAMP_GREEN);
        // 50% → green
        assert_eq!(budget_color(5, 10), STAMP_GREEN);
        // 75% → amber
        assert_eq!(budget_color(8, 10), AMBER);
        // 100% → amber (still at cap)
        assert_eq!(budget_color(10, 10), AMBER);
        // Over budget → coral
        assert_eq!(budget_color(11, 10), CORAL);
    }

    #[test]
    fn selected_signal_follows_cursor() {
        let mut app = app_with_signals(3);
        app.signal_cursor_down();
        let sel = app.selected_signal().unwrap();
        assert_eq!(sel.brief.as_deref(), Some("brief-001"));
    }

    // ── signals_show_buzz toggle (brief-120) ──────────────────────────────────

    #[test]
    fn signals_show_buzz_defaults_false() {
        let app = App::new();
        assert!(!app.signals_show_buzz);
    }

    #[test]
    fn signals_show_buzz_toggle_focuses_signals_on_activation() {
        let mut app = App::new();
        app.focused = Panel::DanceFloor;
        // Simulate pressing `b` (toggle ON)
        app.signals_show_buzz = !app.signals_show_buzz;
        if app.signals_show_buzz {
            app.focused = Panel::Signals;
        }
        assert!(app.signals_show_buzz);
        assert_eq!(app.focused, Panel::Signals);
    }

    #[test]
    fn signals_show_buzz_toggle_off_does_not_change_focus() {
        let mut app = App::new();
        app.signals_show_buzz = true;
        app.focused = Panel::Signals;
        // Simulate pressing `b` again (toggle OFF)
        app.signals_show_buzz = !app.signals_show_buzz;
        assert!(!app.signals_show_buzz);
        // Focus stays on Signals (not reset)
        assert_eq!(app.focused, Panel::Signals);
    }

    #[test]
    fn signals_slot_buzz_suppressed_when_alerts_present() {
        // Even if signals_show_buzz is true, alerts (non-empty signals) take priority.
        // The rendering condition is: signals_slot_buzz = signals_show_buzz && signals_slot_calm.
        let mut app = app_with_signals(1);
        app.signals_show_buzz = true;
        let signals_slot_calm = app.signals.signals.is_empty();
        let signals_slot_buzz = app.signals_show_buzz && signals_slot_calm;
        assert!(!signals_slot_buzz, "alerts should override buzz toggle");
    }

    // ── run cards (brief-125 C2) ──────────────────────────────────────────────

    fn make_run_card(run_id: &str, status: state::RunStatus) -> state::RunCard {
        state::RunCard {
            run_id: run_id.to_string(),
            policy: Some("act".to_string()),
            dataset: Some("test-dataset".to_string()),
            machine: Some("modal:a10g".to_string()),
            status,
            started_at: None,
            completed_at: None,
            heartbeats: vec![],
            heartbeat_sidecar_present: false,
            failure_signal: None,
        }
    }

    #[test]
    fn run_card_slots_all_empty_when_no_active_runs() {
        let cards: Vec<state::RunCard> = vec![
            make_run_card("run-001", state::RunStatus::Complete),
            make_run_card("run-002", state::RunStatus::Failed),
        ];
        let slots = run_card_slots(&cards);
        assert!(slots.iter().all(|s| s.is_none()), "no active runs → all 4 slots None");
    }

    #[test]
    fn run_card_slots_fills_first_two_for_two_active_runs() {
        // C2 pass criterion 3: 2 active runs → first 2 slots filled, last 2 empty
        let cards: Vec<state::RunCard> = vec![
            make_run_card("run-001", state::RunStatus::Running),
            make_run_card("run-002", state::RunStatus::Running),
            make_run_card("run-003", state::RunStatus::Complete),
        ];
        let slots = run_card_slots(&cards);
        assert!(slots[0].is_some(), "slot 0 should be filled");
        assert!(slots[1].is_some(), "slot 1 should be filled");
        assert!(slots[2].is_none(), "slot 2 should be empty");
        assert!(slots[3].is_none(), "slot 3 should be empty");
    }

    #[test]
    fn run_card_slots_all_empty_for_zero_active_runs() {
        // C2 pass criterion 4: 0 active runs → all 4 slots empty (placeholders)
        let cards: Vec<state::RunCard> = vec![];
        let slots = run_card_slots(&cards);
        assert!(slots.iter().all(|s| s.is_none()), "0 active → all 4 slots None");
    }

    #[test]
    fn run_card_slots_caps_at_four_for_five_active_runs() {
        // C2 pass criterion 5: >4 active → only first 4 shown, footer implicit
        let cards: Vec<state::RunCard> = (0..5)
            .map(|i| make_run_card(&format!("run-{:03}", i), state::RunStatus::Running))
            .collect();
        let slots = run_card_slots(&cards);
        assert!(slots.iter().all(|s| s.is_some()), "all 4 slots filled when 5 active");
    }

    #[test]
    fn run_card_slots_includes_stale_runs() {
        let cards: Vec<state::RunCard> = vec![
            make_run_card("run-stale", state::RunStatus::Stale),
            make_run_card("run-complete", state::RunStatus::Complete),
        ];
        let slots = run_card_slots(&cards);
        assert!(slots[0].is_some(), "Stale run should appear in active grid");
        assert!(slots[1].is_none());
    }

    #[test]
    fn compute_pace_returns_none_for_fewer_than_two_points() {
        let hbs = vec![state::RunHeartbeat {
            ts: chrono::Utc::now(),
            last_step: Some(100),
            last_loss: None,
            app_state: None,
            alert: None,
        }];
        assert!(compute_pace(&hbs).is_none());
    }

    #[test]
    fn compute_pace_computes_correctly() {
        use chrono::TimeZone;
        let base = chrono::Utc.with_ymd_and_hms(2026, 5, 2, 12, 0, 0).unwrap();
        let hbs = vec![
            state::RunHeartbeat {
                ts: base,
                last_step: Some(1000),
                last_loss: None,
                app_state: None,
                alert: None,
            },
            state::RunHeartbeat {
                ts: base + chrono::Duration::seconds(100),
                last_step: Some(1500),
                last_loss: None,
                app_state: None,
                alert: None,
            },
        ];
        let pace = compute_pace(&hbs).expect("should compute pace");
        // 500 steps / 100 secs = 5.0 step/s
        assert!((pace - 5.0).abs() < 0.01, "expected 5.0 step/s, got {}", pace);
    }

    // ── recent list (brief-125 C3) ────────────────────────────────────────────

    #[test]
    fn recent_run_lines_sorted_by_completed_at_desc() {
        // C3 pass criterion 6: mixed statuses, sorted by completed_at desc
        use chrono::TimeZone;
        let base = chrono::Utc.with_ymd_and_hms(2026, 5, 1, 12, 0, 0).unwrap();
        let cards = vec![
            // Older complete run
            state::RunCard {
                completed_at: Some(base),
                ..make_run_card("run-old-complete", state::RunStatus::Complete)
            },
            // Newest failed run
            state::RunCard {
                completed_at: Some(base + chrono::Duration::hours(5)),
                ..make_run_card("run-new-failed", state::RunStatus::Failed)
            },
            // Active (must NOT appear in recent list)
            make_run_card("run-active", state::RunStatus::Running),
            // Preempted in between
            state::RunCard {
                completed_at: Some(base + chrono::Duration::hours(2)),
                ..make_run_card("run-preempted", state::RunStatus::Preempted)
            },
            // Pending (no completed_at — trails)
            make_run_card("run-pending", state::RunStatus::Pending),
        ];

        let (lines, overflow) = recent_run_lines(&cards);
        assert_eq!(overflow, 0, "no overflow for 4 recent entries");
        assert_eq!(lines.len(), 4, "running excluded → 4 historical rows");

        // Sorted: newest-failed (base+5h), preempted (base+2h), old-complete (base), pending (no date)
        let text = |line: &ratatui::text::Line<'static>| -> String {
            line.spans.iter().map(|s| s.content.as_ref()).collect()
        };
        assert!(text(&lines[0]).contains("run-new-failed"), "newest failed first");
        assert!(text(&lines[1]).contains("run-preempted"), "preempted second");
        assert!(text(&lines[2]).contains("run-old-complete"), "older complete third");
        assert!(text(&lines[3]).contains("run-pending"), "pending (no date) last");
    }

    #[test]
    fn recent_run_lines_caps_at_six_with_overflow() {
        let cards: Vec<state::RunCard> = (0..8)
            .map(|i| make_run_card(&format!("run-{:03}", i), state::RunStatus::Complete))
            .collect();
        let (lines, overflow) = recent_run_lines(&cards);
        assert_eq!(lines.len(), 6, "capped at 6");
        assert_eq!(overflow, 2, "overflow = total(8) - cap(6)");
    }

    #[test]
    fn recent_run_lines_excludes_running_and_stale() {
        let cards = vec![
            make_run_card("run-running", state::RunStatus::Running),
            make_run_card("run-stale", state::RunStatus::Stale),
            make_run_card("run-complete", state::RunStatus::Complete),
        ];
        let (lines, _) = recent_run_lines(&cards);
        assert_eq!(lines.len(), 1, "only complete appears; running/stale excluded");
        let text: String = lines[0].spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.contains("run-complete"));
    }

    #[test]
    fn recent_run_lines_empty_for_all_active() {
        let cards = vec![
            make_run_card("run-a", state::RunStatus::Running),
            make_run_card("run-b", state::RunStatus::Stale),
        ];
        let (lines, overflow) = recent_run_lines(&cards);
        assert!(lines.is_empty(), "no recent rows when all runs are active");
        assert_eq!(overflow, 0);
    }

    // ── signals_show_runs toggle (brief-125 C4) ───────────────────────────────

    #[test]
    fn signals_show_runs_defaults_false_when_no_active_runs() {
        // In test env wiki/runs/ is absent or has no running cards → defaults false.
        let app = App::new();
        assert!(!app.signals_show_runs);
    }

    #[test]
    fn signals_show_runs_toggle_focuses_signals_on_activation() {
        let mut app = App::new();
        app.focused = Panel::DanceFloor;
        // Simulate pressing `r` (toggle ON)
        app.signals_show_runs = !app.signals_show_runs;
        if app.signals_show_runs {
            app.focused = Panel::Signals;
        }
        assert!(app.signals_show_runs);
        assert_eq!(app.focused, Panel::Signals);
    }

    #[test]
    fn signals_show_runs_toggle_off_does_not_change_focus() {
        let mut app = App::new();
        app.signals_show_runs = true;
        app.focused = Panel::Signals;
        // Simulate pressing `r` again (toggle OFF)
        app.signals_show_runs = !app.signals_show_runs;
        assert!(!app.signals_show_runs);
        assert_eq!(app.focused, Panel::Signals);
    }

    #[test]
    fn signals_slot_runs_suppressed_when_alerts_present() {
        let mut app = app_with_signals(1);
        app.signals_show_runs = true;
        let signals_slot_calm = app.signals.signals.is_empty();
        let signals_slot_buzz = app.signals_show_buzz && signals_slot_calm;
        let signals_slot_runs = app.signals_show_runs && signals_slot_calm && !signals_slot_buzz;
        assert!(!signals_slot_runs, "alerts should override runs toggle");
    }

    #[test]
    fn signals_slot_runs_suppressed_when_buzz_active() {
        let mut app = App::new();
        app.signals_show_buzz = true;
        app.signals_show_runs = true;
        let signals_slot_calm = app.signals.signals.is_empty();
        let signals_slot_buzz = app.signals_show_buzz && signals_slot_calm;
        let signals_slot_runs = app.signals_show_runs && signals_slot_calm && !signals_slot_buzz;
        assert!(!signals_slot_runs, "buzz should override runs toggle");
    }

    #[test]
    fn jk_guard_disabled_when_runs_view_active() {
        let mut app = App::new();
        app.focused = Panel::Signals;
        app.signals_show_runs = true;
        // j/k cursor nav requires: focused Signals AND NOT buzz AND NOT runs
        let should_nav_cursor =
            app.focused == Panel::Signals && !app.signals_show_buzz && !app.signals_show_runs;
        assert!(!should_nav_cursor, "j/k cursor nav disabled when runs view is active");
    }

    #[test]
    fn signals_slot_runs_active_when_calm_and_no_buzz() {
        let mut app = App::new();
        app.signals_show_runs = true;
        app.signals_show_buzz = false;
        let signals_slot_calm = app.signals.signals.is_empty();
        let signals_slot_buzz = app.signals_show_buzz && signals_slot_calm;
        let signals_slot_runs = app.signals_show_runs && signals_slot_calm && !signals_slot_buzz;
        assert!(signals_slot_runs, "runs view active when calm and buzz off");
    }
}
