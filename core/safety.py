"""Shared lexical path, marker and reparse rules (Python 3.9+)."""
import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
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

GAME_AREAS = {'games', 'my games', 'steamlibrary', 'steamapps', 'epic games', 'battle.net'}
SAVE_AREAS = {'save', 'saves', 'saved games', 'savegames', 'savedgames', 'savedata',
              'save data', 'save_data', '存档', '游戏存档'}


@dataclass(frozen=True)
class NestedProtection:
    parent: tuple
    exceptions: tuple


def compile_nested_protection(root, policy):
    records = policy.get('nested_protection', [])
    if not isinstance(records, list):
        raise SafetyError('nested_protection 必须是规则数组')
    if not records:
        return []
    root_parts = Path(key(root)).parts

    def pattern(value, *, allow_wildcard):
        if not isinstance(value, str) or not value.strip():
            raise SafetyError('嵌套保护路径必须是非空字符串')
        raw = Path(value)
        if raw.anchor and not raw.is_absolute():
            raise SafetyError('请使用绝对路径或相对整理根的路径')
        raw_parts = re.split(r'[\\/]' if os.name == 'nt' else '/', value)
        if os.name == 'nt' and any(':' in part for part in raw_parts[1:]):
            raise SafetyError('嵌套保护路径含无效的 Windows 路径段')
        if any(part and (part in ('.', '..') or part.rstrip(' .') != part
                        or re.search(r'[?\[\]<>|"\x00-\x1f]', part)
                        or ('*' in part and (part != '*' or not allow_wildcard))) for part in raw_parts):
            raise SafetyError('嵌套保护只支持普通路径和整段的单层 *')
        parts = (Path(root) / raw).parts
        first_wildcard = parts.index('*') if '*' in parts else len(parts)
        prefix = Path(key(Path(*parts[:first_wildcard]))).parts
        return prefix + tuple(os.path.normcase(part) for part in parts[first_wildcard:])

    compiled = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('exceptions', []), list):
            raise SafetyError('嵌套保护规则需要 no_touch 路径和 exceptions 数组')
        parent = pattern(record.get('no_touch'), allow_wildcard=False)
        if len(parent) <= len(root_parts) or parent[:len(root_parts)] != root_parts:
            raise SafetyError('嵌套保护的母目录必须在整理根内')
        exceptions = []
        for value in record.get('exceptions', []):
            allowed = pattern(value, allow_wildcard=True)
            if len(allowed) <= len(parent) or allowed[:len(parent)] != parent:
                raise SafetyError('例外必须是母目录内的子路径，母目录本身不可放行')
            exceptions.append(allowed)
        compiled.append(NestedProtection(parent, tuple(exceptions)))
    return compiled


def active_game_save(parts):
    game_context = False
    for part in parts:
        name = part.casefold()
        if name in GAME_AREAS:
            game_context = True
        if game_context and name in SAVE_AREAS:
            return True
    return False


def nested_policy_verdict(path, rules, *, descendants=False, allow_container=False):
    """Return whether configured scope admits this path and the waived soft prefix length."""
    if not rules:
        return True, 0
    parts = Path(key(path)).parts
    through = 0
    for rule in rules:
        parent = rule.parent
        if len(parts) < len(parent) and parent[:len(parts)] == parts:
            if descendants:
                return False, 0
            if allow_container and rule.exceptions and os.path.isdir(path):
                through = max(through, len(parts) - 1)
            continue
        if parts[:len(parent)] != parent:
            continue
        admitted = 0
        if not any(part.casefold() in SAVE_AREAS for part in parts[len(parent)-1:]):
            for exception in rule.exceptions:
                if not all(expected == '*' or expected == actual for expected, actual in zip(exception, parts)):
                    continue
                if len(parts) >= len(exception):
                    admitted = max(admitted, len(exception) - 1)
                elif allow_container and os.path.isdir(path):
                    admitted = max(admitted, len(parts) - 1)
        if not admitted:
            return False, 0
        through = max(through, admitted)
    return True, through

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
                refs.append((var, norm, k))
    result = tuple(refs)
    _ENV_REFS_CACHE = (env_sig, result)
    return result


def _key_within(p_key, root_key, inclusive=True):
    try:
        return os.path.commonpath([p_key, root_key]) == root_key and (inclusive or p_key != root_key)
    except ValueError:
        return False


def env_dependency_reason(path, *, root=None, allow_ancestor=False):
    """Return a human-readable block reason if path is tied to PATH/env vars, else None.

    When allow_ancestor=False (moving/renaming/deleting path itself):
      blocks if path is equal to, inside, OR an ancestor of any env-referenced path.
    When allow_ancestor=True (browsing a container directory):
      blocks only if path is equal to or inside an env-referenced path.
    """
    p_key = key(path)
    root_key = key(root) if root is not None else None
    for var, _ref, ref_key in env_referenced_paths():
        # If the user explicitly scoped root inside an outer PYTHONPATH workspace,
        # don't treat that outer PYTHONPATH entry as blocking subdirectories inside root.
        if var.upper() == 'PYTHONPATH' and root_key is not None and _key_within(root_key, ref_key, inclusive=False):
            continue
        if _key_within(p_key, ref_key):
            return f'路径位于环境变量 {var} 引用范围内，移动或改名会破坏依赖'
        if not allow_ancestor and _key_within(ref_key, p_key, inclusive=False):
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


# 清理腾空间模式下，AppData 内绝对禁止触碰的高危子目录（程序本体/UWP/DPAPI凭据/证书/本工具状态）
APPDATA_NEVER_TOUCH = {
    'programs', 'packages', 'credentials', 'protect', 'crypto',
    'systemcertificates', 'windowsapps', 'vault', 'identitycrl',
    'tidy-file-tree', 'tidy-file-tree-mirror', 'tidy-file-tree-system-state',
    'packtidy', 'packtidy-mirror',
}

# 清理腾空间模式下明确允许的 AppData 缓存根。通用 Cache/Caches 名称不构成授权。
# 路径均相对 Local 或 Roaming；Chrome/Edge 的 Cache 仅允许在明确的浏览器配置目录中。
APPDATA_SAFE_CACHE_PATHS = {
    ('temp',), ('tmp',), ('crashdumps',), ('inetcache',),
    ('pip', 'cache'), ('npm-cache',), ('pnpm-store',),
    ('nvidia', 'dxcache'), ('nvidia', 'glcache'),
    ('nvidia', 'nv_cache'), ('d3dscache',),
    ('google', 'chrome', 'user data', 'default', 'cache'),
    ('google', 'chrome', 'user data', 'default', 'code cache'),
    ('google', 'chrome', 'user data', 'default', 'gpucache'),
    ('microsoft', 'edge', 'user data', 'default', 'cache'),
    ('microsoft', 'edge', 'user data', 'default', 'code cache'),
    ('microsoft', 'edge', 'user data', 'default', 'gpucache'),
}

# 清理腾空间模式下，用户主目录下允许检查的已知开发缓存点目录（不含 .ssh/.gnupg/.vscode 等配置与凭据）
USER_SAFE_CACHE_SUBPATHS = (
    ('.cache',),
    ('.gradle', 'caches'),
    ('.nuget', 'packages'),
    ('.npm', '_cacache'),
    ('.m2', 'repository'),
)

# 清理腾空间模式下允许"穿过进入"的只读父容器（本身不可移动/删除）
USER_SAFE_CACHE_CONTAINERS = {'.gradle', '.nuget', '.npm', '.m2'}


def _appdata_cleanup_verdict(parts, *, allow_container=False):
    """Evaluate AppData / user dev-cache paths when allow_cache_cleanup=True.

    Returns (handled, allowed, reason).
    """
    if not parts:
        return (False, False, '')

    # Case 1: Path contains 'appdata'
    if 'appdata' in parts:
        idx = parts.index('appdata')
        tail = parts[idx + 1:]
        if any(seg in APPDATA_NEVER_TOUCH for seg in tail):
            bad = next(seg for seg in tail if seg in APPDATA_NEVER_TOUCH)
            return (True, False, f"AppData 内的 '{bad}' 属于程序本体/凭据/状态保护区，永久禁止触碰")
        if any(seg in SYSTEM_PROTECTED_FILES or seg.startswith(('ntuser.dat', 'usrclass.dat')) for seg in tail):
            return (True, False, 'AppData 内系统注册表配置单元永久禁止触碰')
        # Bare AppData or AppData\Local / Roaming container: browsable only, never movable
        if len(tail) == 0 or (len(tail) == 1 and tail[0] in ('local', 'roaming')):
            if allow_container:
                return (True, True, '清理模式允许浏览 AppData 安全缓存容器')
            return (True, False, '不能移动或删除 AppData 根容器本身，仅允许处理内部安全缓存子目录')
        if tail[0] not in ('local', 'roaming'):
            return (True, False, f"AppData\\{tail[0]} 不在安全缓存清理范围内（保留存档与配置）")
        sub = tuple(tail[1:])
        # Exact known roots; unknown paths containing a segment named Cache remain protected.
        if any(sub[:len(cache_root)] == cache_root for cache_root in APPDATA_SAFE_CACHE_PATHS):
            return (True, True, '清理模式放行明确列出的 AppData 缓存目录')
        # Firefox cache is stored per profile under Profiles\<profile>\cache2.
        firefox_profiles = ('mozilla', 'firefox', 'profiles')
        firefox_profile = (len(sub) >= 4 and sub[:3] == firefox_profiles
                           and sub[3].endswith('.default-release'))
        if firefox_profile and sub[4:5] == ('cache2',):
            return (True, True, '清理模式放行 Firefox profile cache')
        # Only browse parents of a known cache path, without moving the application/profile.
        if allow_container and (any(sub == cache_root[:len(sub)] for cache_root in APPDATA_SAFE_CACHE_PATHS)
                                or sub == firefox_profiles[:len(sub)]
                                or (firefox_profile and len(sub) == 4)):
            return (True, True, '清理模式允许查看应用目录下是否存在安全缓存')
        return (True, False, 'AppData 仅放行已知安全缓存/临时目录，其余应用配置与数据默认保护')

    # Case 2: C:\Users\<user>\.<dev-cache>
    if len(parts) >= 3 and parts[0] == 'users' and parts[2].startswith('.'):
        dot_tail = tuple(parts[2:])
        for allowed_prefix in USER_SAFE_CACHE_SUBPATHS:
            if dot_tail[:len(allowed_prefix)] == allowed_prefix:
                return (True, True, f"清理模式放行已知开发缓存 '{'/'.join(allowed_prefix)}'")
        if allow_container and len(dot_tail) == 1 and dot_tail[0] in USER_SAFE_CACHE_CONTAINERS:
            return (True, True, f"清理模式允许浏览开发缓存父目录 '{dot_tail[0]}'")
        return (True, False, f"'{parts[2]}' 属于用户主目录隐藏配置/凭据，禁止整理或清理")

    return (False, False, '')


def nested_verdict(path, *, allow_cache_cleanup=False, allow_container=False,
                   nested_rules=(), descendants=False):
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

    if active_game_save(parts) or (drive and drive == _system_drive() and len(parts) >= 3
                                   and parts[0] == 'users' and parts[2] == 'saved games'):
        return (False, '游戏可能正在使用该存档目录，保留原路径')
    configured, soft_prefix = nested_policy_verdict(path, nested_rules, descendants=descendants,
                                                    allow_container=allow_container)
    if not configured:
        return (False, '命中嵌套保护，只有明确列出的子目录可整理')

    # 清理腾空间模式下，对 AppData 及已知开发缓存先走精细白名单判定（其余强制禁区仍优先拦截）
    if allow_cache_cleanup:
        hard_deny = NESTED_DENY - {'appdata'}
        for seg in parts:
            if seg in hard_deny:
                return (False, f"'{seg}' 属于强制保护目录，整棵子树禁止整理")
        handled, ok, why = _appdata_cleanup_verdict(parts, allow_container=allow_container)
        if handled:
            return (ok, why)

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
        elif seg in PROTECTED_NAMES and i >= soft_prefix:
            deny_at, deny_name = i, seg

    if deny_at > allow_at:
        return (False, f"'{deny_name}' 在更深处，优先保护")
    if allow_at >= 0:
        return (True, f"'{allow_name}' 是允许整理的区域")
    if soft_prefix:
        return (True, '命中嵌套保护的显式例外或只读通道')
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
    if not markers:
        return []
    p_key = key(path)
    hits = []
    for m in markers:
        m_key = m.get('_key')
        if m_key is None:
            m_key = key(m['path'])
            m['_key'] = m_key
        if _key_within(p_key, m_key) or (descendants and _key_within(m_key, p_key)):
            hits.append(m)
    return hits


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
