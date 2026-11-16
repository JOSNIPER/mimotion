# -*- coding: utf8 -*-
"""
mimotion 图形化控制台（多账号版）
- 启动本地 Web 服务，浏览器打开 HTML 控制面板
- 账号管理：增/删/勾选，数据保存在 .env（USER/PWD，井号分隔）
- 执行：对勾选账号调用 main.py，支持随机范围或指定步数，实时回显日志
- 仅使用 Python 标准库，无需额外安装依赖
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
PY_EXE = os.path.join(BASE_DIR, "venv", "Scripts", "python.exe")
if not os.path.exists(PY_EXE):
    PY_EXE = sys.executable

# ---------------- 运行状态 ----------------
state_lock = threading.Lock()
run_state = {
    "running": False,
    "lines": [],
    "exit_code": None,
    "last_result": None,          # success / failed / None
    "last_run_time": None,
    "success_count": 0,
    "fail_count": 0,
}
proc_ref = {"proc": None}
account_status = {}              # 脱敏账号 -> none/running/success/failed
account_steps = {}               # 脱敏账号 -> 上次刷成功的步数值
STEPS_PATH = os.path.join(BASE_DIR, "last_steps.json")


def load_steps():
    """加载上次成功的步数记录"""
    try:
        with open(STEPS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def persist_steps():
    """保存步数记录到本地 json"""
    try:
        with open(STEPS_PATH, "w", encoding="utf-8") as f:
            json.dump(account_steps, f, ensure_ascii=False)
    except Exception:
        pass


account_steps.update(load_steps())

# ---------------- 历史记录 ----------------
HISTORY_PATH = os.path.join(BASE_DIR, "run_history.jsonl")


def load_history_records():
    """读取全部历史记录（JSONL，每行一条）"""
    records = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    return records


def append_history(user, masked, remark, step, ok):
    """追加一条执行记录"""
    now = beijing_now()
    rec = {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "user": user, "masked": masked, "remark": remark,
        "step": step, "ok": bool(ok),
    }
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def today_records_by_user():
    """当日每个账号的最后一次成功记录 {user: rec}，用于启动时标注"""
    today = beijing_now().strftime("%Y-%m-%d")
    result = {}
    for rec in load_history_records():
        if rec.get("date") == today and rec.get("ok") and rec.get("user"):
            result[rec["user"]] = rec
    return result


# ---------------- .env 账号读写 ----------------
def read_env_lines():
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, encoding="utf-8") as f:
        return f.read().splitlines()


def write_env_lines(lines):
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def parse_accounts(lines=None):
    """解析 .env 中的 USER/PWD，返回 [(user, pwd), ...] 及是否数量不匹配"""
    if lines is None:
        lines = read_env_lines()
    users, pwds = [], []
    for ln in lines:
        s = ln.strip()
        if s.startswith("USER="):
            users = [x for x in s[5:].split("#") if x]
        elif s.startswith("PWD="):
            pwds = [x for x in s[4:].split("#") if x]
    n = min(len(users), len(pwds))
    return [(users[i], pwds[i]) for i in range(n)], len(users) != len(pwds)


def get_accounts():
    return parse_accounts()[0]


def parse_remarks(lines=None):
    """解析 .env 中的 REMARK 行，返回备注列表（井号分隔，与 USER 顺序一一对应）"""
    if lines is None:
        lines = read_env_lines()
    for ln in lines:
        s = ln.strip()
        if s.startswith("REMARK="):
            return [x.strip() for x in s[7:].split("#")]
    return []


def get_remarks():
    """返回与账号列表对齐的备注列表（长度不足补空串，超出则截断）"""
    remarks = parse_remarks()
    n = len(get_accounts())
    while len(remarks) < n:
        remarks.append("")
    return remarks[:n]


def _write_remark_line(lines, remarks):
    """把备注列表写回 lines 中的 REMARK 行；全空时移除该行。返回新的 lines"""
    if any(r for r in remarks):
        value = "REMARK=" + "#".join(remarks)
        for i, ln in enumerate(lines):
            if ln.strip().startswith("REMARK="):
                lines[i] = value
                return lines
        # 没有 REMARK 行则插到 PWD 行之后，没有 PWD 行则追加
        for i, ln in enumerate(lines):
            if ln.strip().startswith("PWD="):
                lines.insert(i + 1, value)
                return lines
        lines.append(value)
        return lines
    # 全空备注：移除 REMARK 行
    return [ln for ln in lines if not ln.strip().startswith("REMARK=")]


def add_account(user, pwd, remark=""):
    if not user or not pwd:
        return False, "账号和密码不能为空"
    if "#" in user or "#" in pwd:
        return False, "账号或密码不能包含 # 符号"
    if "#" in remark:
        return False, "备注不能包含 # 符号"
    lines = read_env_lines()
    accounts, _ = parse_accounts(lines)
    if any(u == user for u, _ in accounts):
        return False, "该账号已存在"
    users = [u for u, _ in accounts] + [user]
    pwds = [p for _, p in accounts] + [pwd]
    remarks = parse_remarks(lines)
    while len(remarks) < len(accounts):   # 兼容旧数据：对齐已有账号数
        remarks.append("")
    remarks.append(remark.strip())
    ui = pi = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("USER="):
            ui = i
        elif s.startswith("PWD="):
            pi = i
    if ui is not None:
        lines[ui] = "USER=" + "#".join(users)
    else:
        lines.append("USER=" + "#".join(users))
    if pi is not None:
        lines[pi] = "PWD=" + "#".join(pwds)
    else:
        lines.append("PWD=" + "#".join(pwds))
    lines = _write_remark_line(lines, remarks)
    write_env_lines(lines)
    return True, "添加成功"


def delete_account(idx):
    lines = read_env_lines()
    accounts, _ = parse_accounts(lines)
    if not (0 <= idx < len(accounts)):
        return False, "账号不存在"
    users = [u for i, (u, _) in enumerate(accounts) if i != idx]
    pwds = [p for i, (_, p) in enumerate(accounts) if i != idx]
    remarks = parse_remarks(lines)
    while len(remarks) < len(accounts):
        remarks.append("")
    remarks = [r for i, r in enumerate(remarks) if i != idx]
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("USER="):
            lines[i] = "USER=" + "#".join(users)
        elif s.startswith("PWD="):
            lines[i] = "PWD=" + "#".join(pwds)
    lines = _write_remark_line(lines, remarks)
    write_env_lines(lines)
    masked = desensitize(accounts[idx][0])
    if masked in account_steps:
        del account_steps[masked]
        persist_steps()
    return True, "已删除"


def set_remark(idx, remark):
    """给已有账号设置/修改备注"""
    remark = str(remark or "").strip()
    if "#" in remark:
        return False, "备注不能包含 # 符号"
    lines = read_env_lines()
    accounts, _ = parse_accounts(lines)
    if not (0 <= idx < len(accounts)):
        return False, "账号不存在"
    remarks = parse_remarks(lines)
    while len(remarks) < len(accounts):
        remarks.append("")
    remarks[idx] = remark
    lines = _write_remark_line(lines, remarks)
    write_env_lines(lines)
    return True, "备注已更新"


# ---------------- 工具函数 ----------------
def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def parse_custom_step(value):
    """解析自定义步数：'20000' -> (20000, 20000)；'12000-18000' -> (12000, 18000)。
    返回 (lo, hi)，格式非法或越界返回 None。
    """
    value = str(value if value is not None else "").strip()
    m = re.match(r"^(\d+)\s*[-~]\s*(\d+)$", value)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo, hi) if 1 <= lo <= hi <= 100000 else None
    if value.isdigit():
        n = int(value)
        return (n, n) if 1 <= n <= 100000 else None
    return None


def set_custom_step(value):
    """保存/清除「每天默认步数」到 .env；空字符串表示清除，恢复按时间随机。

    main.py 会读取 CUSTOM_STEP 并跳过时间折算，因此面板、run.bat、定时任务
    三条入口只要共用同一个 .env，就会刷出完全一致的步数。
    """
    raw = str(value if value is not None else "").strip()
    if raw:
        parsed = parse_custom_step(raw)
        if parsed is None:
            return False, "格式：单个数字（如 20000）或范围（如 12000-18000），取值 1 ~ 100000"
        lo, hi = parsed
        value = str(lo) if lo == hi else "%d-%d" % (lo, hi)
    else:
        value = ""
    lines = read_env_lines()
    found = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("CUSTOM_STEP="):
            lines[i] = "CUSTOM_STEP=" + value
            found = True
            break
    if not found:
        lines.append("CUSTOM_STEP=" + value)
    write_env_lines(lines)
    return True, ("已设置每天默认 %s 步" % value) if value else "已恢复按时间随机"


def beijing_now():
    try:
        import pytz
        return datetime.now().astimezone(pytz.timezone("Asia/Shanghai"))
    except Exception:
        return datetime.now()


def desensitize(user):
    if len(user) <= 8:
        ln = max(len(user) // 3, 1)
        return user[:ln] + "***" + user[-ln:]
    return user[:3] + "****" + user[-4:]


def current_rate():
    """当前时间占全天进度比例（与 main.py 的折算逻辑一致，22 点封顶）"""
    now = beijing_now()
    return min((now.hour * 60 + now.minute) / (22 * 60), 1)


def build_info():
    env = load_env()
    now = beijing_now()
    rate = current_rate()
    min_step = int(env.get("MIN_STEP", "18000") or 18000)
    max_step = int(env.get("MAX_STEP", "25000") or 25000)
    accounts, mismatch = parse_accounts()
    return {
        "user": desensitize(accounts[0][0]) if accounts else "未配置",
        "account_count": len(accounts),
        "mismatch": mismatch,
        "min_step": min_step,
        "max_step": max_step,
        "custom_step": env.get("CUSTOM_STEP", "").strip(),
        "cur_min": int(rate * min_step),
        "cur_max": int(rate * max_step),
        "bj_time": now.strftime("%H:%M:%S"),
        "bj_hour": now.hour,
        "bj_minute": now.minute,
        "has_token": os.path.exists(os.path.join(BASE_DIR, "encrypted_tokens.data")),
        "push_ready": any(env.get(k) for k in
                          ("PUSH_PLUS_TOKEN", "PUSH_WECHAT_WEBHOOK_KEY", "TELEGRAM_BOT_TOKEN")),
    }


def build_accounts():
    accounts, mismatch = parse_accounts()
    remarks = get_remarks()
    today_map = today_records_by_user()
    return {
        "mismatch": mismatch,
        "accounts": [
            {"idx": i, "user": u, "masked": desensitize(u),
             "remark": remarks[i] if i < len(remarks) else "",
             "step": account_steps.get(desensitize(u), ""),
             "today": ({"done": True,
                        "time": today_map[u]["time"][11:16],
                        "step": today_map[u].get("step", "")} if u in today_map else None),
             "status": account_status.get(desensitize(u), "none")}
            for i, (u, _) in enumerate(accounts)
        ],
    }


# ---------------- 执行 ----------------
def start_run(indexes, fixed_step=None, mode=None):
    """启动一次执行；indexes 为勾选账号下标，fixed_step 为本次自定义步数，mode 为 random/fixed。

    步数优先级：本次输入 > .env 的 CUSTOM_STEP（每天默认） > 按时间随机范围。
    选「随机范围」时本次临时屏蔽每天默认，但不改动 .env。
    """
    with state_lock:
        if run_state["running"]:
            return False, "正在运行中，请稍候"
    accounts = get_accounts()
    sel = [accounts[i] for i in indexes if isinstance(i, int) and 0 <= i < len(accounts)]
    if not sel:
        return False, "请先勾选要执行的账号"

    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["USER"] = "#".join(u for u, _ in sel)
    child_env["PWD"] = "#".join(p for _, p in sel)
    step_note = "随机范围"
    if fixed_step is not None:
        parsed = parse_custom_step(fixed_step)
        if parsed is None:
            return False, "步数格式：单个数字（如 20000）或范围（如 12000-18000）"
        lo, hi = parsed
        # 直接把目标值交给 main.py，跳过时间折算，全天精确落在指定值/区间
        # （旧实现靠 target/rate 反向折算，早上会被放大且 int 截断有偏差）
        child_env["CUSTOM_STEP"] = str(lo) if lo == hi else "%d-%d" % (lo, hi)
        step_note = "本次自定义 %s 步" % child_env["CUSTOM_STEP"]
    elif mode == "random":
        # 本次临时按时间随机，忽略 .env 里保存的每天默认值
        child_env["CUSTOM_STEP"] = ""
        step_note = "随机范围（本次）"
    else:
        # 强制以 .env 的当前内容为准，避免面板进程启动时缓存的旧环境变量盖掉新设置
        custom = load_env().get("CUSTOM_STEP", "").strip()
        child_env["CUSTOM_STEP"] = custom
        step_note = ("每天默认 %s 步" % custom) if custom else "随机范围"

    with state_lock:
        run_state["running"] = True
        run_state["lines"] = []
        run_state["exit_code"] = None
        for u, _ in sel:
            account_status[desensitize(u)] = "running"

    # 脱敏账号 -> (完整账号, 备注)，用于写历史记录
    # 注意：这里不能用字典推导式引用外层遗留变量 u，它永远指向 sel 的最后一个账号，
    # 会导致除最后一个账号外全部匹配失败（备注丢空、user 退化成脱敏串）。
    remarks_all = get_remarks()
    sel_user_map = {}
    for i in indexes:
        if isinstance(i, int) and 0 <= i < len(accounts):
            full_user = accounts[i][0]
            sel_user_map[desensitize(full_user)] = (
                full_user, remarks_all[i] if i < len(remarks_all) else "")

    def worker():
        try:
            proc = subprocess.Popen(
                [PY_EXE, "main.py"], cwd=BASE_DIR,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", env=child_env, bufsize=1)
            proc_ref["proc"] = proc
            cur_user = None
            failed_written = set()          # 同一次执行里，每个账号只记一条失败
            for line in proc.stdout:
                line = line.rstrip("\n")
                with state_lock:
                    run_state["lines"].append(line)
                    if len(run_state["lines"]) > 500:
                        run_state["lines"].pop(0)
                m = re.search(r"账号：(\S+)", line)
                if m:
                    cur_user = m.group(1)
                    account_status[cur_user] = "running"
                elif "修改步数" in line and "success" in line:
                    if cur_user:
                        account_status[cur_user] = "success"
                        ms = re.search(r"修改步数（(\d+)）", line)
                        if ms:
                            account_steps[cur_user] = ms.group(1)
                            persist_steps()
                            full = sel_user_map.get(cur_user)
                            append_history(full[0] if full else cur_user, cur_user,
                                           full[1] if full else "", ms.group(1), True)
                # 只认「终态失败」，不能见到「失败」二字就判失败：
                # token 降级流程会打印「access_token 已失效：客户端登录失败：None」这类
                # 中间提示，实际上紧接着就登录成功并刷成功了。
                elif ("登陆失败！" in line or "登录获取accessToken失败" in line
                      or line.startswith("执行异常:")
                      or ("修改步数" in line and "[" in line and "success" not in line)):
                    if cur_user and cur_user not in failed_written:
                        failed_written.add(cur_user)
                        account_status[cur_user] = "failed"
                        full = sel_user_map.get(cur_user)
                        append_history(full[0] if full else cur_user, cur_user,
                                       full[1] if full else "", "", False)
            proc.wait()
            with state_lock:
                run_state["exit_code"] = proc.returncode
                run_state["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ok = any("success" in l for l in run_state["lines"])
                run_state["last_result"] = "success" if ok else "failed"
                run_state["success_count" if ok else "fail_count"] += 1
        except Exception as e:
            with state_lock:
                run_state["lines"].append("启动失败: %s" % e)
                run_state["exit_code"] = -1
                run_state["last_result"] = "failed"
                run_state["last_run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                run_state["fail_count"] += 1
        finally:
            with state_lock:
                run_state["running"] = False
            proc_ref["proc"] = None

    threading.Thread(target=worker, daemon=True).start()
    return True, "已启动（%d 个账号，%s）" % (len(sel), step_note)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mimotion 控制台</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family:-apple-system,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
    background:#0b0e14;
    background-image:radial-gradient(900px 380px at 50% -180px, rgba(79,142,247,.14), transparent 70%);
    color:#e6edf3; min-height:100vh; padding:34px 20px 52px;
  }
  .wrap { max-width:1080px; margin:0 auto; }
  header { display:flex; align-items:center; justify-content:space-between; margin-bottom:22px; }
  .title { display:flex; align-items:center; gap:13px; }
  .logo { width:40px; height:40px; border-radius:12px; display:flex; align-items:center; justify-content:center;
          font-size:20px; background:linear-gradient(135deg,#4f8ef7,#2dd4bf);
          box-shadow:0 4px 18px rgba(79,142,247,.35); }
  h1 { font-size:20px; font-weight:650; letter-spacing:.3px; }
  .sub { font-size:12px; color:#8b949e; margin-top:3px; }
  .pill { display:inline-flex; align-items:center; gap:7px; font-size:12.5px; padding:6px 14px;
          border-radius:999px; border:1px solid rgba(255,255,255,.09); background:#151a23; color:#8b949e; }
  .pill .dot { width:8px; height:8px; border-radius:50%; background:#8b949e; }
  .pill.running { color:#fbbf24; border-color:rgba(251,191,36,.35); background:rgba(251,191,36,.08); }
  .pill.running .dot { background:#fbbf24; animation:pulse 1s ease-in-out infinite; }
  .pill.success { color:#34d399; border-color:rgba(52,211,153,.35); background:rgba(52,211,153,.08); }
  .pill.success .dot { background:#34d399; }
  .pill.failed { color:#f87171; border-color:rgba(248,113,113,.35); background:rgba(248,113,113,.08); }
  .pill.failed .dot { background:#f87171; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
  .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:14px; }
  @media (max-width:720px){ .grid{ grid-template-columns:repeat(2,1fr);} }
  .card { background:linear-gradient(180deg,#151a23,#12161f); border:1px solid rgba(255,255,255,.07);
          border-radius:14px; padding:16px 18px; }
  .card .label { font-size:12px; color:#8b949e; margin-bottom:8px; }
  .card .value { font-size:19px; font-weight:650; font-variant-numeric:tabular-nums; }
  .card .foot { font-size:11.5px; color:#6e7681; margin-top:6px; }
  .ok { color:#34d399 !important; } .warn { color:#fbbf24 !important; }
  .panel { background:linear-gradient(180deg,#151a23,#12161f); border:1px solid rgba(255,255,255,.07);
           border-radius:14px; padding:18px 20px; margin-bottom:14px; }
  .panel-head { display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }
  .panel-title { font-size:14px; font-weight:600; }
  .panel-note { font-size:12px; color:#6e7681; }
  #curve { width:100%; height:auto; display:block; }
  /* 账号表格 */
  table.acct { width:100%; border-collapse:collapse; font-size:13.5px; }
  .acct th { text-align:left; font-size:12px; color:#6e7681; font-weight:500; padding:6px 8px;
             border-bottom:1px solid rgba(255,255,255,.07); }
  .acct td { padding:9px 8px; border-bottom:1px solid rgba(255,255,255,.04); vertical-align:middle; }
  .acct tr:last-child td { border-bottom:none; }
  .acct input[type=checkbox] { width:15px; height:15px; accent-color:#4f8ef7; cursor:pointer; }
  .mono { font-family:"Cascadia Code",Consolas,monospace; font-size:13px; }
  .acct .step { font-variant-numeric:tabular-nums; font-size:13.5px; font-weight:600; color:#34d399; }
  .acct .step.empty { color:#545d68; font-weight:400; }
  .acct .todayline { font-size:11px; color:#34d399; margin-top:4px; font-variant-numeric:tabular-nums;
                     white-space:nowrap; }
  .badge { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; padding:3px 10px;
           border-radius:999px; border:1px solid rgba(255,255,255,.09); color:#8b949e; background:#151a23; }
  .badge .dot { width:6px; height:6px; border-radius:50%; background:#8b949e; }
  .badge.running { color:#fbbf24; border-color:rgba(251,191,36,.35); }
  .badge.running .dot { background:#fbbf24; animation:pulse 1s ease-in-out infinite; }
  .badge.success { color:#34d399; border-color:rgba(52,211,153,.35); }
  .badge.success .dot { background:#34d399; }
  .badge.failed { color:#f87171; border-color:rgba(248,113,113,.35); }
  .badge.failed .dot { background:#f87171; }
  .delbtn { background:transparent; border:1px solid rgba(248,113,113,.3); color:#f87171; font-size:12px;
            padding:4px 12px; border-radius:8px; white-space:nowrap; }
  .delbtn:hover { background:rgba(248,113,113,.12); }
  /* 备注列：独立一列，可点击编辑 */
  .remark { font-size:13px; color:#e6edf3; cursor:pointer; max-width:172px;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; display:inline-block;
           vertical-align:middle; border-bottom:1px dashed rgba(230,237,243,.25); }
  .remark:hover { color:#4f8ef7; border-bottom-color:#4f8ef7; }
  .remark.none { color:#545d68; font-size:12px; border-bottom-color:transparent; }
  .remark.none:hover { color:#4f8ef7; border-bottom-color:#4f8ef7; }
  input.remark-edit { background:#0a0d12; border:1px solid #4f8ef7; border-radius:7px;
                      padding:5px 10px; color:#e6edf3; font-size:13px; width:170px; outline:none; }
  .addrow { display:flex; gap:10px; margin-top:14px; flex-wrap:wrap; }
  .addrow input { flex:1; min-width:180px; background:#0a0d12; border:1px solid rgba(255,255,255,.1);
                  border-radius:10px; padding:10px 14px; color:#e6edf3; font-size:13.5px; outline:none; }
  .addrow input:focus { border-color:#4f8ef7; }
  #btnAdd { padding:10px 22px; color:#fff; background:linear-gradient(135deg,#2dd4bf,#14b8a6);
            box-shadow:0 4px 14px rgba(45,212,191,.25); font-size:13.5px; }
  #btnAdd:hover { transform:translateY(-1px); }
  .formmsg { font-size:12px; margin-top:8px; min-height:16px; }
  .formmsg.ok { color:#34d399; } .formmsg.err { color:#f87171; }
  .empty { text-align:center; color:#545d68; font-size:13px; padding:22px 0; }
  /* 执行区 */
  .actions { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  button { font-family:inherit; border:none; cursor:pointer; border-radius:11px; font-size:14.5px;
           font-weight:600; transition:transform .12s ease, box-shadow .12s ease, opacity .12s; }
  #btnRun { padding:12px 30px; color:#fff; background:linear-gradient(135deg,#4f8ef7,#3b82f6);
            box-shadow:0 4px 16px rgba(59,130,246,.35); }
  #btnRun:hover:not(:disabled) { transform:translateY(-1px); box-shadow:0 6px 22px rgba(59,130,246,.45); }
  #btnRun:active:not(:disabled) { transform:translateY(0); }
  #btnRun:disabled { opacity:.45; cursor:not-allowed; }
  #btnStop { padding:12px 22px; color:#f87171; background:transparent; border:1px solid rgba(248,113,113,.4); }
  #btnStop:hover:not(:disabled) { background:rgba(248,113,113,.1); }
  #btnStop:disabled { opacity:.35; cursor:not-allowed; }
  .moderow { display:flex; align-items:center; gap:18px; margin-top:14px; flex-wrap:wrap; font-size:13px; color:#8b949e; }
  .moderow label { display:inline-flex; align-items:center; gap:6px; cursor:pointer; }
  .moderow input[type=radio] { accent-color:#4f8ef7; }
  #fixedStep { width:170px; background:#0a0d12; border:1px solid rgba(255,255,255,.1); border-radius:9px;
               padding:8px 12px; color:#e6edf3; font-size:13px; outline:none; }
  #fixedStep:disabled { opacity:.4; }
  #selInfo { margin-left:auto; color:#8b949e; }
  .minibtn { background:transparent; border:1px solid rgba(79,142,247,.45); color:#4f8ef7; font-size:12px;
             padding:7px 12px; border-radius:8px; cursor:pointer; white-space:nowrap; }
  .minibtn:hover { background:rgba(79,142,247,.12); }
  .defaultstep { font-size:12px; color:#34d399; white-space:nowrap; }
  .defaultstep a { color:#8b949e; margin-left:8px; cursor:pointer; text-decoration:underline; }
  .modenote { margin-top:7px; font-size:11.5px; color:#6e7681; }
  .runinfo { font-size:12.5px; color:#8b949e; }
  #logbox { background:#0a0d12; border:1px solid rgba(255,255,255,.06); border-radius:11px;
            padding:14px 16px; height:264px; overflow-y:auto; font-size:12.8px; line-height:1.75;
            font-family:"Cascadia Code",Consolas,"JetBrains Mono",monospace; white-space:pre-wrap; word-break:break-all; }
  #logbox .ok { color:#34d399; } #logbox .err { color:#f87171; } #logbox .dim { color:#6e7681; }
  #logbox::-webkit-scrollbar { width:8px; } #logbox::-webkit-scrollbar-thumb { background:#252b36; border-radius:4px; }
  footer { text-align:center; font-size:12px; color:#545d68; margin-top:20px; line-height:1.8; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="title">
      <div class="logo">👟</div>
      <div>
        <h1>mimotion 控制台</h1>
        <div class="sub">Zepp Life 自动刷步数 · 多账号管理 · 本地服务</div>
      </div>
    </div>
    <div id="pill" class="pill"><span class="dot"></span><span id="pillText">就绪</span></div>
  </header>

  <div class="grid">
    <div class="card"><div class="label">账号总数</div><div id="acctCount" class="value">-</div><div class="foot">保存在本机 .env</div></div>
    <div class="card"><div class="label">当前时段步数范围</div><div id="currange" class="value">-</div><div class="foot">北京时间 22 点后刷满</div></div>
    <div class="card"><div class="label">配置范围</div><div id="range" class="value">-</div><div class="foot">.env 中 MIN/MAX_STEP</div></div>
    <div class="card"><div class="label">北京时间</div><div id="bjtime" class="value">-</div><div id="token" class="foot">-</div></div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">账号管理</div>
      <div class="panel-note">勾选要执行的账号 · 账号密码仅存本机 .env</div>
    </div>
    <table class="acct">
      <thead><tr>
        <th style="width:34px"><input type="checkbox" id="chkAll" title="全选"></th>
        <th>账号</th><th style="width:90px">步数</th><th style="width:190px">备注</th><th style="width:150px">上次执行</th><th style="width:76px">操作</th>
      </tr></thead>
      <tbody id="acctBody"></tbody>
    </table>
    <div id="acctEmpty" class="empty" style="display:none">还没有账号，请在下方添加</div>
    <div id="mismatchWarn" class="formmsg err" style="display:none">警告：.env 中账号与密码数量不一致，请检查配置</div>
    <div class="addrow">
      <input id="newUser" placeholder="账号（Zepp Life 邮箱 / 手机号）">
      <input id="newPwd" type="password" placeholder="密码">
      <input id="newRemark" placeholder="备注（选填）如：我的小号">
      <button id="btnAdd">添加账号</button>
    </div>
    <div id="acctMsg" class="formmsg"></div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">执行</div>
      <div id="lasttime" class="panel-note">尚未执行</div>
    </div>
    <div class="actions">
      <button id="btnRun">立即执行刷步数</button>
      <button id="btnStop" disabled>停止</button>
      <span class="runinfo">执行约需 5~15 秒，期间请勿重复点击</span>
    </div>
    <div class="moderow">
      <label><input type="radio" name="mode" value="random" checked> 随机范围</label>
      <label><input type="radio" name="mode" value="fixed"> 自定义步数</label>
      <input id="fixedStep" type="text" inputmode="numeric" placeholder="20000 或 12000-18000" disabled>
      <button id="btnSaveStep" class="minibtn">设为每天默认</button>
      <span id="defaultStep" class="defaultstep"></span>
      <span id="selInfo">已选 0 个账号</span>
    </div>
    <div class="modenote">每天默认写进 .env，面板 / run.bat / 定时任务共用；不保存只对本次生效。填区间（如 12000-18000）则每天随机，比固定值更自然。</div>
  </div>

  <div class="panel">
    <div class="panel-head">
      <div class="panel-title">步数随时间增长曲线</div>
      <div class="panel-note">纵轴为步数，横轴为北京时间（小时）</div>
    </div>
    <svg id="curve" viewBox="0 0 840 210" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>

  <div class="panel">
    <div class="panel-head"><div class="panel-title">执行日志</div></div>
    <div id="logbox"><span class="dim">尚无执行记录，勾选账号后点击上方按钮开始。</span></div>
  </div>

  <footer>
    账号与配置仅保存在本机 · 关闭命令行窗口即停止服务<br>
    日常使用：双击项目目录下的 gui.bat 启动本面板
  </footer>
</div>

<script>
var $ = function(id){ return document.getElementById(id); };
var lastLogLen = -1;
var selected = {};        // idx -> true
var accounts = [];

function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function colorize(line){
  var t = esc(line);
  if (/success/.test(line)) return '<span class="ok">'+t+'</span>';
  if (/失败|异常|error|Error|401|429/.test(line)) return '<span class="err">'+t+'</span>';
  if (/^\[/.test(line)) return '<span class="dim">'+t+'</span>';
  return t;
}
var STATUS_TEXT = { none:'未执行', running:'执行中', success:'成功', failed:'失败' };

function setStatus(kind){
  var pill = $('pill'), txt = $('pillText');
  pill.className = 'pill ' + kind;
  txt.textContent = kind==='running' ? '运行中' : kind==='success' ? '上次成功' :
                    kind==='failed' ? '上次失败' : '就绪';
}

/* ---------- 账号 ---------- */
function fetchAccounts(){
  fetch('/api/accounts').then(function(r){return r.json();}).then(function(d){
    accounts = d.accounts;
    $('mismatchWarn').style.display = d.mismatch ? 'block' : 'none';
    var body = $('acctBody'), html = '';
    accounts.forEach(function(a){
      var checked = selected[a.idx] ? 'checked' : '';
      var remark = a.remark
        ? '<span class="remark" data-idx="'+a.idx+'" title="点击修改备注">'+esc(a.remark)+'</span>'
        : '<span class="remark none" data-idx="'+a.idx+'" title="点击添加备注">+ 添加备注</span>';
      html += '<tr>'
        + '<td><input type="checkbox" class="chkAcct" data-idx="'+a.idx+'" '+checked+'></td>'
        + '<td class="mono">'+esc(a.masked)+'</td>'
        + '<td class="step'+(a.step ? '' : ' empty')+'">'+(a.step ? esc(a.step) : '—')+'</td>'
        + '<td>'+remark+'</td>'
        + '<td><span class="badge '+a.status+'"><span class="dot"></span>'+STATUS_TEXT[a.status]+'</span>'
        + (a.today && a.today.done ? '<div class="todayline">今日已刷 '+esc(a.today.time)+' · '+esc(a.today.step)+'</div>' : '')
        + '</td>'
        + '<td><button class="delbtn" data-idx="'+a.idx+'">删除</button></td>'
        + '</tr>';
    });
    body.innerHTML = html;
    $('acctEmpty').style.display = accounts.length ? 'none' : 'block';
    $('acctCount').textContent = accounts.length + ' 个';
    Array.prototype.forEach.call(document.querySelectorAll('.chkAcct'), function(c){
      c.onchange = function(){
        var i = +c.dataset.idx;
        if (c.checked) selected[i] = true; else delete selected[i];
        updateSelInfo();
      };
    });
    Array.prototype.forEach.call(document.querySelectorAll('.delbtn'), function(b){
      b.onclick = function(){ delAccount(+b.dataset.idx); };
    });
    Array.prototype.forEach.call(document.querySelectorAll('.remark'), function(r){
      r.onclick = function(){ editRemark(+r.dataset.idx, r); };
    });
    $('chkAll').checked = accounts.length > 0 && accounts.every(function(a){ return selected[a.idx]; });
    updateSelInfo();
  }).catch(function(){});
}
function updateSelInfo(){
  var n = Object.keys(selected).length;
  $('selInfo').textContent = '已选 ' + n + ' / ' + accounts.length + ' 个账号';
  $('btnRun').disabled = n === 0;
}
function addAccount(){
  var u = $('newUser').value.trim(), p = $('newPwd').value;
  var r = $('newRemark').value.trim();
  var msg = $('acctMsg');
  if (!u || !p){ msg.textContent = '账号和密码不能为空'; msg.className = 'formmsg err'; return; }
  if (r.indexOf('#') >= 0){ msg.textContent = '备注不能包含 # 符号'; msg.className = 'formmsg err'; return; }
  fetch('/api/accounts/add', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user:u, pwd:p, remark:r}) })
    .then(function(r){return r.json();}).then(function(d){
      msg.textContent = d.msg; msg.className = 'formmsg ' + (d.ok ? 'ok' : 'err');
      if (d.ok){ $('newUser').value = ''; $('newPwd').value = ''; $('newRemark').value = ''; fetchAccounts(); }
    });
}
function editRemark(idx, span){
  var input = document.createElement('input');
  input.className = 'remark-edit';
  input.type = 'text';
  input.value = span.textContent === '+ 添加备注' ? '' : span.textContent;
  input.placeholder = '备注（留空清除）';
  span.parentNode.replaceChild(input, span);
  input.focus(); input.select();
  var done = false;
  var composing = false;   // 输入法合成状态（打中文时）
  input.addEventListener('compositionstart', function(){ composing = true; });
  input.addEventListener('compositionend', function(){ composing = false; });
  function commit(save){
    if (done) return; done = true;
    var val = input.value.trim();
    var orig = span.textContent === '+ 添加备注' ? '' : span.textContent;
    if (save && val !== orig && val.indexOf('#') < 0){
      fetch('/api/accounts/remark', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({idx:idx, remark:val}) })
        .then(function(r){return r.json();}).then(function(d){
          $('acctMsg').textContent = d.msg; $('acctMsg').className = 'formmsg ' + (d.ok ? 'ok' : 'err');
          fetchAccounts();
        });
    } else { fetchAccounts(); }
  }
  input.addEventListener('keydown', function(e){
    // 输入法选词中（中文拼音按回车是在确认候选词，不是提交），必须跳过，
    // 否则字还没输完就被当成"保存"确认掉了
    if (e.isComposing || composing || e.keyCode === 229) return;
    if (e.key === 'Enter') commit(true);
    else if (e.key === 'Escape') commit(false);
  });
  input.addEventListener('blur', function(){ commit(true); });
}
function delAccount(idx){
  if (!confirm('确定删除该账号？')) return;
  fetch('/api/accounts/delete', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({idx:idx}) })
    .then(function(r){return r.json();}).then(function(d){
      $('acctMsg').textContent = d.msg; $('acctMsg').className = 'formmsg ' + (d.ok ? 'ok' : 'err');
      delete selected[idx]; fetchAccounts();
    });
}
$('btnAdd').onclick = addAccount;
/* 回车快捷提交，但要跳过输入法选词状态（中文拼音按回车是在确认候选词，不是提交） */
function enterGuard(fn){
  return function(e){
    if (e.key !== 'Enter') return;
    if (e.isComposing || e.keyCode === 229) return;
    fn();
  };
}
$('newPwd').addEventListener('keydown', enterGuard(function(){ addAccount(); }));
$('newRemark').addEventListener('keydown', enterGuard(function(){ addAccount(); }));
$('chkAll').onchange = function(){
  accounts.forEach(function(a){ if ($('chkAll').checked) selected[a.idx] = true; else delete selected[a.idx]; });
  fetchAccounts();
};

/* ---------- 信息卡片与曲线 ---------- */
var customInit = false;
function fetchInfo(){
  fetch('/api/info').then(function(r){return r.json();}).then(function(d){
    $('currange').textContent = d.custom_step ? ('自定义 ' + d.custom_step)
                                              : (d.cur_min + ' ~ ' + d.cur_max);
    $('range').textContent = d.custom_step ? ('自定义 ' + d.custom_step)
                                           : (d.min_step + ' ~ ' + d.max_step);
    $('bjtime').textContent = d.bj_time;
    $('token').textContent = d.has_token ? 'Token 已加密缓存' : 'Token 未缓存';
    $('token').className = 'foot ' + (d.has_token ? 'ok' : 'warn');
    $('defaultStep').innerHTML = d.custom_step
      ? '每天默认 ' + esc(d.custom_step) + ' 步 · 已启用 <a id="btnClearStep">清除</a>'
      : '未设每天默认（按时间随机）';
    if (!customInit){
      customInit = true;
      if (d.custom_step){
        $('fixedStep').value = d.custom_step;
        document.querySelector('input[name=mode][value=fixed]').checked = true;
        $('fixedStep').disabled = false;
      }
    }
    drawCurve(d);
  }).catch(function(){});
}

function drawCurve(d){
  var W=840, H=210, PL=52, PR=16, PT=18, PB=30;
  var iw=W-PL-PR, ih=H-PT-PB, maxY=d.max_step;
  function X(h){ return PL + iw*h/24; }
  function Y(step){ return PT + ih*(1 - step/maxY); }
  function rate(h){ return Math.min(h/22, 1); }
  var pathMax='', pathMin='', rev=[];
  for(var h=0; h<=24.001; h+=0.25){
    pathMax += (h===0?'M':'L') + X(h).toFixed(1) + ' ' + Y(rate(h)*maxY).toFixed(1) + ' ';
    pathMin += (h===0?'M':'L') + X(h).toFixed(1) + ' ' + Y(rate(h)*d.min_step).toFixed(1) + ' ';
  }
  for(var h2=24; h2>=0; h2-=0.25) rev.push(X(h2).toFixed(1)+' '+Y(rate(h2)*d.min_step).toFixed(1));
  var area = pathMax + 'L' + rev.join(' L') + ' Z';
  var s = '';
  s += '<defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">'
     + '<stop offset="0%" stop-color="#4f8ef7" stop-opacity=".28"/>'
     + '<stop offset="100%" stop-color="#4f8ef7" stop-opacity=".03"/></linearGradient></defs>';
  for(var i=0; i<=4; i++){
    var v = maxY*i/4, y = Y(v);
    s += '<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="rgba(255,255,255,.06)"/>';
    s += '<text x="'+(PL-8)+'" y="'+(y+4)+'" text-anchor="end" font-size="10.5" fill="#6e7681">'+Math.round(v).toLocaleString()+'</text>';
  }
  [0,6,12,18,22,24].forEach(function(h){
    s += '<line x1="'+X(h)+'" y1="'+PT+'" x2="'+X(h)+'" y2="'+(H-PB)+'" stroke="rgba(255,255,255,.05)"/>';
    s += '<text x="'+X(h)+'" y="'+(H-PB+16)+'" text-anchor="middle" font-size="10.5" fill="#6e7681">'+h+':00</text>';
  });
  s += '<path d="'+area+'" fill="url(#ag)"/>';
  s += '<path d="'+pathMax+'" fill="none" stroke="#4f8ef7" stroke-width="2" stroke-linecap="round"/>';
  s += '<path d="'+pathMin+'" fill="none" stroke="#2dd4bf" stroke-width="1.6" stroke-dasharray="5 4"/>';
  var t = d.bj_hour + d.bj_minute/60, r = rate(t);
  var cx = X(t), cyMax = Y(r*maxY), cyMin = Y(r*d.min_step);
  s += '<line x1="'+cx+'" y1="'+PT+'" x2="'+cx+'" y2="'+(H-PB)+'" stroke="#fbbf24" stroke-width="1.4" stroke-dasharray="4 4"/>';
  s += '<circle cx="'+cx+'" cy="'+cyMax+'" r="4.5" fill="#fbbf24"/>';
  s += '<circle cx="'+cx+'" cy="'+cyMin+'" r="4" fill="#fbbf24" opacity=".65"/>';
  var label = '现在 ' + d.bj_time.slice(0,5) + ' · ' + d.cur_min + '~' + d.cur_max + ' 步';
  var lw = label.length * 11.5 + 18, lx = Math.min(cx + 10, W - PR - lw);
  s += '<rect x="'+lx+'" y="'+(PT+2)+'" width="'+lw+'" height="22" rx="6" fill="rgba(251,191,36,.12)" stroke="rgba(251,191,36,.4)"/>';
  s += '<text x="'+(lx+9)+'" y="'+(PT+17)+'" font-size="11.5" fill="#fbbf24">'+label+'</text>';
  s += '<text x="'+(PL+iw/2)+'" y="'+(H-2)+'" text-anchor="middle" font-size="10.5" fill="#545d68">时间（北京时间）</text>';
  $('curve').innerHTML = s;
}

/* ---------- 执行与日志 ---------- */
function fetchLog(){
  fetch('/api/log').then(function(r){return r.json();}).then(function(d){
    if (d.lines.length !== lastLogLen){
      lastLogLen = d.lines.length;
      var box = $('logbox');
      box.innerHTML = d.lines.length ? d.lines.map(colorize).join('\n')
                                     : '<span class="dim">尚无执行记录，勾选账号后点击上方按钮开始。</span>';
      box.scrollTop = box.scrollHeight;
    }
    setStatus(d.running ? 'running' : (d.last_result || 'ready'));
    $('btnRun').disabled = d.running || Object.keys(selected).length === 0;
    $('btnStop').disabled = !d.running;
    $('lasttime').textContent = d.last_run_time ?
      ('上次执行 ' + d.last_run_time + ' · 成功 ' + d.success_count + ' 次 / 失败 ' + d.fail_count + ' 次') : '尚未执行';
    if (!d.running && d.last_run_time) fetchAccounts();   // 刷新账号状态徽章
  }).catch(function(){});
}
$('btnRun').onclick = function(){
  var idxs = Object.keys(selected).map(Number);
  var mode = document.querySelector('input[name=mode]:checked').value;
  var body = { indexes: idxs, mode: mode };
  if (mode === 'fixed'){
    var v = $('fixedStep').value.trim();
    if (!/^\d+(?:\s*[-~]\s*\d+)?$/.test(v)){ alert('步数格式：20000 或 12000-18000'); return; }
    body.fixed_step = v;
  }
  $('btnRun').disabled = true;
  fetch('/api/run', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body) })
    .then(function(r){return r.json();}).then(function(d){
      if (!d.started && d.msg) alert(d.msg);
      fetchLog();
    }).catch(function(){});
};
$('btnStop').onclick = function(){ fetch('/api/stop', {method:'POST'}).then(fetchLog); };
Array.prototype.forEach.call(document.querySelectorAll('input[name=mode]'), function(r){
  r.onchange = function(){
    $('fixedStep').disabled = document.querySelector('input[name=mode]:checked').value !== 'fixed';
  };
});
function saveDefaultStep(v){
  fetch('/api/config/custom_step', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({value: v}) })
    .then(function(r){return r.json();}).then(function(d){
      if (!d.ok){ alert(d.msg); return; }
      customInit = false;                      // 允许按新默认值重新同步输入框
      if (!v){
        $('fixedStep').value = '';
        document.querySelector('input[name=mode][value=random]').checked = true;
        $('fixedStep').disabled = true;
      }
      fetchInfo();
    }).catch(function(){});
}
$('btnSaveStep').onclick = function(){
  var v = $('fixedStep').value.trim();
  if (!/^\d+(?:\s*[-~]\s*\d+)?$/.test(v)){ alert('步数格式：20000 或 12000-18000'); return; }
  saveDefaultStep(v);
};
$('defaultStep').onclick = function(e){
  if (e.target && e.target.id === 'btnClearStep') saveDefaultStep('');
};

setInterval(fetchInfo, 5000);
setInterval(fetchLog, 1000);
setInterval(fetchAccounts, 3000);
fetchInfo(); fetchLog(); fetchAccounts();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif path == "/api/info":
            self._json(build_info())
        elif path == "/api/accounts":
            self._json(build_accounts())
        elif path == "/api/log":
            with state_lock:
                self._json({
                    "running": run_state["running"],
                    "lines": list(run_state["lines"]),
                    "exit_code": run_state["exit_code"],
                    "last_result": run_state["last_result"],
                    "last_run_time": run_state["last_run_time"],
                    "success_count": run_state["success_count"],
                    "fail_count": run_state["fail_count"],
                })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            data = self._body()
            ok, msg = start_run(data.get("indexes") or [], data.get("fixed_step"), data.get("mode"))
            self._json({"started": ok, "msg": msg})
        elif path == "/api/config/custom_step":
            data = self._body()
            ok, msg = set_custom_step(data.get("value", ""))
            self._json({"ok": ok, "msg": msg,
                        "value": load_env().get("CUSTOM_STEP", "").strip()})
        elif path == "/api/stop":
            proc = proc_ref["proc"]
            if proc and proc.poll() is None:
                proc.terminate()
                self._json({"stopped": True})
            else:
                self._json({"stopped": False, "msg": "没有正在运行的任务"})
        elif path == "/api/accounts/add":
            data = self._body()
            ok, msg = add_account(str(data.get("user", "")).strip(),
                                  str(data.get("pwd", "")),
                                  str(data.get("remark", "") or "").strip())
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/accounts/remark":
            data = self._body()
            try:
                idx = int(data.get("idx"))
            except (TypeError, ValueError):
                idx = -1
            ok, msg = set_remark(idx, str(data.get("remark", "") or ""))
            self._json({"ok": ok, "msg": msg})
        elif path == "/api/accounts/delete":
            data = self._body()
            try:
                idx = int(data.get("idx"))
            except (TypeError, ValueError):
                idx = -1
            ok, msg = delete_account(idx)
            self._json({"ok": ok, "msg": msg})
        else:
            self._json({"error": "not found"}, 404)


def pick_port(start=8000, tries=12):
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def main():
    port = pick_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d" % port
    print("=" * 46)
    print("  mimotion 图形化控制台已启动")
    print("  本地地址: %s" % url)
    print("  按 Ctrl+C 或关闭本窗口停止服务")
    print("=" * 46)
    # 今日已刷概况：从历史记录读取，避免重复刷
    today_map = today_records_by_user()
    if today_map:
        print("今日已刷记录：")
        for u, rec in today_map.items():
            print("  - %s %s 步（%s）" % (rec.get("remark") or rec.get("masked") or u,
                                          rec.get("step", "?"), rec.get("time", "")[11:16]))
    else:
        print("今日还没有刷步数记录")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
