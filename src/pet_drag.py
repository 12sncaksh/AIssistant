"""Pet window dragging and the staged drag/drop animation."""

from __future__ import annotations
from PyQt6.QtCore import QTimer
from pet_scripts import PET_DRAG_FALL_MS
from pet_scripts import PET_DRAG_RECOVER_MS
from pet_scripts import PET_DRAG_RESTORE_MS
from pet_scripts import PET_DRAG_STAGE_ENABLED
from pet_scripts import PET_DRAG_STAGE_SCRIPT
import json
import logging
import time



def begin_drag(self, global_pos):
    self._drag_offset = global_pos - self.frameGeometry().topLeft()
    self._drag_last_pos = None
    self._drag_last_time = 0.0
    self._drag_vx = 0.0
    self._drag_vy = 0.0


def update_drag(self, global_pos):
    if self._drag_offset is None:
        return
    now = time.monotonic()
    if self._drag_last_pos is not None:
        dt = now - self._drag_last_time
        if dt > 0.001:
            inst_vx = (global_pos.x() - self._drag_last_pos.x()) / dt
            inst_vy = (global_pos.y() - self._drag_last_pos.y()) / dt
            self._drag_vx = self._drag_vx * 0.6 + inst_vx * 0.4
            self._drag_vy = self._drag_vy * 0.6 + inst_vy * 0.4
    self._drag_last_pos = global_pos
    self._drag_last_time = now
    if self._drag_active:
        self._push_pet_drag_stage("drag", self._drag_vx, self._drag_vy)
    self.move(global_pos - self._drag_offset)


def end_drag(self):
    self._drag_offset = None
    self._drag_last_pos = None
    if self._drag_active:
        self._drag_active = False
        self._play_pet_drop_sequence()


def start_pet_drag_stage(self):
    """右键真正拖动起来后调用：先确保演出脚本在位，再切到被拎起来的状态。"""
    self._install_pet_drag_stage()
    self._pet_stage_gen += 1
    self._drag_active = True
    self._drag_vx = 0.0
    self._drag_vy = 0.0
    self._push_pet_drag_stage("hold", 0.0, 0.0)
    QTimer.singleShot(500, self._probe_pet_drag_stage)


def _install_pet_drag_stage(self):
    self._drag_active = False
    self._pet_stage_gen += 1
    if not PET_DRAG_STAGE_ENABLED or self.webview is None:
        return
    self.webview.page().runJavaScript(PET_DRAG_STAGE_SCRIPT)


def _push_pet_drag_stage(self, phase, vx=0.0, vy=0.0):
    if not PET_DRAG_STAGE_ENABLED or self.webview is None:
        return
    script = "window.__petStageSet && window.__petStageSet(%s, %s, %s);" % (
        json.dumps(phase), round(float(vx), 1), round(float(vy), 1))
    self.webview.page().runJavaScript(script)


def _probe_pet_drag_stage(self):
    if not PET_DRAG_STAGE_ENABLED or self.webview is None:
        return
    script = "JSON.stringify(window.__petStageProbe ? window.__petStageProbe() : {missing: true})"
    self.webview.page().runJavaScript(script, self._log_pet_drag_probe)


def _log_pet_drag_probe(self, result):
    logging.info("[pet-stage] probe: %s", result)


def _play_pet_drop_sequence(self):
    """松手：扑通摔一下 -> 爬起来 -> 道具淡回来。"""
    self._pet_stage_gen += 1
    generation = self._pet_stage_gen
    self._push_pet_drag_stage("fall", 0.0, 0.0)
    QTimer.singleShot(PET_DRAG_FALL_MS, lambda: self._advance_pet_stage(generation, "recover"))
    QTimer.singleShot(PET_DRAG_FALL_MS + PET_DRAG_RECOVER_MS, lambda: self._advance_pet_stage(generation, "restore"))
    QTimer.singleShot(
        PET_DRAG_FALL_MS + PET_DRAG_RECOVER_MS + PET_DRAG_RESTORE_MS,
        lambda: self._advance_pet_stage(generation, "idle"),
    )


def _advance_pet_stage(self, generation, phase):
    if generation != self._pet_stage_gen:
        return
    self._push_pet_drag_stage(phase, 0.0, 0.0)
