import requests
import hashlib
import os

# --- ОТРИМУЄМО НАЛАШТУВАННЯ З GITHUB SECRETS ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")

SOLARMAN_APP_ID = os.environ.get("SOLARMAN_APP_ID")
SOLARMAN_APP_SECRET = os.environ.get("SOLARMAN_APP_SECRET")
SOLARMAN_EMAIL = os.environ.get("SOLARMAN_EMAIL")
SOLARMAN_PASSWORD = os.environ.get("SOLARMAN_PASSWORD")
DEVICE_SN = os.environ.get("DEVICE_SN")
KVDB_BUCKET = os.environ.get("KVDB_BUCKET")

# Точний URL з твого скріншота
API_URL = "https://eu1-developer.deyecloud.com"
# -------------------------

def send_telegram_message(text):
    """Відправляє повідомлення у Telegram"""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Помилка відправки в Telegram: {e}")

def get_battery_soc():
    """Отримує заряд акумулятора згідно з офіційною документацією Deye Cloud"""
    if not SOLARMAN_PASSWORD:
        return "OFFLINE"

    pwd_hash = hashlib.sha256(SOLARMAN_PASSWORD.encode('utf-8')).hexdigest()
    
    # 1. Авторизація (згідно зі скріншотом 3)
    auth_url = f"{API_URL}/v1.0/account/token?appId={SOLARMAN_APP_ID}"
    auth_payload = {
        "appSecret": SOLARMAN_APP_SECRET, 
        "email": SOLARMAN_EMAIL, 
        "password": pwd_hash
    }
    
    try:
        auth_res = requests.post(auth_url, json=auth_payload, timeout=10).json()
        
        if not auth_res.get("success"):
            print("Помилка авторизації:", auth_res)
            return "OFFLINE"
            
        # Отримуємо токен (він вже містить слово Bearer згідно з доками)
        token = auth_res.get("accessToken", "")
        
        if not token:
            print("Помилка: Токен не знайдено!")
            return "OFFLINE"
            
        # 2. Отримання даних
        data_url = f"{API_URL}/v1.0/device/currentData?appId={SOLARMAN_APP_ID}"
        
        # ВИПРАВЛЕННЯ: якщо токен вже має "Bearer", не додаємо його вдруге!
        auth_header = token if token.lower().startswith("bearer") else f"Bearer {token}"
        
        # Заголовок authorization (згідно зі скріншотом 4)
        headers = {
            "Authorization": auth_header,
            "Content-Type": "application/json"
        }
        data_payload = {"deviceSn": DEVICE_SN}
        
        data_res = requests.post(data_url, headers=headers, json=data_payload, timeout=10).json()
        
        if str(data_res.get("deviceState", "")) == "2":
            return "OFFLINE"

        # Витягуємо SOC
        for item in data_res.get("dataList", []):
            if item.get("key", "").upper() in ["SOC", "BATTERY_SOC", "BATTERY CAPACITY", "BMS_SOC"]:
                return float(item.get("value", 100))
        
        print("Не знайдено параметр SOC. Повна відповідь:", data_res)
        return "OFFLINE"
                
    except Exception as e:
        print(f"Помилка з'єднання з API: {e}")
        return "OFFLINE"

# --- ЛОГІКА ЗБЕРЕЖЕННЯ СТАНУ ЧЕРЕЗ ХМАРУ KVDB ---
def get_current_state():
    if not KVDB_BUCKET: return 0
    url = f"https://kvdb.io/{KVDB_BUCKET}/elevator_state"
    try:
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return int(res.text.strip())
    except:
        pass
    return 0

def set_current_state(state):
    if not KVDB_BUCKET: return
    url = f"https://kvdb.io/{KVDB_BUCKET}/elevator_state"
    try:
        requests.post(url, data=str(state), timeout=5)
    except:
        pass
# ------------------------------------------------

def main():
    soc = get_battery_soc()
    current_state = get_current_state()

    print(f"Отримано SOC: {soc}, Поточний стан: {current_state}")

    if soc == "OFFLINE":
        if current_state != 4:
            msg = (f"⚠️ <b>Увага! Втрачено зв'язок з інвертором ліфта.</b>\n\n"
                   f"Дані про заряд не оновлюються (можливо, зник інтернет або живлення роутера). "
                   f"Будь ласка, будьте обережні з ліфтом!")
            send_telegram_message(msg)
            set_current_state(4)
        return

    if soc <= 30 and current_state != 3:
        msg = (f"🔴 ⛔️ <b>КРИТИЧНИЙ ЗАРЯД ({soc}%)! НЕ СІДАЙТЕ В ЛІФТ!</b> ⛔️\n\n"
               f"Є високий ризик зупинки кабіни між поверхами.")
        send_telegram_message(msg)
        set_current_state(3)

    elif 30 < soc <= 50 and current_state not in [2, 3]:
        msg = (f"🟠 <b>Заряд акумулятора ліфта: {soc}%</b>\n\n"
               f"Запас ходу обмежений. Просимо максимально скоротити "
               f"використання ліфта і за можливості йти сходами.")
        send_telegram_message(msg)
        set_current_state(2)

    elif 50 < soc <= 95 and current_state not in [1, 2, 3]:
        msg = (f"🟡 <b>Увага! Ліфт працює від акумуляторів.</b>\n\n"
               f"Будь ласка, користуйтеся ним лише за крайньої потреби. Економте заряд!")
        send_telegram_message(msg)
        set_current_state(1)

    elif soc > 95 and current_state > 0:
        set_current_state(0)
        print("Батарея заряджена. Стан скинуто на 0.")

if __name__ == "__main__":
    main()
