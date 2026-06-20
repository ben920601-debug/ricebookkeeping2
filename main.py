import os
import re
import json
import random
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal, List, Optional

# LINE SDK v3
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
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

app = FastAPI(title="飯糰小幫手 ｜ 強制 Tag 控場無消耗版")

# ==========================================
# ⚙️ 1. 核心客戶端與資料庫初始化
# ==========================================
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

MY_LIFF_ID = "2010446205-W1G1WDQQ" 

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
ai_client = genai.Client(api_key=GEMINI_API_KEY)

if os.path.exists("firebase-adminsdk.json"):
    try:
        cred = credentials.Certificate("firebase-adminsdk.json")
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("🔥 [DATABASE] 成功建立 Firestore 雙軌安全連線通道！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 連線初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 嚴重錯誤：根目錄未尋獲 firebase-adminsdk.json！", flush=True)

# ==========================================
# 🛡️ 2. 全域型別定義
# ==========================================
SENSITIVE_KEYWORDS = ["政治", "選舉", "總統", "政黨", "戰爭", "吸毒", "賭博", "情色", "自殺", "殺人"]

class SingleRecord(BaseModel):
    record_type: Literal["expense", "income"] = Field(default="expense")
    amount: int = Field(default=0)
    item: str = Field(default="")
    category: str = Field(default="生活雜費")
    note: str = Field(default="")

class SingleSettlement(BaseModel):
    payer_name: str = Field(description="付出款項還錢的人名字。若自稱我請填寫『發話者』")
    receiver_name: str = Field(description="收到款項拿回錢的人名字。若自稱我請填寫『發話者』")
    amount: int = Field(default=0)

class GroupOrderItem(BaseModel):
    buyer_name: str = Field(description="點餐者名字。若自稱我或空白請寫『發話者』")
    item_name: str = Field(description="品項名稱")
    price: int = Field(description="單價金額")

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "analyze", "sensitive", "settlement", "order_item", "order_end", "order_start", "settle_start", "settle_pay", "settle_query"] = Field(
        description="核心意圖分流"
    )
    records: Optional[List[SingleRecord]] = Field(default_factory=list)
    settlement: Optional[SingleSettlement] = Field(default=None)
    order_items: Optional[List[GroupOrderItem]] = Field(default_factory=list)
    target_payer: Optional[str] = Field(default="")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def get_cached_nickname(target_id: str, user_id: str, is_group: bool) -> str:
    if not db: return "記帳夥伴"
    if not is_group: return "個人帳本主"
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists: return doc_snap.to_dict().get("display_name", "群組夥伴")
    except Exception: pass
    return "群組夥伴"

def send_line_reply(target_id: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
    except Exception as e:
        print(f"❌ LINE 推播失敗: {e}", flush=True)

# ==========================================
# 🌐 4. Webhook 核心流動（強制 Tag 監禁閘門）
# ==========================================
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    if not signature: raise HTTPException(status_code=400, detail="Missing Signature")
    body = await request.body()
    body_str = body.decode("utf-8")
    background_tasks.add_task(handle_line_events_safe, body_str, signature)
    return Response(content="OK", status_code=200)

def handle_line_events_safe(body_str: str, signature: str):
    try: line_handler.handle(body_str, signature)
    except InvalidSignatureError: pass

@line_handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    if not db: return

    user_text = event.message.text.strip()
    creator_id = event.source.user_id 
    is_group = event.source.type == "group"
    
    target_id = event.source.group_id if is_group else creator_id
    root_collection = "groups" if is_group else "users"

    # 📥 1. 讀取或初始化群組狀態機
    current_mode = "normal"
    active_code = ""
    master_payer_name = ""
    
    if is_group:
        group_doc_ref = db.collection("groups").document(target_id)
        group_snap = group_doc_ref.get()
        if group_snap.exists:
            g_data = group_snap.to_dict()
            current_mode = g_data.get("state", "normal")
            active_code = g_data.get("active_order_code", "")
            master_payer_name = g_data.get("master_payer", "")
        else:
            group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})

    # ====================================================
    # 🚨 🛡️ 【全時段 Tag 監禁閘門】群組環境下，沒被 Tag 一律秒速阻斷！
    # ====================================================
    if is_group:
        is_liff_tagged = False
        
        # 檢查 LINE 官方提供的實體 Mention 節點
        mention = getattr(event.message, "mention", None)
        if mention and mention.mentionees:
            is_liff_tagged = True
            
        # 檢查文字字串比對保底
        if any(kw in user_text for kw in ["@飯糰", "飯糰"]):
            is_liff_tagged = True
            
        # 🎯 鐵律：不管目前是什麼模式（常態/點單/結算），只要沒 Tag 訊號，通通視為閒聊雜訊，直接中斷！
        if not is_liff_tagged:
            return 

    # 清洗掉 Tag 關鍵字，還原純淨內文送給 Gemini 拆解
    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()

    # 🛑 敏感字防線
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            send_line_reply(target_id, "🤖 飯糰小幫手為純財務平帳系統，請勿探討敏感議題喔！")
            return

    # 系統固定硬格式回覆
    if user_text in ["使用說明", "怎麼用", "功能", "規定"]:
        instructions = (
            "📝 【飯糰小幫手 使用說明】\n"
            "⚠️ 群組內所有人發言必須「@飯糰」才會觸發助理！\n"
            "👉 記帳範例：『@飯糰 早餐 200』\n"
            "👉 開團範例：『@飯糰 開團』\n"
            "👉 結單範例：『@飯糰 結單』"
        )
        send_line_reply(target_id, instructions)
        return

    creator_name = get_cached_nickname(target_id, creator_id, is_group)

    # ----------------------------------------------------
    # 🧠 第二層防禦：Gemini 核心智慧解析線
    # ----------------------------------------------------
    try:
        prompt = f"""
        你是一個幽默、控場能力極強的記帳助理「飯糰小幫手」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
        請透視分析使用者訊息：『{user_text}』
        
        【分流任務說明】：
        1. 判定 intent (record, order_start, order_end, order_item, settle_start, settle_pay, settle_query, chat)。
        2. 如果包含開團、團購開始，為 order_start。
        3. 如果包含截止、結單、結束、團購結束，為 order_end。
        4. 如果只是純輸入金額項目（如：早餐 200），為 "record"。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
        ).parsed

        # A. 常態/點單模式下的普通 Tag 記帳 (record)
        if result.intent == "record":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, "amount": rec.amount, "item": rec.item, "category": rec.category,
                            "timestamp": datetime.utcnow(), "created_by_name": creator_name
                        })
                # 🪙 依照規定，記帳成功後，發送 LINE 訊息回覆「收到！」
                send_line_reply(target_id, f"👌 收到！已成功幫 {creator_name} 登記一筆花費至雲端後台。")

        # B. 開團模式 (order_start) ➡️ 必須被 Tag 才會走到這裡
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            db.collection("groups").document(target_id).update({"state": "order", "active_order_code": code_str, "order_items_temp": []})
            send_line_reply(target_id, f"🚀 【飯團團購模式・正式啟動】\n🔢 本團結算編號：#{code_str}\n👉 請大家點單時同樣記得「@飯糰 品項 金額」叫單喔！")

        # C. 點餐品項蒐集 (order_item) ➡️ 必須被 Tag 才會搜集
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                for item in result.order_items:
                    buyer = creator_name if not item.buyer_name or item.buyer_name == "發話者" else item.buyer_name.strip()
                    temp_items.append({"buyer": buyer, "item": item.item_name, "price": item.price, "timestamp": datetime.utcnow().isoformat()})
                g_ref.update({"order_items_temp": temp_items})
                send_line_reply(target_id, f"📝 收到！已幫 {creator_name} 掛載點單品項。")

        # D. 截止結單 (order_end) ➡️ 被 Tag 收到截止訊息，封存資料庫並「恢復正常模式」
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                m_payer = creator_name if not result.target_payer or result.target_payer == "發話者" else result.target_payer.strip()
                total_amt = sum(i["price"] for i in temp_items)
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                
                # 正式寫入資料庫 orders 封存
                g_ref.collection("orders").document(f"{datetime.now().strftime('%Y%m%d')}_{code_str}").set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), "order_code": code_str, "total_amount": total_amt,
                    "master_payer_name": m_payer, "items": temp_items, "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"🏁 【團購截止 ｜ 單號 #{code_str}】\n💰 總金額：${total_amt} 元\n💳 墊款買單：{m_payer}\n\n🤖 數據已安全封存！群組已「恢復正常常態模式」。")
            else:
                send_line_reply(target_id, "🛑 因無人叫單，本團已直接關閉，群組「恢復正常常態模式」。")
                
            # 🎯 核心修正：收單截止後，強制更新資料庫，將群組狀態機恢復成正常模式 (normal)
            g_ref.update({"state": "normal", "order_items_temp": []})

        # E. 啟動結算控制台 (settle_start)
        elif result.intent == "settle_start" and is_group:
            match_code = re.search(r'(\d{4})', user_text)
            if match_code:
                req_code = match_code.group(1)
                db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": req_code})
                send_line_reply(target_id, f"🔔 【結算模式已啟動 ｜ 單號 #{req_code}】\n🌐 網頁後台紅綠燈對帳報表已同步解鎖！核銷請輸入「@飯糰 結算結束」歸位。")

        # F. 登記付款核銷 (settle_pay)
        elif result.intent == "settle_pay" and current_mode == "settle" and is_group:
            if result.settlement:
                s = result.settlement
                p_name = creator_name if s.payer_name == "發話者" or not s.payer_name else s.payer_name.strip()
                r_name = master_payer_name if s.receiver_name == "發話者" or not s.receiver_name else s.receiver_name.strip()
                if p_name != r_name:
                    db.collection("groups").document(target_id).collection("settlements").document().set({
                        "payer_name": p_name, "receiver_name": r_name, "amount": s.amount, "order_code_ref": active_code, "timestamp": datetime.utcnow()
                    })
                send_line_reply(target_id, f"✅ 收到！已核銷登記 {p_name} 還給 {r_name} ${s.amount} 元。")

        # G. 結束結算模式
        elif "結算結束" in user_text and current_mode == "settle" and is_group:
            db.collection("groups").document(target_id).update({"state": "normal"})
            send_line_reply(target_id, "🔓 結算完畢！群組已「恢復正常常態模式」。")

    except Exception as e:
        print(f"🧠 大腦解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "strict_mention_mode_active", "version": "v6.0-SaaS-StrictTag"}
