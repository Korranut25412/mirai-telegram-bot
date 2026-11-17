"""บอท Telegram คุยกับ Claude ผ่าน Claude Code CLI (claude -p) บนเครื่องนี้ ใช้แพ็กเกจ Claude เดิม

รัน:  python bot.py              (ดู README.md)
ทดสอบ Claude อย่างเดียว ไม่ต่อ Telegram:  python bot.py --selftest "คำถาม"
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from common import (BASE_DIR, Telegram, TelegramError, allowed_chat_ids, env, load_env,
                    save_report, setup_console)

STATE_FILE = BASE_DIR / "state.json"
INBOX = BASE_DIR / "inbox"
REPORTS = BASE_DIR / "reports"

HELP = """👓 มิไร · คำสั่ง
พิมพ์อะไรก็ได้ = คุยกับมิไร (จำบทสนทนาต่อเนื่อง)
/new - เริ่มบทสนทนาใหม่
/daily - สรุปข่าวเช้าเดี๋ยวนี้เลย
/vocab - ศัพท์ที่เคยได้ล่าสุด (พิมพ์ "ทวนอังกฤษ" ให้มิไรถามทวน)
/files - ดูรายงานที่เก็บไว้
/get <ชื่อไฟล์> - ส่งรายงานเป็นไฟล์
/status - สถานะบอท
/id - ดู chat id
ส่งไฟล์/รูปมาได้ (ใส่คำสั่งใน caption)"""

log = logging.getLogger("bot")


# ---------------------------------------------------------------- state
class State:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            self.data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key, value):
        with self.lock:
            if value is None:
                self.data.pop(key, None)
            else:
                self.data[key] = value
            STATE_FILE.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- Claude
def today_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d (%A)")


def read_prompt_file(name: str) -> str:
    p = BASE_DIR / name
    return p.read_text(encoding="utf-8").replace("{date}", today_str()) if p.exists() else ""


def find_claude() -> str:
    exe = env("CLAUDE_BIN") or shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if not exe:
        raise SystemExit("หา claude ไม่เจอ ให้ใส่ path ใน CLAUDE_BIN ในไฟล์ .env")
    return exe


DEFAULT_TOOLS = ("WebSearch,WebFetch,Read,Glob,Grep,Skill,mcp__yfinance,mcp__context7,"
                 "Bash(agent-reach:*),Bash(markitdown:*)")


def mcp_config() -> str | None:
    """เลือกเฉพาะ MCP ที่บอทใช้ (BOT_MCP_SERVERS) จาก ~/.claude.json ตัวที่ต้องเปิดโปรแกรมก่อน (jupyter/anki) ไม่โหลด"""
    names = [n.strip() for n in (env("BOT_MCP_SERVERS") or "yfinance,context7").split(",") if n.strip()]
    try:
        servers = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8")).get("mcpServers", {})
    except (OSError, ValueError):
        return None
    picked = {n: servers[n] for n in names if n in servers}
    return json.dumps({"mcpServers": picked}) if picked else None


def run_claude(prompt: str, session_id: str | None = None) -> tuple[str, str | None, bool]:
    """คืน (ข้อความตอบ, session_id ใหม่, สำเร็จไหม)"""
    cmd = [find_claude(), "-p",
           "--output-format", "json",
           "--permission-mode", "dontAsk",
           "--allowedTools", env("ALLOWED_TOOLS") or DEFAULT_TOOLS,
           "--append-system-prompt", read_prompt_file("system_prompt.md")]
    mcp = mcp_config()
    cmd += ["--strict-mcp-config"] + (["--mcp-config", mcp] if mcp else [])
    for d in (env("EXTRA_READ_DIRS") or "").split(";"):
        if d.strip():
            cmd += ["--add-dir", d.strip()]
    if env("CLAUDE_MODEL"):
        cmd += ["--model", env("CLAUDE_MODEL")]
    if session_id:
        cmd += ["--resume", session_id]

    timeout = int(env("CLAUDE_TIMEOUT", "900"))
    # ตัดตัวแปรของ Claude/Anthropic ทิ้ง: ให้ใช้ login แพ็กเกจของ CLI เสมอ (ไม่เผลอใช้ API key ที่คิดเงินแยก)
    child_env = {k: v for k, v in os.environ.items()
                 if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))
                 or k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")}
    log.info("claude ← %s", prompt[:200].replace("\n", " "))
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=workdir(), env=child_env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return f"⏱️ Claude ใช้เวลานานเกิน {timeout} วินาที เลยยกเลิก ลองถามให้แคบลง", session_id, False

    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        err = (p.stderr or p.stdout or "").strip()[-1500:]
        if session_id and "session" in err.lower():
            log.warning("resume ไม่ได้ เริ่มบทสนทนาใหม่: %s", err[:200])
            return run_claude(prompt, None)
        log.warning("claude exit=%s: %s", p.returncode, err[-300:].replace("\n", " "))
        return f"❌ เรียก Claude ไม่สำเร็จ (exit {p.returncode})\n{err}", session_id, False

    text = out.get("result") or ""
    ok = not out.get("is_error") and out.get("subtype") == "success"
    log.info("claude → ok=%s len=%d cost=%s", ok, len(text), out.get("total_cost_usd"))
    if not ok:
        log.warning("claude error subtype=%s: %s", out.get("subtype"), text[:300].replace("\n", " "))
    if not ok and "authenticate" in text.lower():
        return "🔑 Claude CLI บนคอมยังไม่ได้ล็อกอิน/หมดอายุ ให้เปิด PowerShell แล้วรัน: claude auth login", session_id, False
    if not ok and "limit" in text.lower():
        return f"⏳ โควตา Claude เต็มแล้วค่ะ — {text}\nส่งใหม่หลังเวลานั้นนะคะ", session_id, False
    if not ok:
        if session_id and not text:
            return run_claude(prompt, None)
        text = f"❌ Claude ตอบผิดพลาด ({out.get('subtype')})\n{text}"
    return text, out.get("session_id") or session_id, ok


def workdir() -> Path:
    d = Path(env("CLAUDE_WORKDIR") or (BASE_DIR / "workspace"))
    d.mkdir(parents=True, exist_ok=True)
    return d


INLINE_WORD = re.compile(r"\*\*([A-Za-z][A-Za-z' -]{1,40})\*\*\s*\(([^()\n]*[฀-๿][^()\n]*)\)")


def log_vocab(text: str) -> int:
    """เก็บศัพท์ที่แทรกแบบ **word** (ไทย) + บรรทัด 📘 / ✏️ ลง vocab.md ไว้ทวน
    (Claude อ่านไฟล์นี้ตอนผู้ใช้พิมพ์ 'ทวนอังกฤษ') ข้ามคำที่เคยเก็บแล้ว"""
    f = workdir() / "vocab.md"
    old = f.read_text(encoding="utf-8").lower() if f.exists() else ""
    lines, seen = [], set()
    for w, th in INLINE_WORD.findall(text):
        key = w.strip().lower()
        if key not in seen and f"📘 {key} " not in old:
            seen.add(key)
            lines.append(f"📘 {w.strip()} = {th.strip()}")
    lines += [l.strip() for l in text.splitlines() if l.strip().startswith(("📘", "✏️", "✏"))
              and l.strip().lower() not in old]
    if lines:
        head = f"## {dt.date.today().isoformat()}"
        # หัววันที่ใส่ครั้งเดียวต่อวัน (ไฟล์ต่อท้ายเรียงตามเวลา หัววันนี้มีแล้ว = อยู่ท้ายไฟล์)
        prefix = "" if head in old else f"\n{head}\n"
        with open(workdir() / "vocab.md", "a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(lines) + "\n")
    return len(lines)


def quoted_with_links(m: dict) -> str:
    """ข้อความที่ผู้ใช้กดตอบกลับ + ใส่ลิงก์ที่ซ่อนอยู่ (text_link เช่นในข่าวเช้า) กลับเข้าไปตรงตำแหน่งเดิม"""
    text = m.get("text") or m.get("caption") or ""
    ents = m.get("entities") or m.get("caption_entities") or []
    links = sorted(((e["offset"] + e["length"], e["url"]) for e in ents if e.get("type") == "text_link"), reverse=True)
    u = text.encode("utf-16-le")  # offset ของ Telegram นับเป็นหน่วย UTF-16
    for end, url in links:
        u = u[:end * 2] + f" ⟨ลิงก์อ้างอิง: {url}⟩".encode("utf-16-le") + u[end * 2:]
    return u.decode("utf-16-le").strip()


# ---------------------------------------------------------------- bot
class Bot:
    def __init__(self):
        self.tg = Telegram(env("TELEGRAM_BOT_TOKEN"))
        self.allowed = allowed_chat_ids()
        self.state = State()
        self.jobs: queue.Queue = queue.Queue()
        self.busy = False
        self.started = time.time()
        self.max_parts = int(env("MAX_MESSAGE_PARTS", "6"))

    # ----- งานในคิว
    def worker(self):
        while True:
            job = self.jobs.get()
            self.busy = True
            try:
                job()
            except Exception as e:
                log.exception("job พัง")
            finally:
                self.busy = False

    def typing(self, chat_id: int, stop: threading.Event):
        while not stop.is_set():
            try:
                self.tg.call("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass
            stop.wait(4.5)

    def ask(self, chat_id: int, prompt: str, reply_to: int | None = None):
        def job():
            stop = threading.Event()
            threading.Thread(target=self.typing, args=(chat_id, stop), daemon=True).start()
            try:
                key = f"session:{chat_id}"
                text, sid, ok = run_claude(prompt, self.state.get(key))
                if ok and sid:
                    self.state.set(key, sid)
                if ok:
                    log_vocab(text)
            finally:
                stop.set()
            self.tg.send_long(chat_id, text, self.max_parts, file_name="reply", reply_to=reply_to)
        self.enqueue(chat_id, job)

    def daily(self, chat_id: int):
        def job():
            stop = threading.Event()
            threading.Thread(target=self.typing, args=(chat_id, stop), daemon=True).start()
            try:
                text, _, ok = run_claude(read_prompt_file("daily_prompt.md"))  # ไม่ปนกับแชทปกติ
            finally:
                stop.set()
            if ok:
                log_vocab(text)
                path = save_report(text, "daily")
                log.info("บันทึกสรุปเช้า %s", path.name)
            header = f"☀️ Morning briefing {dt.date.today():%d/%m/%Y}\n\n"
            self.tg.send_long(chat_id, header + text, self.max_parts, file_name="daily")
        self.enqueue(chat_id, job)

    def enqueue(self, chat_id: int, job):
        waiting = self.jobs.qsize() + (1 if self.busy else 0)
        if waiting:
            self.tg.send_text(chat_id, f"⏳ รอคิวอยู่ {waiting} งาน")
        self.jobs.put(job)

    # ----- ตั้งเวลา
    def scheduler(self):
        t = env("DAILY_TIME", "07:00")
        if not t or not self.allowed:
            log.info("ไม่ได้ตั้งเวลาสรุปเช้า")
            return
        hh, mm = (int(x) for x in t.split(":"))
        catchup = float(env("DAILY_CATCHUP_HOURS", "4"))
        log.info("ตั้งสรุปเช้าไว้ %s น. ทุกวัน", t)
        while True:
            now = dt.datetime.now()
            due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            today = now.date().isoformat()
            if due <= now < due + dt.timedelta(hours=catchup) and self.state.get("last_daily") != today:
                self.state.set("last_daily", today)
                log.info("ถึงเวลาสรุปเช้า")
                self.daily(self.allowed[0])
            time.sleep(20)

    # ----- รับข้อความ
    def handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()

        if not self.allowed:  # โหมดตั้งค่า: บอก chat id อย่างเดียว ไม่เรียก Claude
            self.tg.send_text(chat_id, f"chat id ของคุณคือ {chat_id}\nใส่ใน .env เป็น ALLOWED_CHAT_IDS={chat_id} แล้วรันบอทใหม่")
            log.info("โหมดตั้งค่า: chat id %s", chat_id)
            return
        if chat_id not in self.allowed:
            log.warning("ไม่อนุญาต chat id %s (%s)", chat_id, msg.get("from", {}).get("username"))
            return

        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        arg = text[len(text.split()[0]):].strip() if cmd else ""

        if cmd in ("/start", "/help"):
            self.tg.send_text(chat_id, HELP)
        elif cmd == "/id":
            self.tg.send_text(chat_id, f"chat id: {chat_id}")
        elif cmd == "/new":
            self.state.set(f"session:{chat_id}", None)
            self.tg.send_text(chat_id, "🆕 เริ่มบทสนทนาใหม่แล้ว")
        elif cmd == "/daily":
            self.daily(chat_id)
        elif cmd == "/status":
            up = dt.timedelta(seconds=int(time.time() - self.started))
            self.tg.send_text(chat_id, f"ทำงานมา {up}\nกำลังทำงาน: {'ใช่' if self.busy else 'ว่าง'} · คิว {self.jobs.qsize()}\n"
                                       f"สรุปเช้า: {env('DAILY_TIME', '07:00') or 'ปิด'} · ส่งล่าสุด {self.state.get('last_daily', '-')}")
        elif cmd == "/vocab":
            f = workdir() / "vocab.md"
            words = [l for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("📘")] if f.exists() else []
            self.tg.send_long(chat_id, "\n".join(words[-15:]) or "ยังไม่มีศัพท์ที่เก็บไว้")
        elif cmd == "/files":
            files = sorted(REPORTS.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:15] if REPORTS.exists() else []
            self.tg.send_text(chat_id, "\n".join(f"`{p.name}`" for p in files) or "ยังไม่มีรายงาน")
        elif cmd == "/get":
            p = (REPORTS / Path(arg).name) if arg else None
            if p and p.is_file():
                self.tg.send_document(chat_id, p)
            else:
                self.tg.send_text(chat_id, "ไม่เจอไฟล์ ใช้ /files ดูชื่อก่อน")
        elif cmd:
            self.tg.send_text(chat_id, "ไม่รู้จักคำสั่งนี้\n\n" + HELP)
        else:
            prompt = text
            att = msg.get("document") or (msg.get("photo") or [None])[-1]
            if att:
                path = self.tg.download_file(att["file_id"], INBOX, (msg.get("document") or {}).get("file_name") or "photo.jpg")
                prompt = f"ผู้ใช้ส่งไฟล์มา: {path}\n(อ่านด้วย Read tool)\n\n{text or 'ช่วยอ่านและสรุปไฟล์นี้'}"
            quoted = quoted_with_links(msg.get("reply_to_message") or {})
            if prompt and quoted:
                prompt = f"[พี่กดตอบกลับข้อความนี้]\n{quoted[:4000]}\n[จบข้อความที่อ้าง]\n\n{prompt}"
            if prompt:
                self.ask(chat_id, prompt, reply_to=msg["message_id"])

    def run(self):
        me = self.tg.call("getMe")
        log.info("บอท @%s พร้อม · อนุญาต chat id: %s", me["username"], self.allowed or "ยังไม่ตั้ง (โหมดตั้งค่า)")
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": c, "description": d} for c, d in
                [("new", "เริ่มบทสนทนาใหม่"), ("daily", "สรุปข่าวเช้าเดี๋ยวนี้"), ("vocab", "ศัพท์ล่าสุด"), ("files", "รายงานที่เก็บไว้"),
                 ("get", "ส่งรายงานเป็นไฟล์"), ("status", "สถานะบอท"), ("help", "วิธีใช้")]])
        except TelegramError:
            pass
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.scheduler, daemon=True).start()

        offset = self.state.get("offset")
        while True:
            try:
                updates = self.tg.call("getUpdates", http_timeout=70, timeout=50, offset=offset,
                                       allowed_updates=["message"])
            except TelegramError as e:
                if e.code == 409:
                    log.error("มีบอทตัวอื่นใช้ token นี้รันอยู่ (409) ปิดตัวอื่นก่อน")
                    time.sleep(30)
                else:
                    log.error("getUpdates: %s", e)
                    time.sleep(5)
                continue
            except Exception as e:  # เน็ตหลุด / เครื่องเพิ่งตื่น
                log.warning("เชื่อมต่อไม่ได้: %s", e)
                time.sleep(10)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                self.state.set("offset", offset)
                if "message" in u:
                    try:
                        self.handle(u["message"])
                    except Exception:
                        log.exception("จัดการข้อความพัง")


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


if __name__ == "__main__":
    setup_console()
    load_env()
    setup_logging()
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        q = " ".join(sys.argv[2:]) or "What is the latest Nvidia stock news today? Answer briefly."
        text, sid, ok = run_claude(q)
        print(f"\n--- ok={ok} session={sid} len={len(text)} ---\n{text}")
        sys.exit(0 if ok else 1)
    Bot().run()
