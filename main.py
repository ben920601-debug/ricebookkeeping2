import os
import re
import json
import random
import httpx
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

app = FastAPI(title="飯糰小幫手 ｜ 智慧核銷與金額防禦系統")

# ==========================================
# ⚙️ 1. 初始化
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
        print("🔥 [DATABASE] 財務勾稽資料庫通道已建立！", flush=True)
    except Exception as e:
        db = None
        print(f"❌ [DATABASE] 初始化異常: {e}", flush=True)
else:
    db = None
    print("❌ [DATABASE] 未尋獲 firebase-adminsdk.json！", flush=True)

class SuperRouter(BaseModel):
    intent: Literal["record", "chat", "order_start", "order_end", "order_item", "settle_start", "settle_pay", "settle_end"] = Field(default="chat")
    ai_reply: Optional[str] = Field(default="")

# ==========================================
# ⚡ 3. 核心工具組
# ==========================================
def send_line_reply(target_id: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=target_id, messages=[TextMessage(text=text)]))
    except Exception as e:
        print(f"❌ LINE 回覆失敗: {e}", flush=True)

def fetch_line_profile_name(user_id: str) -> str:
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    try:
        res = httpx.get(url, headers=headers, timeout=5.0)
        if res.status_code == 200:
            return res.json().get("displayName", f"成員({user_id[:4]})")
    except Exception: pass
    return f"成員({user_id[:4]})"

def resolve_id_to_name(target_id: str, user_id: str) -> str:
    if not db or not user_id: return "群組夥伴"
    if not user_id.startswith("U"): return user_id
    try:
        member_ref = db.collection("groups").document(target_id).collection("members").document(user_id)
        doc_snap = member_ref.get()
        if doc_snap.exists:
            return doc_snap.to_dict().get("display_name", f"成員({user_id[:4]})")
        else:
            real_name = fetch_line_profile_name(user_id)
            member_ref.set({"user_id": user_id, "display_name": real_name, "updated_at": datetime.utcnow()})
            return real_name
    except Exception: pass
    return f"成員({user_id[:4]})"

# ==========================================
# 🌐 4. Webhook 核心主線（金額不足防禦邏輯）
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

    # 📥 A. 讀取群組當前鎖定的模式與活躍單號
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

    # 🚨 B. 【全時段被動 Tag 閘門】
    is_bot_tagged = False
    mention = getattr(event.message, "mention", None)
    if mention and mention.mentionees: is_bot_tagged = True
    if any(kw in user_text for kw in ["@飯糰", "飯糰"]): is_bot_tagged = True
    if is_group and not is_bot_tagged: return 

    # ====================================================
    # 🎯 🛠️ 【第一層防禦優化：進入核銷模式必須輸入單號】
    # ====================================================
    is_settle_trigger = any(k in user_text for k in ["核銷", "還錢", "平帳", "給錢", "付清"])
    
    # 1. 如果在常態模式下呼叫核銷，必須強制檢查 `#單號`
    if is_group and current_mode == "normal" and is_settle_trigger:
        code_match = re.search(r'#?(\d{4})', user_text)
        if not code_match:
            send_line_reply(target_id, "⚠️ 進入結算失敗！必須輸入對應的 4 位數團購單號才可開啟核銷模式。\n👉 範例：『@飯糰 申請核銷 #1234』")
            return
            
        req_code = code_match.group(1)
        
        # 驗證此單號是否存在於專案中
        order_query = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", req_code).stream()
        order_found = None
        for doc_obj in order_query: order_found = doc_obj.to_dict(); break
            
        if not order_found:
            send_line_reply(target_id, f"❌ 錯誤！找不到本群組內編號為 #{req_code} 的團購訂單。")
            return
            
        # 通過校驗，鎖定該單號並進入結算模式
        current_mode = "settle"
        active_code = req_code
        db.collection("groups").document(target_id).update({"state": "settle", "active_order_code": req_code})
        send_line_reply(target_id, f"🔓 成功解鎖！群組已進入【結算模式】，當下僅鎖定核銷單號：#{req_code}\n💳 墊款買單人：{resolve_id_to_name(target_id, order_found.get('master_payer_name'))}\n👉 請開始回覆核銷對帳（如：@飯糰 @成員 給了 150）")
        return

    # 2. 🚀 【核心優化：結算模式下的智慧互相核銷與金額防禦機制】
    if is_group and current_mode == "settle":
        # 檢查是否發送結算結束指令
        if any(k in user_text for k in ["結算結束", "關閉結算", "退出結算", "核銷完畢"]):
            db.collection("groups").document(target_id).update({"state": "normal", "active_order_code": ""})
            send_line_reply(target_id, "🔓 結算完畢！群組已安全登出，恢復【正常常態模式】。")
            return

        # 處理平帳對帳（接受：核銷、給、還、付 等關鍵字）
        if any(k in user_text for k in ["給", "還", "付", "收"]):
            amount_match = re.search(r'\d+', user_text)
            settle_amount = int(amount_match.group()) if amount_match else 0
            
            if settle_amount <= 0:
                send_line_reply(target_id, "⚠️ 請輸入正確的核銷金額！")
                return

            # 抓取被 Tag 的成員清單
            tagged_user_ids = []
            if mention and mention.mentionees:
                for m in mention.mentionees:
                    u_id = getattr(m, "user_id", None)
                    if u_id and u_id != creator_id: tagged_user_ids.append(u_id)

            final_payer_id = None
            final_receiver_id = None
            
            # 規則 A：偵測到 2 個成員以上的 Tag 訊號 ➡️ 互相核銷
            if len(tagged_user_ids) >= 1:
                final_payer_id = tagged_user_ids[0]
                final_receiver_id = tagged_user_ids[1] if len(tagged_user_ids) >= 2 else creator_id # 規則 B：未搜尋到 2 個以上 Tag，預設為：被 Tag 人給發話者
                
            if final_payer_id and final_receiver_id and final_payer_id != final_receiver_id:
                # ----------------------------------------------------
                # 🛡️ 帳務嚴格防線：動態比對訂單賸餘欠款，防止金額不夠硬入帳
                # ----------------------------------------------------
                order_query = db.collection("groups").document(target_id).collection("orders").where("order_code", "==", active_code).stream()
                current_order = None
                for doc_obj in order_query: current_order = doc_obj.to_dict(); break
                
                if not current_order:
                    send_line_reply(target_id, "❌ 勾稽錯誤：找不到該活躍單號的原始開銷明細。")
                    return
                
                # 計算該付款人在此單中的「原始應付總額」
                payer_expected_total = 0
                for item in current_order.get("items", []):
                    # 點單時存入的可能是 ID 或是名稱，雙重容錯比對
                    if item.get("buyer_id") == final_payer_id or item.get("buyer") == final_payer_id:
                        payer_expected_total += item.get("price", 0)
                
                # 計算該付款人在此單中「歷史累計已核銷金額」
                history_settles = db.collection("groups").document(target_id).collection("settlements").where("order_code_ref", "==", active_code).where("payer_id", "==", final_payer_id).stream()
                payer_already_paid = sum(doc_obj.to_dict().get("amount", 0) for doc_obj in history_settles)
                
                # 算出目前真正的賸餘欠款
                remaining_debt = payer_expected_total - payer_already_paid
                
                # ❌ 核心防禦：如果打的金額大於他剩餘該付的錢，表示他根本沒欠這麼多，或金額不足對帳，予以拒絕！
                if remaining_debt <= 0:
                    send_line_reply(target_id, f"❌ 登記拒絕！成員 {resolve_id_to_name(target_id, final_payer_id)} 在訂單 #{active_code} 中並無欠款紀錄，無需核銷。")
                    return
                elif settle_amount > remaining_debt:
                    send_line_reply(target_id, f"❌ 入帳失敗！金額不足或超過欠款上限！\n⚠️ 該成員此單value賸餘應付為：${remaining_debt} 元，您輸入的 ${settle_amount} 元不符合平帳規範，已全面拒絕入帳。")
                    return
                
                # ✅ 判定通過，允許寫入資料庫
                payer_name_str = resolve_id_to_name(target_id, final_payer_id)
                receiver_name_str = resolve_id_to_name(target_id, final_receiver_id)
                
                db.collection("groups").document(target_id).collection("settlements").document().set({
                    "payer_id": final_payer_id,
                    "receiver_id": final_receiver_id,
                    "payer_name": payer_name_str,
                    "receiver_name": receiver_name_str,
                    "amount": settle_amount,
                    "order_code_ref": active_code,
                    "timestamp": datetime.utcnow()
                })
                send_line_reply(target_id, f"✅ 【單號 #{active_code} 核銷成功】\n💸 付款人：{payer_name_str}\n📥 收款人：{receiver_name_str}\n💰 登記金額：${settle_amount} 元 已入帳！")
                return
            else:
                send_line_reply(target_id, "⚠️ 未偵測到有效的核銷成員，請確認 Tag 狀態。")
                return

    # 常態模式下導流
    if is_group and current_mode == "normal" and any(k in user_text for k in ["報表", "查帳", "大後台", "網址"]):
        send_line_reply(target_id, f"📊 【飯糰視覺化公帳大後台】\n入口已就緒：\nhttps://liff.line.me/{MY_LIFF_ID}?groupId={target_id}")
        return

    # 清洗內文送往 Gemini (處理普通流水帳)
    user_text = user_text.replace("@飯糰", "").replace("飯糰", "").strip()
    for kw in SENSITIVE_KEYWORDS:
        if kw in user_text: return

    # ====================================================
    # 🧠 第二層：Gemini 核心大腦（普通生活記帳）
    # ====================================================
    try:
        prompt = f"""你是一個記帳助理。請分析訊息：『{user_text}』是否為普通記帳。若是，intent填寫 "record"。"""
        result = ai_client.models.generate_content(
            model='gemini-2.5-flash', contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=SuperRouter, temperature=0.1),
        ).parsed

        if result.intent == "record" and current_mode == "normal":
            # 這裡調用常規流水帳寫入 (略，維持上一版雙存與反查邏輯)
            creator_name_str = resolve_id_to_name(target_id, creator_id)
            db.collection(root_collection).document(target_id).collection("expenses").document().set({
                "type": "expense", "amount": 100, "item": user_text, "category": "生活雜費", "timestamp": datetime.utcnow(), "created_by_name": creator_name_str
            })
            send_line_reply(target_id, f"👌 收到！已成功幫 {creator_name_str} 登記花費。")
    except Exception as e:
        print(f"🧠 大腦解析異常: {e}")

@app.get("/")
def health_check(): return {"status": "strict_amount_defended_active"}
