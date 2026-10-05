# -*- coding: utf-8 -*-
"""i18n 门禁：语言包结构 + 「界面文案不许写死在代码里」。

只管静态能判死的事（不用建窗口、不起进程），跑一次一秒：

  A. 语言包本身
     A1 每份包都能解析、顶层是对象、值都是字符串
     A2 ``LANGUAGES`` 里每个语言都有对应文件；反过来也没有孤儿包
     A3 ★ key 集合与基准包（zh_CN）**完全一致**（哪个方向缺都算）
     A4 ★ 每条 key 的 ``{占位符}`` 集合一致
     A5 不许有空文案（空串 = 界面上凭空少一句话，比缺 key 更难发现）

  B. 代码侧
     B1 ★ 裸中文界面文案**只减不增**（见下面「基线」）
     B2 代码里 ``t("...")`` 用到的 key 必须在基准包里存在（拼错 key 是最常见的错）
     B3 ★ ``t("key", a=...)`` 传的占位符要和语言包里的一致
        （少传一个 → 运行时 .format 抛 KeyError → 这句文案原样带花括号显示）

为什么 A3/A4 必须是硬门禁
-------------------------
「少一条翻译」和「多一条翻译」都是 bug，但**占位符对不上更隐蔽**：
译文里写成 ``{nane}`` 而原文是 ``{name}``，界面上就是一句带着 ``{nane}``
的怪话，只有走到那条分支才看得见。所以这里按集合逐条比，不靠人工眼扫。

基线（B1）
----------
i18n 是**横切关注点**，不可能一次把 874 条文案全搬完，所以按模块迁移、
一轮搬一块。B1 的作用是**保证只减不增**：本轮之后谁再往代码里写死一句
界面文案，这里就红。迁移完一块，把对应数字改小（只许往下改）。

判「裸中文界面文案」的规则（三条豁免）：
  * docstring 不算（那是给开发者看的说明，不是界面）；
  * ``logger.*(...)`` 里的不算（**日志不翻译**，这是刻意的 —— 日志要能在
    任何语言下被搜到、被贴到 issue 里）；
  * 送进 ``t(...)`` 的不算（那本来就是取词）。
其余的含中文字符串常量都算 —— 设置页的标签、按钮、对话框正文都长这样。

用法：
    python _tools/verify/i18n_check.py
    python _tools/verify/i18n_check.py --report   # 只打印统计，不判通过与否
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _paths import ROOT  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

# 自检里跑人造用例时把它置 True：那些 FAIL 是**故意造出来的**，
# 打到屏幕上会跟真失败混在一起，看不出来到底有没有问题。
_SILENT = False


def check(label: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    if _SILENT:
        return
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}"
          + (f"   {detail}" if detail else ""))


# ---------- 基线：每个文件还剩多少条「写死的界面文案」----------
#
# ★ 2026-10-05：全部模块已迁完，基线清零。这是一张**空的硬门禁表** ——
#   任何文件只要再写死一条界面文案，门禁当场报错（不在表里 = 要求 0 条）。
#
# 想把某句话豁免掉，只有两个正当做法，**都不许往这张表里加数字**：
#
#   ① 它是**故意不翻译**的 —— 语言名（`"简体中文"`）、内部标识（`APP_NAME`）、
#      写进 config.json 当数据用的名字（`DEFAULT_PROFILE_NAME`）、
#      只进日志的计时标签。做法：在那一行（或上方紧邻的纯注释块里）写
#      `# i18n: keep`，并讲清为什么。
#   ② 它根本不是给用户看的 —— 日志正文、`what=` 诊断标签、编程错误消息。
#      这类走 bare_ui_strings() 里已有的豁免口径，别在这里破例。
#
# 判据就一句：**用户有可能在界面上读到它吗？** 会 → 进语言包；
# 不会 → 上面两条之一。放不下的说明它不是"不翻译"，只是你懒得翻。
BASELINE_BARE: dict[str, int] = {}

# 语言解析：{name} / {max} 这种
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 行尾豁免标记：`# i18n: keep`（见 bare_ui_strings 的口径 ⑤）
_KEEP_MARK = re.compile(r"#\s*i18n:\s*keep\b")


# ---------- A. 语言包 ----------

def load_packs(lang_dir: Path):
    """读 lang/*.json，返回 ({code: path}, {code: {key: text}})。"""
    files = {p.stem: p for p in sorted(lang_dir.glob("*.json"))}
    packs: dict[str, dict[str, str]] = {}
    for code, path in files.items():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            check(f"语言包 {path.name} 能解析", False, str(e))
            packs[code] = {}
            continue
        if not isinstance(raw, dict):
            check(f"语言包 {path.name} 顶层是对象", False, type(raw).__name__)
            packs[code] = {}
            continue
        packs[code] = {
            k: v for k, v in raw.items()
            if isinstance(k, str) and not k.startswith("_")
        }
        bad = [k for k, v in packs[code].items() if not isinstance(v, str)]
        check(f"语言包 {path.name} 的值都是字符串", not bad, str(bad[:5]))
        packs[code] = {k: v for k, v in packs[code].items()
                       if isinstance(v, str)}
    return files, packs


def placeholders(text: str) -> set[str]:
    """文案里的 ``{占位符}`` 集合。

    ``{{`` / ``}}`` 是转义出来的字面花括号，不算占位符。
    """
    out: set[str] = set()
    for raw in _PLACEHOLDER.findall(text.replace("{{", "").replace("}}", "")):
        name = raw.split(":")[0].split("!")[0].strip()
        if name:
            out.add(name)
    return out


def check_packs(lang_dir: Path, codes: tuple[str, ...], base: str) -> dict:
    files, packs = load_packs(lang_dir)

    # --- A2：语言清单 ↔ lang/ 目录，双向对账 ---
    missing = [c for c in codes if c not in packs]
    check("★ LANGUAGES 里每个语言都有 lang/<code>.json", not missing,
          f"缺：{missing}")
    orphans = [c for c in packs if c not in codes]
    check("lang/ 里没有 LANGUAGES 之外的语言包（孤儿包＝永远不会被加载）",
          not orphans, f"多出来：{orphans}")
    if base not in packs:
        check(f"基准语言包 {base}.json 存在", False, str(sorted(files)))
        return {}

    base_pack = packs[base]
    check("基准包不是空的", bool(base_pack), f"{len(base_pack)} 条")

    # --- A5：空文案 ---
    for code, pack in packs.items():
        empty = [k for k, v in pack.items() if not v.strip()]
        check(f"{code}：没有空文案（空串＝界面凭空少一句话）", not empty,
              str(empty[:5]))

    # --- A3 / A4：逐语言与基准对账 ---
    for code, pack in sorted(packs.items()):
        if code == base:
            continue
        miss = sorted(set(base_pack) - set(pack))
        extra = sorted(set(pack) - set(base_pack))
        check(f"★ {code} 的 key 集合与 {base} 一致", not miss and not extra,
              f"缺 {len(miss)} 条 {miss[:4]}；多 {len(extra)} 条 {extra[:4]}")
        diff = sorted(
            k for k in set(pack) & set(base_pack)
            if placeholders(pack[k]) != placeholders(base_pack[k])
        )
        detail = ""
        if diff:
            k = diff[0]
            detail = (f"{len(diff)} 条不一致，如 {k!r}："
                      f"{base}={sorted(placeholders(base_pack[k]))} vs "
                      f"{code}={sorted(placeholders(pack[k]))}")
        check(f"★ {code} 每条文案的占位符与 {base} 一致", not diff, detail)

    return base_pack


# ---------- B. 代码侧 ----------

def _is_docstring(node) -> bool:
    return (isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str))


def bare_ui_strings(source: str) -> list[tuple[int, str]]:
    """这份源码里「写死的界面文案」，返回 [(行号, 文案)]。

    什么算「界面文案」：含中日韩字符的字符串常量 —— 除非它命中了下面任何
    一条豁免。豁免的口径要**窄而且有理由**，宽了就变成给自己开后门：

      ① docstring —— 给读代码的人看的，不进界面
      ② ``logger.*(...)`` / ``logging.*(...)`` / ``_note_once(...)``
         （i18n 自己的日志出口）/ ``_add(...)``（gamelog 的行缓冲）——
         日志正文一律不翻译：它是给开发者看的，而且换语言后新旧日志会混血
      ③ ``t(...)`` 自己
      ④ ``what=...`` 诊断标签（以及形参 ``what`` 的默认值）—— 它只拼进
         那条 WARNING 的正文，是给用户拿去 config.json 里找键名的路标，
         翻了反而对不上
      ⑤ 行尾标了 ``# i18n: keep`` 的 —— 精确豁免。用于**故意不翻译**的
         字符串（语言名「简体中文」、APP_NAME 这种内部标识、会写进
         config.json 当数据用的名字）；标了就得在旁边写清为什么

    ★ 别为了清零去放宽这里 —— 放不下的字符串应该老实进语言包。
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    skip: set[int] = set()          # 要跳过的节点 id

    # ① docstring
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and _is_docstring(body[0]):
                skip.add(id(body[0].value))

    for node in ast.walk(tree):
        # ② / ③：日志出口与 t() 的整棵实参树
        if isinstance(node, ast.Call):
            fn = node.func
            name = ""
            if isinstance(fn, ast.Attribute):
                base = fn.value
                if isinstance(base, ast.Name):
                    name = f"{base.id}.{fn.attr}"
            elif isinstance(fn, ast.Name):
                name = fn.id
            is_log = (name.startswith("logger.")
                      or name.startswith("logging.")
                      or name.endswith("._add")
                      # i18n 自己的日志出口：它内部才调 logger，外面看不出来
                      or name == "_note_once")
            if is_log or name == "t":
                for sub in ast.walk(node):
                    skip.add(id(sub))

            # ④ what= 诊断标签
            for kw in node.keywords:
                if kw.arg == "what":
                    for sub in ast.walk(kw.value):
                        skip.add(id(sub))

        # ④ 形参 what 的默认值：def normalize_bool(raw, default, *, what="开关")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 位置参数：defaults 跟参数列表「右对齐」（只覆盖最后几个）
            pos = getattr(node.args, "posonlyargs", []) + list(node.args.args)
            for a, d in zip(pos[len(pos) - len(node.args.defaults):],
                            node.args.defaults):
                if a.arg == "what":
                    skip.add(id(d))
            # 仅关键字参数：kw_defaults 与 kwonlyargs 一一对应（可能是 None）
            for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if a.arg == "what" and d is not None:
                    skip.add(id(d))

    def _kept(lineno: int) -> bool:
        """这一行的字符串是不是标了 `# i18n: keep`。

        两种写法都认：① 标在字符串那一行的行尾；② 标在**上方紧邻的注释块**
        里（长语句挤不下行尾注释，或者要写清楚为什么时用这种）。注释块只要
        有一行带了标记就算 —— 往上扫到第一个非注释行为止，纯注释行不可能
        是别的语句的一部分，所以不会误伤。
        """
        if not (1 <= lineno <= len(lines)):
            return False
        if _KEEP_MARK.search(lines[lineno - 1]):
            return True
        j = lineno - 2
        while j >= 0 and lines[j].lstrip().startswith("#"):
            if _KEEP_MARK.search(lines[j]):
                return True
            j -= 1
        return False

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and _CJK.search(node.value)):
            continue
        lineno = node.lineno
        if _kept(lineno):           # ⑤
            continue
        out.append((lineno, node.value))
    return out


def read_language_table(utils_py: Path):
    """从 ``launcher/utils.py`` 里静态读出 ``LANGUAGES`` 与 ``LANGUAGE_DEFAULT``。

    刻意**不 import** launcher：一是这脚本号称「纯静态」，import 会顺手
    初始化日志模块、在 cwd 里落一个 launcher.dev.log；二是语言清单本来就是
    写死在源码里的常量，用 AST 读出来比 import 更老实（谁把它改成运行时
    算出来的，这里会立刻报「读不出」）。
    """
    tree = ast.parse(utils_py.read_text(encoding="utf-8"))
    langs: tuple[tuple[str, str], ...] = ()
    default = ""
    for node in ast.walk(tree):
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if not (isinstance(target, ast.Name) and value is not None):
            continue
        if target.id == "LANGUAGES":
            try:
                langs = tuple(ast.literal_eval(value))
            except ValueError:
                langs = ()
        elif target.id == "LANGUAGE_DEFAULT":
            try:
                default = ast.literal_eval(value)
            except ValueError:
                default = ""
    return langs, default


def t_calls(source: str) -> list[tuple[int, str, set[str] | None]]:
    """源码里字面量参数的 ``t("key")`` 调用 → [(行号, key, 传的占位符 | None)]。

    ``t("a" if x else "b")`` 这种取不到静态 key，直接跳过。
    用了 ``**kwargs`` 的也跳过（静态看不出传了啥）—— 返回的占位符是 None。
    """
    tree = ast.parse(source)
    out: list[tuple[int, str, set[str] | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "t"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        key = node.args[0].value
        if not isinstance(key, str):
            continue
        if any(k.arg is None for k in node.keywords):
            out.append((node.lineno, key, None))
            continue
        out.append((node.lineno, key, {k.arg for k in node.keywords}))
    return out


def self_test() -> None:
    """反向对照：证明这套判据**真的能抓到东西**。

    一个只会返回「通过」的检查等于没有检查。这里拿几段人造代码/语言包喂进去，
    要求它在该报的地方报出来 —— 包括「不该报的不许报」（日志、docstring、t()）。
    """
    print("\n[自检] 反向对照：这些情况必须报，那些情况必须不报")

    # --- 裸中文判定 ---
    def n(src: str) -> int:
        return len(bare_ui_strings(src))

    check("查得出来：写死的标签 text=\"保存\"", n('x = 1\ntext = "保存"\n') == 1)
    check("查得出来：对话框正文", n('messagebox.showinfo("提示", "游戏运行中")\n') == 2)
    check("不误报：docstring", n('"""说明文字。"""\nx = 1\n') == 0)
    check("不误报：logger.* 里的（日志不翻译）",
          n('logger.warning(f"路径为空: {p}")\n') == 0)
    check("不误报：_note_once 里的（i18n 自己的日志出口）",
          n('_note_once(f"缺文案 {k!r}")\n') == 0)
    check("不误报：送进 t() 的", n('ttk.Label(f, text=t("a.b"))\n') == 0)
    check("不误报：纯 ASCII", n('mod = "gui_main"\n') == 0)
    check("不误报：_add(...) 里的（gamelog 的行缓冲，同样是日志）",
          n('self._add(f"[启动器] 读输出中断: {e}")\n') == 0)
    check("不误报：what= 诊断标签（拼进 WARNING 给用户找 config 键名）",
          n('normalize_bool(v, True, what="开关")\n') == 0)
    check("不误报：形参 what 的默认值",
          n('def f(raw, *, what: str = "开关"):\n    return raw\n') == 0)
    check("不误报：行尾标了 # i18n: keep 的（故意不翻译）",
          n('APP_NAME = "Mindustry 启动器"  # i18n: keep\n') == 0)
    check("不误报：上方注释块里标了 # i18n: keep 的",
          n('# i18n: keep —— 内部标识\nAPP_NAME = "Mindustry 启动器"\n') == 0)
    # ★ 基线清零之后，这两条才是真正要守住的东西
    check("★ 新写一句界面文案必须当场被算出来（基线已清零）",
          n('def f():\n    return "这是一句新写的界面文案"\n') == 1)
    check("★ keep 只豁免自己那一条，不会顺手放过紧挨着的下一条",
          n('# i18n: keep\nX = "内部标识"\ntext = "这是界面文案"\n') == 1)

    # --- t() 调用收集 ---
    calls = t_calls('t("jre.ok", text=x)\nkey = "jre.idle"\nt("jre.idle")\n')
    check("只收 t() 的调用，且带上了占位符名",
          len(calls) == 2 and calls[0][1] == "jre.ok"
          and calls[0][2] == {"text"} and calls[1][2] == set(),
          str(calls))
    star = t_calls('t("a.b", **kw)\n')
    check("用了 **kwargs 的标成「静态看不出」（None）",
          len(star) == 1 and star[0][2] is None, str(star))
    check("非字面量 key 直接跳过", t_calls('t(k)\nt("a" if x else "b")\n') == [])

    # --- 占位符解析 ---
    check("占位符：普通 {x} 认，转义 {{y}} 不认",
          placeholders("a {x} b {{y}} c") == {"x"},
          str(placeholders("a {x} b {{y}} c")))
    check("占位符：{n:>4} 这种带格式说明的取名字 n",
          placeholders("{n:>4}") == {"n"})

    # --- 语言包对账：人造一份「少一条 + 占位符写错」的，必须报出来 ---
    import tempfile
    global PASS, FAIL, _SILENT
    keep_pass, keep_fail = list(PASS), list(FAIL)

    def probe(make_files) -> list[str]:
        """在临时目录里造一份语言包，返回 check_packs 报出来的失败项。"""
        global _SILENT
        with tempfile.TemporaryDirectory() as td:
            make_files(Path(td))
            PASS.clear()
            FAIL.clear()
            _SILENT = True
            try:
                check_packs(Path(td), ("zh_CN", "en_US"), "zh_CN")
                return list(FAIL)
            finally:
                _SILENT = False
                PASS[:], FAIL[:] = keep_pass, keep_fail

    def _write(d: Path, name: str, obj: dict) -> None:
        (d / name).write_text(json.dumps(obj, ensure_ascii=False),
                              encoding="utf-8")

    caught = probe(lambda d: (
        _write(d, "zh_CN.json", {"a.one": "一", "a.two": "值 {x}"}),
        _write(d, "en_US.json", {"a.one": "one", "a.two": "value {nane}"}),
    ))
    check("语言包对账抓得住「占位符拼错」",
          any("占位符" in c for c in caught), str(caught))

    caught = probe(lambda d: (
        _write(d, "zh_CN.json", {"a.one": "一"}),
        _write(d, "en_US.json", {"a.one": "one", "a.old": "gone"}),
    ))
    check("语言包对账抓得住「key 多一条 / 少一条」",
          any("key 集合" in c for c in caught), str(caught))

    caught = probe(lambda d: _write(d, "zh_CN.json", {"a.one": "一"}))
    check("语言包对账抓得住「LANGUAGES 里的语言没有语言包」（否则界面全是 key）",
          any("每个语言都有" in c for c in caught), str(caught))

    caught = probe(lambda d: (
        _write(d, "zh_CN.json", {"a.one": "一"}),
        _write(d, "en_US.json", {"a.one": "one"}),
        _write(d, "ja_JP.json", {"a.one": "一"}),      # 孤儿包
    ))
    check("语言包对账抓得住「孤儿包」（永远加载不到，白翻译）",
          any("孤儿包" in c for c in caught), str(caught))

    caught = probe(lambda d: (
        _write(d, "zh_CN.json", {"a.one": "一", "a.empty": ""}),
        _write(d, "en_US.json", {"a.one": "one", "a.empty": " "}),
    ))
    check("语言包对账抓得住「空文案」（界面凭空少一句话）",
          any("空文案" in c for c in caught), str(caught))


def main() -> int:
    ap = argparse.ArgumentParser(description="i18n 门禁（静态）")
    ap.add_argument("--report", action="store_true",
                    help="只打印统计，不判通过与否")
    args = ap.parse_args()

    langs, default = read_language_table(ROOT / "launcher" / "utils.py")
    codes = tuple(code for code, _ in langs)
    check("launcher/utils.py 里能读出 LANGUAGES（语言清单的唯一来源）",
          bool(langs), f"{len(langs)} 门")
    check("默认语言在语言清单里", default in codes, default)

    lang_dir = ROOT / "lang"
    print("=" * 64)
    print(f"语言：{'、'.join(f'{c}（{n}）' for c, n in langs)}")
    print("=" * 64)

    print("\n[A] 语言包")
    pack = check_packs(lang_dir, codes, default)

    print("\n[B] 代码侧")
    files = sorted((ROOT / "launcher").glob("*.py"))
    check("扫到了 launcher/*.py", bool(files), f"{len(files)} 个文件")

    all_keys: set[str] = set()
    counts: dict[str, int] = {}
    bad_keys: list[str] = []
    bad_params: list[str] = []
    skip_kwargs: list[str] = []

    for path in files:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as e:                                # pragma: no cover
            check(f"{path.name} 语法正常", False, str(e))
            continue

        # --- B1：裸中文界面文案 ---
        bare = bare_ui_strings(source)
        counts[path.name] = len(bare)
        allowed = BASELINE_BARE.get(path.name, 0)
        if len(bare) > allowed:
            new = [f"L{ln} {txt[:24]!r}" for ln, txt in bare[allowed:]]
            check(f"{path.name}：写死的界面文案没超过基线（{allowed} 条）",
                  False, f"多出 {len(bare) - allowed} 条：{new[:3]}")

        # --- B2 / B3：t() 的 key 与占位符 ---
        for lineno, key, params in t_calls(source):
            all_keys.add(key)
            if pack and key not in pack:
                bad_keys.append(f"{path.name}:{lineno} {key!r}")
                continue
            if params is None:
                skip_kwargs.append(f"{path.name}:{lineno} {key!r}")
                continue
            if not pack:
                continue
            need = placeholders(pack[key])
            if params != need:
                bad_params.append(
                    f"{path.name}:{lineno} {key!r} 传{sorted(params)}"
                    f" 需要{sorted(need)}"
                )

    # B1 的「没超基线」只为违规的文件报，通过时给一条汇总免得刷屏
    over = [n for n, c in counts.items() if c > BASELINE_BARE.get(n, 0)]
    check("★ 没有任何文件新增写死的界面文案（B1）", not over,
          f"违规 {len(over)} 个" if over else
          f"共剩 {sum(counts.values())} 条待迁移（只减不增）")

    check("★ 代码里用到的 key 都在基准包里存在（B2 拼写）", not bad_keys,
          f"{len(bad_keys)} 处：" + "；".join(bad_keys[:3]))
    check("★ 代码传的占位符和语言包里的一致（B3 少传会带花括号显示）",
          not bad_params, "；".join(bad_params[:3]))
    if skip_kwargs:
        print(f"  [SKIP] 用了 **kwargs、静态看不出占位符的 t() 调用："
              f"{len(skip_kwargs)} 处")

    # 反向：语言包里有没有**没人用**的 key（可能是改文案时留下的孤儿）
    if pack:
        unused = sorted(set(pack) - all_keys)
        # app.title_profile 之类可能只在非字面量位置用，所以只提示不判错
        print(f"\n[提示] 基准包 {len(pack)} 条，代码里静态用到 {len(all_keys)} 条；"
              f"没被字面量引用的 {len(unused)} 条"
              + (f"（如 {unused[:4]}）" if unused else ""))

    print("\n[统计] 每个文件还剩多少条写死的界面文案")
    for name, cnt in sorted(counts.items(), key=lambda kv: -kv[1]):
        if cnt or name in BASELINE_BARE:
            mark = "" if cnt <= BASELINE_BARE.get(name, 0) else "  ← 超基线"
            print(f"    {name:24s} {cnt:4d}{mark}")

    self_test()

    if args.report:
        print("\n（--report 模式：不判通过与否）")
        return 0

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  失败:", f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
