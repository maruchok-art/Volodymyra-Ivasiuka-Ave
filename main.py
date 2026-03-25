import requests
import hashlib
import os
import time
import json
import logging

# Професійне логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- ОТРИМУЄМО НАЛАШТУВАННЯ З GITHUB SECRETS ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID")
SOLARMAN_APP_ID = os.environ.get("SOLARMAN_APP_ID")
SOLARMAN_APP_SECRET = os.environ.get("SOLARMAN_APP_SECRET")
SOLARMAN_EMAIL = os.environ.get("SOLARMAN_EMAIL")
SOLARMAN_PASSWORD = os.environ.get("SOLARMAN_PASSWORD")
DEVICE_SN = os.environ.get("DEVICE_SN")

API_URL = "https://eu1-developer.deyecloud.com"

# НАДІЙНА БАЗА ДАНИХ JSONBLOB
JSONBLOB_ID = "019d1fde-f71e-7530-83fb-a58fe38ac1c7"
STORAGE_URL = f"https://jsonblob.com/api/jsonBlob/{JSONBLOB_ID}"

# Час життя токена (12 годин)
TOKEN_TTL = 43200


def send_telegram_message(text, silent=False):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_notification": silent}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Помилка відправки в Telegram: {e}")


# --- РОБОТА З БАЗОЮ JSONBLOB ---
def get_state():
    default_state = {"state": 0, "token": "", "token_time": 0}
    try:
        res = requests.get(STORAGE_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            if not data:
                return default_state
            return data
    except Exception as e:
        logging.warning(f"Помилка читання JSONBlob: {e}")
    return default_state


def save_state(state_dict):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    for attempt in range(3):
        try:
            res = requests.put(STORAGE_URL, json=state_dict, headers=headers, timeout=10)
            res.raise_for_status()
            logging.info("Пам'ять бота успішно оновлено та збережено в JSONBlob!")
            return
        except Exception as e:
            logging.warning(f"Помилка запису в JSONBlob (спроба {attempt+1}/3): {e}")
        time.sleep(3)
    logging.error("КРИТИЧНО: Не вдалося зберегти стан.")


# --- ЛОГІКА API DEYE ---
def fetch_new_token():
    if not SOLARMAN_PASSWORD:
        return None
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
    if not token:
        return None
    url = f"{API_URL}/v1.0/device/latest?appId={SOLARMAN_APP_ID}"
    auth_header = token if token.lower().startswith("bearer") else f"Bearer {token}"
    headers = {"Authorization": auth_header, "Content-Type": "application/json"}
    payload = {"deviceList": [DEVICE_SN]}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10).json()
        if not res.get("success"):
            return "AUTH_ERROR"

        data_list = res.get("deviceDataList", [])
        if not data_list:
            return None
        device_data = data_list[0]
        if str(device_data.get("deviceState", "")) == "2":
            return None

        for item in device_data.get("dataList", []):
            key = str(item.get("key", "")).upper()
            if key in ["SOC", "BATTERY_SOC", "BMS_SOC"]:
                return float(item.get("value", 100))
        return None
    except Exception as e:
        logging.error(f"Помилка запиту даних: {e}")
        return None


def get_battery_soc_with_retry(state, max_retries=3, delay=15):
    for attempt in range(max_retries):
        token = state.get("token", "")
        token_time = state.get("token_time", 0)

        if not token or (time.time() - token_time) > TOKEN_TTL:
            logging.info("Отримуємо новий токен...")
            token = fetch_new_token()
            if token:
                state["token"] = token
                state["token_time"] = time.time()

        if token:
            soc = fetch_soc_data(token)
            if soc == "AUTH_ERROR":
                logging.info("Токен відхилено. Оновлюємо...")
                state["token"] = ""
                continue
            elif soc is not None:
                return soc

        logging.warning(f"API Deye не відповів (спроба {attempt+1}/3). Чекаємо {delay} сек...")
        if attempt < max_retries - 1:
            time.sleep(delay)

    return "OFFLINE"


# --- ГОЛОВНА ЛОГІКА ---
def main():
    state = get_state()
    current_state_level = state.get("state", 0)

    soc = get_battery_soc_with_retry(state)
    logging.info(f"Отримано SOC: {soc}, Поточний рівень тривоги: {current_state_level}")

    # 1. Визначаємо, який стан МАЄ БУТИ зараз
    if soc == "OFFLINE":
        new_state = 4
    elif soc <= 30:
        new_state = 3
    elif soc <= 50:
        new_state = 2
    elif soc <= 95:
        new_state = 1
    else:
        new_state = 0

    # 2. Якщо були в стані OFFLINE, а зараз отримали реальні дані — повідомляємо про відновлення
    if current_state_level == 4 and new_state != 4:
        msg = (f"✅ <b>Зв'язок з інвертором відновлено!</b>\n\n"
               f"Поточний заряд акумулятора ліфта: <b>{soc}%</b>")
        send_telegram_message(msg)

        # Скидаємо поточний стан на 0, щоб наступний блок міг
        # надіслати тривогу якщо заряд зараз критично низький
        current_state_level = 0
        state["state"] = 0

    # 3. Якщо стан змінився — реагуємо
    if new_state != current_state_level:

        if new_state == 4:
            msg = (f"⚠️ <b>Увага! Втрачено зв'язок з інвертором ліфта.</b>\n\n"
                   f"Дані про заряд не оновлюються (можливо, зник інтернет або живлення роутера). "
                   f"Будь ласка, будьте обережні з ліфтом!")
            send_telegram_message(msg)

        elif new_state == 3 and current_state_level < 3:
            msg = (f"🔴 ⛔️ <b>КРИТИЧНИЙ ЗАРЯД ({soc}%)! НЕ СІДАЙТЕ В ЛІФТ!</b> ⛔️\n\n"
                   f"Є високий ризик зупинки кабіни між поверхами.")
            send_telegram_message(msg, silent=False)

        elif new_state == 2 and current_state_level < 2:
            msg = (f"🟠 <b>Заряд акумулятора ліфта: {soc}%</b>\n\n"
                   f"Запас ходу обмежений. Просимо максимально скоротити "
                   f"використання ліфта і за можливості йти сходами.")
            send_telegram_message(msg, silent=False)

        elif new_state == 1 and current_state_level < 1:
            msg = (f"🟡 <b>Увага! Ліфт працює від акумуляторів (Заряд: {soc}%).</b>\n\n"
                   f"Будь ласка, користуйтеся ним лише за крайньої потреби. Економте заряд!")
            send_telegram_message(msg, silent=True)

        elif new_state == 0:
            logging.info("Батарея заряджена. Стан скинуто на 0 (тихо).")

        else:
            logging.info(f"Батарея заряджається. Тихий перехід стану: {current_state_level} -> {new_state}")

        state["state"] = new_state

    else:
        logging.info("Стан не змінився. Дій не потрібно.")

    # 4. Зберігаємо завжди
    if soc != "OFFLINE":
        state["last_soc"] = soc
        state["last_update"] = int(time.time())
    save_state(state)

if __name__ == "__main__":
    main()
