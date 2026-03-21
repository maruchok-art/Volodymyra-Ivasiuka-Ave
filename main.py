import requests
import hashlib
import os
import time
import json
import logging

# 1. Професійне логування з часовими мітками
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ОТРИМУЄМО НАЛАШТУВАННЯ З GITHUB SECRETS ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
SOLARMAN_APP_ID = os.environ.get("SOLARMAN_APP_ID")
SOLARMAN_APP_SECRET = os.environ.get("SOLARMAN_APP_SECRET")
SOLARMAN_EMAIL = os.environ.get("SOLARMAN_EMAIL")
SOLARMAN_PASSWORD = os.environ.get("SOLARMAN_PASSWORD")
DEVICE_SN = os.environ.get("DEVICE_SN")
KVDB_BUCKET = os.environ.get("KVDB_BUCKET")

API_URL = "https://eu1-developer.deyecloud.com"

# --- ЛОГІКА ТЕЛЕГРАМУ ---
def send_telegram_message(text, silent=False):
    """Відправляє повідомлення у Telegram (можливість відправляти без звуку)"""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML",
        "disable_notification": silent  # Робить сповіщення тихим
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Помилка відправки в Telegram: {e}")

# --- БЕЗПЕЧНА РОБОТА З БАЗОЮ ДАНИХ (ПЛАН "Б") ---
def get_state():
    """Отримує словник стану з KVDB. Якщо помилка - повертає безпечні значення по замовчуванню."""
    default_state = {
        "state": 0, 
        "token": "", 
        "token_time": 0,
        "last_soc": 100.0,
        "last_soc_time": time.time()
    }
    if not KVDB_BUCKET: return default_state
    
    url = f"https://kvdb.io/{KVDB_BUCKET}/elevator_state_v2"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return json.loads(res.text)
    except Exception as e:
        logging.warning(f"KVDB тимчасово недоступний (Читання). Використовуємо План Б: {e}")
    return default_state

def save_state(state_dict):
    """Зберігає словник стану у KVDB."""
    if not KVDB_BUCKET: return
    url = f"https://kvdb.io/{KVDB_BUCKET}/elevator_state_v2"
    try:
        requests.post(url, json=state_dict, timeout=5)
    except Exception as e:
        logging.warning(f"KVDB тимчасово недоступний (Запис): {e}")

# --- ЛОГІКА API DEYE (З КЕШУВАННЯМ) ---
def fetch_new_token():
    """Генерує новий токен доступу"""
    if not SOLARMAN_PASSWORD: return None
    pwd_hash = hashlib.sha256(SOLARMAN_PASSWORD.encode('utf-8')).hexdigest()
    auth_url = f"{API_URL}/v1.0/account/token?appId={SOLARMAN_APP_ID}"
    payload = {"appSecret": SOLARMAN_APP_SECRET, "email": SOLARMAN_EMAIL, "password": pwd_hash}
    try:
        res = requests.post(auth_url, json=payload, timeout=10).json()
        if res.get("success"):
            return res.get("accessToken", "")
    except Exception as e:
        logging.error(f"Помилка генерації токена: {e}")
    return None

def fetch_soc_data(token):
    """Отримує заряд по токену. Повертає SOC, None або 'AUTH_ERROR'"""
    if not token: return None
    url = f"{API_URL}/v1.0/device/latest?appId={SOLARMAN_APP_ID}"
    auth_header = token if token.lower().startswith("bearer") else f"Bearer {token}"
    headers = {"Authorization": auth_header, "Content-Type": "application/json"}
    payload = {"deviceList": [DEVICE_SN]}
    
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10).json()
        
        # Перевірка на прострочений/невалідний токен
        if not res.get("success"):
            code = str(res.get("code", ""))
            if "2101" in code or code in ["1000001", "1000002"]: 
                return "AUTH_ERROR"
            return None
            
        data_list = res.get("deviceDataList", [])
        if not data_list: return None
        
        device_data = data_list[0]
        if str(device_data.get("deviceState", "")) == "2": return None # Офлайн
            
        for item in device_data.get("dataList", []):
            if str(item.get("key", "")).upper() in ["SOC", "BATTERY_SOC", "BATTERY CAPACITY", "BMS_SOC"]:
                return float(item.get("value", 100))
        return None
    except Exception as e:
        logging.error(f"Помилка запиту даних: {e}")
        return None

def get_battery_soc_with_retry(state, max_retries=3, delay=15):
    """Надійна функція отримання заряду з використанням кешованого токена"""
    for attempt in range(max_retries):
        token = state.get("token", "")
        token_time = state.get("token_time", 0)
        
        # Якщо токена немає або він старший за 12 годин (43200 сек) - оновлюємо
        if not token or (time.time() - token_time) > 43200:
            logging.info("Токен відсутній або застарів. Отримуємо новий...")
            token = fetch_new_token()
            if token:
                state["token"] = token
                state["token_time"] = time.time()
                
        if token:
            soc = fetch_soc_data(token)
            if soc == "AUTH_ERROR":
                logging.info("Сервер відхилив токен. Примусове оновлення...")
                state["token"] = "" # Змусить оновити токен на наступному циклі
                continue
            elif soc is not None:
                return soc
                
        logging.warning(f"Спроба {attempt + 1} невдала. Чекаємо {delay} сек...")
        if attempt < max_retries - 1:
            time.sleep(delay)
            
    return "OFFLINE"

# --- ГОЛОВНА ЛОГІКА ---
def main():
    state = get_state()
    current_state_level = state.get("state", 0)
    
    soc = get_battery_soc_with_retry(state)
    logging.info(f"Отримано SOC: {soc}, Поточний рівень тривоги: {current_state_level}")

    # Перевірка на "зависання"
    if isinstance(soc, (int, float)):
        if soc == state.get("last_soc", 100):
            # Якщо заряд < 100% і не мінявся більше 4 годин
            if soc < 100 and (time.time() - state.get("last_soc_time", time.time())) > 14400:
                logging.warning(f"Можливе зависання даних! Заряд {soc}% не змінюється більше 4 годин.")
        else:
            state["last_soc"] = soc
            state["last_soc_time"] = time.time()

    # Втрата зв'язку
    if soc == "OFFLINE":
        if current_state_level != 4:
            msg = (f"⚠️ <b>Увага! Втрачено зв'язок з інвертором ліфта.</b>\n\n"
                   f"Дані про заряд не оновлюються (можливо, зник інтернет або живлення роутера). "
                   f"Будь ласка, будьте обережні з ліфтом!")
            send_telegram_message(msg)
            state["state"] = 4
            save_state(state)
        return

    # КРИТИЧНО (Червона зона)
    if soc <= 30 and current_state_level != 3:
        msg = (f"🔴 ⛔️ <b>КРИТИЧНИЙ ЗАРЯД ({soc}%)! НЕ СІДАЙТЕ В ЛІФТ!</b> ⛔️\n\n"
               f"Є високий ризик зупинки кабіни між поверхами.")
        send_telegram_message(msg, silent=False)
        state["state"] = 3

    # ОБМЕЖЕНО (Оранжева зона)
    elif 30 < soc <= 50 and current_state_level not in [2, 3]:
        msg = (f"🟠 <b>Заряд акумулятора ліфта: {soc}%</b>\n\n"
               f"Запас ходу обмежений. Просимо максимально скоротити "
               f"використання ліфта і за можливості йти сходами.")
        send_telegram_message(msg, silent=False)
        state["state"] = 2

    # РЕЗЕРВ (Жовта зона - ТИХЕ повідомлення)
    elif 50 < soc <= 95 and current_state_level not in [1, 2, 3]:
        msg = (f"🟡 <b>Увага! Ліфт працює від акумуляторів (Заряд: {soc}%).</b>\n\n"
               f"Будь ласка, користуйтеся ним лише за крайньої потреби. Економте заряд!")
        send_telegram_message(msg, silent=True) # Сповіщення без звуку!
        state["state"] = 1

    # ЗАРЯДЖЕНО (Зелена зона)
    elif soc > 95 and current_state_level > 0:
        state["state"] = 0
        logging.info("Батарея заряджена. Стан скинуто на 0 (тихо).")

    save_state(state)

if __name__ == "__main__":
    main()
