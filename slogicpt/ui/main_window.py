"""Main window: app bar -> status chips -> stepper -> big result card ->
bottom drawer (report / log / environment setup).

The window only renders; the station state machine is StationController.
Everything the operator can see is kept in plain data (step states, result
state, log/report text), so a language or theme switch rebuilds the widgets
and restores the screen exactly -- including mid-run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from PyQt5.QtCore import QSettings, QSize, Qt, QTimer
from PyQt5.QtGui import QFontDatabase, QTextCursor
from PyQt5.QtWidgets import (
    QAction, QActionGroup, QApplication, QComboBox, QFrame, QHBoxLayout,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea,
    QSizePolicy,
    QVBoxLayout, QWidget,
)

from .. import env_setup
from ..i18n import LANGS, get_language, set_language, t
from ..pipeline import StepStatus, sequence_plan
from ..profiles import OUTPUT_DIR, check_resources, load_profiles
from ..sigrok import SigrokCli, find_sigrok_binary
from . import icons, report, theme
from .station import StationController
from .widgets import (Checklist, Chip, Drawer, GripSplitter, ModeMatrix,
                      ResultCard, Stepper,
                      button, label)


def _linkify(text: str) -> str:
    """Escape `text` for a rich-text label and make its URLs clickable."""
    import html
    import re
    return re.sub(r"(https?://[^\s<]+)", r"<a href='\1'>\1</a>", html.escape(text))


def _clock(seconds: float) -> str:
    """1:35, or 1:02:03 past an hour."""
    m, sec = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


@dataclass
class _Step:
    label: str
    status: StepStatus = StepStatus.PENDING
    t_start: float | None = None
    duration: float | None = None
    note: str = ""                    # why it failed (shown under the step)
    retries: int = 0                  # operator retries on this board


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.profiles, self.problems = load_profiles()
        sigrok_bin = find_sigrok_binary()
        self.problems += check_resources(self.profiles, sigrok_bin)
        self.ctl = StationController(self.profiles,
                                     SigrokCli(sigrok_bin) if sigrok_bin else None, self)

        # operator-visible state, independent of the widgets showing it
        self._steps: dict[str, _Step] = {}
        self._shown_id: str | None = None
        self._fixture_need: dict[str, bool | None] = {}
        self._result: tuple[str, dict] = ("idle", {})
        self._caption = ""                  # "", "remove" or "next"
        self._prompt = ""
        self._current_sid = ""
        self._retry_label = ""              # set while an operator retry runs
        self._run_detail = ""               # this board's report detail so far
        self._run_elapsed = 0.0
        self._run_t0 = 0.0                  # for the live clock
        self._run_kind = ""                 # "full" | "retry" | "manual"
        self._board_sn = ""
        self._progress = ""                 # latest "what is happening" line
        self._modes: dict[tuple[str, str], tuple[str, str]] = {}  # cell -> (state, why)
        self._session: list[bool] = []      # verdict per board this session (memory only)
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._render_result)

        ctl = self.ctl
        ctl.log.connect(self._append_log)
        ctl.step.connect(self._on_step)
        ctl.step_note.connect(self._on_step_note)
        ctl.progress.connect(self._on_progress)
        ctl.mode_result.connect(self._on_mode_result)
        ctl.prompt.connect(self._on_prompt)
        ctl.prompt_clear.connect(self._on_prompt_clear)
        ctl.run_started.connect(self._on_run_started)
        ctl.run_finished.connect(self._on_finished)
        ctl.devices_changed.connect(self._on_devices)
        ctl.state_changed.connect(self._on_state)
        ctl.notice.connect(lambda text: self._show_result("waiting", text=text))
        ctl.board_removed.connect(lambda: self._set_caption("next"))
        ctl.env_done.connect(self._on_env_done)
        ctl.env_status_changed.connect(self._on_env_status)
        ctl.tools_changed.connect(self._on_tools)
        ctl.tool_progress.connect(self._on_tool_progress)
        ctl.tools_done.connect(self._on_tools_done)
        self._tools_guided = False          # opened the setup page once for missing tools
        self._tool_hint = ""                # manual-download instructions after a failure
        ctl.refresh_tools()                 # after connecting, so its signal is not lost

        self.setWindowTitle(t("SLogic Production Test"))
        self.setWindowIcon(icons.icon("usb", "primary", 64))
        self.setMinimumSize(960, 640)
        self._build()
        if not self.profiles:
            QMessageBox.critical(
                self, t("No product profiles"),
                t("No usable product profiles under resources/products/; "
                  "see resources/README.md to add them."))
        ctl.refresh_devices()

    # ================================================================ build

    def _build(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_appbar())
        body = QVBoxLayout()
        body.setContentsMargins(24, 16, 24, 16)
        body.setSpacing(16)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.product_chip = Chip("board")
        self.product_chip.setToolTip(t("Recognised automatically from the board"))
        self.device_chip = Chip("usb")
        chips.addWidget(self.product_chip)
        chips.addWidget(self.device_chip)
        chips.addStretch(1)
        self.stats_chip = Chip("check_circle")
        self.stats_chip.setToolTip(t("Boards tested since the program started "
                                     "(not saved)"))
        chips.addWidget(self.stats_chip)
        body.addLayout(chips)

        step_card = QFrame()
        step_card.setObjectName("card")
        sc = QVBoxLayout(step_card)
        sc.setContentsMargins(8, 8, 8, 8)
        self.stepper = Stepper()
        self.stepper.setMinimumHeight(150)
        self.stepper.run_requested.connect(self._run_single_step)
        self.stepper.retry_requested.connect(self._retry_step)
        sc.addWidget(self.stepper)
        body.addWidget(step_card)

        # verdict card and drawer share the rest; the drawer's height is set by
        # dragging the handle between them and remembered
        self.result_card = ResultCard()
        self.checklist = Checklist()
        self.result_card.add_body(self.checklist)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.result_card.add_body(self.progress_bar)
        self.matrix = ModeMatrix()
        self.result_card.add_body(self.matrix)
        self.split = GripSplitter(Qt.Vertical)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(12)
        self.split.addWidget(self.result_card)
        self.split.addWidget(self._build_drawer())
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.splitterMoved.connect(self._remember_drawer_height)
        body.addWidget(self.split, 1)
        root.addLayout(body, 1)
        self.setCentralWidget(central)

        self._render_problem_chip()
        self._render_stats()
        self._render_tools()
        self._layout_steps(force=True, keep_status=True)
        self._render_result()
        self._on_devices()
        self._on_state()
        self._refresh_env_buttons()

    def _build_appbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("appbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(20, 10, 16, 10)
        h.setSpacing(12)
        title = label(t("SLogic Production Test"), "title")
        h.addWidget(title)
        h.addSpacing(12)

        h.addStretch(1)

        self.warn_chip = Chip("warning", clickable=True)
        self.warn_chip.clicked.connect(self._show_problems)
        h.addWidget(self.warn_chip)

        lang = button("", "icon", "translate", tip=t("Language"))
        menu = QMenu(lang)
        group = QActionGroup(menu)
        for code, name in LANGS:
            a = QAction(name, menu, checkable=True)
            a.setChecked(code == get_language())
            a.triggered.connect(lambda _c, c=code: self._on_language(c))
            group.addAction(a)
            menu.addAction(a)
        lang.setMenu(menu)
        h.addWidget(lang)

        th = button("", "icon", "contrast", tip=t("Theme"))
        menu = QMenu(th)
        group = QActionGroup(menu)
        current = QSettings().value("theme", "auto")
        for mode, text in (("auto", t("Follow system")), ("light", t("Light")),
                           ("dark", t("Dark"))):
            a = QAction(text, menu, checkable=True)
            a.setChecked(mode == current)
            a.triggered.connect(lambda _c, m=mode: self._on_theme(m))
            group.addAction(a)
            menu.addAction(a)
        th.setMenu(menu)
        h.addWidget(th)
        h.addSpacing(4)

        self.reset_btn = button(t("Reset"), "tonal", "refresh",
                                tip=t("Clear the result and reset the bench: the "
                                      "fixture back to M0, devices rescanned"))
        self.reset_btn.clicked.connect(self._on_reset)
        h.addWidget(self.reset_btn)
        self.action_btn = button("", "filled")
        self.action_btn.setMinimumWidth(112)
        self.action_btn.clicked.connect(self._on_action)
        h.addWidget(self.action_btn)
        return bar

    def _build_drawer(self) -> Drawer:
        d = Drawer()
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)

        # each page's action sits in the tab bar, so the text gets the height
        self.report_box = QPlainTextEdit()
        self.report_box.setReadOnly(True)
        self.report_box.setFont(mono)
        self.copy_btn = button(t("Copy Report"), "text", "copy", "primary",
                               tip=t("Session report (paste directly into feedback):"))
        self.copy_btn.clicked.connect(self._copy_report)
        d.add_page(t("Report"), self._padded(self.report_box), self.copy_btn)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(mono)
        self.log_box.setMaximumBlockCount(20000)
        clear = button(t("Clear"), "text")
        clear.clicked.connect(lambda: self.log_box.clear())
        d.add_page(t("Run Log"), self._padded(self.log_box), clear)

        self.env_deploy_btn = self.env_remove_btn = None
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 12, 16, 16)
        v.setSpacing(12)
        # 1. the external tools (resources/tools.toml)
        v.addWidget(label(t("Tools"), "title"))
        # one label per tool: a single wrapped rich-text label under-reports
        # its height and clips its last line
        self.tool_list = QVBoxLayout()
        self.tool_list.setSpacing(4)
        self._tool_rows: list = []
        v.addLayout(self.tool_list)
        row = QHBoxLayout()
        self.tool_source = QComboBox()
        for key, text in (("auto", t("Source: automatic")),
                          ("github", t("Source: GitHub first")),
                          ("sipeed", t("Source: Sipeed download site first"))):
            self.tool_source.addItem(text, key)
        saved = QSettings().value("tool_source", "auto")
        self.tool_source.setCurrentIndex(max(0, self.tool_source.findData(saved)))
        self.tool_source.currentIndexChanged.connect(
            lambda _i: QSettings().setValue("tool_source", self.tool_source.currentData()))
        self.tool_btn = button(t("Download missing tools"), "filled", "refresh")
        self.tool_btn.clicked.connect(self._tool_action)
        row.addWidget(self.tool_source)
        row.addWidget(self.tool_btn)
        row.addStretch(1)
        v.addLayout(row)
        self.tool_bar = QProgressBar()
        self.tool_bar.setTextVisible(True)
        self.tool_bar.hide()
        v.addWidget(self.tool_bar)
        self.tool_hint_lbl = label("", "secondary")
        self.tool_hint_lbl.setTextFormat(Qt.RichText)
        self.tool_hint_lbl.setWordWrap(True)
        self.tool_hint_lbl.setOpenExternalLinks(True)
        self.tool_hint_lbl.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.tool_hint_lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        v.addWidget(self.tool_hint_lbl)
        # 2. the platform prerequisite (udev rule / WinUSB driver)
        if env_setup.requirement():
            v.addWidget(label(t("Environment Setup (one-time) · {name}").format(
                name=env_setup.title()), "title"))
            hint = label(env_setup.hint(), "secondary")
            hint.setWordWrap(True)
            v.addWidget(hint)
            row = QHBoxLayout()
            self.env_deploy_btn = button(t("Deploy"), "filled")
            self.env_deploy_btn.clicked.connect(lambda: self._env("deploy"))
            self.env_remove_btn = button(t("Remove"))
            self.env_remove_btn.clicked.connect(lambda: self._env("remove"))
            row.addWidget(self.env_deploy_btn)
            row.addWidget(self.env_remove_btn)
            row.addStretch(1)
            v.addLayout(row)
        v.addStretch(1)
        # the drawer may be short: scroll rather than clip the hints
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self._env_page = d.add_page(t("Environment Setup"), scroll)

        self.drawer = d
        d.toggled.connect(self._on_drawer_toggled)
        saved = QSettings().value("drawer", -1)
        try:
            d.open(int(saved))
        except (TypeError, ValueError):
            d.open(-1)
        return d

    @staticmethod
    def _padded(w: QWidget) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(12, 0, 12, 12)
        v.addWidget(w)
        return page

    _DRAWER_DEFAULT = 0.6       # share of the card + drawer area when opened

    def _on_drawer_toggled(self, idx: int):
        QSettings().setValue("drawer", idx)
        # deferred: on first build the splitter has no size yet
        QTimer.singleShot(0, self._size_drawer)

    def _size_drawer(self):
        split = getattr(self, "split", None)
        if split is None:
            return
        total = sum(split.sizes())
        if not total:                 # not laid out yet (fresh rebuild): retry
            if self.isVisible():
                QTimer.singleShot(10, self._size_drawer)
            return
        open_ = self.drawer.current() >= 0
        handle = split.handle(1)
        handle.setEnabled(open_)
        handle.setCursor(Qt.SplitVCursor if open_ else Qt.ArrowCursor)
        if open_:
            try:
                h = int(QSettings().value("drawer_height", 0))
            except (TypeError, ValueError):
                h = 0
            if h <= 0:
                h = int(total * self._DRAWER_DEFAULT)
            h = max(self.drawer.minimumSizeHint().height(),
                    min(h, total - self.result_card.minimumHeight()))
        else:
            h = self.drawer.sizeHint().height()
        split.setSizes([total - h, h])

    def _remember_drawer_height(self, *_):
        if self.drawer.current() >= 0:
            QSettings().setValue("drawer_height", self.split.sizes()[1])

    def _rebuild(self):
        """Rebuild every widget (new language / theme) and restore the screen."""
        log_text = self.log_box.toPlainText()
        report_text = self.report_box.toPlainText()
        self._build()
        self.log_box.setPlainText(log_text)
        self.log_box.moveCursor(QTextCursor.End)
        self.report_box.setPlainText(report_text)
        self.setWindowTitle(t("SLogic Production Test"))

    # ============================================================ rendering

    def _render_problem_chip(self):
        errors = sum(p.severity == "error" for p in self.problems)
        warnings = len(self.problems) - errors
        if not self.problems:
            self.warn_chip.hide()
            return
        parts = ([t("{n} errors").format(n=errors)] if errors else []) + \
                ([t("{n} warnings").format(n=warnings)] if warnings else [])
        self.warn_chip.set_state(" / ".join(parts), "error" if errors else "warning",
                                 "error" if errors else "warning")
        self.warn_chip.setToolTip(t("Click for resource self-check details"))
        self.warn_chip.show()

    def _show_problems(self):
        box = QMessageBox(self)
        box.setWindowTitle(t("Resource Self-check"))
        box.setIcon(QMessageBox.Warning if self.problems else QMessageBox.Information)
        box.setText(t("Startup self-check found the following issues (missing items "
                      "disable the related function; add resources and restart to "
                      "take effect):"))
        box.setDetailedText("\n\n".join(str(p) for p in self.problems) or t("No issues"))
        box.exec_()

    def _on_devices(self):
        ctl = self.ctl
        if not ctl.backend_ok:
            # without a libusb backend every usb.core.find fails silently and no
            # device is ever seen; say so (typical: libusb-1.0.dll missing)
            self.device_chip.set_state(
                t("libusb backend not found (libusb-1.0.dll): cannot enumerate "
                  "USB devices, DFU/APP detection disabled — see resources/README.md"),
                "error", "warning")
        elif not ctl.detected:
            self.device_chip.set_state(t("No analyzer detected"), "error", "usb")
        else:
            self.device_chip.set_state(
                t("Device: {dev}").format(dev=", ".join(str(d) for d in ctl.detected)),
                "success" if ctl.test_link_ok() else "warning", "usb")
        p = ctl.profile
        self.product_chip.set_state(
            t("Product: {name}").format(name=p.display_name if p else "—"),
            "info" if p else "neutral")
        # keep the step preview in step with what is plugged in (idle only)
        if ctl.profile is None and not ctl.busy:
            self._layout_steps()
        self._render_fixture_reqs()
        self._update_enablement()
        if self._result[0] in ("idle", "ready"):
            self._render_result()        # the readiness checklist follows the bench

    def _on_state(self):
        ctl = self.ctl
        if ctl.running or ctl.armed:
            self.action_btn.setText(t("Stop"))
            self.action_btn.setIcon(icons.icon("stop", "on_error", 20))
            theme.set_prop(self.action_btn, "variant", "danger")
            self.action_btn.setToolTip("")
        else:
            self.action_btn.setText(t("Start"))
            self.action_btn.setIcon(icons.icon("play", "on_primary", 20))
            theme.set_prop(self.action_btn, "variant", "filled")
        self.action_btn.setIconSize(QSize(20, 20))
        if ctl.armed and ctl.arm_state == "wait_insert" and self._result[0] in ("idle",):
            self._show_result("ready")
        elif not ctl.armed and not ctl.busy and self._result[0] in ("ready", "waiting"):
            self._caption = ""
            self._show_result("idle")
        elif not ctl.armed and self._caption:
            self._set_caption("")
        self._update_enablement()
        self._render_tools()           # no download while a test runs

    def _update_enablement(self):
        ctl = self.ctl
        missing = ctl.missing()
        if not (ctl.busy or ctl.armed):
            self.action_btn.setEnabled(not missing)
            self.action_btn.setToolTip(
                t("Missing: {items}").format(items=", ".join(missing)) if missing else
                t("Arm the station: each newly connected board is recognised and run "
                  "automatically"))
        else:
            self.action_btn.setEnabled(True)
        if ctl.resetting:
            self.action_btn.setEnabled(False)
        self.reset_btn.setEnabled(not ctl.busy)
        single = ctl.idle and ctl.profile is not None and not missing
        self.stepper.set_interactive(
            single, lambda n: t("Single step: {label}").format(label=n.name_text)
            if single else ("" if ctl.profile is not None else
                            t("Connect a board first (click Start to recognise it)")))
        can_retry = ctl.can_retry()
        for sid, s in self._steps.items():
            self.stepper.set_retry(sid, can_retry and s.status == StepStatus.FAILED)

    # ----------------------------------------------------------- the steps

    def _layout_steps(self, force: bool = False, keep_status: bool = False):
        """Lay the stepper out for the recognised board, or preview the steps
        of what is plugged in; with nothing to show, say what to do."""
        ctl = self.ctl
        shown = ctl.profile or ctl.preview_profile()
        sid = shown.id if shown else None
        if not force and sid == self._shown_id:
            return
        self._shown_id = sid
        if shown is None:
            self._steps.clear()
            self._fixture_need = {}
            self.stepper.show_hint(t("Connect the board under test"))
            self._layout_matrix(None)
            return
        old = self._steps if keep_status else {}
        plan = sequence_plan(shown)
        self._steps = {}
        self._fixture_need = {}
        rows = []
        for step_id, name in plan:
            prev = old.get(step_id)
            self._steps[step_id] = _Step(name, *(
                (prev.status, prev.t_start, prev.duration, prev.note, prev.retries)
                if prev else ()))
            self._fixture_need[step_id] = self._fixture_need_of(step_id)
            rows.append((step_id, name, ""))
        self.stepper.set_steps(rows)
        self._render_fixture_reqs()
        for step_id, s in self._steps.items():
            self.stepper.set_status(step_id, s.status, s.duration)
            self.stepper.set_note(step_id, s.note)
        self._layout_matrix(shown)
        self._update_enablement()

    @staticmethod
    def _fixture_need_of(sid: str) -> bool | None:
        """True when the step needs the fixture (Tang Nano 9K) plugged in."""
        return True if sid.startswith("signal") else None

    def _render_fixture_reqs(self):
        """Show a step's fixture requirement only while the fixture is missing;
        a permanent hint under every step reads like the cause of whatever
        failed there."""
        present = self.ctl.fixture_present
        for sid, need in self._fixture_need.items():
            self.stepper.set_req(sid, t("Requires Tang Nano 9K plugged in")
                                 if need and not present else "")

    def _on_step_note(self, sid: str, note: str):
        s = self._steps.get(sid)
        if s is not None:
            s.note = note
            self.stepper.set_note(sid, note)

    def _on_step(self, sid: str, status: StepStatus):
        s = self._steps.get(sid)
        if s is not None:
            if status == StepStatus.RUNNING and s.t_start is None:
                s.t_start = time.time()
                s.note = ""
                self.stepper.set_note(sid, "")
            if status in (StepStatus.PASSED, StepStatus.FAILED) and s.t_start:
                s.duration = time.time() - s.t_start
            s.status = status
            self.stepper.set_status(sid, status, s.duration)
            self._apply_matrix()
        if status == StepStatus.RUNNING:
            self._current_sid = sid
            if not self._prompt:
                self._show_result("running", sid=sid)

    # ------------------------------------------------------ the mode matrix

    @staticmethod
    def _row_of(sid: str) -> str:
        """"signal:all@10G" -> "10G"; no link suffix -> ""."""
        return sid.split("@", 1)[1] if "@" in sid else ""

    def _layout_matrix(self, profile):
        """Rows = link passes, columns = link check + every planned mode; hidden
        for a product without a pattern plan."""
        if profile is None or not profile.plan:
            self._matrix_rows = []
            self.matrix.set_layout([], [])
            return
        from ..profiles import link_name
        self._matrix_rows = [link_name(lk) for lk in profile.links] or [""]
        cols = [("link", t("Link"), t("USB link check"))] + [
            ("M%d" % e.mode, "M%d" % e.mode, e.label) for e in profile.plan]
        self.matrix.set_layout(self._matrix_rows, cols)
        self._apply_matrix()

    def _apply_matrix(self):
        state_of = {StepStatus.PASSED: "pass", StepStatus.FAILED: "fail",
                    StepStatus.RUNNING: "running", StepStatus.WAITING_USER: "running"}
        for sid, st in self._steps.items():
            if sid.startswith("link:"):
                self.matrix.set_cell(self._row_of(sid), "link",
                                     state_of.get(st.status, "pending"), st.note)
        for (row, key), (state, why) in self._modes.items():
            self.matrix.set_cell(row, key, state, why)

    def _on_progress(self, text: str):
        self._progress = text
        head = text.split(" ", 1)[0]
        row = self._row_of(self._current_sid)
        if head[:1] == "M" and head[1:].isdigit() and (row, head) not in self._modes:
            # first progress line of a mode: it is running now
            for k, (state, _w) in list(self._modes.items()):
                if state == "running" and k[0] == row:
                    del self._modes[k]
            self._modes[(row, head)] = ("running", "")
            self._apply_matrix()
        if self._result[0] in ("running", "waiting"):
            self._render_result()

    def _on_mode_result(self, link: str, mode: int, ok: bool, why: str):
        self._modes[(link, "M%d" % mode)] = ("pass" if ok else "fail", why)
        self._apply_matrix()

    def _readiness(self) -> list[tuple[str, str, str]]:
        """(ok | bad | info, name, detail) rows for the idle checklist."""
        ctl = self.ctl
        rows = []
        if not ctl.backend_ok:
            rows.append(("bad", t("Analyzer"), t("libusb backend not found")))
        elif not ctl.detected:
            rows.append(("bad", t("Analyzer"), t("not connected")))
        else:
            d = ctl.detected[0]
            what = " · ".join(x for x in (d.profile.display_name,
                                         "SN " + d.serial if d.serial else "",
                                         d.link) if x)
            if len(ctl.detected) > 1:
                rows.append(("bad", t("Analyzer"),
                             t("{n} connected; keep only the board under test")
                             .format(n=len(ctl.detected))))
            else:
                rows.append(("ok" if ctl.test_link_ok() else "bad", t("Analyzer"), what))
        rows.append(("ok", t("Fixture"), ", ".join(ctl.fixture_port_list))
                    if ctl.fixture_port_list else
                    ("bad", t("Fixture"), t("not connected (Tang Nano 9K)")))
        missing = ctl.tools_missing()
        if missing:
            rows.append(("bad", t("Tools"),
                         t("missing {names}: download them on the Environment Setup page")
                         .format(names=", ".join(tl.name for tl in missing))))
        if ctl.sigrok is None:
            if not missing:
                rows.append(("bad", "sigrok-cli", t("not found; see resources/README.md")))
        else:
            rows.append(("ok", "sigrok-cli", ctl.sigrok_version or "…"))
        st = ctl.env_status
        if env_setup.requirement() == "udev":
            rows.append(("ok", t("Device permissions"), t("udev rule deployed"))
                        if st == "deployed" else
                        ("bad", t("Device permissions"),
                         t("udev rule missing: deploy it on the Environment Setup page")))
        elif env_setup.requirement() == "winusb" and st != "unknown":
            # unknown = fixture absent or not read yet; its own row covers that
            rows.append(("ok", t("Fixture driver"), "WinUSB")
                        if st == "deployed" else
                        ("bad", t("Fixture driver"),
                         t("not WinUSB: deploy it on the Environment Setup page")))
        return rows

    def _render_stats(self):
        n = len(self._session)
        p = sum(self._session)
        self.stats_chip.setVisible(n > 0)
        self.stats_chip.set_state(
            t("This session: {n} tested · {p} pass · {f} fail").format(n=n, p=p, f=n - p),
            "success" if n and p == n else ("warning" if n else "neutral"))

    # ---------------------------------------------------------- the result

    def _show_result(self, kind: str, **data):
        self._result = (kind, data)
        self._render_result()

    def _set_caption(self, which: str):
        self._caption = which
        self._render_result()

    def _render_result(self):
        kind, d = self._result
        ctl = self.ctl
        p = ctl.profile
        caption = {"remove": t("Remove the board under test"),
                   "next": t("Waiting for the next board…")}.get(self._caption, "")
        has_matrix = bool(getattr(self, "_matrix_rows", []))
        show_check = kind in ("idle", "ready")
        self.checklist.setVisible(show_check)
        self.progress_bar.setVisible(kind == "running")
        self.matrix.setVisible(has_matrix and not show_check)
        if show_check:
            self.checklist.set_items(self._readiness())
        board = " · ".join(x for x in (p.display_name if p else "",
                                       "SN " + self._board_sn if self._board_sn else "")
                           if x)
        if kind == "idle":
            self.result_card.set_state(
                "idle", t("Bench readiness"),
                t("Click Start, then connect the board under test; it is recognised "
                  "automatically"))
        elif kind == "ready":
            self.result_card.set_state(
                "ready", t("Waiting for the board under test…"),
                t("Armed: every board connected is tested automatically"),
                caption=caption, icon="usb")
        elif kind == "running":
            elapsed = time.time() - self._run_t0 if self._run_t0 else 0.0
            order = list(self._steps)
            k = order.index(self._current_sid) + 1 if self._current_sid in order else 0
            self.progress_bar.setRange(0, max(1, len(order)))
            self.progress_bar.setValue(max(0, k - 1))
            s = self._steps.get(self._current_sid)
            now = " · ".join(x for x in (s.label if s else "", self._progress) if x)
            self.result_card.set_state(
                "running", t("Running…") + "  " + _clock(elapsed), board,
                t("Step {k}/{n}: {what}").format(k=k, n=len(order), what=now)
                if k else now)
            if not self._clock.isActive():
                self._clock.start()
            return
        elif kind == "waiting":
            seen = self._progress if self._progress.startswith(
                t("Now detected: {devs}").split("{")[0]) else ""
            self.result_card.set_state("waiting", d["text"], board, seen)
        else:
            sub = " · ".join(x for x in (board, _clock(d["elapsed"])) if x)
            failed = [s.label + (f" — {s.note}" if s.note else "")
                      for s in self._steps.values() if s.status == StepStatus.FAILED]
            retried = [s.label for s in self._steps.values() if s.retries]
            if kind == "fail" and failed:
                detail = t("Failed steps: {items}").format(items="\n".join(failed))
            elif kind == "pass" and retried:
                detail = t("Passed after retry: {items}").format(items=", ".join(retried))
            else:
                detail = ""
            self.result_card.set_state(kind, "PASS" if kind == "pass" else "FAIL",
                                       sub, detail, caption)
        self._clock.stop()

    # ============================================================== actions

    def _on_action(self):
        if self.ctl.busy or self.ctl.armed:
            self.ctl.disarm()
        else:
            self._set_caption("")
            self.ctl.arm()
            if self.ctl.armed:
                self._show_result("ready")

    def _on_language(self, code: str):
        if code == get_language():
            return
        if self.ctl.busy:
            QMessageBox.information(self, t("Cannot switch"),
                                    t("Cannot switch language while a test is running."))
            self._rebuild()           # resets the menu's checked item
            return
        set_language(code)
        QSettings().setValue("language", code)
        self._rebuild()

    def _on_theme(self, mode: str):
        QSettings().setValue("theme", mode)
        theme.apply(QApplication.instance(), mode)
        self._rebuild()

    def _run_single_step(self, sid: str):
        s = self._steps.get(sid)
        self.ctl.run_single_step(sid, s.label if s else sid)

    def _on_reset(self):
        """Clear everything this board left on screen, then reset the
        hardware in the background.  An armed station stays armed; a board
        still plugged in is not retested until it has been removed."""
        ctl = self.ctl
        if ctl.busy:
            return
        self._modes.clear()
        self._progress = self._board_sn = self._run_detail = self._retry_label = ""
        self._run_elapsed, self._run_t0, self._run_kind = 0.0, 0.0, ""
        self.report_box.clear()
        ctl.profile = None
        self._layout_steps(force=True)        # fresh, all pending
        self._caption = "remove" if ctl.armed and ctl.arm_state == "wait_remove" else ""
        self._show_result("ready" if ctl.armed else "idle")
        ctl.reset_hardware()

    def _retry_step(self, sid: str):
        """Operator pressed Retry under a failed step: rerun it, then the
        steps right after it that never ran because it aborted the run.  Stop
        at the first step that did run -- a later failed step has its own
        Retry, and the steps behind it depend on it (e.g. the 5G replug)."""
        order = list(self._steps)
        if sid not in order or not self.ctl.can_retry():
            return
        todo = [sid]
        for x in order[order.index(sid) + 1:]:
            if self._steps[x].status != StepStatus.PENDING:
                break
            todo.append(x)
        s = self._steps[sid]
        s.retries += 1
        self._retry_label = s.label
        self.ctl.retry_steps(todo, s.label)

    def _on_run_started(self, full: bool):
        if full:
            self._run_kind = "full"
            self._modes.clear()
            self._progress = ""
            d = self.ctl.detected
            self._board_sn = (d[0].serial or "") if d else ""
            self._run_t0 = time.time()
            self._run_detail, self._run_elapsed = "", 0.0
            self._retry_label = ""
            # a new board: its own steps, all pending
            self._layout_steps(force=True)
            self._caption = ""
            self._on_devices()
        else:
            self._run_kind = "retry" if self._retry_label else "manual"
            self._run_t0 = time.time() - (self._run_elapsed if self._retry_label else 0.0)
            for step in self.ctl.pipeline.steps:
                if step.id.startswith("signal:"):     # its mode cells start over
                    row = self._row_of(step.id)
                    for k in [k for k in self._modes if k[0] == row]:
                        del self._modes[k]
                s = self._steps.get(step.id)
                if s is not None:
                    s.status, s.t_start, s.duration = StepStatus.PENDING, None, None
                    s.note = ""
                    self.stepper.set_status(step.id, s.status)
                    self.stepper.set_note(step.id, "")
        self._prompt = ""
        self._show_result("running")
        self._update_enablement()

    def _on_finished(self, ok: bool, detail: str, elapsed: float):
        # a cancelled run leaves its current step without a final status: it did
        # not pass, so it failed
        for sid, s in self._steps.items():
            if s.status in (StepStatus.RUNNING, StepStatus.WAITING_USER):
                s.status = StepStatus.FAILED
                s.note = s.note or t("Cancelled")
                self.stepper.set_status(sid, s.status, s.duration)
                self.stepper.set_note(sid, s.note)
        self._prompt = ""
        if self.ctl.armed:
            self._caption = "remove"
        if self._retry_label:
            # a retry judges the whole board again: pass only when every step
            # of it has now passed; the report keeps the earlier detail
            ok = all(s.status == StepStatus.PASSED for s in self._steps.values())
            detail = (self._run_detail + "\n\n-- "
                      + t("Retry: {label}").format(label=self._retry_label)
                      + " --\n" + detail)
            elapsed = self._run_elapsed + elapsed
            self._retry_label = ""
        self._run_detail, self._run_elapsed = detail, elapsed
        # a mode left "running" by a cancelled run did not finish
        for k, (state, why) in list(self._modes.items()):
            if state == "running":
                self._modes[k] = ("fail", t("Cancelled"))
        self._apply_matrix()
        if self._run_kind == "full":
            self._session.append(ok)
        elif self._run_kind == "retry" and self._session:
            self._session[-1] = ok             # the same board, judged again
        self._render_stats()
        self._show_result("pass" if ok else "fail", elapsed=elapsed)
        steps = [(s.status, f"{i}. {s.label}", s.duration, s.retries)
                 for i, s in enumerate(self._steps.values(), start=1)]
        self.report_box.setPlainText(report.render(
            ok, detail, elapsed, self.ctl.profile,
            self.ctl.detected, steps))
        self._update_enablement()

    def _on_prompt(self, text: str):
        self._prompt = text
        self._show_result("waiting", text=text)

    def _on_prompt_clear(self):
        self._prompt = ""
        if self.ctl.busy:
            self._show_result("running", sid=self._current_sid)

    def _append_log(self, s: str):
        self.log_box.appendPlainText(s)

    def _copy_report(self):
        QApplication.clipboard().setText(self.report_box.toPlainText())
        self.copy_btn.setText(t("Copied"))
        self.copy_btn.setIcon(icons.icon("check", "primary", 20))
        btn = self.copy_btn

        def restore():
            try:
                btn.setText(t("Copy Report"))
                btn.setIcon(icons.icon("copy", "primary", 20))
            except RuntimeError:      # rebuilt in the meantime
                pass
        QTimer.singleShot(1500, restore)

    # ------------------------------------------------------ custom capture

    # ------------------------------------------------- environment setup

    # ------------------------------------------------------------- tools

    def _render_tools(self):
        ctl = self.ctl
        names = {"ok": t("ready"), "other": t("another build (works, can update)"),
                 "missing": t("missing"), "": "…"}
        colors = {"ok": theme.color("success"), "other": theme.color("warning"),
                  "missing": theme.color("error"), "": theme.color("on_surface_variant")}
        texts = ["<span style='color:%s'>●</span> <b>%s</b> — %s &nbsp;"
                 "<span style='color:%s'>%s</span>"
                 % (colors[st], tl.name, names[st], theme.color("on_surface_variant"),
                    tl.dest.relative_to(tl.dest.parents[2])
                    if len(tl.dest.parents) > 2 else tl.dest)
                 for tl, st in zip(ctl.tools, ctl.tool_status)] or [t("No tools listed")]
        while len(self._tool_rows) < len(texts):
            lbl = label("", "secondary")
            lbl.setTextFormat(Qt.RichText)
            self.tool_list.addWidget(lbl)
            self._tool_rows.append(lbl)
        for lbl, text in zip(self._tool_rows, texts):
            lbl.setText(text)
        missing = [st for st in ctl.tool_status if st == "missing"]
        other = [st for st in ctl.tool_status if st == "other"]
        if ctl.downloading:
            self.tool_btn.setText(t("Cancel"))
            self.tool_btn.setEnabled(True)
        else:
            self.tool_btn.setText(t("Download missing tools") if missing or not other
                                  else t("Update to the listed builds"))
            self.tool_btn.setEnabled(bool(missing or other) and not ctl.busy)
        self.tool_source.setEnabled(not ctl.downloading)
        self.tool_hint_lbl.setText(self._tool_hint)
        self.tool_hint_lbl.setVisible(bool(self._tool_hint))

    def _tool_action(self):
        ctl = self.ctl
        if ctl.downloading:
            ctl.cancel_download()
            return
        self._tool_hint = ""
        self.tool_bar.setValue(0)
        self.tool_bar.show()
        ctl.download_tools(self.tool_source.currentData() or "auto",
                           also_other="missing" not in ctl.tool_status)
        self._render_tools()

    def _on_tool_progress(self, name: str, got: int, total: int):
        self.tool_bar.setRange(0, total or 0)
        if total:
            self.tool_bar.setValue(got)
        self.tool_bar.setFormat("%s  %.1f / %s MB" % (
            name, got / 1e6, "%.1f" % (total / 1e6) if total else "?"))

    def _on_tools(self):
        self._render_tools()
        if self._result[0] in ("idle", "ready"):
            self._render_result()
        # first look after start-up: open the setup page if something required is missing
        if not self._tools_guided and "" not in self.ctl.tool_status:
            self._tools_guided = True
            if self.ctl.tools_missing():
                self.drawer.open(self._env_page)

    def _on_tools_done(self, ok: bool, hints: list):
        self.tool_bar.hide()
        if hints:
            self._tool_hint = (t("Some tools could not be downloaded. Get them by hand:")
                               + "<br>" + "<br>".join(_linkify(h) for h in hints))
        self.problems = [p for p in self.problems if "sigrok-cli" not in p.message]
        if self.ctl.sigrok is None:
            self.problems += check_resources(self.profiles, None)
        self._render_problem_chip()
        self._render_tools()
        self._update_enablement()
        self._append_log(t("Tools: all in place") if ok else
                         t("Tools: some could not be downloaded, see the Environment Setup page"))

    def _env(self, action: str):
        self.env_deploy_btn.setEnabled(False)
        self.env_remove_btn.setEnabled(False)
        self.ctl.run_env_setup(action)

    def _on_env_done(self, ok: bool, _action: str):
        self._refresh_env_buttons()
        if ok:
            self.ctl.refresh_devices()    # permissions/driver may now differ

    def _on_env_status(self):
        self._refresh_env_buttons()
        if self._result[0] in ("idle", "ready"):
            self._render_result()

    def _refresh_env_buttons(self):
        """Linux: the rule file is directly checkable; Windows: the WinUSB
        binding isn't cheaply queryable, so both buttons stay enabled."""
        if self.env_deploy_btn is None:
            return
        st = self.ctl.env_status
        self.env_deploy_btn.setEnabled(True)
        self.env_remove_btn.setEnabled(True)
        if st == "deployed":
            self.env_deploy_btn.setText(t("Redeploy"))
            self.env_deploy_btn.setToolTip(t("Rule exists; rewrite to update"))
            self.env_remove_btn.setToolTip(t("Remove the deployed rule"))
        elif st == "absent":
            self.env_deploy_btn.setText(t("Deploy"))
            self.env_deploy_btn.setToolTip("")
            self.env_remove_btn.setEnabled(False)
            self.env_remove_btn.setToolTip(t("Not deployed; nothing to remove"))
        else:
            self.env_deploy_btn.setText(t("Deploy"))
            self.env_deploy_btn.setToolTip("")
            self.env_remove_btn.setToolTip("")

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._size_drawer)

    def closeEvent(self, e):
        self.ctl.disarm()
        self.ctl.shutdown()
        super().closeEvent(e)
