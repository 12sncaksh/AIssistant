"""常驻块编辑器（方案 B）：唯一能改「每一轮都注入的那段话」的界面。

**为什么必须有**：常驻块在**每一次**提问前都会注入，而且**不参与任何相关性打分** ——
push 的 `min_ratio`/`min_score` 只管检索结果，常驻块压根不过那道门。也就是说，
写歪一条**没有任何阈值能挡住它**，会一直污染之后的每一轮。所以"能改"是它唯一的
安全阀。在这之前，唯一的改法是手工编辑 `data/core_memory.json`。

**为什么"改写"是 add + supersede，而不是就地改字**：就地改会丢掉"这条曾经是什么"。
常驻块最典型的错法恰恰是"某条旧偏好早该失效了却还在注入"，事后要能回答
"它是什么时候、被什么替换的"，就必须留原文。**界面上没有"删除"按钮**，这是刻意的。

**为什么它是个低频界面**：常驻块是几十字到几百字的短文本，正常情况下几个月不用动。
把它放进设置页只是让它**可被找到**，不是让它变成常用功能。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core_memory import (
    FIELD_ORDER,
    FIELD_TAB_TITLES,
    UNLIMITED_BUDGET,
    format_core_block,
    get_core_memory,
)

SUPERSEDED_COLOR = "#999999"


class CoreMemoryDialog(QDialog):
    """列出四个字段的条目，支持新增 / 改写 / 标失效，并预览实际注入的内容。"""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self._show_superseded = False
        self.setWindowTitle("常驻块 —— 每一轮都会注入")
        self.setMinimumSize(760, 560)

        root = QVBoxLayout(self)
        hint = QLabel(
            "常驻块在每一次提问前都会注入，且**不经过任何相关性打分** —— "
            "没有任何阈值能挡住写歪的一条。\n"
            "「用户自定义」是你手写的：程序永不改写、永不淘汰，也不限制长度。"
            "「模型提炼」由后台滚动维护（合并重复 + 淘汰过时），默认上限 600 字符。\n"
            "「改写」和「标失效」都会保留原文、来源与时间（不删除），方便事后追溯。"
        )
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.lists = {}
        self.budget_labels = {}
        for field in FIELD_ORDER:
            self.tabs.addTab(self._build_field_page(field), FIELD_TAB_TITLES.get(field, field))
        root.addWidget(self.tabs, 1)

        root.addWidget(QLabel("实际注入的内容："))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("（常驻块当前为空）")
        root.addWidget(self.preview, 1)

        footer = QHBoxLayout()
        self.history_checkbox = QCheckBox("同时显示已失效的条目（审计用）")
        self.history_checkbox.toggled.connect(self._on_toggle_history)
        footer.addWidget(self.history_checkbox)
        footer.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._reload)
        footer.addWidget(refresh)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        root.addLayout(footer)

        self._reload()

    # ---- 构造 ----------------------------------------------------------

    def _build_field_page(self, field):
        page = QWidget()
        layout = QVBoxLayout(page)
        listing = QListWidget()
        listing.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(listing)
        budget = QLabel("")
        budget.setStyleSheet("color: #666;")
        layout.addWidget(budget)
        buttons = QHBoxLayout()
        for label, handler in (
            ("新增…", self._add),
            ("改写…", self._edit),
            ("标失效", self._retire),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, f=field, h=handler: h(f))
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.lists[field] = listing
        self.budget_labels[field] = budget
        return page

    # ---- 数据 ----------------------------------------------------------

    def _store(self):
        return get_core_memory(self.config).store

    def _on_toggle_history(self, checked):
        self._show_superseded = bool(checked)
        self._reload()

    def _reload(self):
        try:
            store = self._store()
        except Exception as error:  # 读取失败不该让对话框打不开
            self.preview.setPlainText(f"读取常驻块失败：{error}")
            return
        for field in FIELD_ORDER:
            listing = self.lists[field]
            listing.clear()
            used = 0
            for item in store.items(field, status=None if self._show_superseded else "active"):
                active = item.get("status") == "active"
                text = str(item.get("text") or "")
                if active:
                    used += len(text)
                source = str(item.get("source_session") or "") or "—"
                entry = QListWidgetItem(
                    f"{text}\n     来源 {source} · {item.get('created_at', '')}"
                    + ("" if active else " · 已失效")
                )
                entry.setData(Qt.ItemDataRole.UserRole, dict(item))
                if not active:
                    entry.setForeground(QColor(SUPERSEDED_COLOR))
                listing.addItem(entry)
            budget = store.field_budget(field)
            if budget == UNLIMITED_BUDGET:
                self.budget_labels[field].setText(
                    f"生效中 {used} 字符（不限制长度，写多少注入多少）"
                )
            else:
                self.budget_labels[field].setText(
                    f"生效中 {used} / {budget} 字符（超出会在下次提炼时合并压缩）"
                )
        try:
            self.preview.setPlainText(format_core_block(store) or "")
        except Exception as error:
            self.preview.setPlainText(f"渲染常驻块失败：{error}")

    def _selected(self, field):
        entry = self.lists[field].currentItem()
        if entry is None:
            QMessageBox.information(self, "先选一条", "请先在列表里选中一条。")
            return None
        return entry.data(Qt.ItemDataRole.UserRole)

    # ---- 三个动作 ------------------------------------------------------

    def _add(self, field):
        text, ok = QInputDialog.getMultiLineText(
            self, "新增条目", "内容（陈述句，主语可省略）：", ""
        )
        if not ok or not str(text or "").strip():
            return
        # source_session 记成 manual：一眼能看出这条是人写的、不是提炼来的。
        # locked=True：**人写的条目优先级最高** —— 模型提炼出来的猜测不该有权覆盖或淘汰它，
        # 条目内部也不截断（用户写的话不该被程序砍掉半句）。
        written = self._store().add_item(
            field, text, source_session="manual", confidence=1.0, locked=True
        )
        if written is None:
            QMessageBox.warning(self, "没写进去", "内容为空或字段不合法。")
        self._reload()

    def _edit(self, field):
        item = self._selected(field)
        if item is None:
            return
        if item.get("status") != "active":
            QMessageBox.information(
                self, "已失效的条目", "已失效的条目不能改写，只能作为历史查看。"
            )
            return
        old_text = str(item.get("text") or "")
        text, ok = QInputDialog.getMultiLineText(self, "改写条目", "新内容：", old_text)
        if not ok or not str(text or "").strip() or str(text).strip() == old_text.strip():
            return
        store = self._store()
        new_item = store.add_item(
            field, text, source_session="manual", confidence=1.0, locked=True
        )
        if new_item and str(new_item.get("id")) != str(item.get("id")):
            # 老条目留着（不删），只是不再注入 —— 审计线索比省几十字符值钱。
            # allow_locked=True：这是**用户自己**在改自己的条目，不是模型在覆盖它。
            store.supersede_item(
                field,
                item.get("id"),
                new_item_id=new_item.get("id"),
                reason="manual-edit",
                allow_locked=True,
            )
        self._reload()

    def _retire(self, field):
        item = self._selected(field)
        if item is None:
            return
        if item.get("status") != "active":
            return
        answer = QMessageBox.question(
            self,
            "标为失效",
            f"把这条标为失效？\n\n{item.get('text')}\n\n原文和来源会保留，只是不再注入。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # allow_locked=True：用户手动标失效，包括标自己的手写条目。
        self._store().supersede_item(
            field, item.get("id"), reason="manual-retire", allow_locked=True
        )
        self._reload()


# ---- 挂到 ConfigDialog 上的两个函数（跟 dsh_settings_ui 同一套约定）----


def open_core_memory_dialog(self):
    """打开编辑器。挂成 ConfigDialog 的方法。"""
    CoreMemoryDialog(self.config, parent=self).exec()


def build_core_memory_tab(self):
    """构造设置页里的「常驻块」标签页：说明 + 编辑入口 + 当前注入内容预览。

    刻意只放一个按钮和一个只读预览：这一页的作用是让常驻块**可被找到、可被核对**，
    真正的改动都在对话框里做。
    """
    # 注意 `_make_tab_page()` 给的是 **QFormLayout**（config_dialog 里用 addRow 填它），
    # 所以这里只能 addRow —— addWidget(w, stretch) 会直接 TypeError。
    page, layout = self._make_tab_page()
    title = QLabel("常驻块（每一轮都会注入，且不经过相关性打分）")
    title.setWordWrap(True)
    layout.addRow(title)
    note = QLabel(
        "它装长期偏好、能力索引和当前任务。因为它不过任何阈值，写歪一条会一直影响之后的"
        "每一轮 —— 所以这里提供唯一的修改入口。"
    )
    note.setWordWrap(True)
    note.setStyleSheet("color: #666;")
    layout.addRow(note)
    button = QPushButton("编辑常驻块…")
    button.clicked.connect(lambda: open_core_memory_dialog(self))
    layout.addRow(button)
    layout.addRow(QLabel("当前注入的内容："))
    preview = QTextEdit()
    preview.setReadOnly(True)
    try:
        preview.setPlainText(format_core_block(get_core_memory(self.config).store) or "")
    except Exception as error:
        preview.setPlainText(f"读取常驻块失败：{error}")
    layout.addRow(preview)
    return page
