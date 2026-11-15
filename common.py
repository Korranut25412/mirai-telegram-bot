"""ของใช้ร่วมกันระหว่าง bot.py กับ notify.py: อ่าน .env, เรียก Telegram API, แบ่งข้อความยาว"""
from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TG_LIMIT = 4096          # ข้อจำกัดของ Telegram ต่อ 1 ข้อความ
CHUNK_SIZE = 3800        # เผื่อที่ให้แท็ก HTML / เลขหน้า


def load_env(path: Path = BASE_DIR / ".env") -> None:
    """อ่าน .env แบบง่าย (KEY=VALUE) ไม่ทับค่าที่มีใน environment อยู่แล้ว"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), val)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def allowed_chat_ids() -> list[int]:
    raw = env("ALLOWED_CHAT_IDS")
    return [int(x) for x in re.split(r"[,\s]+", raw) if x.strip().lstrip("-").isdigit()]


class TelegramError(Exception):
    def __init__(self, code: int, desc: str):
        super().__init__(f"{code}: {desc}")
        self.code = code
        self.desc = desc


class Telegram:
    def __init__(self, token: str):
        if not token or ":" not in token:
            raise SystemExit("ยังไม่ได้ใส่ TELEGRAM_BOT_TOKEN ในไฟล์ .env (ดู README.md)")
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"

    # ---------- ระดับล่าง ----------
    def call(self, method: str, http_timeout: float = 30, **params):
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(self.base + method, data=data,
                                     headers={"Content-Type": "application/json"})
        return self._send(req, http_timeout)

    def _send(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode())
            except Exception:
                raise TelegramError(e.code, str(e)) from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", 0), body.get("description", ""))
        return body["result"]

    def call_multipart(self, method: str, fields: dict, file_field: str, file_path: Path,
                       timeout: float = 120):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            if v is None:
                continue
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        fname = file_path.name.replace('"', "")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
                      f'filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n').encode())
        parts.append(file_path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.base + method, data=b"".join(parts),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return self._send(req, timeout)

    # ---------- ระดับใช้งาน ----------
    def send_text(self, chat_id: int, text: str, reply_to: int | None = None):
        """ส่ง 1 ข้อความ (ต้องไม่เกิน limit) แปลง Markdown เป็น HTML ถ้าพังให้ส่งแบบข้อความล้วน"""
        try:
            return self.call("sendMessage", chat_id=chat_id, text=md_to_html(text), parse_mode="HTML",
                             link_preview_options={"is_disabled": True},
                             reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None)
        except TelegramError as e:
            if e.code != 400:
                raise
            return self.call("sendMessage", chat_id=chat_id, text=text[:TG_LIMIT],
                             link_preview_options={"is_disabled": True})

    def send_long(self, chat_id: int, text: str, max_parts: int = 6, file_name: str | None = None,
                  reply_to: int | None = None):
        """ส่งข้อความยาว: แบ่งเป็นหลายข้อความ ถ้าเกิน max_parts ส่งส่วนแรก + แนบไฟล์ .md ฉบับเต็ม"""
        text = text.strip() or "(ไม่มีข้อความ)"
        chunks = split_text(text, CHUNK_SIZE)
        if len(chunks) <= max_parts:
            for i, c in enumerate(chunks):
                tag = f"\n\n[{i + 1}/{len(chunks)}]" if len(chunks) > 1 else ""
                self.send_text(chat_id, c + tag, reply_to=reply_to if i == 0 else None)
                time.sleep(0.4)
            return
        self.send_text(chat_id, chunks[0] + f"\n\n… (ยาว {len(text):,} ตัวอักษร ฉบับเต็มอยู่ในไฟล์แนบ)",
                       reply_to=reply_to)
        path = save_report(text, file_name or "reply")
        self.send_document(chat_id, path)

    def send_document(self, chat_id: int, path: Path, caption: str | None = None):
        return self.call_multipart("sendDocument", {"chat_id": chat_id, "caption": caption},
                                   "document", Path(path))

    def download_file(self, file_id: str, dest_dir: Path, name: str | None = None) -> Path:
        info = self.call("getFile", file_id=file_id)
        remote = info["file_path"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|]', "_", name or Path(remote).name)
        dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
        with urllib.request.urlopen(self.file_base + remote, timeout=120) as r:
            dest.write_bytes(r.read())
        return dest


def split_text(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """แบ่งข้อความให้แต่ละท่อน <= size โดยพยายามตัดที่ย่อหน้า > บรรทัด > ช่องว่าง"""
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        if cut < size // 2:
            cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


def md_to_html(text: str) -> str:
    """แปลง Markdown ที่ Claude ชอบใช้ ให้เป็น HTML ที่ Telegram รองรับ (แบบพอใช้)"""
    blocks = []

    def keep(s):
        blocks.append(s)
        return f"\x00{len(blocks) - 1}\x00"

    # code block ```...```
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: keep(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"), text)
    # ลิงก์ [ข้อความ](url)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
                  lambda m: keep(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'),
                  text)
    text = html.escape(text, quote=False)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.M)          # หัวข้อ
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)                           # ตัวหนา
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)  # ตัวเอียง
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)                     # bullet
    text = re.sub(r"^---+$", "──────────", text, flags=re.M)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def save_report(text: str, prefix: str = "report") -> Path:
    d = BASE_DIR / "reports"
    d.mkdir(exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\s]+', "-", prefix).strip("-") or "report"
    p = d / f"{safe}-{time.strftime('%Y-%m-%d-%H%M%S')}.md"
    p.write_text(text, encoding="utf-8")
    return p


def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
