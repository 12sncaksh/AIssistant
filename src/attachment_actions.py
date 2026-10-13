"""File/image attachments and the screenshot overlay."""

from __future__ import annotations
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QImage
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QFileDialog
from PyQt6.QtWidgets import QMessageBox
from screen_overlays import ATTACHMENT_MAX_BYTES
from screen_overlays import ATTACHMENT_MAX_MB
from screen_overlays import ScreenCaptureOverlay
import base64
import mimetypes
import os



def _attach_file(self):
    """选择并添加文本类文件附件。"""
    files, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件", "", "所有文件 (*.*)")
    text_exts = {".txt", ".md", ".json", ".csv", ".log", ".ini", ".yml", ".yaml", ".toml",
                 ".html", ".htm", ".css", ".js", ".ts", ".xml", ".bat", ".cmd", ".ps1", ".sh",
                 ".sql", ".py", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".php", ".rb",
                 ".vue", ".jsx", ".tsx", ".ipynb", ".cfg", ".conf", ".env", ".gitignore"}
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
            QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
            break
        try:
            size = os.path.getsize(path)
            if size > ATTACHMENT_MAX_BYTES:
                QMessageBox.warning(self, "文件过大", f"「{name}」超过 {ATTACHMENT_MAX_MB}MB（{size / (1024 * 1024):.1f}MB），已跳过。")
                continue
            if ext not in text_exts and not self._is_text_file(path):
                QMessageBox.warning(self, "无法读取", f"「{name}」不是文本文件或无法读取，已跳过。")
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            QMessageBox.warning(self, "读取失败", f"读取「{name}」失败：{e}")
            continue
        self._pending_attachments.append({"name": name, "content": content})
    self._update_attach_label()


def _attach_screenshot(self):
    """框选屏幕区域，直接作为图片附件加入待发送列表。"""
    if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
        QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
        return
    # 先隐藏聊天窗口，等桌面刷新后再抓屏，避免把自己截进画面
    self.hide()
    QTimer.singleShot(180, self._open_screenshot_overlay)


def _open_screenshot_overlay(self):
    canvas, virtual_rect, ratio = ScreenCaptureOverlay.grab_desktop()
    if canvas is None or canvas.isNull():
        self._restore_after_screenshot()
        QMessageBox.warning(self, "截图失败", "没有抓到屏幕内容，请重试。")
        return
    # 抓屏已经完成，再收起自家置顶窗口：它们留在画面里，但不能继续抢鼠标
    self._hide_floating_windows()
    overlay = ScreenCaptureOverlay(canvas, virtual_rect, ratio)
    self._screenshot_overlay = overlay
    overlay.finished.connect(self._on_screenshot_finished)
    overlay.begin()


def _hide_floating_windows(self):
    """临时收起自家置顶窗口，避免框选时被它们截走鼠标事件。"""
    pet_window = getattr(self, "pet_window", None)
    candidates = [
        pet_window,
        getattr(pet_window, "speech_bubble", None) if pet_window is not None else None,
        getattr(self, "computer_overlay", None),
    ]
    self._hidden_floating = []
    for window in candidates:
        if window is None or not window.isVisible():
            continue
        window.hide()
        self._hidden_floating.append(window)


def _restore_floating_windows(self):
    for window in getattr(self, "_hidden_floating", []):
        window.show()
    self._hidden_floating = []


def _restore_after_screenshot(self):
    self._screenshot_overlay = None
    self._restore_floating_windows()
    self.show()
    self.raise_()
    self.activateWindow()


def _on_screenshot_finished(self, pixmap):
    self._restore_after_screenshot()
    if pixmap is None or pixmap.isNull():
        self.status_bar.showMessage("已取消截图", 2500)
        return
    try:
        data_url, saved_path = self._screenshot_to_attachment(pixmap)
    except OSError as exc:
        QMessageBox.warning(self, "截图失败", f"保存截图失败：{exc}")
        return
    if not data_url:
        QMessageBox.warning(self, "截图过大", f"截图压缩后仍超过 {ATTACHMENT_MAX_MB}MB，请缩小框选范围后重试。")
        return
    name = os.path.basename(saved_path)
    self._pending_image_attachments.append({
        "name": name,
        "path": saved_path,
        "data_url": data_url,
    })
    self._update_attach_label()
    self.status_bar.showMessage(f"已添加截图：{name}", 3000)


def _on_image_pasted(self, image):
    """把剪贴板里的图片变成附件，复用截图那条落盘/编码链路。"""
    if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
        QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
        return
    if isinstance(image, QImage):
        pixmap = QPixmap.fromImage(image)
    elif isinstance(image, QPixmap):
        pixmap = image
    else:
        return
    if pixmap.isNull():
        return
    pixmap.setDevicePixelRatio(1.0)
    try:
        data_url, saved_path = self._screenshot_to_attachment(pixmap, prefix="粘贴")
    except OSError as exc:
        QMessageBox.warning(self, "粘贴失败", f"保存剪贴板图片失败：{exc}")
        return
    if not data_url:
        QMessageBox.warning(self, "图片过大", f"剪贴板图片压缩后仍超过 {ATTACHMENT_MAX_MB}MB，请改用「添加图片」选择文件。")
        return
    name = os.path.basename(saved_path)
    self._pending_image_attachments.append({
        "name": name,
        "path": saved_path,
        "data_url": data_url,
    })
    self._update_attach_label()
    self.status_bar.showMessage(f"已从剪贴板添加图片：{name}", 3000)


def _attach_images(self):
    """选择图片附件，作为视觉模型的 image_url 内容发送。"""
    files, _ = QFileDialog.getOpenFileNames(
        self,
        "添加图片",
        "",
        "图片文件 (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.ico)",
    )
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico"}
    for path in files:
        if len(self._pending_attachments) + len(self._pending_image_attachments) >= 5:
            QMessageBox.information(self, "附件数量上限", "一次最多上传 5 个附件。")
            break
        name = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        if ext not in image_exts:
            continue
        try:
            size = os.path.getsize(path)
            if size > ATTACHMENT_MAX_BYTES:
                QMessageBox.warning(self, "图片过大", f"「{name}」超过 {ATTACHMENT_MAX_MB}MB，已跳过。")
                continue
            with open(path, "rb") as image_file:
                encoded = base64.b64encode(image_file.read()).decode("ascii")
        except OSError as exc:
            QMessageBox.warning(self, "读取失败", f"读取图片「{name}」失败：{exc}")
            continue
        mime_type = mimetypes.guess_type(path)[0] or "image/png"
        self._pending_image_attachments.append({
            "name": name,
            "path": path,
            "data_url": f"data:{mime_type};base64,{encoded}",
        })
    self._update_attach_label()


def _update_attach_label(self):
    if not self._pending_attachments and not self._pending_image_attachments:
        self.attach_label.hide()
        self.attach_clear_btn.hide()
        self._resize_composer_input()
        return
    text_names = [f"{a['name']} ({len(a['content'])}字)" for a in self._pending_attachments]
    image_names = [f"{a['name']}（图片）" for a in self._pending_image_attachments]
    names = "、".join(text_names + image_names)
    self.attach_label.setText(f"附件：{names}")
    self.attach_label.show()
    self.attach_clear_btn.show()
    self._resize_composer_input()


def _clear_attachments(self):
    self._pending_attachments = []
    self._pending_image_attachments = []
    self._update_attach_label()
