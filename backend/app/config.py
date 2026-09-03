from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
USER_DATA_DIR = DATA_DIR / "user_data"
LEAGUE_DIR = USER_DATA_DIR / "research_league"
NEWS_DIR = USER_DATA_DIR / "research_news"
RUNTIME_DIR = DATA_DIR / "runtime"
AI_ARCHIVE_DIR = USER_DATA_DIR / "ai_reviews"
DATA_UPDATE_STATUS_PATH = RUNTIME_DIR / "data-updates.json"
NEWS_SYNC_SETTINGS_PATH = RUNTIME_DIR / "news-sync-settings.json"
HITHINK_RAW_DIR = DATA_DIR / "raw" / "hithink"

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_FAST_MODEL = "deepseek-v4-flash"
DEEPSEEK_DEEP_MODEL = "deepseek-v4-pro"
HITHINK_BASE_URL = "https://fuyao.aicubes.cn"

for directory in (USER_DATA_DIR, RUNTIME_DIR, AI_ARCHIVE_DIR, HITHINK_RAW_DIR):
    directory.mkdir(parents=True, exist_ok=True)
