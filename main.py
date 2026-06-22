"""LINE Bot: 催事グループの「実績」報告を製造表の催事ブロック実績へ反映。

頻繁にグループが変わっても壊れない設計:
- 会場は「グループ名」から自動判定（GROUP_VENUE_MAP の明示指定があれば優先）。
- 会場を確信を持って特定できない／新規グループ → 書かずにログ（シート無傷）。
- サイレント運用（LINEへ返信しない）。
"""
import datetime as dt
import logging
import os

try:
    from zoneinfo import ZoneInfo
    JST = ZoneInfo("Asia/Tokyo")
except Exception:  # pragma: no cover
    JST = dt.timezone(dt.timedelta(hours=9))

from fastapi import FastAPI, HTTPException, Request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
from linebot.v3.webhooks import GroupSource, MessageEvent, TextMessageContent

from parser import parse, starts_with_jisseki
from sheets import get_client_from_env

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

# 任意の明示マップ: "groupid1:大丸東京,groupid2:JR秋葉原"
GROUP_VENUE_MAP = {}
for pair in os.environ.get("GROUP_VENUE_MAP", "").split(","):
    if ":" in pair:
        k, v = pair.split(":", 1)
        if k.strip():
            GROUP_VENUE_MAP[k.strip()] = v.strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seizou-jisseki-bot")

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

app = FastAPI()


@app.get("/")
def root():
    return {"status": "ok", "service": "dw_line_seizou_jisseki_bot"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return {"status": "ok"}


def _group_name(group_id: str) -> str:
    try:
        with ApiClient(configuration) as api:
            return MessagingApi(api).get_group_summary(group_id).group_name or ""
    except Exception:
        log.exception("get_group_summary failed for %s", group_id)
        return ""


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event: MessageEvent):
    text = event.message.text or ""
    if not starts_with_jisseki(text):
        return

    src = event.source
    group_id = getattr(src, "group_id", None) if isinstance(src, GroupSource) else None
    if not group_id:
        log.info("not a group message; skip")
        return

    # 会場ヒント = 明示マップ優先、無ければグループ名
    hint = GROUP_VENUE_MAP.get(group_id) or _group_name(group_id)
    if not hint:
        log.warning("venue hint empty for group_id=%s; skip (sheet untouched)", group_id)
        return

    products = parse(text)
    if not products:
        log.info("no products parsed")
        return

    date = dt.datetime.fromtimestamp(event.timestamp / 1000, tz=JST).date()
    try:
        res = get_client_from_env().write_jisseki(hint, products, date)
    except Exception:
        log.exception("sheets write failed")
        return

    if res.get("ok"):
        log.info("wrote venue=%s tab=%s col=%s products=%s",
                 res.get("venue"), res.get("tab"), res.get("col"), res.get("wrote"))
    else:
        log.warning("skip group_id=%s hint=%s: %s", group_id, hint, res.get("reason"))
