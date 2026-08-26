"""
setup_screen.py — Sanju AI
First-run setup wizard, styled to match the app's dark/glassy aesthetic
(loosely "Alexa-like" first-run flow, not a copy of it).

Steps:
  1. Choose a personality: Assistant / Friend Assistant / Love Assistant
     (all three are named "Sanju")
  2. Master's name (typed)
  3. Master's gender — Male / Female (affects pronouns Sanju uses)
  4. Master's occupation (typed)
  5. Security check toggle — wake-word password question, on or off,
     entirely the user's choice
  6. Quick instructions on how to use Sanju, then finish

Usage (blocking, called once before the main app starts):
    from setup_screen import run_setup_if_needed
    profile = run_setup_if_needed()   # returns existing or newly-created profile
"""

from PyQt6.QtCore    import Qt
from PyQt6.QtGui     import QFont, QColor, QPainter, QLinearGradient, QBrush
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFrame, QScrollArea,
)

from ai_profile import save_profile, load_profile

# Wider window + wider cards fixes the cramped text-wrapping from before.
WIN_W, WIN_H = 800, 540

BG_TOP    = QColor(10, 14, 26)
BG_BOTTOM = QColor(18, 10, 26)

PERSONALITIES = [
    {
        "mode":  "Assistant",
        "title": "Assistant",
        "desc":  "Professional and efficient.\nFocuses on getting things done.",
        "color": QColor(0, 170, 255),
    },
    {
        "mode":  "Friend",
        "title": "Friend Assistant",
        "desc":  "Casual and easygoing.\nTalks like your best buddy.",
        "color": QColor(255, 200, 40),
    },
    {
        "mode":  "Love",
        "title": "Love Assistant",
        "desc":  "Warm and affectionate.\nSpeaks like a caring partner.",
        "color": QColor(255, 60, 90),
    },
]

CARD_W, CARD_H = 210, 200

INSTRUCTIONS = [
    ("🎤", "Say \"Sanju\"", "Wake her up anytime by saying her name."),
    ("💬", "Just talk", "Ask her to open apps, search things, or just chat."),
    ("🎵", "\"Play me a song\"", "She'll search and play music for you on Spotify."),
    ("👀", "\"Keep an eye on my code\"", "She'll watch for bugs while you work and ask before fixing."),
    ("✖", "Close anytime", "Click the X in the corner of her window to close her."),
]


# ═══════════════════════════════════════════════════════════════════════════════
class GradientBackground(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, BG_TOP)
        grad.setColorAt(1.0, BG_BOTTOM)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRect(self.rect())


# ═══════════════════════════════════════════════════════════════════════════════
def _heading(text, size=20):
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", size, QFont.Weight.Bold))
    lbl.setStyleSheet("color: #F5F7FC; background: transparent;")
    lbl.setWordWrap(True)
    return lbl


def _subtext(text):
    lbl = QLabel(text)
    lbl.setFont(QFont("Segoe UI", 11))
    lbl.setStyleSheet("color: #A8B2C8; background: transparent;")
    lbl.setWordWrap(True)
    return lbl


# ═══════════════════════════════════════════════════════════════════════════════
class PersonalityCard(QFrame):
    """One selectable card for a personality option — widened + shorter
    description text so nothing wraps into a cramped stack anymore."""

    def __init__(self, data: dict, on_select, parent=None):
        super().__init__(parent)
        self._data      = data
        self._on_select = on_select
        self._selected  = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(CARD_W, CARD_H)
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 22, 20, 22)
        layout.setSpacing(12)

        dot = QLabel()
        dot.setFixedSize(16, 16)
        c = self._data["color"]
        dot.setStyleSheet(
            f"background-color: rgba({c.red()},{c.green()},{c.blue()},230); "
            f"border-radius: 8px;"
        )
        layout.addWidget(dot, alignment=Qt.AlignmentFlag.AlignLeft)

        title = QLabel(self._data["title"])
        title.setStyleSheet("color: #F5F7FC; background: transparent;")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title.setWordWrap(True)
        layout.addWidget(title)

        name_tag = QLabel("Name: Sanju")
        name_tag.setStyleSheet("color: #8FA2C0; background: transparent;")
        name_tag.setFont(QFont("Segoe UI", 9, QFont.Weight.Normal))
        layout.addWidget(name_tag)

        desc = QLabel(self._data["desc"])
        desc.setStyleSheet("color: #B8C2D8; background: transparent;")
        desc.setFont(QFont("Segoe UI", 10))
        desc.setWordWrap(True)
        layout.addWidget(desc)

        layout.addStretch()
        self._apply_style()

    def _apply_style(self):
        c = self._data["color"]
        if self._selected:
            self.setStyleSheet(f"""
                QFrame {{
                    background-color: rgba({c.red()},{c.green()},{c.blue()},30);
                    border: 1.8px solid rgba({c.red()},{c.green()},{c.blue()},230);
                    border-radius: 18px;
                }}
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: rgba(255,255,255,10);
                    border: 1px solid rgba(255,255,255,25);
                    border-radius: 18px;
                }
                QFrame:hover {
                    background-color: rgba(255,255,255,16);
                    border: 1px solid rgba(255,255,255,55);
                }
            """)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_select(self._data["mode"])


# ═══════════════════════════════════════════════════════════════════════════════
class ChoiceCard(QFrame):
    """Generic wide selectable card — used for gender + security toggle."""

    def __init__(self, title: str, subtitle: str, accent: QColor, on_select, value, parent=None):
        super().__init__(parent)
        self._on_select = on_select
        self._value     = value
        self._selected  = False
        self._accent    = accent
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(220, 110)
        self._build(title, subtitle)

    def _build(self, title, subtitle):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(6)

        t = QLabel(title)
        t.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        t.setStyleSheet("color: #F5F7FC; background: transparent;")
        layout.addWidget(t)

        if subtitle:
            s = QLabel(subtitle)
            s.setFont(QFont("Segoe UI", 9))
            s.setStyleSheet("color: #A8B2C8; background: transparent;")
            s.setWordWrap(True)
            layout.addWidget(s)
        layout.addStretch()
        self._apply_style()

    def _apply_style(self):
        c = self._accent
        if self._selected:
            self.setStyleSheet(f"""
                QFrame {{
                    background-color: rgba({c.red()},{c.green()},{c.blue()},30);
                    border: 1.8px solid rgba({c.red()},{c.green()},{c.blue()},230);
                    border-radius: 16px;
                }}
            """)
        else:
            self.setStyleSheet("""
                QFrame {
                    background-color: rgba(255,255,255,10);
                    border: 1px solid rgba(255,255,255,25);
                    border-radius: 16px;
                }
                QFrame:hover {
                    background-color: rgba(255,255,255,16);
                    border: 1px solid rgba(255,255,255,55);
                }
            """)

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_select(self._value)


# ═══════════════════════════════════════════════════════════════════════════════
class StyledLineEdit(QLineEdit):
    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setFont(QFont("Segoe UI", 13))
        self.setFixedHeight(48)
        self.setStyleSheet("""
            QLineEdit {
                background-color: rgba(255,255,255,12);
                border: 1px solid rgba(255,255,255,35);
                border-radius: 12px;
                padding: 0 16px;
                color: #F5F7FC;
            }
            QLineEdit:focus {
                border: 1.4px solid rgba(0,170,255,180);
                background-color: rgba(255,255,255,18);
            }
        """)


class PrimaryButton(QPushButton):
    def __init__(self, text, parent=None, enabled=True, ghost=False):
        super().__init__(text, parent)
        self.setFixedHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.setEnabled(enabled)
        bg    = "rgba(255,255,255,18)" if ghost else "rgba(0,170,255,210)"
        hover = "rgba(255,255,255,28)" if ghost else "rgba(30,185,255,230)"
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                color: white;
                border: none;
                border-radius: 12px;
                padding: 0 26px;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:disabled {{
                background-color: rgba(255,255,255,12);
                color: rgba(255,255,255,80);
            }}
        """)


# ═══════════════════════════════════════════════════════════════════════════════
class InstructionRow(QFrame):
    def __init__(self, emoji, title, desc, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: rgba(255,255,255,8);
                border: 1px solid rgba(255,255,255,18);
                border-radius: 12px;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(14)

        icon = QLabel(emoji)
        icon.setFont(QFont("Segoe UI", 16))
        icon.setFixedWidth(30)
        icon.setStyleSheet("background: transparent;")
        layout.addWidget(icon)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        t = QLabel(title)
        t.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        t.setStyleSheet("color: #F0F3FA; background: transparent;")
        text_col.addWidget(t)
        d = QLabel(desc)
        d.setFont(QFont("Segoe UI", 9))
        d.setStyleSheet("color: #9AA6C0; background: transparent;")
        d.setWordWrap(True)
        text_col.addWidget(d)

        layout.addLayout(text_col)


# ═══════════════════════════════════════════════════════════════════════════════
class SetupWindow(QWidget):
    """Multi-step setup wizard. After exec, check .completed / .result_profile."""

    def __init__(self):
        super().__init__()
        self.completed       = False
        self.result_profile  = None
        self._chosen_mode    = None
        self._master_name    = ""
        self._gender         = "Female"
        self._occupation     = ""
        self._security_on    = True
        self._cards          = []

        self._init_window()
        self._build_step1()

    # ── Window shell ─────────────────────────────────────────────────────────
    def _init_window(self):
        self.setWindowTitle("Welcome to Sanju")
        self.setFixedSize(WIN_W, WIN_H)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)

        self._bg = GradientBackground(self)
        self._bg.setGeometry(0, 0, WIN_W, WIN_H)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(48, 40, 48, 36)
        self._root.setSpacing(0)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - WIN_W) // 2, (screen.height() - WIN_H) // 2)

    def _clear_root(self):
        while self._root.count():
            item = self._root.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            lay = item.layout()
            if lay:
                while lay.count():
                    sub = lay.takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

    def _nav_row(self, back_cb=None, next_cb=None, next_label="Next", next_enabled=True):
        row = QHBoxLayout()
        if back_cb:
            back_btn = PrimaryButton("Back", ghost=True)
            back_btn.clicked.connect(back_cb)
            row.addWidget(back_btn)
        row.addStretch()
        next_btn = None
        if next_cb:
            next_btn = PrimaryButton(next_label, enabled=next_enabled)
            next_btn.clicked.connect(next_cb)
            row.addWidget(next_btn)
        return row, next_btn

    # ── Step 1: personality choice ──────────────────────────────────────────
    def _build_step1(self):
        self._clear_root()
        self._root.addWidget(_heading("Hi, I'm Sanju 👋", 24))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext("Choose how you'd like me to be with you."))
        self._root.addSpacing(28)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(20)
        self._cards = []
        for data in PERSONALITIES:
            card = PersonalityCard(data, self._on_personality_selected)
            if data["mode"] == self._chosen_mode:
                card.set_selected(True)
            self._cards.append(card)
            cards_row.addWidget(card)
        cards_row.addStretch()
        self._root.addLayout(cards_row)
        self._root.addStretch()

        row, self._next_btn1 = self._nav_row(
            next_cb=self._build_step2, next_enabled=bool(self._chosen_mode))
        self._root.addLayout(row)

    def _on_personality_selected(self, mode: str):
        self._chosen_mode = mode
        for card in self._cards:
            card.set_selected(card._data["mode"] == mode)
        self._next_btn1.setEnabled(True)

    # ── Step 2: master's name ───────────────────────────────────────────────
    def _build_step2(self):
        self._clear_root()
        self._root.addWidget(_heading("What should I call you?"))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext("I'll use this whenever I talk to you."))
        self._root.addSpacing(30)

        self._name_input = StyledLineEdit("Your name…")
        self._name_input.setFixedWidth(380)
        if self._master_name:
            self._name_input.setText(self._master_name)
        self._root.addWidget(self._name_input)
        self._root.addStretch()

        row, self._next_btn2 = self._nav_row(
            back_cb=self._build_step1, next_cb=self._go_to_step3,
            next_enabled=bool(self._master_name))
        self._root.addLayout(row)

        self._name_input.textChanged.connect(
            lambda t: self._next_btn2.setEnabled(bool(t.strip())))
        self._name_input.setFocus()
        self._name_input.returnPressed.connect(
            lambda: self._next_btn2.isEnabled() and self._go_to_step3())

    def _go_to_step3(self):
        self._master_name = self._name_input.text().strip()
        self._build_step3()

    # ── Step 3: master's gender ─────────────────────────────────────────────
    def _build_step3(self):
        self._clear_root()
        self._root.addWidget(_heading(f"One more thing, {self._master_name}"))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext("How should I refer to you?"))
        self._root.addSpacing(28)

        self._gender_cards = []
        row = QHBoxLayout()
        row.setSpacing(20)
        for label, value, color in (("Male", "Male", QColor(0, 170, 255)),
                                     ("Female", "Female", QColor(255, 100, 180))):
            card = ChoiceCard(label, "", color, self._on_gender_selected, value)
            card.set_selected(value == self._gender)
            self._gender_cards.append(card)
            row.addWidget(card)
        row.addStretch()
        self._root.addLayout(row)
        self._root.addStretch()

        nav, _ = self._nav_row(back_cb=self._build_step2, next_cb=self._build_step4)
        self._root.addLayout(nav)

    def _on_gender_selected(self, value):
        self._gender = value
        for card in self._gender_cards:
            card.set_selected(card._value == value)

    # ── Step 4: master's occupation ─────────────────────────────────────────
    def _build_step4(self):
        self._clear_root()
        self._root.addWidget(_heading(f"What do you do, {self._master_name}?"))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext("This helps me understand your world a little better."))
        self._root.addSpacing(30)

        self._occ_input = StyledLineEdit("e.g. Software Engineer, Student, Doctor…")
        self._occ_input.setFixedWidth(420)
        if self._occupation:
            self._occ_input.setText(self._occupation)
        self._root.addWidget(self._occ_input)
        self._root.addStretch()

        row, _ = self._nav_row(back_cb=self._build_step3, next_cb=self._go_to_step5)
        self._root.addLayout(row)

        self._occ_input.setFocus()
        self._occ_input.returnPressed.connect(self._go_to_step5)

    def _go_to_step5(self):
        self._occupation = self._occ_input.text().strip()
        self._build_step5()

    # ── Step 5: security check toggle (entirely optional) ──────────────────
    def _build_step5(self):
        self._clear_root()
        self._root.addWidget(_heading("Want a security check?"))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext(
            "When I wake up, I can ask a quick question to make sure it's "
            "really you before I respond. Totally up to you — you can skip this."
        ))
        self._root.addSpacing(28)

        self._security_cards = []
        row = QHBoxLayout()
        row.setSpacing(20)
        for label, sub, value, color in (
            ("Yes, ask me", "I'll confirm it's you each time I wake up",
             True, QColor(0, 230, 110)),
            ("No, skip it", "Just respond right away, no questions asked",
             False, QColor(150, 150, 160)),
        ):
            card = ChoiceCard(label, sub, color, self._on_security_selected, value)
            card.set_selected(value == self._security_on)
            self._security_cards.append(card)
            row.addWidget(card)
        row.addStretch()
        self._root.addLayout(row)
        self._root.addStretch()

        nav, _ = self._nav_row(back_cb=self._build_step4, next_cb=self._build_step6)
        self._root.addLayout(nav)

    def _on_security_selected(self, value):
        self._security_on = value
        for card in self._security_cards:
            card.set_selected(card._value == value)

    # ── Step 6: instructions, then finish ───────────────────────────────────
    def _build_step6(self):
        self._clear_root()
        self._root.addWidget(_heading("You're all set! 🎉"))
        self._root.addSpacing(6)
        self._root.addWidget(_subtext("Here's a quick guide to get you started."))
        self._root.addSpacing(16)

        rows_container = QWidget()
        rows_box = QVBoxLayout(rows_container)
        rows_box.setContentsMargins(0, 0, 8, 0)
        rows_box.setSpacing(10)
        for emoji, title, desc in INSTRUCTIONS:
            rows_box.addWidget(InstructionRow(emoji, title, desc))

        scroll = QScrollArea()
        scroll.setWidget(rows_container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        scroll.viewport().setStyleSheet("background: transparent;")
        self._root.addWidget(scroll, stretch=1)

        nav, _ = self._nav_row(
            back_cb=self._build_step5, next_cb=self._finish, next_label="Get Started")
        self._root.addLayout(nav)

    def _finish(self):
        self.result_profile = save_profile(
            mode=self._chosen_mode or "Love",
            master=self._master_name or "my love",
            occupation=self._occupation,
            gender=self._gender,
            security_enabled=self._security_on,
        )
        self.completed = True
        self.close()


# ══════════════════════════════════════════════════════════════════════════════
def run_setup_if_needed() -> dict:
    """
    If a profile already exists, returns it immediately (no UI shown).
    Otherwise shows the setup wizard (blocking) and returns the new profile.
    Safe to call before the main QApplication/event loop is fully running —
    creates a temporary app instance if needed.
    """
    existing = load_profile()
    if existing:
        return existing

    app = QApplication.instance()
    if app is None:
        import sys
        app = QApplication(sys.argv)

    window = SetupWindow()
    window.show()
    app.exec()

    if window.completed and window.result_profile:
        return window.result_profile

    # User closed the window without finishing — fall back to a sensible
    # default so the app can still start.
    return save_profile(mode="Love", master="my love", occupation="",
                         gender="Female", security_enabled=True)