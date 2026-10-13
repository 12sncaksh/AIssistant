"""Render the settings dialog tabs to PNG so layout regressions are visible.

Offscreen only: no window is shown, nothing is written to user config.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from config_dialog import ConfigDialog  # noqa: E402
from config_manager import ConfigManager  # noqa: E402


def main():
    out_dir = os.path.join(ROOT, "lab", "ui_shots")
    os.makedirs(out_dir, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    config = ConfigManager()
    dialog = ConfigDialog(config, section="config")
    dialog.resize(760, 560)
    dialog.show()
    app.processEvents()

    paths = []
    for index in range(dialog.tabs.count()):
        dialog.tabs.setCurrentIndex(index)
        app.processEvents()
        title = dialog.tabs.tabText(index)
        path = os.path.join(out_dir, f"tab_{index}.png")
        dialog.tabs.currentWidget().grab().save(path)
        paths.append((title, path))
        print(f"tab {index}: {title} -> {path}")

    # 单独截「记忆检索」分组，便于确认字段没有被裁掉
    group = dialog.embedding_backend_combo.parentWidget()
    group_path = os.path.join(out_dir, "embedding_group.png")
    group.grab().save(group_path)
    print(f"embedding group -> {group_path}")

    dialog.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
