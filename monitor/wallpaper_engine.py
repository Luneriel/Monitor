"""Read-only Wallpaper Engine discovery and bounded, non-executing asset import.

All functions are synchronous and Qt-free; run them in a UI worker. ``cancel``
is an optional callable returning True and ``progress`` receives status text.
No Steam/WE source file is ever written; third-party runtime license texts ship
under ``monitor/assets/we-runtime/``. Scene/Web projects can be copied for the isolated optional WebGL runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import struct
import tempfile
from typing import Callable, Iterable, BinaryIO

APP_ID = "431960"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v"})
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_PKG_BYTES = 8 * 1024 * 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_PKG_ENTRIES = 20_000
MAX_BLOCK_BYTES = 64 * 1024 * 1024
MAX_PROJECTS = 20_000
MAX_IMAGE_PIXELS = 64_000_000
MAX_IMAGE_DIMENSION = 32768
Cancel = Callable[[], bool] | None


class WallpaperEngineError(ValueError):
    """A source cannot safely supply a supported media asset."""


class ImportCancelled(WallpaperEngineError):
    pass


@dataclass(frozen=True)
class WallpaperProject:
    id: str
    title: str
    project_dir: str
    type: str
    entry: str
    preview: str | None
    import_mode: str
    reason: str = ""


@dataclass(frozen=True)
class ScanResult:
    projects: tuple[WallpaperProject, ...]
    warnings: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImportResult:
    id: str
    title: str
    path: str
    kind: str
    source_type: str
    mode: str
    message: str
    duplicate: bool
    preview: str | None = None

    @property
    def media_path(self) -> str:
        return self.path

    @property
    def media_type(self) -> str:
        return self.kind

    @property
    def degraded(self) -> bool:
        return self.mode == "static_preview" or (self.source_type == "scene" and self.kind not in {"live_scene", "live_web"})


def _check(cancel: Cancel) -> None:
    if cancel and cancel():
        raise ImportCancelled("已取消壁纸导入。")


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value:
        raise WallpaperEngineError("无效的项目资源路径。")
    value = value.replace("\\", "/")
    win = PureWindowsPath(value)
    parts = value.split("/")
    if win.drive or win.root or value.startswith("/") or any(
        part in {"", ".", ".."} or ":" in part or part.endswith((" ", "."))
        for part in parts
    ):
        raise WallpaperEngineError("项目资源路径越界或不安全。")
    # Windows reserved device names are not normal disk assets.
    if any(re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p, re.I) for p in parts):
        raise WallpaperEngineError("项目资源路径包含设备名称。")
    return str(PurePosixPath(value))


def safe_project_path(root: str | Path, value: str, *, directory: bool = False) -> Path:
    base = Path(root).resolve()
    result = (base / _relative_name(value)).resolve()
    if result == base or not result.is_relative_to(base):
        raise WallpaperEngineError("项目资源路径越过项目目录。")
    if not (result.is_dir() if directory else result.is_file()):
        raise WallpaperEngineError("项目资源文件不存在。")
    return result


def _json(path: Path) -> dict:
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise WallpaperEngineError("项目元数据过大。")
    try:
        # Bounded read also handles growth between stat and read.
        with path.open("rb") as stream:
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            raise WallpaperEngineError("项目元数据过大。")
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WallpaperEngineError("项目 JSON 无效。") from exc
    if not isinstance(obj, dict):
        raise WallpaperEngineError("项目 JSON 必须是对象。")
    return obj


def parse_libraryfolders(text: str) -> tuple[str, ...]:
    """Parse current and legacy Valve KeyValues; malformed input is rejected."""
    if len(text.encode("utf-8")) > MAX_METADATA_BYTES:
        raise WallpaperEngineError("Steam 库列表过大。")
    tokens: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace() or ch == "\ufeff":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
        elif ch in "{}":
            tokens.append(ch)
            i += 1
        elif ch == '"':
            i += 1
            value: list[str] = []
            while i < len(text) and text[i] != '"':
                if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in {'"', "\\"}:
                    i += 1
                value.append(text[i])
                i += 1
            if i == len(text):
                raise WallpaperEngineError("Steam VDF 字符串未闭合。")
            tokens.append("".join(value))
            i += 1
        else:
            start = i
            while i < len(text) and not text[i].isspace() and text[i] not in '{}"':
                i += 1
            tokens.append(text[start:i])
        if len(tokens) > 100_000:
            raise WallpaperEngineError("Steam VDF 条目过多。")
    pos = 0

    def read_object(depth: int, nested: bool = False) -> dict:
        nonlocal pos
        if depth > 16:
            raise WallpaperEngineError("Steam VDF 嵌套过深。")
        out = {}
        while pos < len(tokens):
            key = tokens[pos]
            pos += 1
            if key == "}":
                if nested:
                    return out
                raise WallpaperEngineError("Steam VDF 多余闭括号。")
            if key == "{" or pos >= len(tokens):
                raise WallpaperEngineError("Steam VDF 键值不完整。")
            value = tokens[pos]
            pos += 1
            if value == "{":
                out[key] = read_object(depth + 1, True)
            elif value == "}":
                raise WallpaperEngineError("Steam VDF 缺少值。")
            else:
                out[key] = value
        if nested:
            raise WallpaperEngineError("Steam VDF 对象未闭合。")
        return out

    data = read_object(0)
    folders = data.get("libraryfolders", data.get("LibraryFolders", data))
    if not isinstance(folders, dict):
        raise WallpaperEngineError("Steam VDF 库列表无效。")
    paths = []
    for key, value in folders.items():
        if key.isdecimal():
            path = value.get("path") if isinstance(value, dict) else value
            if isinstance(path, str) and path.strip() and "\0" not in path:
                paths.append(path)
    return tuple(dict.fromkeys(paths))


def _registry_roots() -> list[Path]:
    """Windows registry reads only; never invokes Steam, executables or scripts."""
    if os.name != "nt":
        return []
    import winreg
    paths = []
    for hive, key, name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
            if isinstance(value, str) and value:
                paths.append(Path(value))
        except OSError:
            continue
    return paths


def discover_roots(selected_path: str | Path | None = None, *, discover: bool = True) -> tuple[Path, ...]:
    """Return project containers for selected Steam/WE/project/Workshop directory.

    A selection is exclusive: even with discover=True it never probes registry
    or any unrelated Steam install. No recursive drive search is performed.
    """
    if selected_path is not None:
        selected = Path(selected_path).expanduser().resolve()
        if selected.is_file():
            return (selected.parent if selected.name.lower() == "project.json" else selected,)
        if not selected.is_dir():
            raise WallpaperEngineError("所选目录不存在。")
        candidates = [selected]
    elif discover:
        candidates = _registry_roots()
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                candidates.append(Path(os.environ[env]) / "Steam")
    else:
        return ()
    libraries: list[Path] = []
    for candidate in candidates:
        if candidate.name.lower() == "steamapps":
            candidate = candidate.parent
        libraries.append(candidate.resolve())
        vdf = candidate / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                if vdf.stat().st_size <= MAX_METADATA_BYTES:
                    with vdf.open("r", encoding="utf-8-sig") as stream:
                        text = stream.read(MAX_METADATA_BYTES + 1)
                    libraries.extend(Path(p).resolve() for p in parse_libraryfolders(text))
            except (OSError, WallpaperEngineError, UnicodeError):
                # Keep the selected/default library; a broken VDF cannot erase it.
                pass
    roots: list[Path] = []
    for library in dict.fromkeys(libraries):
        workshop = library / "steamapps" / "workshop" / "content" / APP_ID
        if workshop.is_dir():
            roots.append(workshop)
        installs = [library, library / "steamapps" / "common" / "wallpaper_engine"]
        # Steam's manifest supports renamed/nonstandard install directory names.
        manifest = library / "steamapps" / f"appmanifest_{APP_ID}.acf"
        if manifest.is_file() and manifest.stat().st_size <= MAX_METADATA_BYTES:
            try:
                text = manifest.read_text(encoding="utf-8-sig")
                match = re.search(r'"installdir"\s+"([^"\r\n]+)"', text, re.I)
                if match:
                    name = _relative_name(match.group(1))
                    installs.append(library / "steamapps" / "common" / name)
            except (OSError, UnicodeError, WallpaperEngineError):
                pass
        for install in installs:
            for sub in ("defaultprojects", "myprojects"):
                project_root = install / "projects" / sub
                if project_root.is_dir():
                    roots.append(project_root)
        if selected_path is not None and not roots and library == libraries[0]:
            # A project, projects folder, Workshop 431960 folder, or collection.
            if (library / "projects").is_dir():
                roots.append(library / "projects")
            else:
                roots.append(library)
    return tuple(dict.fromkeys(p.resolve() for p in roots))


def _optional_file(root: Path, name: object, extensions: frozenset[str] | None = None) -> Path | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        path = safe_project_path(root, name)
        if extensions is None or path.suffix.lower() in extensions:
            return path
    except (WallpaperEngineError, OSError, RuntimeError):
        pass
    return None


def _natural_key(path: Path) -> list:
    return [(1, int(p)) if p.isdigit() else (0, p.casefold()) for p in re.split(r"(\d+)", str(path))]


def _picture(root: Path, obj: dict, cancel: Cancel) -> tuple[Path | None, bool]:
    preset = obj.get("preset")
    if not isinstance(preset, dict):
        return None, False
    single = _optional_file(root, preset.get("image"), IMAGE_EXTENSIONS)
    custom = preset.get("customdirectory")
    mode = str(preset.get("wallpapermode", ""))
    if isinstance(custom, str) and custom and mode in {"1", "2"}:
        try:
            directory = safe_project_path(root, custom, directory=True)
            images = _list_assets(directory, IMAGE_EXTENSIONS, root=root, cancel=cancel, depth=3)
            if images:
                return sorted(images, key=_natural_key)[0], len(images) > 1 or mode == "2"
        except ImportCancelled:
            raise
        except (OSError, WallpaperEngineError):
            pass
    return single, False


def _list_assets(directory: Path, extensions: frozenset[str], *, root: Path, cancel: Cancel, depth: int = 4) -> list[Path]:
    out = []
    visited = 0
    def walk(current: Path, level: int) -> None:
        nonlocal visited
        with os.scandir(current) as entries:
            for entry in entries:
                _check(cancel)
                visited += 1
                if visited > 10_000 or len(out) >= 2000:
                    return
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                if entry.is_dir(follow_symlinks=False) and level < depth:
                    walk(path, level + 1)
                elif entry.is_file(follow_symlinks=False) and path.suffix.lower() in extensions:
                    out.append(path)
    walk(directory, 0)
    return out


def read_project(path: str | Path, *, cancel: Cancel = None, prefer_live: bool = True) -> WallpaperProject:
    _check(cancel)
    root = Path(path).resolve()
    if root.is_file() and root.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        kind = "video" if is_video(root) else "image"
        return WallpaperProject(hashlib.sha256(str(root).encode()).hexdigest()[:20], root.stem,
                                str(root.parent), kind, str(root), str(root) if kind == "image" else None, kind)
    if root.is_file() and root.name.lower() == "project.json":
        root = root.parent
    obj = _json(safe_project_path(root, "project.json"))
    title = obj.get("title")
    title = title.strip()[:512] if isinstance(title, str) and title.strip() else root.name
    declared = obj.get("type", "")
    declared = declared.lower() if isinstance(declared, str) else ""
    name = obj.get("file")
    entry = _optional_file(root, name)
    # A declared traversal is an error, even if a fallback preview exists.
    if isinstance(name, str) and name:
        _relative_name(name)
    preview = _optional_file(root, obj.get("preview"), IMAGE_EXTENSIONS)
    inferred = "video" if entry and is_video(entry) else "image" if entry and entry.suffix.lower() in IMAGE_EXTENSIONS else "scene"
    kind = declared or inferred
    reason = ""
    mode = "unsupported"
    if kind == "application":
        reason = "应用程序壁纸不支持导入，也不会运行其程序。"
    elif kind in {"image", "picture"} or (not entry and isinstance(obj.get("preset"), dict)):
        if not entry or entry.suffix.lower() not in IMAGE_EXTENSIONS:
            entry, slideshow = _picture(root, obj, cancel)
            if slideshow:
                reason = "幻灯片项目仅导入首张图片；不包含 Wallpaper Engine 的轮播。"
        if entry and entry.suffix.lower() in IMAGE_EXTENSIONS:
            mode = "image"
            preview = preview or entry
            kind = "image"
    elif kind == "video" and entry and is_video(entry):
        mode = "video"
    elif kind == "scene":
        if not entry or entry.suffix.lower() not in {".pkg", ".json"}:
            entry = _optional_file(root, "scene.pkg") or _optional_file(root, "scene.json")
        if not entry:
            pkgs = list(root.glob("*.pkg"))[:2]
            if len(pkgs) == 1:
                entry = _optional_file(root, pkgs[0].name)
        if prefer_live and entry:
            mode = "live_scene"
            reason = "使用离线 WebWallGL 实时渲染；不兼容的效果会明确报告失败，可回退预览。"
        else:
            mode, reason = _legacy_scene_mode(root, entry, preview, cancel)
    elif kind == "web":
        if prefer_live and entry and entry.suffix.lower() in {".html", ".htm"}:
            mode = "live_web"
            reason = "在离线脚本沙箱内运行网页壁纸；禁止外网、程序、弹窗和下载。"
        elif preview:
            mode = "static_preview"
            reason = "网页壁纸仅导入静态预览。"
        else:
            reason = "网页壁纸缺少本地 HTML 入口或静态预览。"
    else:
        reason = "此项目类型或入口媒体不受支持。"
    if mode == "unsupported" and not reason:
        reason = "项目没有可导入的图片或视频。"
    identity = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
    return WallpaperProject(identity, title, str(root), kind, str(entry) if entry else "",
                            str(preview) if preview else None, mode, reason)



def _legacy_scene_mode(root, entry, preview, cancel):
    reason = ""
    try:
        candidate = _scene_candidate(root, entry, cancel)
    except ImportCancelled:
        raise
    except (OSError, WallpaperEngineError, struct.error) as exc:
        candidate = None
        reason = f"场景媒体检测失败：{exc} "
    if candidate:
        return "video", "仅提取场景内嵌视频；不包含场景图层、粒子、脚本、交互或音频响应。"
    if preview:
        return "static_preview", reason + "此场景仅导入静态预览。"
    return "unsupported", reason + "场景没有可导入的内嵌视频或静态预览。"

def scan_projects(roots: Iterable[str | Path], cancel: Cancel = None,
                  progress: Callable[[str], None] | None = None) -> ScanResult:
    projects, warnings, seen = [], [], set()
    roots = tuple(Path(p).resolve() for p in roots)
    count = 0
    for root in roots:
        _check(cancel)
        if progress:
            progress(f"正在读取壁纸目录：{root.name}")
        if root.is_file() or (root / "project.json").is_file():
            candidates = [root]
        else:
            try:
                def child_directories(parent: Path):
                    with os.scandir(parent) as entries:
                        for index, item in enumerate(entries):
                            _check(cancel)
                            if index >= MAX_PROJECTS:
                                warnings.append(f"{parent.name} 条目已达扫描上限，请选择更具体的目录。")
                                return
                            if item.is_dir(follow_symlinks=False) and not item.is_symlink():
                                yield Path(item.path)
                candidates = list(child_directories(root))
                # A manually selected projects/ parent has one extra layout level.
                expanded = []
                for p in candidates:
                    if p.name.lower() in {"defaultprojects", "myprojects"} and not (p / "project.json").is_file():
                        expanded.extend(child_directories(p))
                    else:
                        expanded.append(p)
                candidates = expanded
            except OSError as exc:
                warnings.append(f"无法读取 {root.name}：{exc}")
                continue
        for candidate in candidates:
            _check(cancel)
            if candidate.resolve() in seen:
                continue
            seen.add(candidate.resolve())
            count += 1
            if count > MAX_PROJECTS:
                warnings.append("项目数量已达扫描上限，请选择更具体的目录。")
                break
            try:
                if candidate.is_dir() and not (candidate / "project.json").is_file():
                    continue
                projects.append(read_project(candidate, cancel=cancel))
            except ImportCancelled:
                raise
            except (OSError, WallpaperEngineError, RuntimeError) as exc:
                warnings.append(f"{candidate.name}：{exc}")
    return ScanResult(tuple(sorted(projects, key=lambda p: p.title.casefold())), tuple(warnings), tuple(map(str, roots)))


@dataclass(frozen=True)
class _PkgEntry:
    name: str
    offset: int
    length: int


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    result = stream.read(count)
    if len(result) != count:
        raise WallpaperEngineError("场景包已截断。")
    return result


def _pkg_index(stream: BinaryIO, cancel: Cancel = None) -> tuple[_PkgEntry, ...]:
    size = os.fstat(stream.fileno()).st_size
    if size > MAX_PKG_BYTES:
        raise WallpaperEngineError("场景包超过大小上限。")
    def string(maximum: int) -> str:
        length, = struct.unpack("<i", _read_exact(stream, 4))
        if not 0 < length <= maximum or stream.tell() + length > MAX_INDEX_BYTES:
            raise WallpaperEngineError("场景包字符串长度无效。")
        try:
            return _read_exact(stream, length).decode("utf-8")
        except UnicodeError as exc:
            raise WallpaperEngineError("场景包路径编码无效。") from exc
    if not re.fullmatch(r"PKGV\d{4}", string(32)):
        raise WallpaperEngineError("不支持的场景包格式。")
    count, = struct.unpack("<i", _read_exact(stream, 4))
    if not 0 <= count <= MAX_PKG_ENTRIES:
        raise WallpaperEngineError("场景包条目数量无效。")
    records, names = [], set()
    for _ in range(count):
        _check(cancel)
        name = _relative_name(string(1024))
        if name.casefold() in names:
            raise WallpaperEngineError("场景包存在重复资源路径。")
        names.add(name.casefold())
        offset, length = struct.unpack("<II", _read_exact(stream, 8))
        records.append((name, offset, length))
        if stream.tell() > MAX_INDEX_BYTES:
            raise WallpaperEngineError("场景包索引过大。")
    start = stream.tell()
    entries = []
    for name, offset, length in records:
        absolute = start + offset
        if absolute + length > size:
            raise WallpaperEngineError("场景包资源偏移越界。")
        entries.append(_PkgEntry(name, absolute, length))
    return tuple(entries)


def _lz4_block(data: bytes, size: int, cancel: Cancel) -> bytes:
    if not 0 < size <= MAX_BLOCK_BYTES:
        raise WallpaperEngineError("LZ4 解压块过大。")
    out = bytearray()
    pos = 0
    while pos < len(data):
        _check(cancel)
        token = data[pos]
        pos += 1
        def length_of(value: int) -> int:
            nonlocal pos
            if value == 15:
                while True:
                    if pos >= len(data):
                        raise WallpaperEngineError("LZ4 长度已截断。")
                    extra = data[pos]
                    pos += 1
                    value += extra
                    if extra != 255:
                        break
            return value
        literal = length_of(token >> 4)
        if pos + literal > len(data) or len(out) + literal > size:
            raise WallpaperEngineError("LZ4 字面量越界。")
        out.extend(data[pos:pos + literal])
        pos += literal
        if pos == len(data):
            break
        if pos + 2 > len(data):
            raise WallpaperEngineError("LZ4 匹配已截断。")
        offset = int.from_bytes(data[pos:pos + 2], "little")
        pos += 2
        length = length_of(token & 15) + 4
        if not offset or offset > len(out) or len(out) + length > size:
            raise WallpaperEngineError("LZ4 匹配越界。")
        # Repeat an overlapping match in chunks, avoiding a Python loop per byte.
        while length:
            _check(cancel)
            chunk = out[-offset:][:min(offset, length)]
            out.extend(chunk)
            length -= len(chunk)
    if len(out) != size:
        raise WallpaperEngineError("LZ4 解压长度不一致。")
    return bytes(out)


def _compressed_blocks(stream: BinaryIO, entry: _PkgEntry, cancel: Cancel = None) -> tuple[int, list[tuple[int, int, int]]] | None:
    if entry.length < 8:
        return None
    stream.seek(entry.offset)
    original, = struct.unpack("<Q", _read_exact(stream, 8))
    if original <= entry.length or original > 0x7fffffff:
        return None
    total, pos, end, blocks = 0, entry.offset + 8, entry.offset + entry.length, []
    while total < original:
        _check(cancel)
        if pos + 8 > end:
            return None
        stream.seek(pos)
        raw, packed = struct.unpack("<ii", _read_exact(stream, 8))
        if raw <= 0 or packed <= 0 or pos + 8 + packed > end:
            return None
        total += raw
        blocks.append((pos + 8, raw, packed))
        pos += 8 + packed
        if len(blocks) > 65536:
            raise WallpaperEngineError("场景包压缩块过多。")
    if total != original or pos != end:
        return None
    if original > MAX_ASSET_BYTES or any(raw > MAX_BLOCK_BYTES or packed > MAX_BLOCK_BYTES for _, raw, packed in blocks):
        raise WallpaperEngineError("场景包解压资源超过大小上限。")
    return original, blocks


def _entry_prefix(stream: BinaryIO, entry: _PkgEntry, cancel: Cancel) -> bytes:
    chain = _compressed_blocks(stream, entry, cancel)
    if not chain:
        stream.seek(entry.offset)
        return _read_exact(stream, min(entry.length, 204))
    prefix = bytearray()
    for offset, raw, packed in chain[1]:
        _check(cancel)
        stream.seek(offset)
        prefix.extend(_lz4_block(_read_exact(stream, packed), raw, cancel)[:204 - len(prefix)])
        if len(prefix) >= 204:
            break
    return bytes(prefix)


def _ftyp_offset(head: bytes) -> int | None:
    # TEXB0004 wraps MP4 near the entry head. Require a plausible ftyp box,
    # not a bare string match; the UI multimedia decoder validates playback.
    for index in range(4, min(200, len(head) - 7)):
        if head[index:index + 4] == b"ftyp":
            size = int.from_bytes(head[index - 4:index], "big")
            if 16 <= size <= 1024 * 1024 and all(32 <= b <= 126 for b in head[index + 4:index + 8]):
                return index - 4
    return None


def _video_score(name: str) -> tuple[int, str]:
    lower = name.casefold()
    score = 50 if any(s in lower for s in ("background", "wallpaper", "main", "day", "白天")) else 0
    if any(s in lower for s in ("mask", "noise", "flow", "lut", "overlay", "logo", "particle", "audio")):
        score -= 100
    return -score, lower


def _scene_candidate(root: Path, entry: Path | None, cancel: Cancel):
    oversized = False
    if entry and entry.suffix.lower() == ".pkg":
        with entry.open("rb") as stream:
            entries = _pkg_index(stream, cancel)
            for candidate in sorted(entries, key=lambda e: _video_score(e.name)):
                _check(cancel)
                if Path(candidate.name).suffix.lower() not in {".tex", ".mp4", ".m4v"}:
                    continue
                if candidate.length > MAX_ASSET_BYTES:
                    oversized = True
                    continue
                offset = _ftyp_offset(_entry_prefix(stream, candidate, cancel))
                if offset is not None:
                    return (entry, candidate, offset)
    elif entry:
        files = _list_assets(root, frozenset({".tex", ".mp4", ".m4v"}), root=root, cancel=cancel)
        for path in sorted(files, key=lambda p: _video_score(str(p))):
            _check(cancel)
            if path.stat().st_size > MAX_ASSET_BYTES:
                oversized = True
                continue
            with path.open("rb") as stream:
                offset = _ftyp_offset(stream.read(204))
            if offset is not None:
                return (path, None, offset)
    if oversized:
        raise WallpaperEngineError("场景中的候选视频资源超过导入大小上限。")
    return None


def _image_dimensions(head: bytes) -> tuple[int, int] | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
        return struct.unpack(">II", head[16:24])
    if head[:6] in {b"GIF87a", b"GIF89a"} and len(head) >= 10:
        return struct.unpack("<HH", head[6:10])
    if head.startswith(b"BM") and len(head) >= 26:
        return tuple(abs(v) for v in struct.unpack("<ii", head[18:26]))
    if head.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 4 <= len(head):
            if head[pos] != 255:
                break
            marker = head[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            length = int.from_bytes(head[pos:pos + 2], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and pos + 7 <= len(head):
                height, width = struct.unpack(">HH", head[pos + 3:pos + 7])
                return width, height
            if length < 2:
                break
            pos += length
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        if head[12:16] == b"VP8X" and len(head) >= 30:
            return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
        if head[12:16] == b"VP8L" and len(head) >= 25 and head[20] == 0x2f:
            bits = int.from_bytes(head[21:25], "little")
            return (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
        if head[12:16] == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", head[26:30])
            return w & 0x3fff, h & 0x3fff
    # AVIF's ispe box supplies image spatial extents without decoding pixels.
    if head[4:8] == b"ftyp" and b"avif" in head[8:32]:
        start = head.find(b"ispe")
        if start >= 0 and start + 16 <= len(head):
            return struct.unpack(">II", head[start + 8:start + 16])
    return None


def _validate_asset(path: Path, kind: str) -> None:
    size = path.stat().st_size
    ceiling = MAX_IMAGE_BYTES if kind == "image" else MAX_ASSET_BYTES
    if not 0 < size <= ceiling:
        raise WallpaperEngineError("媒体为空或超过导入大小上限。")
    with path.open("rb") as stream:
        head = stream.read(min(size, MAX_METADATA_BYTES))
    if kind == "image":
        dimensions = _image_dimensions(head)
        if not dimensions:
            raise WallpaperEngineError("无法验证图片格式或尺寸，请使用标准 PNG/JPEG/WebP/GIF/BMP/AVIF。")
        width, height = dimensions
        if not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
            raise WallpaperEngineError("图片尺寸超过安全上限。")
    elif not (_ftyp_offset(head[:204]) == 0 or head.startswith(b"\x1aE\xdf\xa3") or (head.startswith(b"RIFF") and head[8:12] == b"AVI ") or (head[4:8] in {b"wide", b"moov", b"mdat"})):
        raise WallpaperEngineError("视频容器格式无效。")


def _copy_stream(source: BinaryIO, target: BinaryIO, length: int, cancel: Cancel) -> None:
    if not 0 <= length <= MAX_ASSET_BYTES:
        raise WallpaperEngineError("媒体超过导入大小上限。")
    while length:
        _check(cancel)
        chunk = _read_exact(source, min(1024 * 1024, length))
        target.write(chunk)
        length -= len(chunk)


def _extract(candidate, target: Path, cancel: Cancel) -> None:
    path, entry, skip = candidate
    with path.open("rb") as source, target.open("xb") as dest:
        if entry is None:
            source.seek(skip)
            _copy_stream(source, dest, os.fstat(source.fileno()).st_size - skip, cancel)
        else:
            # Re-parse the same handle so stale offsets cannot address a replaced file.
            entries = _pkg_index(source, cancel)
            current = next((e for e in entries if e.name == entry.name), None)
            if current != entry:
                raise WallpaperEngineError("场景包在导入期间发生变化，请重试。")
            chain = _compressed_blocks(source, entry, cancel)
            if chain:
                remaining_skip = skip
                for offset, raw, packed in chain[1]:
                    _check(cancel)
                    source.seek(offset)
                    block = _lz4_block(_read_exact(source, packed), raw, cancel)
                    dest.write(block[remaining_skip:])
                    remaining_skip = max(0, remaining_skip - len(block))
            else:
                source.seek(entry.offset + skip)
                _copy_stream(source, dest, entry.length - skip, cancel)


# Browser resources only; native executables, shell scripts, links and arbitrary
# unknown files are never copied into a live project or exposed over HTTP.
LIVE_EXTENSIONS = frozenset({".json", ".pkg", ".tex", ".html", ".htm", ".js", ".mjs", ".css",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".svg", ".ico",
    ".mp4", ".webm", ".m4v", ".mov", ".ogg", ".mp3", ".wav", ".flac", ".aac",
    ".woff", ".woff2", ".ttf", ".otf", ".glsl", ".vert", ".frag", ".shader",
    ".vsh", ".fsh", ".vfx", ".mdl", ".obj", ".mtl", ".fbx", ".gltf", ".glb",
    ".bin", ".txt", ".csv", ".xml", ".wasm"})
MAX_LIVE_FILES = 10_000
MAX_LIVE_BYTES = 2 * 1024 * 1024 * 1024


def _live_assets(root: Path, cancel: Cancel):
    files, total, visited = [], 0, 0
    for folder, directories, names in os.walk(root, followlinks=False):
        _check(cancel)
        parent = Path(folder)
        visited += len(directories) + len(names)
        if visited > 20_000:
            raise WallpaperEngineError("实时项目目录条目超过扫描上限。")
        if len(parent.relative_to(root).parts) > 16:
            raise WallpaperEngineError("实时项目目录层级超过上限。")
        for name in list(directories):
            path = parent / name
            if path.is_symlink() or path.is_junction() or name.startswith("."):
                directories.remove(name)
        for name in names:
            _check(cancel)
            path = parent / name
            if (path.is_symlink() or path.suffix.lower() not in LIVE_EXTENSIONS
                    or name.startswith(".") or (parent == root and name.casefold() == "import.json")):
                continue
            rel = path.relative_to(root).as_posix()
            safe_project_path(root, rel)
            size = path.stat().st_size
            if size > (MAX_PKG_BYTES if path.suffix.lower() == ".pkg" else MAX_ASSET_BYTES):
                raise WallpaperEngineError("实时项目单个资源超过上限。")
            total += size
            files.append((rel, path, size))
            if len(files) > MAX_LIVE_FILES or total > MAX_LIVE_BYTES:
                raise WallpaperEngineError("实时项目超过 10000 个文件或 2 GiB 大小上限。")
    return sorted(files, key=lambda item: item[0])


def _live_digest(root: Path, records: list[dict], kind: str, cancel: Cancel):
    digest = hashlib.sha256(kind.encode() + b"\0")
    for record in records:
        name = _relative_name(record["path"])
        path = safe_project_path(root, name)
        h = hashlib.sha256()
        with path.open("rb") as src:
            size = 0
            while chunk := src.read(1024 * 1024):
                _check(cancel)
                size += len(chunk)
                if size > record["size"]:
                    raise WallpaperEngineError("已有导入缓存校验失败。")
                h.update(chunk)
        if size != record["size"] or h.hexdigest() != record["sha256"]:
            raise WallpaperEngineError("已有导入缓存校验失败。")
        digest.update(name.encode() + b"\0" + str(size).encode() + b"\0" + h.digest())
    return digest.hexdigest()


def _import_live(current, managed, cancel, progress):
    root = Path(current.project_dir).resolve()
    files = _live_assets(root, cancel)
    names = {name for name, _, _ in files}
    entry = Path(current.entry).relative_to(root).as_posix()
    if "project.json" not in names or entry not in names:
        raise WallpaperEngineError("实时项目入口不在允许的本地资源范围内。")
    managed.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".we-import-", dir=managed))
    try:
        records = []
        for index, (name, source, size) in enumerate(files):
            _check(cancel)
            if progress:
                progress(f"正在复制实时项目资源 {index + 1}/{len(files)}")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            copied = 0
            with source.open("rb") as src, target.open("xb") as dest:
                while chunk := src.read(1024 * 1024):
                    _check(cancel)
                    copied += len(chunk)
                    if copied > size:
                        raise WallpaperEngineError("项目在导入期间发生变化，请重试。")
                    dest.write(chunk)
                    h.update(chunk)
            if copied != size:
                raise WallpaperEngineError("项目在导入期间发生变化，请重试。")
            records.append({"path": name, "size": size, "sha256": h.hexdigest()})
        kind = current.import_mode
        identity = _live_digest(stage, records, kind, cancel)
        preview = Path(current.preview).relative_to(root).as_posix() if current.preview else None
        metadata = {"version": 2, "id": identity, "title": current.title, "kind": kind,
            "source_type": current.type, "mode": kind, "message": current.reason,
            "asset": "project.json", "entry": entry, "preview": preview, "files": records}
        (stage / "import.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        final = managed / identity
        duplicate = False
        _check(cancel)
        try:
            stage.rename(final)
        except OSError:
            if not final.is_dir() or final.is_symlink():
                raise
            old = _json(final / "import.json")
            if old.get("files") != records or _live_digest(final, records, kind, cancel) != identity:
                raise WallpaperEngineError("已有导入缓存校验失败。")
            duplicate = True
        return ImportResult(identity, current.title, str(final / "project.json"), kind,
            current.type, kind, current.reason, duplicate, str(final / preview) if preview else None)
    except OSError as exc:
        raise WallpaperEngineError(f"实时项目导入失败：{exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def import_project(project: WallpaperProject | str | Path, asset_dir: str | Path,
                   cancel: Cancel = None, progress: Callable[[str], None] | None = None,
                   *, prefer_live: bool = True) -> ImportResult:
    """Copy one required image/video into a content-addressed managed directory.

    Scene/Web preview fallback and embedded video omissions are explicit in
    ``mode``/``message``. Applications are never copied or executed. Failed or
    cancelled imports remove their staging directory and publish no partial item.
    """
    _check(cancel)
    if isinstance(project, WallpaperProject):
        # Re-read untrusted source instead of trusting editable/stale dataclass fields.
        source = Path(project.project_dir)
        if project.type in {"image", "video"} and not (source / "project.json").is_file():
            source = Path(project.entry)
        current = read_project(source, cancel=cancel, prefer_live=prefer_live)
    else:
        current = read_project(project, cancel=cancel, prefer_live=prefer_live)
    if current.import_mode == "unsupported":
        raise WallpaperEngineError(current.reason)
    managed = Path(asset_dir).resolve()
    root = Path(current.project_dir).resolve()
    if managed == root or managed.is_relative_to(root):
        raise WallpaperEngineError("导入目录不能放在原始 Wallpaper Engine 项目中。")
    if current.import_mode in {"live_scene", "live_web"}:
        return _import_live(current, managed, cancel, progress)
    managed.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".we-import-", dir=managed))
    try:
        mode, kind, message = current.import_mode, current.import_mode, current.reason
        candidate = None
        if current.type == "scene":
            try:
                candidate = _scene_candidate(root, Path(current.entry) if current.entry else None, cancel)
            except ImportCancelled:
                raise
            except (OSError, WallpaperEngineError):
                if not current.preview:
                    raise
            if candidate:
                kind, mode = "video", "video"
                message = "已提取场景内嵌视频；不包含场景图层、粒子、脚本、交互或音频响应。"
            elif current.preview:
                kind, mode = "image", "static_preview"
                message = "仅导入静态预览；不支持 Wallpaper Engine 实时场景效果。"
            else:
                raise WallpaperEngineError("场景没有可导入的媒体。")
        elif mode == "static_preview":
            kind = "image"
        if candidate:
            output = stage / "wallpaper.mp4"
            _extract(candidate, output, cancel)
        else:
            filename = current.preview if mode == "static_preview" else current.entry
            if not filename:
                raise WallpaperEngineError("项目没有可导入的媒体。")
            source = Path(filename).resolve()
            if not source.is_relative_to(root) or source == root:
                raise WallpaperEngineError("媒体路径越过项目目录。")
            output = stage / ("wallpaper" + source.suffix.lower())
            _validate_asset(source, kind)
            with source.open("rb") as src, output.open("xb") as dest:
                _copy_stream(src, dest, os.fstat(src.fileno()).st_size, cancel)
        _validate_asset(output, kind)
        digest = hashlib.sha256()
        digest.update(kind.encode() + b"\0")
        with output.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                _check(cancel)
                digest.update(chunk)
        identity = digest.hexdigest()
        final = managed / identity
        metadata = {"version": 1, "id": identity, "title": current.title, "kind": kind,
                    "source_type": current.type, "mode": mode, "message": message, "asset": output.name}
        (stage / "import.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        _check(cancel)
        duplicate = False
        try:
            stage.rename(final)
        except OSError:
            # Same-content imports racing each other converge on the first item.
            if not final.is_dir() or final.is_symlink():
                raise
            existing = _json(final / "import.json")
            existing_name = existing.get("asset")
            if not isinstance(existing_name, str):
                raise WallpaperEngineError("已有导入缓存不完整。")
            existing_asset = safe_project_path(final, existing_name)
            check = hashlib.sha256(kind.encode() + b"\0")
            with existing_asset.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    _check(cancel)
                    check.update(chunk)
            if check.hexdigest() != identity:
                raise WallpaperEngineError("已有导入缓存校验失败。")
            output = stage / existing_name
            duplicate = True
        result_path = str(final / output.name)
        return ImportResult(identity, current.title, result_path, kind, current.type, mode,
                            message or "已复制到 Monitor 壁纸库。", duplicate,
                            result_path if kind == "image" else None)
    except (OSError, struct.error) as exc:
        raise WallpaperEngineError(f"壁纸导入失败：{exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def scan_wallpapers(selected_path: str | Path | None = None, *, discover: bool = True,
                    cancel: Cancel = None, progress: Callable[[str], None] | None = None) -> ScanResult:
    return scan_projects(discover_roots(selected_path, discover=discover), cancel, progress)


import_wallpaper = import_project
