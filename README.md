# WhatsApp ADHD Task Bot (FastAPI + Supabase + Vercel)

這是一個可部署在 Vercel 的 WhatsApp 工作任務機器人，支援：
- 自然語言轉任務（日期、時間、優先度）
- `list` / `today` / `done <id...>` / `edit <id> <text>` 指令
- 每日推播今日任務
- 串接 OpenAI API，按 ADHD 友善方式安排順序

## 1. 專案結構

```text
api/index.py               # Vercel Python 入口
app/main.py                # FastAPI routes
app/services.py            # 任務流程與命令邏輯
app/parser.py              # 文字解析
app/supabase_repo.py       # Supabase REST 存取
app/openai_planner.py      # OpenAI ADHD 排序
app/whatsapp.py            # WhatsApp API 封裝
supabase/schema.sql        # 建表 SQL
vercel.json                # Vercel routes + cron
```

## 2. Supabase（無 CLI 手動）

1. 在 Supabase Dashboard 建立專案
2. 打開 SQL Editor，貼上 [`supabase/schema.sql`](supabase/schema.sql)
   (如你已有舊資料庫，請重新執行一次作 migration，會加入 `task_no` 與 `edit` 需要欄位)
3. 執行後取得：
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

## 3. 環境變數

參考 `.env.example`：

```env
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_APP_SECRET=
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
TIMEZONE=Asia/Hong_Kong
DAILY_PUSH_TIME=09:00
MAX_DAILY_TASKS=6
CRON_SECRET=
```

## 4. 本地啟動

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Webhook 驗證 URL：
- `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`

## 5. Vercel 部署（無 CLI）

1. 把 repo 推到 GitHub
2. Vercel Dashboard `Add New Project` 匯入此 repo
3. 在 Vercel 設定同一組環境變數
4. 重新部署
5. 在 Meta Developer 後台把 Webhook 指向：
- `https://<your-vercel-domain>/webhook`

## 6. 每日推播

`vercel.json` 已加 cron：
- `0 1 * * *`（即香港時間每日 09:00）呼叫 `/internal/daily-push`

保護方式：
- 設定 `CRON_SECRET`
- endpoint 會接受：
  - `Authorization: Bearer <CRON_SECRET>`
  - 或 `?cron_secret=<CRON_SECRET>`

## 7. 指令與訊息示例

- `list`: 列出所有待辦
- `today`: 今日建議順序（會嘗試用 OpenAI 排序）
- `done 3 5 8`: 一次完成多項任務
- `edit 3 明天 4pm 跟客開會`: 修改任務 #3 內容
- 任務編號 `#id` 以每個電話號碼（chat）獨立重新計數
- 自然語言：`下星期二 3pm 同客開會`

## 8. 狀態頁（Web UI）

- `GET /status`: 伺服器狀態看板（黑/白/灰/橙）
- `GET /status.json`: 狀態 JSON
- 會顯示：
  - App / Supabase / Webhook / OpenAI / Cron 是否就緒
  - 哪些 env 還未設定

## 9. 法務頁（Meta 用）

- `GET /privacy`: Privacy Policy
- `GET /data-deletion`: Data Deletion Instructions
- 建議在 Vercel 設定 `PRIVACY_CONTACT_EMAIL` 作為聯絡電郵

## 10. ADHD 排程設計

流程：
1. 先取出當日任務
2. 丟給 OpenAI 產生 `ordered_task_ids`
3. 若 OpenAI 失敗，回退到本地規則排序

回傳 JSON 預期：
- `ordered_task_ids`
- `top_3_now`
- `reasons`
- `suggested_time_blocks`

## 11. 視覺主調（若日後做 Dashboard）

建議色票（黑/白/灰/橙）：
- `#111111`
- `#FFFFFF`
- `#E5E7EB`
- `#F97316`
