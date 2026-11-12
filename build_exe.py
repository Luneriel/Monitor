"""把 Monitor 打包成 Windows 可执行文件（PyInstaller）。

用法（在项目目录下执行）：

    python build_exe.py              # 单文件 dist/Monitor.exe + 便携 zip
    python build_exe.py --onedir     # 目录版 dist/Monitor/（启动更快，整体打包）
    python build_exe.py --console    # 带控制台窗口，排查启动问题用

脚本做四件事：
1. 用程序自己的图标绘制代码生成 ``build/monitor.ico``（16~256 多尺寸）；
2. 按 ``monitor.config.APP_VERSION`` 生成 Windows 版本资源 ``build/version_info.txt``；
3. 调 PyInstaller 打包（QtCore/QtGui/QtWidgets + psutil + pywin32 + comtypes），
   并挂上 ``packaging/rthook_stdio.py``（窗口模式下给标准流兜底）；
4. 把产物压成 ``dist/Monitor-<版本>-win64.zip`` 并打印 SHA256，方便挂到 GitHub Release。

可执行文件默认以**普通用户**权限启动（manifest 为 asInvoker）：双击即普通启动，
需要管理员权限时右键「以管理员身份运行」（程序内设置页的「以管理员身份启动」
也是走同一套 UAC 提权逻辑）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(ROOT, "build")
DIST_DIR = os.path.join(ROOT, "dist")
# 窗口模式没有控制台，PyInstaller 会把 sys.stdout/stderr/stdin 设成 None，
# 程序里一句 print() 就会 AttributeError（例如日志页删除完成的提示）。
# 这个运行期钩子把三个流兜到 devnull，见文件里的说明。
RTHOOK = os.path.join(ROOT, "packaging", "rthook_stdio.py")

# 仅排除未使用的 Qt 模块。WebEngineCore/Widgets 的官方 hook 必须收集
# QtWebEngineProcess、resources、locales 及其 QtQml/Quick/WebChannel 依赖。
# 注意别排除标准库的网络相关模块：企业微信通知走 urllib.request → http.client。
EXCLUDES = [
    "PySide6.QtWebEngineQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    # QtMultimedia's PyInstaller hook includes its platform/media plugins.
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.Qt3DCore", "PySide6.QtBluetooth",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtLocation", "PySide6.QtNfc",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtSvg",
    "PySide6.QtSvgWidgets", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
    "PySide6.QtWebSockets",
    "tkinter", "numpy", "pandas", "matplotlib", "docx", "lxml", "IPython", "pytest",
]

ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def app_version() -> str:
    sys.path.insert(0, ROOT)
    from monitor import config

    return str(config.APP_VERSION)


def make_icon(target: str) -> str:
    """用程序的默认图标绘制代码生成多尺寸 ico。

    图标条目写成 32 位未压缩 DIB（BMP）而不是 PNG 压缩条目：PyInstaller 写
    Windows 资源时不认 PNG 条目（那需要额外装 Pillow），BMP 条目是原生支持的。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QGuiApplication, QImage

    from monitor import icon_extract

    app = QGuiApplication.instance() or QGuiApplication([])
    images = []
    for size in ICO_SIZES:
        image = icon_extract._draw_default_icon(size, rounded=True).toImage()
        images.append((size, _dib_bytes(image.convertToFormat(QImage.Format_ARGB32))))
    del app

    # ICO 容器：6 字节头 + 每张图 16 字节目录项 + DIB 数据
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size          # 256 在 ICO 里记 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(header + entries + blobs)
    return target


def _dib_bytes(image) -> bytes:
    """QImage(ARGB32) → ICO 里的 32 位 DIB：信息头 + 自下而上的 BGRA + 全零掩码。"""
    width, height = image.width(), image.height()
    stride = image.bytesPerLine()
    raw = bytes(image.constBits())
    out = bytearray(struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                                width * height * 4, 0, 0, 0, 0))
    for row in range(height - 1, -1, -1):
        start = row * stride
        out += raw[start:start + width * 4]
    out += b"\x00" * ((((width + 31) // 32) * 4) * height)      # AND 掩码（透明度看 alpha）
    return bytes(out)


def make_version_file(target: str, version: str) -> str:
    quad = tuple(int(part) for part in (version.split(".") + ["0", "0", "0"])[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad}, prodvers={quad},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Luneriel'),
        StringStruct('FileDescription', 'Monitor 使用时间监控与限制'),
        StringStruct('FileVersion', '{'.'.join(str(p) for p in quad)}'),
        StringStruct('InternalName', 'Monitor'),
        StringStruct('OriginalFilename', 'Monitor.exe'),
        StringStruct('ProductName', 'Monitor'),
        StringStruct('ProductVersion', '{version}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text)
    return target


def build(mode: str, console: bool, version: str) -> str:
    icon = make_icon(os.path.join(BUILD_DIR, "monitor.ico"))
    version_file = make_version_file(os.path.join(BUILD_DIR, "version_info.txt"), version)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--name", "Monitor",
        "--icon", icon,
        "--version-file", version_file,
        "--distpath", DIST_DIR,
        "--workpath", os.path.join(BUILD_DIR, "work"),
        "--specpath", BUILD_DIR,
        "--collect-all", "comtypes",          # 网站追踪靠 UIAutomation，comtypes.gen 必须进包
        "--hidden-import", "PySide6.QtMultimedia",
        "--hidden-import", "PySide6.QtWebEngineCore",
        "--hidden-import", "PySide6.QtWebEngineWidgets",
        "--hidden-import", "PySide6.QtQml",
        "--hidden-import", "PySide6.QtQuick",
        "--hidden-import", "PySide6.QtWebChannel",
        "--hidden-import", "PySide6.QtPositioning",
        "--add-data", os.path.join(ROOT, "monitor", "assets") + os.pathsep + "monitor/assets",
        "--runtime-hook", RTHOOK,
    ]
    command += ["--onefile"] if mode == "onefile" else ["--onedir"]
    command += ["--console"] if console else ["--windowed"]
    for name in EXCLUDES:
        command += ["--exclude-module", name]
    command.append(os.path.join(ROOT, "main.py"))
    print("[build] " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {result.returncode}")
    name = "Monitor.exe" if mode == "onefile" else "Monitor"
    return os.path.join(DIST_DIR, name)


def zip_output(product: str, version: str) -> str:
    archive = os.path.join(DIST_DIR, f"Monitor-{version}-win64.zip")
    if os.path.exists(archive):
        os.remove(archive)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        if os.path.isdir(product):
            base = os.path.dirname(product)
            for folder, _dirs, files in os.walk(product):
                for item in files:
                    full = os.path.join(folder, item)
                    zf.write(full, os.path.relpath(full, base))
        else:
            zf.write(product, os.path.basename(product))
    return archive


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(path: str) -> str:
    size = os.path.getsize(path)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Monitor 为 Windows 可执行文件")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--onefile", action="store_true", help="单文件 dist/Monitor.exe（默认）")
    group.add_argument("--onedir", action="store_true", help="目录版 dist/Monitor/")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排查用）")
    args = parser.parse_args()

    mode = "onedir" if args.onedir else "onefile"
    version = app_version()
    for folder in (BUILD_DIR, DIST_DIR):
        os.makedirs(folder, exist_ok=True)

    product = build(mode, args.console, version)
    archive = zip_output(product, version)
    print(f"\n[build] 产物   : {product}")
    print(f"[build] 版本   : {version}")
    print(f"[build] 压缩包 : {archive}  ({human_size(archive)})")
    print(f"[build] SHA256 : {sha256(archive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
