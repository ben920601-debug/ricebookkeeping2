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

app = FastAPI(title="飯糰小幫手 ｜ 雙 Tag 智慧核銷終極完全體")

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
        print("🔥 [DATABASE] Firestore 智慧核心連線就位！", flush=True)
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
    payer_name: str = Field(description="付出款項還錢的人名字或UID。若自稱我請填寫『發話者』")
    receiver_name: str = Field(description="收到款項拿回錢的人名字或UID。若自稱我請填寫『發話者』")
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
def send_line_reply(target_id: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
    except Exception as e:
        print(f"❌ LINE 推播失敗: {e}", flush=True)

# ==========================================
# 🌐 4. Webhook 核心流動（雙 Tag 勾稽核心）
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

    # 📥 A. 讀取或初始化群組狀態機
    current_mode = "normal"
    active_code = ""
    
    if is_group:
        group_doc_ref = db.collection("groups").document(target_id)
        group_snap = group_doc_ref.get()
        if group_snap.exists:
            g_data = group_snap.to_dict()
            current_mode = g_data.get("state", "normal")
            active_code = g_data.get("active_order_code", "")
        else:
            group_doc_ref.set({"group_id": target_id, "state": "normal", "created_at": datetime.utcnow()})

    # ====================================================
    # 🚨 🛡️ 【全時段被動 Tag 閘門】群組內未被 Tag 助理，一律原地秒阻斷
    # ====================================================
    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    
    # 檢查有沒有 Tag 訊號
    if mention and mention.mentionees:
        is_bot_tagged = True
    if any(kw in user_text for kw in ["@飯糰", "飯糰"]):
        is_bot_tagged = True
        
    if is_group and not is_bot_tagged:
        return 

    # ====================================================
    # 🎯 🛠️ 【Python 邊緣代打層：雙 Tag 勾稽與核銷意圖攔截】
    # ====================================================
    is_settle_intent = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    
    if is_group and is_settle_intent:
        # 1. 自動把群組模式切換為結算模式 (settle)
        current_mode = "settle"
        db.collection("groups").document(target_id).update({"state": "settle"})
        
        # 2. 智慧提取訊息內含的所有真實 LINE user_id (排除機器人自己)
        tagged_user_ids = []
        if mention and mention.mentionees:
            for m in mention.mentionees:
                u_id = getattr(m, "user_id", None)
                if u_id and u_id != creator_id:  # 排除發話者自己
                    tagged_user_ids.append(u_id)
        
        # 3. 從內文中精準挖出金額
        amount_match = re.search(r'\d+', user_text)
        settle_amount = int(amount_match.group()) if amount_match else 0
        
        if settle_amount > 0:
            final_payer = None
            final_receiver = None
            
            # 🚀 規則 A：偵測到 2 個以上的有效群組成員 Tag 訊號
            if len(tagged_user_ids) >= 2:
                final_payer = tagged_user_ids[0]      # 第一個被 Tag 的人是還錢者
                final_receiver = tagged_user_ids[1]   # 第二個被 Tag 的人是收錢者
            
            # 🚀 規則 B：少於 2 個 Tag ➡️ 預設為【被 Tag 的那個人】給【發話者（你本人）】
            elif len(tagged_user_ids) == 1:
                final_payer = tagged_user_ids[0]      # 被 Tag 的人付錢
                final_receiver = creator_id           # 發話者（我本人）收錢
                
            if final_payer and final_receiver and final_payer != final_receiver:
                # 🔒 全面寫入真實 LINE ID 進入資料庫，保證前後端對帳永不失聯！
                db.collection("groups").document(target_id).collection("settlements").document().set({
                    "payer_name": final_payer,      # 寫入真實還錢者 LINE ID
                    "receiver_name": final_receiver,  # 寫入真實收錢者 LINE ID
                    "amount": settle_amount,
                    "order_code_ref": active_code if active_code else "日常平帳",
                    "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"👌 收到！已切換為【結算模式】並完成帳目核銷！\n💸 付款人：(LINE_ID: {final_payer[:8]}...)\n📥 收款人：(LINE_ID: {final_receiver[:8]}...)\n💰 金額：${settle_amount} 元 已成功落庫！")
                return
            else:
                send_line_reply(target_id, "⚠️ 核銷失敗！請至少 Tag 一位成員並輸入正確的還款金額。")
                return

    # 清洗內文送往 Gemini
    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()

    # 🛑 全域敏感字防線
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text:
            send_line_reply(target_id, "🤖 飯糰助理為純財務系統，請勿探討敏感議題喔！")
            return

    # ====================================================
    # 🧠 🧠 🟨 第二層：Gemini 核心大腦（處理開閉團與普通記帳）
    # ====================================================
    try:
        prompt = f"""
        你是一個高效的財務助理「飯糰小幫手」。目前位於【{root_collection}】環境，模式為【{current_mode}】。
        請分析使用者訊息：『{user_text}』
        
        【分流任務】：
        1. 判定 intent (record, order_start, order_end, order_item, chat)。
        2. 包含開團、團購開始，為 order_start。
        3. 包含截止、結單、團購結束，為 order_end。
        4. 純輸入金額項目（如：早餐 200），為 "record"。
        """

        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
        ).parsed

        # 1. 常態模式普通記帳 (record) ➡️ Payer 改抓真實 LINE ID
        if result.intent == "record":
            if result.records:
                for rec in result.records:
                    if rec.amount > 0:
                        db.collection(root_collection).document(target_id).collection("expenses").document().set({
                            "type": rec.record_type, 
                            "amount": rec.amount, 
                            "item": rec.item, 
                            "category": rec.category,
                            "timestamp": datetime.utcnow(), 
                            "created_by_name": creator_id  # 🎯 修正：全面改抓發話者真實 LINE ID
                        })
                send_line_reply(target_id, "👌 收到！已成功幫您記錄這筆花費至雲端後台。")

        # 2. 開團模式 (order_start)
        elif result.intent == "order_start" and is_group:
            code_str = str(random.randint(1000, 9999))
            db.collection("groups").document(target_id).update({"state": "order", "active_order_code": code_str, "order_items_temp": []})
            send_line_reply(target_id, f"🚀 【飯團團購模式・正式啟動】\n🔢 本團結算編號：#{code_str}\n👉 請大家點單時同樣記得「@飯糰 品項 金額」叫單喔！")

        # 3. 點餐品項蒐集 (order_item) ➡️ Buyer 改抓真實 LINE ID
        elif result.intent == "order_item" and current_mode == "order" and is_group:
            if result.order_items:
                g_ref = db.collection("groups").document(target_id)
                temp_items = g_ref.get().to_dict().get("order_items_temp", [])
                for item in result.order_items:
                    # 🎯 修正：不論自稱是誰，一律強制抓取發話者的真實 LINE ID 存入
                    temp_items.append({
                        "buyer": creator_id, 
                        "item": item.item_name, 
                        "price": item.price, 
                        "timestamp": datetime.utcnow().isoformat()
                    })
                g_ref.update({"order_items_temp": temp_items})
                send_line_reply(target_id, "📝 收到！已幫您掛載點單品項。")

        # 4. 截止結單 (order_end) ➡️ 自動恢復正常常態模式
        elif result.intent == "order_end" and current_mode == "order" and is_group:
            g_ref = db.collection("groups").document(target_id)
            g_data = g_ref.get().to_dict()
            temp_items = g_data.get("order_items_temp", [])
            
            if temp_items:
                code_str = g_data.get("active_order_code", str(random.randint(1000, 9999)))
                total_amt = sum(i["price"] for i in temp_items)
                
                # 正式寫入資料庫 orders 封存 (買單墊款人直接鎖定為結單者的真實 LINE ID)
                g_ref.collection("orders").document(f"{datetime.now().strftime('%Y%m%d')}_{code_str}").set({
                    "order_date": datetime.now().strftime("%Y-%m-%d"), 
                    "order_code": code_str, 
                    "total_amount": total_amt,
                    "master_payer_name": creator_id,  # 🎯 修正：墊款人綁定真實 LINE ID
                    "items": temp_items, 
                    "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"🏁 【團購截止 ｜ 單號 #{code_str}】\n💰 總金額：${total_amt} 元\n💳 墊款買單人：(LINE_ID: {creator_id[:8]}...)\n\n🤖 數據已安全封存！群組已「恢復正常常態模式」。")
            else:
                send_line_reply(target_id, "🛑 因無人叫單，本團已直接關閉，群組已「恢復正常常態模式」。")
                
            # 🎯 截止收單後，狀態機強制恢復成 normal 常態模式
            g_ref.update({"state": "normal", "order_items_temp": []})

        # 5. 手動關閉結算模式，回歸常態
        elif "結算結束" in user_text and current_mode == "settle" and is_group:
            db.collection("groups").document(target_id).update({"state": "normal"})
            send_line_reply(target_id, "🔓 結算完畢！群組已「恢復正常常態模式」。")

        # 6. 簡單閒聊
        elif result.intent == "chat" and result.ai_reply:
            send_line_reply(target_id, f"🤖 {result.ai_reply}")

    except Exception as e:
        print(f"🧠 大腦解析異常: {e}")

@app.get("/")
def health_check(): 
    return {"status": "double_tag_settle_active", "version": "v6.5-SaaS-LineIDLocked"}
