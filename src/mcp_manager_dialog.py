"""MCP tool manager dialog."""

from __future__ import annotations
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QDialog
from PyQt6.QtWidgets import QHBoxLayout
from PyQt6.QtWidgets import QLabel
from PyQt6.QtWidgets import QListWidget
from PyQt6.QtWidgets import QListWidgetItem
from PyQt6.QtWidgets import QMessageBox
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtWidgets import QSplitter
from PyQt6.QtWidgets import QTextBrowser
from PyQt6.QtWidgets import QVBoxLayout
from app_paths import get_runtime_dir
from mcp_client import MCPManager
import html
import os
import sys
import webbrowser



def show_mcp_manager_dialog(self):
    """查看 MCP server，并允许即时启用或禁用。"""
    dialog = QDialog(self)
    dialog.setWindowTitle("MCP 工具管理")
    dialog.resize(780, 480)
    layout = QVBoxLayout(dialog)

    layout.addWidget(QLabel(
        "MCP 工具由外部 server 提供，工具名格式 mcp__<server>__<工具名>。\n"
        "勾选左侧复选框可立即启用或禁用；更改命令、地址等配置后仍需重启 Aissistant。"
    ))

    splitter = QSplitter(Qt.Orientation.Horizontal)
    server_list = QListWidget()
    tool_view = QTextBrowser()
    splitter.addWidget(server_list)
    splitter.addWidget(tool_view)
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 2)
    layout.addWidget(splitter, 1)

    status_label = QLabel("")
    layout.addWidget(status_label)

    btn_layout = QHBoxLayout()
    refresh_btn = QPushButton("刷新状态")
    config_btn = QPushButton("打开配置文件")
    log_btn = QPushButton("打开日志目录")
    close_btn = QPushButton("关闭")
    btn_layout.addWidget(refresh_btn)
    btn_layout.addWidget(config_btn)
    btn_layout.addWidget(log_btn)
    btn_layout.addStretch(1)
    btn_layout.addWidget(close_btn)
    layout.addLayout(btn_layout)

    # 只在内容变化时更新视图，避免定时刷新把滚动位置重置到顶部
    view_state = {"server_sig": None, "tool_html": None}

    def build_tool_html(server_name):
        if server_name and not MCPManager.is_server_enabled(server_name):
            return "<p style='color:#888'>该 MCP server 已禁用。勾选左侧复选框即可启用。</p>"
        tools = MCPManager.server_tools(server_name)
        if not tools:
            return "<p style='color:#888'>该 server 暂无可用工具（未就绪或没有工具）。</p>"
        parts = ["<h3>%s（%d 个工具）</h3>" % (html.escape(server_name), len(tools))]
        for t in tools:
            props = list((t.get("parameters") or {}).get("properties", {}).keys())
            parts.append(
                "<p><b>%s</b><br><span style='color:#888'>参数：%s</span><br>%s</p>"
                % (html.escape(t.get("name", "")),
                   html.escape(", ".join(props) or "无"),
                   html.escape(t.get("description", "") or ""))
            )
        return "".join(parts)

    def show_tools(server_name):
        if server_name:
            content = build_tool_html(server_name)
        else:
            content = "<p style='color:#888'>未配置任何 MCP server（或尚未选择）。可在 config/mcp.json 的 mcpServers 中添加。</p>"
        if content == view_state["tool_html"]:
            return
        view_state["tool_html"] = content
        tool_view.setHtml(content)

    def refresh(force=False):
        servers = MCPManager.status()
        server_sig = tuple(
            (
                s.get("name"),
                bool(s.get("enabled")),
                bool(s.get("running")),
                bool(s.get("ready")),
                s.get("tools", 0),
                str(s.get("error") or ""),
            )
            for s in servers
        )
        if force or server_sig != view_state["server_sig"]:
            view_state["server_sig"] = server_sig
            current = server_list.currentItem()
            current_name = current.data(Qt.ItemDataRole.UserRole) if current else None
            row_to_select = 0
            server_list.blockSignals(True)
            server_list.clear()
            for i, s in enumerate(servers):
                enabled = bool(s.get("enabled"))
                if not enabled:
                    state = "已禁用"
                elif s.get("error"):
                    state = "错误"
                elif s.get("ready"):
                    state = "已就绪"
                elif s.get("running"):
                    state = "启动中"
                else:
                    state = "未启动"
                item = QListWidgetItem("%s   [%s]   工具 %d 个" % (s.get("name"), state, s.get("tools", 0)))
                item.setData(Qt.ItemDataRole.UserRole, s.get("name"))
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked
                )
                if not enabled:
                    item.setForeground(QColor("#777777"))
                if s.get("error"):
                    item.setForeground(QColor("#c0392b"))
                    item.setToolTip(str(s.get("error")))
                server_list.addItem(item)
                if s.get("name") == current_name:
                    row_to_select = i
            if servers:
                server_list.setCurrentRow(row_to_select)
            server_list.blockSignals(False)
        item = server_list.currentItem()
        show_tools(item.data(Qt.ItemDataRole.UserRole) if item else "")
        enabled_count = sum(1 for server in servers if server.get("enabled"))
        status_label.setText("共 %d 个 server，%d 个已启用，%d 个 MCP 工具。" % (
            len(servers), enabled_count, len(MCPManager.get_tool_definitions())))

    def on_select():
        item = server_list.currentItem()
        show_tools(item.data(Qt.ItemDataRole.UserRole) if item else "")

    def on_server_toggled(item):
        server_name = item.data(Qt.ItemDataRole.UserRole)
        enabled = item.checkState() == Qt.CheckState.Checked
        try:
            MCPManager.set_server_enabled(server_name, enabled)
        except Exception as exc:
            server_list.blockSignals(True)
            item.setCheckState(
                Qt.CheckState.Unchecked if enabled else Qt.CheckState.Checked
            )
            server_list.blockSignals(False)
            QMessageBox.warning(dialog, "MCP 操作失败", str(exc))
            refresh(force=True)
            return
        status_label.setText(
            "正在%s %s..." % ("启用" if enabled else "禁用", server_name)
        )
        view_state["server_sig"] = None
        refresh(force=True)

    def open_path(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            else:
                webbrowser.open("file://" + path)
        except Exception as exc:
            QMessageBox.warning(dialog, "打开失败", str(exc))

    refresh_btn.clicked.connect(lambda: refresh(force=True))
    server_list.currentItemChanged.connect(lambda *_: on_select())
    server_list.itemChanged.connect(on_server_toggled)
    config_btn.clicked.connect(lambda: open_path(MCPManager.config_path()))
    log_btn.clicked.connect(lambda: open_path(os.path.join(get_runtime_dir(), "logs")))
    close_btn.clicked.connect(dialog.accept)

    timer = QTimer(dialog)
    timer.setInterval(1500)
    timer.timeout.connect(refresh)
    timer.start()

    refresh()
    dialog.exec()
    timer.stop()
