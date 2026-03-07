import streamlit as st
import sqlite3
import os
from datetime import datetime
from google import genai
from PIL import Image
import io
import pandas as pd
import uuid

# -----------------------------------------------------------------------------
# 0. API Key 設定與驗證區塊
# -----------------------------------------------------------------------------
DEFAULT_API_KEY = "AIzaSyB5OKzPztex0L-yDucKQg9H2ZHoXvb2quo"

def validate_api_key(api_key):
    if not api_key:
        return False
    try:
        client = genai.Client(api_key=api_key)
        client.models.get(model='gemini-2.5-flash')
        return True
    except Exception:
        return False

@st.dialog("⚠️ 系統提示：API Key 無效或未設定")
def api_key_dialog():
    st.write("請輸入有效的 Google Gemini API Key 以啟動 AI 影像分析功能：")
    user_key = st.text_input("API Key", type="password", placeholder="AIzaSy...")
    
    if st.button("驗證並開始使用", width="stretch"):
        with st.spinner("連線測試中..."):
            if validate_api_key(user_key):
                st.session_state.valid_api_key = user_key
                st.rerun()
            else:
                st.error("❌ 您輸入的 API Key 無法連線，請重新確認。")

# -----------------------------------------------------------------------------
# 1. 初始化設定：建立目錄、讀取 CSV、資料庫連線與資料更新功能
# -----------------------------------------------------------------------------
IMAGE_DIR = "images"
os.makedirs(IMAGE_DIR, exist_ok=True)

def get_base_categories():
    try:
        df = pd.read_csv("product.csv")
        return df['類別'].dropna().unique().tolist()
    except Exception as e:
        return []

base_categories = get_base_categories()

def init_db():
    conn = sqlite3.connect('mobile_gallery.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            category TEXT,
            description TEXT,
            filepath TEXT,
            upload_time TEXT
        )
    ''')
    try:
        cursor.execute("ALTER TABLE images ADD COLUMN filepath TEXT")
    except sqlite3.OperationalError:
        pass 
    conn.commit()
    return conn

conn = init_db()

def delete_image(image_id, filepath):
    if filepath and os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError as e:
            st.error(f"無法刪除實體檔案: {e}")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM images WHERE id = ?", (image_id,))
    conn.commit()

# 【更新】將更新分類的函式擴充，使其能同時更新「分類」與「說明」
def update_image_info(image_id, new_category, new_description):
    cursor = conn.cursor()
    cursor.execute("UPDATE images SET category = ?, description = ? WHERE id = ?", (new_category, new_description, image_id))
    conn.commit()

def get_total_images_count():
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM images")
    return cursor.fetchone()[0]

# -----------------------------------------------------------------------------
# 2. 畫面設定、Session State 初始化與對話框定義
# -----------------------------------------------------------------------------
st.set_page_config(page_title="行動版 AI 圖庫", layout="centered", initial_sidebar_state="collapsed")
st.title("📱 AI 圖庫管理")

total_images = get_total_images_count()
st.info(f"📁 目前圖庫中共有 **{total_images}** 張圖片")
st.caption("⚡ Powered by Google GenAI SDK & Gemini-2.5-Flash")

if "valid_api_key" not in st.session_state:
    if validate_api_key(DEFAULT_API_KEY):
        st.session_state.valid_api_key = DEFAULT_API_KEY
    else:
        api_key_dialog()
        st.stop()

client = genai.Client(api_key=st.session_state.valid_api_key)

if 'selected_category' not in st.session_state:
    st.session_state.selected_category = None
if 'uploader_key' not in st.session_state:
    st.session_state.uploader_key = str(uuid.uuid4())
if 'search_keyword' not in st.session_state:
    st.session_state.search_keyword = ""

def clear_search_state():
    st.session_state.selected_category = None
    st.session_state.search_keyword = ""

# 【新增】編輯圖片資訊的彈出式對話框
@st.dialog("✏️ 編輯圖片資訊")
def edit_image_dialog(img_id, current_cat, current_desc, base_cats, db_cats):
    # 準備分類下拉選單的選項
    all_options = sorted(list(set(base_cats + [c for c in db_cats if c != "待分類"])))
    if current_cat not in all_options and current_cat != "待分類":
        all_options.insert(0, current_cat)
    all_options.append("➕ 自行輸入新分類...")
    
    # 決定下拉選單的預設索引
    try:
        default_idx = all_options.index(current_cat)
    except ValueError:
        default_idx = 0
        
    new_cat = st.selectbox("修改分類", options=all_options, index=default_idx)
    
    if new_cat == "➕ 自行輸入新分類...":
        final_cat = st.text_input("輸入新分類名稱：")
    else:
        final_cat = new_cat
        
    # 使用 text_area 讓使用者有較大的空間修改長篇說明
    new_desc = st.text_area("修改說明 (最多 200 字)", value=current_desc, max_chars=200, height=150)
    
    if st.button("💾 儲存修改", width="stretch"):
        if final_cat.strip():
            update_image_info(img_id, final_cat.strip(), new_desc.strip())
            st.rerun()
        else:
            st.error("分類名稱不可為空！")

tab_upload, tab_gallery = st.tabs(["📤 多檔上傳區", "🔍 智慧查詢區"])

# -----------------------------------------------------------------------------
# 3. 框架一：多檔案上傳 (雙層 AI 容錯機制)
# -----------------------------------------------------------------------------
with tab_upload:
    st.header("新增圖片")
    
    if "upload_success_msg" in st.session_state:
        st.success(st.session_state.upload_success_msg)
        del st.session_state.upload_success_msg
    
    uploaded_files = st.file_uploader("點擊這裡選擇多張圖片", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True, key=st.session_state.uploader_key)
    
    if st.button("💾 自動分類並儲存", width="stretch"):
        if uploaded_files:
            progress_text = "圖片上傳與 AI 分析中，請稍候..."
            my_bar = st.progress(0, text=progress_text)
            total_files = len(uploaded_files)
            
            categories_str = ", ".join(base_categories) if base_categories else "無預設分類"
            
            for i, file in enumerate(uploaded_files):
                bytes_data = file.getvalue()
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                timestamp_prefix = datetime.now().strftime("%Y%m%d%H%M%S_")
                safe_filename = timestamp_prefix + file.name
                filepath = os.path.join(IMAGE_DIR, safe_filename)
                
                with open(filepath, "wb") as f:
                    f.write(bytes_data)
                
                img = Image.open(io.BytesIO(bytes_data))
                
                try:
                    prompt = f"""請客觀分析這張圖片的主要內容，不進行任何無根據的假設或推測。
                    1. 分類：請優先從下列預設選項中挑選一個最適合的分類：
                    【{categories_str}】
                    如果以上選項都不適合，請自行創造一個最貼切的簡短分類名稱（限 5 個字以內）。
                    2. 說明：請用 200 個字以內的正體中文，詳盡總結圖片的核心視覺元素、主要文字訊息或主打用途。
                    請嚴格依照以下格式回覆（不要加入其他贅字）：
                    分類：[填入分類]
                    說明：[填入說明]"""
                    
                    response = client.models.generate_content(model='gemini-2.5-flash', contents=[prompt, img])
                    result_text = response.text.strip()
                    
                    ai_category = ""
                    ai_description = ""
                    for line in result_text.split('\n'):
                        if line.startswith('分類：'):
                            ai_category = line.replace('分類：', '').strip()
                        elif line.startswith('說明：'):
                            ai_description = line.replace('說明：', '').strip()
                    
                    if not ai_category or not ai_description:
                        raise ValueError("AI 回傳格式不符")
                        
                except Exception as e:
                    try:
                        fallback_prompt = "請擷取這張圖片中的所有可見文字。如果沒有文字，請用一句話簡短描述畫面內容。"
                        fallback_resp = client.models.generate_content(model='gemini-2.5-flash', contents=[fallback_prompt, img])
                        
                        ai_category = "待分類"
                        ai_description = f"【擷取文字】{fallback_resp.text.strip()}"
                        
                        if len(ai_description) > 250:
                            ai_description = ai_description[:247] + "..."
                    except Exception as inner_e:
                        ai_category = "待分類"
                        ai_description = "AI 分析與文字擷取皆失敗，請手動確認。"
                
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO images (filename, category, description, filepath, upload_time) VALUES (?, ?, ?, ?, ?)",
                    (file.name, ai_category, ai_description, filepath, current_time)
                )
                conn.commit()
                my_bar.progress((i + 1) / total_files, text=f"正在處理: {file.name} ({i+1}/{total_files})")
            
            my_bar.empty()
            
            st.session_state.upload_success_msg = f"✅ 成功上傳 {total_files} 張圖片！若有「待分類」的圖片，可至查詢區手動更新。"
            st.session_state.uploader_key = str(uuid.uuid4())
            st.rerun()
            
        else:
            st.warning("⚠️ 請先選擇要上傳的圖片。")

# -----------------------------------------------------------------------------
# 4. 框架二：分類按鈕與關鍵字查詢
# -----------------------------------------------------------------------------
with tab_gallery:
    st.header("尋找與管理圖片")
    
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT category FROM images WHERE category IS NOT NULL AND category != ''")
    db_categories = [row[0] for row in cursor.fetchall()]
    
    st.write("🏷️ **快速分類查詢：**")
    
    cols = st.columns(4)
    for i, cat in enumerate(db_categories):
        col_idx = i % 4
        btn_label = f"🔴 {cat}" if cat == "待分類" else cat
        if cols[col_idx].button(btn_label, width="stretch"):
            st.session_state.selected_category = cat

    st.caption(f"目前檢視分類：**{st.session_state.selected_category if st.session_state.selected_category else '無'}**")
    st.divider()
    
    st.text_input("🔍 或輸入關鍵字進一步查詢 (如：保養、果汁)：", key="search_keyword")
    st.button("🧹 清除查詢", width="stretch", on_click=clear_search_state)
    
    query = "SELECT id, filename, category, description, filepath, upload_time FROM images WHERE 1=1"
    params = []
    
    if st.session_state.selected_category:
        query += " AND category = ?"
        params.append(st.session_state.selected_category)
        
    if st.session_state.search_keyword:
        query += " AND (filename LIKE ? OR description LIKE ?)"
        params.extend([f'%{st.session_state.search_keyword}%', f'%{st.session_state.search_keyword}%'])
        
    query += " ORDER BY upload_time DESC"
    
    if st.session_state.selected_category or st.session_state.search_keyword:
        cursor.execute(query, params)
        results = cursor.fetchall()
        
        if len(results) > 0:
            st.success(f"共找到 {len(results)} 張圖片")
            for row in results:
                img_id, fname, cat, desc, filepath, up_time = row
                
                with st.container():
                    if filepath and os.path.exists(filepath):
                        st.image(filepath, width="stretch")
                        
                        st.markdown(f"**分類:** `{cat}`")
                        st.markdown(f"**分析/內容:** {desc}") 
                        st.caption(f"上傳時間: {up_time}")
                        
                        # 處理容錯的「待分類」區塊 (與新的修改按鈕共用 update_image_info 邏輯)
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
                                    update_image_info(img_id, final_cat, desc) # 說明維持原樣
                                    st.rerun()
                                else:
                                    st.error("分類名稱不可為空！")
                            st.write("---")
                        
                        # 【修改重點】將功能按鈕劃分為三欄：下載、修改、刪除
                        col1, col2, col3 = st.columns(3)
                        with col1:
                            with open(filepath, "rb") as f:
                                st.download_button(
                                    label="⬇️ 下載", data=f, file_name=fname,
                                    mime="image/jpeg", key=f"download_{img_id}", width="stretch"
                                )
                        with col2:
                            # 點擊「修改」會呼叫上方定義的 @st.dialog 彈出視窗
                            if st.button("✏️ 修改", key=f"edit_{img_id}", width="stretch"):
                                edit_image_dialog(img_id, cat, desc, base_categories, db_categories)
                        with col3:
                            if st.button("🗑️ 刪除", key=f"delete_{img_id}", width="stretch"):
                                delete_image(img_id, filepath)
                                st.rerun()
                    else:
                        st.error(f"⚠️ 找不到實體圖片檔案：{filepath}")
                        if st.button("清理這筆無效紀錄", key=f"cleanup_{img_id}"):
                            delete_image(img_id, None)
                            st.rerun()
                st.divider()
        else:
            st.info("找不到符合條件的圖片。")
    else:
        st.info("💡 請點選上方「分類」或輸入「關鍵字」來尋找圖片。")
