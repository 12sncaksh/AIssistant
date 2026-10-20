"""Layout-checked compatibility patch for the installed DSH ACP server."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

# 已实测通过 @deepseek-ai/dsh-acp 0.1.5-rc.3 与 0.2.0-rc.2 两个版本。
# 版本号**故意不再做相等校验**：patch_source() 本来就会在任何不认识的源码布局上
# 直接抛错，那才是真正的契约；而卡版本号只会把每一次上游升级都变成硬失败。
PATCH_MARKER = "session/set_permission_preset"

_INJECT_OLD = '''const inject = [
\t"agents",
\t"llm",
\t"sessionPersistence",
\t"sessions"
];'''
_INJECT_NEW = '''const inject = [
\t"agents",
\t"llm",
\t"permissionPresets",
\t"sessionPersistence",
\t"sessions"
];'''
_METHOD_ANCHOR = '''\t\tasync setSessionConfigOption(params, signal) {'''
_METHOD = '''\t\tasync setSessionPermissionPreset(params) {
\t\t\tassertOpen();
\t\t\tconst record = requireSession(brandString(params.sessionId));
\t\t\tif (record.inflight !== void 0) throw invalidParams("cannot change permissions while a prompt is in flight");
\t\t\tconst preset = params.preset;
\t\t\tif (typeof preset !== "string" || !ctx.permissionPresets.names.includes(preset)) throw invalidParams(`unknown permission preset: ${String(preset)}`);
\t\t\tctx.permissionPresets.set(record.agent.session, preset);
\t\t\treturn { permissionPreset: ctx.permissionPresets.current(record.agent.session) };
\t\t},
'''
_ROUTE_WRONG = '''.onRequest("session/set_permission_preset", ({ params }) => implementation.setSessionPermissionPreset(params)).onRequest(methods.agent.session.setConfigOption, ({ params, signal }) => implementation.setSessionConfigOption(params, signal))'''
_ROUTE_OLD = '''.onRequest(methods.agent.session.setConfigOption, ({ params, signal }) => implementation.setSessionConfigOption(params, signal))'''
_ROUTE_NEW = '''.onRequest("session/set_permission_preset", Schema.object({ sessionId: Schema.string().required(), preset: Schema.string().required() }), ({ params }) => implementation.setSessionPermissionPreset(params)).onRequest(methods.agent.session.setConfigOption, ({ params, signal }) => implementation.setSessionConfigOption(params, signal))'''


def patch_source(source: str) -> str:
    """Add the custom request handler, failing on unexpected DSH source layouts."""
    if PATCH_MARKER in source:
        if source.count(_ROUTE_WRONG) == 1:
            return source.replace(_ROUTE_WRONG, _ROUTE_NEW, 1)
        if source.count(_ROUTE_NEW) == 1:
            return source
        raise RuntimeError("Installed DSH ACP permission route has an unknown shape")

    replacements = (
        (_INJECT_OLD, _INJECT_NEW),
        (_METHOD_ANCHOR, _METHOD + _METHOD_ANCHOR),
        (_ROUTE_OLD, _ROUTE_NEW),
    )
    patched = source
    for old, new in replacements:
        if patched.count(old) != 1:
            raise RuntimeError("Installed DSH ACP source did not match the expected patch context")
        patched = patched.replace(old, new, 1)
    return patched


def _package_candidates() -> list[Path]:
    """Locate every installed @deepseek-ai/dsh-acp package.

    这个包已经搬过一次家：0.1.5 放在 dsh/node_modules/ 下，0.2.0 改由 dsh-acp-app
    携带（dsh-acp 自己降级成了 devDependency）。所以这里改成在 @deepseek-ai 子树里
    搜，而不是再写死一条下次还会搬的路径。
    """
    roots: list[Path] = []
    launcher = shutil.which("dsh")
    if launcher:
        roots.append(Path(launcher).resolve().parent)
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry:
            roots.append(Path(entry))

    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for metadata in root.glob("node_modules/@deepseek-ai/**/dsh-acp/package.json"):
            package = metadata.parent
            key = os.path.normcase(str(package))
            if key not in seen:
                seen.add(key)
                found.append(package)
    return found


def ensure_permission_api(package_root: str | os.PathLike[str] | None = None) -> Path:
    """Install the narrowly scoped ACP patch and return the patched module path."""
    roots = [Path(package_root)] if package_root is not None else _package_candidates()
    root = next((item for item in roots if (item / "package.json").is_file()), None)
    if root is None:
        raise RuntimeError("Could not locate the installed @deepseek-ai/dsh-acp package")
    module_path = root / "lib/index.js"
    source = module_path.read_text(encoding="utf-8")
    patched = patch_source(source)
    if patched == source:
        return module_path
    backup = module_path.with_name("index.js.aissistant-backup")
    if not backup.exists():
        shutil.copy2(module_path, backup)
    temporary = module_path.with_name("index.js.aissistant-tmp")
    temporary.write_text(patched, encoding="utf-8", newline="\n")
    os.replace(temporary, module_path)
    return module_path
