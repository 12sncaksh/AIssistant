# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


# SPECPATH 是本 spec 文件所在目录（scripts/），项目根目录是它的上一级。
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
datas = []
binaries = []
hiddenimports = [
    "edge_tts",
    "pygame",
    "sounddevice",
    "websocket",
    "sherpa_onnx",
    "cpuinfo",
    "uiautomation",
    "comtypes",
    "comtypes.client",
    "comtypes.gen",
    "comtypes.stream",
    # 外部 MCP 工具：mcp_client.py 里是在函数内按需导入的，
    # PyInstaller 的静态分析可能漏掉动态加载的部分，这里显式列上。
    "mcp",
    "mcp.client",
    "mcp.client.stdio",
    "mcp.os.win32.utilities",
    "mcp.shared.message",
    "mcp_types",
    "pydantic",
    "pydantic_core",
    "anyio",
    "anyio._backends._asyncio",
    "sniffio",
    "typing_inspection",
    "annotated_types",
    "win32job",
]

for package_name in ("uiautomation", "comtypes"):
    try:
        hiddenimports.extend(collect_submodules(package_name))
    except Exception:
        pass

# Keep only the native libraries needed by sherpa-onnx. Its Python package is
# included through the explicit hidden import above; collecting every package
# submodule and data file can pull in test assets and duplicate dependencies.
try:
    binaries.extend(collect_dynamic_libs("sherpa_onnx"))
except Exception:
    pass

a = Analysis(
    [str(SRC / "main.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "unittest",
        "test",
        "tests",
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib",
        "pandas",
        "scipy",
        "torch",
        "tensorflow",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Aissist",
    icon=str(ROOT / "assets" / "Aissist.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    a.zipped_data,
    name="Aissist",
)
