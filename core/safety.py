"""Shared lexical path, marker and reparse rules (Python 3.9+)."""
import csv
import hashlib
import io
import os
from pathlib import Path
import stat

FILE_ATTRIBUTE_SYSTEM = 0x4
BLOCKING_KINDS = {'SECRET', 'FOREIGN', 'WORKSPACE', 'NO_TOUCH', 'ASK'}
KINDS = BLOCKING_KINDS | {'NOTE'}
# 命中即拒绝移动父目录的名字。
#
# 判断标准：移动父目录会不会真的破坏它？
#   venv / node_modules  含绝对路径依赖，移了要重建 → 保护
#   .git                 含工作树索引，移了可能损坏 → 保护
#   program files 等     系统目录，不该动 → 保护
#
# __pycache__ 不在此列：它是 Python 的编译缓存，自动生成、可随时删，
# 移动父目录不影响正确性（最多重新编译一次）。把它当保护项会让几乎所有
# 含 Python 代码的目录都无法整理。
PROTECTED_NAMES = {
    'steamlibrary', 'steamapps', 'games', 'my games', 'epic games', 'battle.net',
    'shaderpacks', 'appdata', 'program files', 'program files (x86)', 'programdata',
    'windows', 'system32', 'syswow64', 'recovery', 'perflogs', 'msocache', 'windows.old',
    'config.msi', 'onedrivetemp', '$windows.~bt', '$windows.~ws', '$winreagent',
    'documents and settings',
    'system volume information', '$recycle.bin', '.git', 'node_modules', '.venv',
    'site-packages', '.ssh', '.gnupg', '.aws', '.kube', '_organize',
}


# ═══════════════════════════════════════════════════════════
# 嵌套白名单：保护区内可以动的点
#
# 场景：D:\Games\某游戏\ 整体不能动，
#       但 D:\Games\某游戏\Screenshots\ 属于"在受保护区里、但内容该整理"。
#       而系统级强制禁区（NESTED_DENY，如 Windows / Program Files / AppData / .ssh）
#       整棵子树永久锁死，绝不允许被 temp / cache / downloads 等反向放行。
#
# 判定顺序：
#   1. 命中 NESTED_DENY      → 立即拒绝（强制保护，不可被更深层放行）
#   2. 命中 PROTECTED_NAMES  → 默认保护
#   3. 命中 NESTED_ALLOW     → 放行（仅覆盖普通 PROTECTED_NAMES）
# ═══════════════════════════════════════════════════════════

# 这些名字出现在普通受保护区内时，其中内容仍可整理
NESTED_ALLOW = {
    "downloads", "download", "桌面", "desktop", "documents", "文档",
    "temp", "tmp", "缓存", "cache", "临时", "捕获", "captures",
    "screenshots", "screenshot", "截图", "录屏", "recordings",
    "output", "outputs", "导出", "export",
    "_待删除", "_重复待确认", "_未归类",
}

# 强制保护区：路径任意一层命中即整棵子树禁止整理（不可被 NESTED_ALLOW 反向放行）
NESTED_DENY = {
    "node_modules", ".git", "site-packages", "venv", ".venv",
    "steamapps", "appdata", "windows", "system32", "syswow64",
    "program files", "program files (x86)", "programdata",
    "recovery", "perflogs", "msocache", "windows.old",
    "config.msi", "onedrivetemp", "$windows.~bt", "$windows.~ws", "$winreagent",
    "documents and settings",
    ".ssh", ".gnupg", ".aws", ".kube",
    "$recycle.bin", "system volume information",
}

# 仅在系统盘根第一层拦截的系统/引导/厂商驱动目录（避免误伤 D:\软件\Drivers 等用户目录）
SYSTEM_DRIVE_TOP_DENY = {
    'boot', 'efi', 'intel', 'amd', 'nvidia', 'drivers',
    'swsetup', 'esupport', 'dell', 'hp', 'lenovo',
    'inetpub', 'system.sav', 'system repair', 'xboxgames',
}

# Windows 系统盘关键系统文件 / 注册表配置单元（部分仅设 Hidden 未设 System，如 NTUSER.DAT）
SYSTEM_PROTECTED_FILES = {
    'pagefile.sys', 'hiberfil.sys', 'swapfile.sys',
    'bootmgr', 'bootnxt', 'bootsect.bak', 'bootstat.dat',
    'dumpstack.log', 'dumpstack.log.tmp',
    'ntuser.dat', 'ntuser.ini', 'usrclass.dat', 'desktop.ini',
}

# 随普通用户文件夹一同移动时可放行的资源管理器元数据文件（作为独立目标时仍拦截）
BENIGN_SYSTEM_DESCENDANTS = {'desktop.ini', 'thumbs.db'}

# 用户主目录下的 Windows 已知 Shell 文件夹：允许扫描进入或作为移动目标容器，但禁止移动/改名其本身
USER_SHELL_FOLDERS = {
    'desktop', 'documents', 'downloads', 'pictures', 'music', 'videos',
    'favorites', 'links', 'contacts', 'saved games', 'searches',
    'onedrive', '3d objects',
}

# 不应被当作"专用工具链路径"的通用系统/会话环境变量
_ENV_IGNORED_VARS = {
    'PWD', 'OLDPWD', 'INIT_CWD', 'VSCODE_CWD', 'TMP', 'TEMP', 'TMPDIR',
    'USERPROFILE', 'HOME', 'HOMEPATH', 'HOMEDRIVE', 'LOCALAPPDATA', 'APPDATA',
    'SYSTEMDRIVE', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PUBLIC', 'ALLUSERSPROFILE',
    'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
    'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432',
    'DRIVERDATA', 'ONEDRIVE', 'ONEDRIVECOMMERCIAL', 'ONEDRIVECONSUMER',
    'PSMODULEPATH', 'PATHEXT', 'OS', 'COMPUTERNAME', 'USERNAME',
}

_ENV_SECRET_TOKENS = ('KEY', 'TOKEN', 'SECRET', 'PASS', 'AUTH', 'COOKIE')
_ENV_MULTI_PATH_VARS = {
    'PATH', 'PYTHONPATH', 'NODE_PATH', 'CLASSPATH', 'CMAKE_PREFIX_PATH',
    'PKG_CONFIG_PATH', 'LD_LIBRARY_PATH', 'DYLD_LIBRARY_PATH', 'INCLUDE', 'LIB',
}


_ENV_REFS_CACHE = (None, ())


def env_referenced_paths():
    """Return existing absolute paths referenced by PATH and toolchain env vars (cached per env snapshot)."""
    global _ENV_REFS_CACHE
    env_sig = tuple(sorted(os.environ.items()))
    if _ENV_REFS_CACHE[0] == env_sig:
        return _ENV_REFS_CACHE[1]
    refs = []
    seen = set()
    for var, raw in env_sig:
        if not raw or not isinstance(raw, str):
            continue
        upper = var.upper()
        if upper in _ENV_IGNORED_VARS or upper.startswith(('AGY_', 'WT_', 'VSCODE_')):
            continue
        if any(tok in upper for tok in _ENV_SECRET_TOKENS):
            continue
        items = raw.split(os.pathsep) if upper in _ENV_MULTI_PATH_VARS else [raw]
        for item in items:
            candidate = item.strip().strip('"')
            if not candidate or not os.path.isabs(candidate):
                continue
            if os.name == 'nt' and candidate.startswith(('\\\\?\\', '\\\\.\\')):
                continue
            try:
                if not os.path.lexists(candidate):
                    continue
                norm = normal(candidate)
            except (OSError, ValueError):
                continue
            p = Path(norm)
            # Never treat a bare drive root (e.g. C:\) as an env-ref entry
            if p == Path(p.anchor):
                continue
            k = key(norm)
            if k not in seen:
                seen.add(k)
                refs.append((var, norm))
    result = tuple(refs)
    _ENV_REFS_CACHE = (env_sig, result)
    return result


def env_dependency_reason(path, *, allow_ancestor=False):
    """Return a human-readable block reason if path is tied to PATH/env vars, else None.

    When allow_ancestor=False (moving/renaming/deleting path itself):
      blocks if path is equal to, inside, OR an ancestor of any env-referenced path.
    When allow_ancestor=True (browsing a container directory):
      blocks only if path is equal to or inside an env-referenced path.
    """
    norm = normal(path)
    for var, ref in env_referenced_paths():
        if within(norm, ref):
            return f'路径位于环境变量 {var} 引用范围内，移动或改名会破坏依赖'
        if not allow_ancestor and within(ref, norm, inclusive=False):
            return f'目录包含环境变量 {var} 引用的路径，移动或改名会破坏依赖'
    return None


def toolchain_marker_reason(path):
    """Detect SDK/runtime/virtualenv root markers directly inside a directory (shallow O(1) check)."""
    p = Path(normal(path))
    if not os.path.isdir(p):
        return None
    checks = (
        (p / 'pyvenv.cfg', 'Python 虚拟环境根目录'),
        (p / 'conda-meta', 'Conda/Miniforge 运行环境根目录'),
        (p / '.vcpkg-root', 'vcpkg 工具链根目录'),
        (p / 'msys2.exe', 'MSYS2 环境根目录'),
        (p / 'Cygwin.bat', 'Cygwin 环境根目录'),
    )
    for marker, label in checks:
        if os.path.lexists(marker):
            return f'检测到 {label}，移动或改名会破坏环境路径依赖'
    if os.path.lexists(p / 'MaintenanceTool.exe') and (
            os.path.isdir(p / 'Tools') or os.path.isdir(p / 'Docs')):
        return '检测到 Qt SDK 安装根目录，移动或改名会破坏工具链依赖'
    if os.path.lexists(p / 'bin' / 'flutter') or os.path.lexists(p / 'bin' / 'flutter.bat'):
        return '检测到 Flutter SDK 根目录，移动或改名会破坏工具链依赖'
    if (os.path.lexists(p / 'bin' / 'go') or os.path.lexists(p / 'bin' / 'go.exe')) and (
            os.path.isdir(p / 'src') and os.path.isdir(p / 'pkg')):
        return '检测到 Go 工具链根目录，移动或改名会破坏工具链依赖'
    if (os.path.lexists(p / 'bin' / 'javac') or os.path.lexists(p / 'bin' / 'javac.exe')) and os.path.isdir(p / 'lib'):
        return '检测到 JDK 根目录，移动或改名会破坏工具链依赖'
    if (os.path.lexists(p / 'bin' / 'gcc.exe') or os.path.lexists(p / 'bin' / 'clang.exe')) and (
            os.path.isdir(p / 'include') and os.path.isdir(p / 'lib')):
        return '检测到编译器工具链根目录，移动或改名会破坏工具链依赖'
    return None


def _system_drive():
    if os.name != 'nt':
        return ''
    sys_drive = os.environ.get('SystemDrive', '').rstrip('\\/').casefold()
    if not sys_drive:
        sys_root = os.environ.get('SystemRoot', '')
        sys_drive = os.path.splitdrive(sys_root)[0].casefold()
    return sys_drive


def _system_drive_parts(path):
    """If path is on the Windows system drive, return its casefolded relative parts; else None."""
    sys_drive = _system_drive()
    if not sys_drive:
        return None
    norm = normal(path)
    drive = os.path.splitdrive(norm)[0].casefold()
    if drive != sys_drive:
        return None
    p = Path(norm)
    return [part.casefold() for part in p.parts[1:]]


def is_system_root(path):
    """Return True if path is the Windows system drive root (e.g. C:\\) or its Users root."""
    parts = _system_drive_parts(path)
    if parts is None:
        return False
    return len(parts) == 0 or parts == ['users']


def is_user_profile_root(path):
    """Return True if path is C:\\Users\\<username> on the Windows system drive."""
    parts = _system_drive_parts(path)
    return parts is not None and len(parts) == 2 and parts[0] == 'users'


def is_user_shell_folder(path):
    """Return True if path is a standard Windows KnownFolder directly under C:\\Users\\<user>\\."""
    parts = _system_drive_parts(path)
    return (parts is not None and len(parts) == 3 and parts[0] == 'users'
            and parts[2] in USER_SHELL_FOLDERS)


def is_skeleton_path(path):
    """Return True for Windows system/profile skeleton directories that must never be moved themselves."""
    return is_system_root(path) or is_user_profile_root(path) or is_user_shell_folder(path)


def is_system_attr(path):
    """Return True if path exists and has Windows FILE_ATTRIBUTE_SYSTEM set."""
    if os.name != 'nt':
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_SYSTEM)


def is_system_protected_file(path):
    """Return True if path matches a critical Windows system/registry file name."""
    if os.name != 'nt':
        return False
    name = os.path.basename(os.fspath(path).rstrip('\\/')).casefold()
    return name in SYSTEM_PROTECTED_FILES or name.startswith(('ntuser.dat', 'usrclass.dat'))


def state_home():
    """Return the per-user writable state directory."""
    local = os.environ.get('LOCALAPPDATA')
    if not local:
        local = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(normal(local))


def organize_dir(root):
    """Return the metadata/state directory for root.

    Normal roots store state in <root>/_organize.
    Windows system roots (C:\\, C:\\Users) store state under the user's writable
    state_home so organizing C: neither pollutes C:\\ nor requires admin rights.
    """
    norm = normal(root)
    if is_system_root(norm):
        token = hashlib.sha256(key(norm).encode('utf-8')).hexdigest()[:24]
        return state_home() / 'tidy-file-tree-system-state' / token
    return Path(norm) / '_organize'


def nested_verdict(path):
    """判断路径的嵌套保护状态。

    返回 (允许整理, 依据)
      允许=True  可以整理
      允许=False 不动
    """
    parts = []
    try:
        import os as _os
        p = _os.path.abspath(_os.fspath(path))
        drive = _os.path.splitdrive(p)[0].casefold()
        while True:
            head, tail = _os.path.split(p)
            if not tail:
                break
            parts.append(tail.casefold())
            if head == p:
                break
            p = head
    except (TypeError, ValueError):
        return (False, "路径无法解析")

    parts.reverse()   # 从根到叶

    # 系统盘专属强制保护：根级驱动/引导目录、根级隐藏项、用户主目录 dot 配置、关键系统文件
    sys_drive = _system_drive()
    if sys_drive and drive == sys_drive and parts:
        if parts[0] in SYSTEM_DRIVE_TOP_DENY:
            return (False, f"'{parts[0]}' 属于系统盘根级受保护目录，整棵子树禁止整理")
        if len(parts) == 1 and parts[0].startswith('.'):
            return (False, f"'{parts[0]}' 属于系统盘根级隐藏配置项，禁止整理")
        if len(parts) >= 3 and parts[0] == 'users' and parts[2].startswith('.'):
            return (False, f"'{parts[2]}' 属于用户主目录隐藏配置，整棵子树禁止整理")
        leaf = parts[-1]
        if leaf in SYSTEM_PROTECTED_FILES or leaf.startswith(('ntuser.dat', 'usrclass.dat')):
            return (False, f"'{leaf}' 属于 Windows 系统关键文件，禁止整理")

    # 从浅到深扫描：NESTED_DENY 命中即永久保护；普通 PROTECTED_NAMES 可被更深的 NESTED_ALLOW 放行
    allow_at = -1
    deny_at = -1
    allow_name = deny_name = ""

    for i, seg in enumerate(parts):
        if seg in NESTED_DENY:
            return (False, f"'{seg}' 属于强制保护目录，整棵子树禁止整理")
        elif seg in NESTED_ALLOW:
            allow_at, allow_name = i, seg
        elif seg in PROTECTED_NAMES:
            deny_at, deny_name = i, seg

    if deny_at > allow_at:
        return (False, f"'{deny_name}' 在更深处，优先保护")
    if allow_at >= 0:
        return (True, f"'{allow_name}' 是允许整理的区域")
    return (True, "无保护标记")


class SafetyError(ValueError):
    pass


def normal(path):
    """Canonical separators/case are compared with key(); never resolve a link."""
    value = os.path.normpath(os.path.abspath(os.fspath(path)))
    if os.name == 'nt':
        if value.startswith(('\\\\?\\', '\\\\.\\')):
            raise SafetyError('不支持设备路径或扩展路径别名')
        drive, tail = os.path.splitdrive(value)
        if ':' in tail:
            raise SafetyError('不支持备用数据流路径')
        # Win32 treats trailing dots/spaces as aliases for the ordinary name.
        tail = os.sep.join(part.rstrip(' .') for part in tail.split(os.sep))
        value = drive + tail
        # Expand NTFS 8.3 aliases without following a junction as realpath would.
        # For new destinations expand their nearest existing ancestor.
        current, missing = value, []
        while not os.path.lexists(current):
            parent, leaf = os.path.split(current)
            if parent == current or not leaf:
                break
            missing.append(leaf)
            current = parent
        if os.path.lexists(current):
            import ctypes
            function = ctypes.windll.kernel32.GetLongPathNameW
            function.argtypes = (ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32)
            function.restype = ctypes.c_uint32
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(current,buffer,len(buffer))
            if not length or length >= len(buffer):
                raise SafetyError('无法规范化 Windows 路径别名')
            value = os.path.join(buffer.value,*reversed(missing)) if missing else buffer.value
    return value


def key(path):
    return os.path.normcase(normal(path))


def within(path, parent, inclusive=True):
    p, root = key(path), key(parent)
    try:
        return os.path.commonpath([p, root]) == root and (inclusive or p != root)
    except ValueError:
        return False


def is_reparse(path):
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def assert_no_links(path):
    """Check all existing ancestors too, including a junction used as Root."""
    current = Path(normal(path))
    while True:
        if os.path.lexists(current) and is_reparse(current):
            raise SafetyError('链接或重解析路径已拒绝')
        if current.parent == current:
            break
        current = current.parent


def read_tsv(path, fields):
    if not os.path.exists(path):
        return []
    assert_no_links(path)
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter='\t')
        if reader.fieldnames != list(fields):
            raise SafetyError('TSV 表头不符合约定: ' + str(path))
        rows = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise SafetyError('TSV 行列数不符合约定: ' + str(path))
            rows.append(row)
        return rows


MARKER_FIELDS = ('path', 'kind', 'by', 'note', 'ts')


def load_markers(root):
    rows = read_tsv(organize_dir(root) / 'markers.tsv', MARKER_FIELDS)
    for row in rows:
        if row['kind'] not in KINDS or not os.path.isabs(row['path']):
            raise SafetyError('保护标记格式错误，停止操作')
        row['path'] = normal(row['path'])
    return rows


def marker_hits(path, markers, descendants=False):
    return [m for m in markers if within(path, m['path']) or
            (descendants and within(m['path'], path))]


def block_kind(path, markers, descendants=False):
    kinds = {m['kind'] for m in marker_hits(path, markers, descendants)}
    # A NOTE may add information but can never revoke protection.
    for kind in ('SECRET', 'FOREIGN', 'WORKSPACE', 'NO_TOUCH', 'ASK'):
        if kind in kinds:
            return kind
    return None


def encode_tsv(rows, fields):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fields, delimiter='\t', lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode('utf-8')


def replace_path(value, old, new):
    if not isinstance(value, str) or not os.path.isabs(value) or not within(value, old):
        return value
    rel = os.path.relpath(normal(value), normal(old))
    return normal(new) if rel == '.' else os.path.join(normal(new), rel)
