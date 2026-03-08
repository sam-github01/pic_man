import streamlit as st
from datetime import datetime
from google import genai
from PIL import Image
import io
import pandas as pd
import uuid

# Google 相關套件
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# -----------------------------------------------------------------------------
# 0. API 金鑰與 Google 雲端連線設定
# -----------------------------------------------------------------------------
try:
    GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
    DRIVE_FOLDER_ID = st.secrets["DRIVE_FOLDER_ID"]
    SHEET_ID = st.secrets["SHEET_ID"]
    # 取得 GCP 服務帳號憑證
    gcp_creds_info = st.secrets["gcp_service_account"]
except KeyError as e:
    st.error(f"⚠️ 缺少環境變數：{e}。請確保已在 Streamlit Secrets 中設定。")
    st.stop()

# 授權 Google Sheets 與 Google Drive
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
credentials = Credentials.from_service_account_info(gcp_creds_info, scopes=SCOPES)
gc = gspread.authorize(credentials)
drive_service = build('drive', 'v3', credentials=credentials)

# 初始化 Google GenAI 用戶端
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 連線到指定的 Google 試算表 (第一個工作表)
try:
    sheet = gc.open_by_key(SHEET_ID).sheet1
except Exception as e:
    st.error(f"無法連線到 Google Sheets，請確認共用權限與 Sheet ID 是否正確。錯誤：{e}")
    st.stop()

# -----------------------------------------------------------------------------
# 1. 輔助功能定義：資料庫操作 (透過 Google Sheets & Drive)
# -----------------------------------------------------------------------------
def get_base_categories():
    try:
        df = pd.read_csv("product.csv")
        return df['類別'].dropna().unique().tolist()
    except Exception:
        return []

base_categories = get_base_categories()

def delete_image(image_id, file_id):
    # 1. 從 Google Drive 刪除實體圖片
    if file_id:
        try:
            drive_service.files().delete(fileId=file_id).execute()
        except Exception as e:
            st.error(f"無法從 Drive 刪除檔案：{e}")
            
    # 2. 從 Google Sheets 刪除該筆紀錄
    try:
        cell = sheet.find(image_id)
        if cell:
            sheet.delete_rows(cell.row)
    except Exception:
        pass # 若找不到紀錄則略過

def update_image_info(image_id, new_category, new_description):
    try:
        cell = sheet.find(image_id)
        if cell:
            # 更新分類 (第3欄) 與 說明 (第4欄)
            sheet.update_cell(cell.row, 3, new_category)
            sheet.update_cell(cell.row, 4, new_description)
    except Exception as e:
        st.error(f"更新試算表失敗：{e}")

def get_total_images_count():
    # 扣除第一行的標題列
    records = sheet.get_all_values()
    return max(0, len(records) - 1)

# -----------------------------------------------------------------------------
# 2. 畫面設定、Session State 與對話框
# -----------------------------------------------------------------------------
st.set_page_config(page_title="雲端 AI 圖庫", layout="centered", initial_sidebar_state="collapsed")
st.title("☁️ Google 隨身 AI 圖庫")

total_images = get_total_images_count()
st.info(f"📁 目前 Google 雲端圖庫中共有 **{total_images}** 張圖片")
st.caption("⚡ Powered by Google Workspace & Gemini-2.5-Flash")

if 'selected_category' not in st.session_state:
    st.session_state.selected_category = None
if 'uploader_key' not in st.session_state:
    st.session_state.uploader_key = str(uuid.uuid4())
if 'search_keyword' not in st.session_state:
    st.session_state.search_keyword = ""

def clear_search_state():
    st.session_state.selected_category = None
    st.session_state.search_keyword = ""

@st.dialog("✏️ 編輯圖片資訊")
def edit_image_dialog(img_id, current_cat, current_desc, base_cats, db_cats):
    all_options = sorted(list(set(base_cats + [c for c in db_cats if c != "待分類"])))
    if current_cat not in all_options and current_cat != "待分類":
        all_options.insert(0, current_cat)
    all_options.append("➕ 自行輸入新分類...")
    
    try: default_idx = all_options.index(current_cat)
    except ValueError: default_idx = 0
        
    new_cat = st.selectbox("修改分類", options=all_options, index=default_idx)
    if new_cat == "➕ 自行輸入新分類...":
        final_cat = st.text_input("輸入新分類名稱：")
    else:
        final_cat = new_cat
        
    new_desc = st.text_area("修改說明 (最多 200 字)", value=current_desc, max_chars=200, height=150)
    
    if st.button("💾 儲存修改", width="stretch"):
        if final_cat.strip():
            update_image_info(img_id, final_cat.strip(), new_desc.strip())
            st.rerun()
        else:
            st.error("分類名稱不可為空！")

tab_upload, tab_gallery = st.tabs(["📤 多檔上傳區", "🔍 智慧查詢區"])

# -----------------------------------------------------------------------------
# 3. 框架一：上傳圖片至 Google Drive 與 AI 分析
# -----------------------------------------------------------------------------
with tab_upload:
    st.header("新增圖片")
    
    if "upload_success_msg" in st.session_state:
        st.success(st.session_state.upload_success_msg)
        del st.session_state.upload_success_msg
    
    uploaded_files = st.file_uploader("選擇圖片", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True, key=st.session_state.uploader_key)
    
    if st.button("💾 自動分類並上傳至雲端", width="stretch"):
        if uploaded_files:
            my_bar = st.progress(0, text="同步至 Google 雲端硬碟中，請稍候...")
            total_files = len(uploaded_files)
            categories_str = ", ".join(base_categories) if base_categories else "無預設分類"
            
            for i, file in enumerate(uploaded_files):
                bytes_data = file.getvalue()
                safe_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.name}"
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                img_id = str(uuid.uuid4()) # 產生資料庫唯一ID
                
                # 1. 上傳實體檔案至 Google Drive
                file_metadata = {'name': safe_filename, 'parents': [DRIVE_FOLDER_ID]}
                media = MediaIoBaseUpload(io.BytesIO(bytes_data), mimetype=file.type, resumable=True)
                
                try:
                    uploaded_file = drive_service.files().create(
                        body=file_metadata, media_body=media, fields='id'
                    ).execute()
                    file_id = uploaded_file.get('id')
                except Exception as e:
                    st.error(f"上傳 Drive 失敗：{e}")
                    continue
                
                # 2. 使用 Gemini 分析圖片
                img = Image.open(io.BytesIO(bytes_data))
                try:
                    prompt = f"""請客觀分析這張圖片的主要內容。
                    1. 分類：請優先從下列預設選項中挑選一個最適合的分類：
                    【{categories_str}】
                    如果以上選項都不適合，請自行創造一個最貼切的簡短分類名稱（限 5 個字以內）。
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
                        
                except Exception:
                    try:
                        fallback_prompt = "請擷取這張圖片中的所有可見文字。如果沒有文字，請用一句話簡短描述畫面內容。"
                        fallback_resp = ai_client.models.generate_content(model='gemini-2.5-flash', contents=[fallback_prompt, img])
                        ai_category = "待分類"
                        ai_description = f"【擷取文字】{fallback_resp.text.strip()}"
                        if len(ai_description) > 250: ai_description = ai_description[:247] + "..."
                    except Exception:
                        ai_category = "待分類"
                        ai_description = "AI 分析與文字擷取皆失敗，請手動確認。"
                
                # 3. 將資料寫入 Google Sheets
                try:
                    sheet.append_row([img_id, file.name, ai_category, ai_description, file_id, current_time])
                except Exception as e:
                    st.error(f"寫入試算表失敗：{e}")
                
                my_bar.progress((i + 1) / total_files, text=f"正在處理: {file.name} ({i+1}/{total_files})")
            
            my_bar.empty()
            st.session_state.upload_success_msg = f"✅ 成功上傳 {total_files} 張圖片！檔案已存入您的 Google Drive。"
            st.session_state.uploader_key = str(uuid.uuid4())
            st.rerun()
        else:
            st.warning("⚠️ 請先選擇要上傳的圖片。")

# -----------------------------------------------------------------------------
# 4. 框架二：向 Google Sheets 查詢與管理圖片
# -----------------------------------------------------------------------------
with tab_gallery:
    st.header("尋找與管理雲端圖片")
    
    # 讀取 Google Sheets 所有紀錄 (轉為 DataFrame 方便篩選)
    try:
        all_records = sheet.get_all_records()
        df_images = pd.DataFrame(all_records)
    except Exception:
        df_images = pd.DataFrame()
    
    # 取得資料庫中所有現有分類
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
    
    # 在 DataFrame 中進行篩選
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
            
        # 依上傳時間反轉排序 (最新的在最上面)
        filtered_df = filtered_df.iloc[::-1]
        
        results = filtered_df.to_dict('records')
        
        if len(results) > 0:
            st.success(f"共找到 {len(results)} 張圖片")
            for row in results:
                img_id = str(row.get("id"))
                fname = str(row.get("filename", "未命名"))
                cat = str(row.get("category", ""))
                desc = str(row.get("description", ""))
                file_id = str(row.get("file_id", ""))
                up_time = str(row.get("upload_time", ""))
                
                with st.container():
                    if file_id:
                        # 產生 Google Drive 圖片直連網址
                        direct_img_url = f"https://drive.google.com/uc?id={file_id}"
                        
                        try:
                            st.image(direct_img_url, width="stretch")
                        except Exception:
                            st.warning("圖片載入失敗，請確認 Drive 資料夾權限是否設為公開檢視。")
                        
                        st.markdown(f"**分類:** `{cat}`")
                        st.markdown(f"**分析/內容:** {desc}") 
                        st.caption(f"上傳時間: {up_time}")
                        
                        if cat == "待分類":
                            st.warning("請為這張圖片指定一個正確的分類：")
                            all_options = sorted(list(set(base_categories + [c for c in db_categories if c != "待分類"])))
                            all_options.append("➕ 自行輸入新分類...")
                            new_cat = st.selectbox("選擇分類", options=all_options, key=f"sel_{img_id}", label_visibility="collapsed")
                            
                            if new_cat == "➕ 自行輸入新分類...":
                                final_cat = st.text_input("輸入新分類名稱：", key=f"txt_{img_id}")
                            else:
                                final_cat = new_cat
                                
                            if st.button("💾 更新分類", key=f"update_{img_id}", width="stretch", type="primary"):
                                if final_cat:
                                    update_image_info(img_id, final_cat, desc)
                                    st.rerun()
                                else:
                                    st.error("分類名稱不可為空！")
                            st.write("---")
                        
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            st.link_button("⬇️ 開啟原圖", url=direct_img_url, use_container_width=True)
                        with col2:
                            if st.button("✏️ 修改", key=f"edit_{img_id}", width="stretch"):
                                edit_image_dialog(img_id, cat, desc, base_categories, db_categories)
                        with col3:
                            if st.button("🗑️ 刪除", key=f"delete_{img_id}", width="stretch"):
                                delete_image(img_id, file_id)
                                st.rerun()
                    else:
                        st.error("這筆資料遺失了 Google Drive 檔案 ID。")
                        if st.button("清理這筆無效紀錄", key=f"cleanup_{img_id}"):
                            delete_image(img_id, None)
                            st.rerun()
                st.divider()
        else:
            st.info("找不到符合條件的圖片。")
    else:
        st.info("💡 請點選上方「分類」或輸入「關鍵字」來尋找雲端圖片。")