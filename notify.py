"""ส่งข้อความหรือไฟล์รายงานเข้า Telegram ของเรา (ใช้จากสคริปต์อื่น หรือให้ Claude บนคอมเรียก)

  python notify.py -m "ข้อความ"
  python notify.py รายงาน.md            # ส่งเนื้อหาเป็นข้อความ (ยาวก็แบ่งให้เอง)
  python notify.py รายงาน.md --file     # ส่งเป็นไฟล์แนบ
"""
import argparse
from pathlib import Path

from common import Telegram, allowed_chat_ids, env, load_env, setup_console

setup_console()
load_env()
ap = argparse.ArgumentParser()
ap.add_argument("path", nargs="?", help="ไฟล์ที่จะส่ง")
ap.add_argument("-m", "--message", help="ข้อความที่จะส่ง")
ap.add_argument("--file", action="store_true", help="ส่งเป็นไฟล์แนบแทนข้อความ")
a = ap.parse_args()
if not a.path and not a.message:
    ap.error("ต้องมี path หรือ -m")

ids = allowed_chat_ids()
if not ids:
    raise SystemExit("ยังไม่ได้ตั้ง ALLOWED_CHAT_IDS ใน .env")
tg = Telegram(env("TELEGRAM_BOT_TOKEN"))
chat = ids[0]

if a.message:
    tg.send_long(chat, a.message, file_name="note")
if a.path:
    p = Path(a.path)
    if a.file or p.suffix.lower() not in (".md", ".txt"):
        tg.send_document(chat, p)
    else:
        tg.send_long(chat, p.read_text(encoding="utf-8-sig"), file_name=p.stem)
print("ส่งแล้ว")
