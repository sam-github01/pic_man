import streamlit as st
from datetime import datetime
from google import genai
from PIL import Image
import io
import pandas as pd
import uuid
import logging
import os
import requests
import base64

# Google Sheets 相關套件
import gspread
from google.oauth2.service_account import Credentials

# -----------------------------------------------------------------------------
# 0. 設定錯誤日誌 (Logging)
# -----------------------------------------------------------------------------
logging.basicConfig(
    filename='ErrorLog.txt',
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 1. API 金鑰與雲端連線設定
# -----------------------------------------------------------------------------
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    IMGBB_API_KEY = st.secrets["IMGBB_API_KEY"]
    SHEET_ID = st.secrets["SHEET_ID"]
    gcp_creds_info = st.secrets["gcp_service_account"]
except KeyError as e:
    st.error(f"⚠️ 缺少環境變數：{e}。請確保已在 Streamlit Secrets 中設定。")
    st.stop()

# 授權 Google Sheets 與防呆初始化
try:
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    credentials = Credentials.from_service_account_info(gcp_creds_info, scopes=SCOPES)
    gc = gspread.authorize(credentials)
    ai_client = genai.Client(api_key=GEMINI_API_KEY)
    sheet = gc.open_by_key(SHEET_ID).sheet1
    
    # 【新增：自動檢測與修復標題列機制】
    existing_data = sheet.get_all_values()
    expected_headers = ['id', 'filename', 'category', 'description', 'file_id', 'upload_time']
    
    if not existing_data:
        # 如果試算表是全空的，自動寫入標題列
        sheet.append_row(expected_headers)
    elif existing_data[0][0] != 'id':
        # 如果第一列開頭不是 'id' (代表剛剛上傳的圖片不小心變成了標題)
        # 程式會自動在最上方插入一行正確的標題列，把圖片資料往下擠
        sheet.insert_row(expected_headers, 1)

except Exception as e:
    logger.error("Google Sheets 授權或連線初始化失敗", exc_info=True)
    st.error("無法連線到 Google Sheets，詳細錯誤已記錄至 ErrorLog.txt")
    st.stop()

# -----------------------------------------------------------------------------
# 2. 輔助功能定義：圖床與資料庫操作
# -----------------------------------------------------------------------------
def get_base_categories():
    try:
        df = pd.read_csv("product.csv")
        return df['類別'].dropna().unique().tolist()
    except Exception as e:
        return []

base_categories = get_base_categories()

# 呼叫 ImgBB API 上傳圖片
def upload_to_imgbb(image_bytes, filename):
    url = "https://api.imgbb.com/1/upload"
    payload = {
        "key": IMGBB_API_KEY,
        "image": base64.b64encode(image_bytes).decode('utf-8'),
        "name": filename
    }
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        return response.json()['data']['url'] # 回傳圖片的直連網址
    else:
        raise Exception(f"ImgBB 上傳失敗: {response.text}")

def delete_image(image_id):
    # ImgBB 圖床免費用戶不支援直接刪除 API，因此我們只要從資料庫(Google Sheets)中移除紀錄，畫面就不會再顯示了
    try:
        cell = sheet.find(image_id)
        if cell:
            sheet.delete_rows(cell.row)
    except Exception as e:
        logger.error(f"從 Sheets 刪除紀錄失敗 (image_id: {image_id})", exc_info=True)

def update_image_info(image_id, new_category, new_description):
    try:
        cell = sheet.find(image_id)
        if cell:
            sheet.update_cell(cell.row, 3, new_category)
            sheet.update_cell(cell.row, 4, new_description)
    except Exception as e:
        logger.error(f"更新 Sheets 失敗 (image_id: {image_id})", exc_info=True)
        st.error("更新試算表失敗，請查看錯誤紀錄。")

def get_total_images_count():
    try:
        records = sheet.get_all_values()
        return max(0, len(records) - 1)
    except Exception as e:
        return 0

# -----------------------------------------------------------------------------
# 3. 畫面設定、Session State 與側邊欄 (下載 Log 功能)
# -----------------------------------------------------------------------------
st.set_page_config(page_title="雲端 AI 圖庫", layout="centered", initial_sidebar_state="collapsed")

with st.sidebar:
    st.header("🔧 系統管理")
    if os.path.exists("ErrorLog.txt"):
        with open("ErrorLog.txt", "r", encoding="utf-8") as f:
            log_data = f.read()
        st.download_button(label="📥 下載錯誤紀錄 (ErrorLog.txt)", data=log_data, file_name="ErrorLog.txt", mime="text/plain", type="primary")
        if st.button("🗑️ 清空錯誤紀錄"):
            open("ErrorLog.txt", "w").close()
            st.rerun()

st.title("☁️ 雲端 AI 智慧圖庫")
total_images = get_total_images_count()
st.info(f"📁 目前雲端圖庫中共有 **{total_images}** 張圖片")
st.caption("⚡ Powered by Google Sheets, ImgBB & Gemini")

if 'selected_category' not in st.session_state: st.session_state.selected_category = None
if 'uploader_key' not in st.session_state: st.session_state.uploader_key = str(uuid.uuid4())
if 'search_keyword' not in st.session_state: st.session_state.search_keyword = ""

def clear_search_state():
    st.session_state.selected_category = None
    st.session_state.search_keyword = ""

@st.dialog("✏️ 編輯圖片資訊")
def edit_image_dialog(img_id, current_cat, current_desc, base_cats, db_cats):
    all_options = sorted(list(set(base_cats + [c for c in db_cats if c != "待分類"])))
    if current_cat not in all_options and current_cat != "待分類": all_options.insert(0, current_cat)
    all_options.append("➕ 自行輸入新分類...")
    
    try: default_idx = all_options.index(current_cat)
    except ValueError: default_idx = 0
        
    new_cat = st.selectbox("修改分類", options=all_options, index=default_idx)
    final_cat = st.text_input("輸入新分類名稱：") if new_cat == "➕ 自行輸入新分類..." else new_cat
    new_desc = st.text_area("修改說明 (最多 200 字)", value=current_desc, max_chars=200, height=150)
    
    if st.button("💾 儲存修改", width="stretch"):
        if final_cat.strip():
            update_image_info(img_id, final_cat.strip(), new_desc.strip())
            st.rerun()
        else: st.error("分類名稱不可為空！")

tab_upload, tab_gallery = st.tabs(["📤 多檔上傳區", "🔍 智慧查詢區"])

# -----------------------------------------------------------------------------
# 4. 框架一：上傳圖片至 ImgBB 與 AI 分析
# -----------------------------------------------------------------------------
with tab_upload:
    st.header("新增圖片")
    if "upload_success_msg" in st.session_state:
        st.success(st.session_state.upload_success_msg)
        del st.session_state.upload_success_msg
    
    uploaded_files = st.file_uploader("選擇圖片", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True, key=st.session_state.uploader_key)
    
    if st.button("💾 自動分類並上傳至雲端", width="stretch"):
        if uploaded_files:
            my_bar = st.progress(0, text="同步至雲端圖床與資料庫中，請稍候...")
            total_files = len(uploaded_files)
            categories_str = ", ".join(base_categories) if base_categories else "無預設分類"
            
            for i, file in enumerate(uploaded_files):
                bytes_data = file.getvalue()
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                img_id = str(uuid.uuid4())
                
                # 1. 上傳至 ImgBB 圖床，取得公開網址
                try:
                    img_url = upload_to_imgbb(bytes_data, file.name)
                except Exception as e:
                    logger.error(f"ImgBB 上傳失敗: {e}", exc_info=True)
                    st.error(f"檔案 {file.name} 上傳圖床失敗，已記錄至錯誤日誌。")
                    continue
                
                # 2. 使用 Gemini 分析圖片
                img = Image.open(io.BytesIO(bytes_data))
                try:
                    prompt = f"""請客觀分析這張圖片的主要內容。
                    1. 分類：請優先從下列預設選項中挑選一個最適合的分類：
                    【{categories_str}】
                    如果都不適合，請自行創造一個最貼切的簡短分類名稱（限 5 個字以內）。
                    2. 說明：請用 200 個字以內的正體中文，詳盡總結圖片的核心視覺元素、主要文字訊息或主打用途。
                    請嚴格依照以下格式回覆：
                    分類：[填入分類]
                    說明：[填入說明]"""
                    response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=[prompt, img])
                    result_text = response.text.strip()
                    
                    ai_category, ai_description = "", ""
                    for line in result_text.split('\n'):
                        if line.startswith('分類：'): ai_category = line.replace('分類：', '').strip()
                        elif line.startswith('說明：'): ai_description = line.replace('說明：', '').strip()
                    if not ai_category or not ai_description: raise ValueError("格式不符")
                        
                except Exception as e:
                    try:
                        fallback_prompt = "請擷取這張圖片中的所有可見文字。如果沒有文字，請用一句話簡短描述畫面內容。"
                        fallback_resp = ai_client.models.generate_content(model='gemini-2.5-flash', contents=[fallback_prompt, img])
                        ai_category = "待分類"
                        ai_description = f"【擷取文字】{fallback_resp.text.strip()}"
                        if len(ai_description) > 250: ai_description = ai_description[:247] + "..."
                    except Exception as inner_e:
                        ai_category = "待分類"
                        ai_description = "AI 分析與文字擷取皆失敗，請手動確認。"
                
                # 3. 將資料寫入 Google Sheets (注意第五欄改存 img_url)
                try:
                    sheet.append_row([img_id, file.name, ai_category, ai_description, img_url, current_time])
                except Exception as e:
                    logger.error("寫入 Google Sheets 失敗", exc_info=True)
                    st.error(f"圖片分析完成，但寫入試算表失敗。")
                
                my_bar.progress((i + 1) / total_files, text=f"正在處理: {file.name} ({i+1}/{total_files})")
            
            my_bar.empty()
            st.session_state.upload_success_msg = f"✅ 成功上傳 {total_files} 張圖片！檔案已存入雲端圖床與 Google Sheets。"
            st.session_state.uploader_key = str(uuid.uuid4())
            st.rerun()
        else:
            st.warning("⚠️ 請先選擇要上傳的圖片。")

# -----------------------------------------------------------------------------
# 5. 框架二：向 Google Sheets 查詢與管理圖片
# -----------------------------------------------------------------------------
with tab_gallery:
    st.header("尋找與管理雲端圖片")
    
    try:
        all_records = sheet.get_all_records()
        df_images = pd.DataFrame(all_records)
    except Exception as e:
        df_images = pd.DataFrame()
    
    db_categories = []
    if not df_images.empty and 'category' in df_images.columns:
        db_categories = df_images['category'].dropna().unique().tolist()
    
    st.write("🏷️ **快速分類查詢：**")
    cols = st.columns(4)
    for i, cat in enumerate(db_categories):
        col_idx = i % 4
        btn_label = f"🔴 {cat}" if cat == "待分類" else cat
        if cols[col_idx].button(btn_label, width="stretch"):
            st.session_state.selected_category = cat

    st.caption(f"目前檢視分類：**{st.session_state.selected_category if st.session_state.selected_category else '無'}**")
    st.divider()
    
    st.text_input("🔍 或輸入關鍵字進一步查詢：", key="search_keyword")
    st.button("🧹 清除查詢", width="stretch", on_click=clear_search_state)
    
    if not df_images.empty and (st.session_state.selected_category or st.session_state.search_keyword):
        filtered_df = df_images.copy()
        
        if st.session_state.selected_category:
            filtered_df = filtered_df[filtered_df['category'] == st.session_state.selected_category]
            
        if st.session_state.search_keyword:
            kw = str(st.session_state.search_keyword).lower()
            filtered_df = filtered_df[
                filtered_df['filename'].astype(str).str.lower().str.contains(kw) |
                filtered_df['description'].astype(str).str.lower().str.contains(kw)
            ]
            
        filtered_df = filtered_df.iloc[::-1]
        results = filtered_df.to_dict('records')
        
        if len(results) > 0:
            st.success(f"共找到 {len(results)} 張圖片")
            for row in results:
                img_id = str(row.get("id"))
                fname = str(row.get("filename", "未命名"))
                cat = str(row.get("category", ""))
                desc = str(row.get("description", ""))
                # 第五欄現在直接是 img_url 網址了
                img_url = str(row.get("file_id", "")) # 為了相容原本您的試算表標題列 file_id
                up_time = str(row.get("upload_time", ""))
                
                with st.container():
                    if img_url and img_url.startswith("http"):
                        try:
                            st.image(img_url, width="stretch")
                        except Exception:
                            st.warning("圖片載入失敗，圖床可能暫時無回應。")
                        
                        st.markdown(f"**分類:** `{cat}`")
                        st.markdown(f"**分析/內容:** {desc}") 
                        st.caption(f"上傳時間: {up_time}")
                        
                        if cat == "待分類":
                            st.warning("請為這張圖片指定一個正確的分類：")
                            all_options = sorted(list(set(base_categories + [c for c in db_categories if c != "待分類"])))
                            all_options.append("➕ 自行輸入新分類...")
                            new_cat = st.selectbox("選擇分類", options=all_options, key=f"sel_{img_id}", label_visibility="collapsed")
                            final_cat = st.text_input("輸入新分類名稱：", key=f"txt_{img_id}") if new_cat == "➕ 自行輸入新分類..." else new_cat
                                
                            if st.button("💾 更新分類", key=f"update_{img_id}", width="stretch", type="primary"):
                                if final_cat:
                                    update_image_info(img_id, final_cat, desc)
                                    st.rerun()
                                else: st.error("分類名稱不可為空！")
                            st.write("---")
                        
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.link_button("⬇️ 開啟原圖", url=img_url, use_container_width=True)
                        with col2:
                            if st.button("✏️ 修改", key=f"edit_{img_id}", width="stretch"):
                                edit_image_dialog(img_id, cat, desc, base_categories, db_categories)
                        with col3:
                            if st.button("🗑️ 刪除", key=f"delete_{img_id}", width="stretch"):
                                delete_image(img_id)
                                st.rerun()
                    else:
                        st.error("這筆資料遺失了圖片網址。")
                        if st.button("清理這筆無效紀錄", key=f"cleanup_{img_id}"):
                            delete_image(img_id)
                            st.rerun()
                st.divider()
        else:
            st.info("找不到符合條件的圖片。")
    else:
        st.info("💡 請點選上方「分類」或輸入「關鍵字」來尋找雲端圖片。")
