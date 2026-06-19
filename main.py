import os
import json
import re
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks # 🚀 引入背景任務
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest, # 🚀 改用 PushMessage（主動推播）代替 Reply
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# Google GenAI & Firebase SDK
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, firestore

from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# 基礎環境變數
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 🚀 移除 4 秒超時限制，讓付費版 Gemini 有充裕的時間（哪怕 6 秒）把結構化 JSON 算得極度精準
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 🔥 Firebase Firestore 初始化
cred_path = "firebase-adminsdk.json"
if os.path.exists(cred_path):
    try:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print(f"🔥 [DATABASE LOG] Firestore 初始化成功！")
    except Exception as e:
        db = None
else:
    db = None

# ==========================================
# 📊 Pydantic 資料結構定義
# ==========================================
class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense", description="expense: 支出, income: 收入")
    amount: int = Field(default=0, description="金額")
    item: str = Field(default="", description="項目名稱")
    category: str = Field(default="生活雜費", description="分類")
    note: str = Field(default="", description="備註")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat_with_record", "chat", "analyze", "sensitive"] = Field(description="意圖分流")
    records: Optional[List[SingleRecord]] = Field(default_factory=list, description="收支明細陣列")
    ai_reply: Optional[str] = Field(default="", description="回應文字")

# ==========================================
# 🤖 核心大腦邏輯 (純同步運算)
# ==========================================

def analyze_with_gemini_sync(user_text: str) -> SuperRouter:
    prompt = f"""
    你是一個極簡現代風格的個人財務助理「飯糰小幫手」。請分析使用者的輸入：『{user_text}』
    請精準判斷收支並拆解存入 records 陣列，或進行日常極簡高情商聊天。
    """
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash', 
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SuperRouter,
            temperature=0.3
        ),
    )
    if response.parsed: return response.parsed
    return SuperRouter(**json.loads(response.text))


def analyze_with_python_fallback(user_text: str) -> SuperRouter:
    user_text_lower = user_text.lower().strip()
    if any(k in user_text_lower for k in ["查", "報表", "分析", "統計", "花多少", "結餘", "速報"]):
        return SuperRouter(intent="analyze")
        
    numbers_find = re.finditer(r'\d+', user_text)
    records = []
    try:
        for match in numbers_find:
            amount = int(match.group())
            start_pos = match.start()
            end_pos = match.end()
            prev_text = user_text[max(0, start_pos-5):start_pos].strip()
            next_text = user_text[end_pos:min(len(user_text), end_pos+5)].strip()
            clean_prev = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', prev_text).replace("花了", "").replace("吃了", "")
            clean_next = re.sub(r'[^\u4e00-\u9fa5a-zA-Z]', '', next_text).replace("元", "").replace("塊", "")
            item = clean_prev if (clean_prev and len(clean_prev) >= 2) else (clean_next if clean_next else "日常收支")
            r_type = "income" if any(k in user_text for k in ["薪水", "收入", "中獎", "賺", "薪資"]) else "expense"
            records.append(SingleRecord(record_type=r_type, amount=amount, item=item, category="生活雜費", note="⚠️ 備用大腦解析"))
            
        if records:
            is_pure_record = len(user_text) <= 10 and not any(k in user_text for k in ["今天", "昨天", "跟", "去", "哈哈", "了"])
            if is_pure_record: return SuperRouter(intent="record", records=records)
            else: return SuperRouter(intent="chat_with_record", records=records, ai_reply="⚠️ 系統繁忙中，已啟動安全確認機制。")
    except Exception: pass
    return SuperRouter(intent="chat", ai_reply="👌")

# ==========================================
# 💾 資料庫與速報邏輯
# ==========================================
def save_records_to_db(user_id: str, records: List[SingleRecord]):
    if db is None or not records: return False
    try:
        user_ref = db.collection("users").document(user_id)
        if not user_ref.get().exists:
            user_ref.set({"line_user_id": user_id, "created_at": datetime.utcnow()})
        batch = db.batch()
        for rec in records:
            if rec.amount <= 0: continue
            batch.set(user_ref.collection("expenses").document(), {
                "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category, "note": rec.note, "timestamp": datetime.utcnow()
            })
        batch.commit()
        return True
    except Exception: return False

def get_monthly_quick_summary(user_id: str) -> str:
    if db is None: return "📴 系統維護中"
    try:
        now = datetime.utcnow()
        start_of_month = datetime(now.year, now.month, 1)
        query = db.collection("users").document(user_id).collection("expenses").where("timestamp", ">=", start_of_month).stream()
        income_total = 0; expense_total = 0
        for doc in query:
            data = doc.to_dict(); amt = data.get("amount", 0)
            if data.get("type", "expense") == "income": income_total += amt
            else: expense_total += amt
        return f"📊 本月極簡速報\n📈 總收入：${income_total}\n📉 總支出：${expense_total}\n💰 淨結餘：${income_total - expense_total}"
    except Exception: return "⚠️ 查詢速報暫時失敗"

# ==========================================
# 🌐 執行緒安全 Webhook 入口
# ==========================================
PENDING_CONFIRMATIONS = {}

@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    """🚀 核心優化：0.1秒極速秒回 LINE 伺服器，將重度 AI 任務打包丟到背景非同步執行！"""
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    
    body = await request.body()
    body_str = body.decode("utf-8")
    
    # 💥 丟給 FastAPI 背景執行緒，立刻 return "OK" 切斷 LINE 的逾時倒數
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return "OK"


def handle_line_events_safe(body_str: str, signature: str):
    try: handler.handle(body_str, signature)
    except InvalidSignatureError: print("❌ LINE 簽章驗證失敗")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    """此處運作於背景執行緒，不受 LINE 5秒限制，可以好整以暇地等 Gemini 算完"""
    user_text = event.message.text.strip()
    user_id = event.source.user_id 
    reply_str = ""
    
    if user_id in PENDING_CONFIRMATIONS:
        if user_text in ["好", "要", "對", "確定", "可以", "好啊", "幫我記", "yes", "correct"]:
            saved_records = PENDING_CONFIRMATIONS.pop(user_id)
            db_success = save_records_to_db(user_id, saved_records)
            reply_str = "👌 已幫您安全記入帳本！" if db_success else "⚠️ 寫入失敗。"
        else:
            PENDING_CONFIRMATIONS.pop(user_id, None) 
            reply_str = "❌ 抱歉抓錯了！已取消該筆紀錄，請重新輸入。✍️"
    else:
        try:
            # 即使付費版冷啟動花了 4.5 秒，這裡也能穩穩等它回傳，絕不觸發超時！
            result = analyze_with_gemini_sync(user_text)
            print("🤖 [LINE LOG] Gemini 運算成功！")
        except Exception as gemini_err:
            print(f"❌ Gemini 真的罷工 ({gemini_err}) ➡️ 切換至 Python 代打！")
            result = analyze_with_python_fallback(user_text)
        
        # 意圖處理
        if result.intent == "record" and result.records:
            db_success = save_records_to_db(user_id, result.records)
            if db_success:
                lines = [f"{'➕ 收入' if r.record_type == 'income' else '➖ 支出'} ${r.amount} ({r.item})" for r in result.records]
                reply_str = "✅ 記帳成功！\n" + "\n".join(lines)
            else: reply_str = "⚠️ 備份延遲。"
        elif result.intent == "chat_with_record" and result.records:
            PENDING_CONFIRMATIONS[user_id] = result.records
            reply_str = f"{result.ai_reply}\n\n🔍 偵測到以下可能的花費：\n"
            for rec in result.records:
                reply_str += f"・[{'收入' if rec.record_type == 'income' else '支出'}] ${rec.amount} 元 的 {rec.item}\n"
            reply_str += "\n👉 正確請回覆「好」，若錯誤請回覆任意文字來重新輸入。"
        elif result.intent == "analyze": reply_str = get_monthly_quick_summary(user_id)
        elif result.intent == "chat" or result.intent == "sensitive": reply_str = result.ai_reply
        else: reply_str = "👌"

    # 🚀 關鍵優化：改用 Push Message 主動推播回使用者的手機上
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(to=user_id, messages=[TextMessage(text=reply_str)])
            )
    except Exception as e: 
        print(f"❌ 主動推播失敗: {e}")
