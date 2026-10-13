"""Loading, cropping and saving of user / assistant avatar images."""

from __future__ import annotations

import os
import uuid

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QImage, QImageReader, QPixmap

from app_paths import get_avatar_storage_dir

AVATAR_OUTPUT_SIDE = 512
AVATAR_MAX_SOURCE_SIDE = 4096
AVATAR_MAX_FILE_BYTES = 20 * 1024 * 1024


def load_avatar_source_pixmap(file_path):
    """读取头像源图；超大图片在解码阶段就缩小，避免占用过多内存。"""
    file_path = os.path.abspath(str(file_path or ""))
    if not os.path.isfile(file_path):
        return QPixmap()
    try:
        if os.path.getsize(file_path) > AVATAR_MAX_FILE_BYTES:
            return QPixmap()
    except OSError:
        return QPixmap()

    reader = QImageReader(file_path)
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid() and max(source_size.width(), source_size.height()) > AVATAR_MAX_SOURCE_SIDE:
        reader.setScaledSize(
            source_size.scaled(
                QSize(AVATAR_MAX_SOURCE_SIDE, AVATAR_MAX_SOURCE_SIDE),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        )
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


def save_avatar_pixmap(pixmap, avatar_kind):
    """把裁剪后的头像原子写入 data/avatars，并返回实际文件路径。"""
    if pixmap.isNull():
        raise ValueError("头像图片无效")
    avatar_kind = "assistant" if avatar_kind == "assistant" else "user"
    scaled = pixmap.scaled(
        AVATAR_OUTPUT_SIDE,
        AVATAR_OUTPUT_SIDE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    image = scaled.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    target_path = os.path.join(
        get_avatar_storage_dir(),
        f"{avatar_kind}_{uuid.uuid4().hex}.png",
    )
    temp_path = target_path + ".tmp"
    if not image.save(temp_path, "PNG"):
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise OSError("头像保存失败")
    os.replace(temp_path, target_path)
    return target_path
