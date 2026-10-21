"""Background worker for probing DSH ACP session options."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from acp_provider_config import discover_provider_models
from dsh_acp_client import probe_session_options


class DshOptionsProbeThread(QThread):
    """Read DSH-published model and reasoning options without sending a prompt."""

    finished_ok = pyqtSignal(object, str)

    def __init__(self, config, provider=None, model=None, provider_info=None, timeout=45, parent=None):
        super().__init__(parent)
        self.config = config
        self.provider = provider
        self.model = model
        self.provider_info = provider_info
        self.timeout = timeout

    def run(self):
        try:
            options, error = probe_session_options(
                self.config,
                provider=self.provider,
                model=self.model,
                provider_info=self.provider_info,
                timeout=self.timeout,
            )
        except Exception as exc:
            options, error = [], str(exc)
        self.finished_ok.emit(options, error)


class ProviderModelsProbeThread(QThread):
    """Fetch a draft provider's advertised models without starting an ACP session."""

    finished_ok = pyqtSignal(object, str)

    def __init__(self, provider_info, parent=None):
        super().__init__(parent)
        self.provider_info = provider_info

    def run(self):
        try:
            models = discover_provider_models(self.provider_info)
            error = ""
        except Exception as exc:
            models, error = [], str(exc)
        self.finished_ok.emit(models, error)
