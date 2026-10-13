"""Background worker for probing DSH ACP session options."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from dsh_acp_client import probe_session_options


class DshOptionsProbeThread(QThread):
    """Read DSH-published model and reasoning options without sending a prompt."""

    finished_ok = pyqtSignal(object, str)

    def __init__(self, config, provider=None, model=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.provider = provider
        self.model = model

    def run(self):
        try:
            options, error = probe_session_options(
                self.config, provider=self.provider, model=self.model
            )
        except Exception as exc:
            options, error = [], str(exc)
        self.finished_ok.emit(options, error)
