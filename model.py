import google.generativeai as genai
import os
import re
import time
from dotenv import load_dotenv
import json
import requests
from llama_cpp import Llama

load_dotenv()


API_KEY_1 = os.getenv('API_KEY_1')
API_KEY_2 = os.getenv('API_KEY_2')

def load_prompt_template(file_path="prompt.json"):

    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
        return data["prompt_template"]

def generate_prompt(job_title, job_description):

    prompt_template = load_prompt_template()

    return prompt_template.format(
        job_title=job_title,
        job_description=job_description
    )


def configure_model(api_key, model_name):
    """Конфигурирует модель и возвращает объект модели."""
    if model_name == "gemini":
        genai.configure(api_key=api_key)
        return genai.GenerativeModel("gemini-1.5-flash")
    else:
        raise ValueError(f"Неизвестное имя модели: {model_name}")


def get_model(model_name, api_key_1=None, api_key_2=None, model_path=None, llama_url=None):
    """Возвращает объект модели в зависимости от имени модели."""

    if model_name == "gemini":
        if api_key_1:  # Используем API_KEY_1 по умолчанию
            return configure_model(api_key_1, model_name)
        else:
            raise ValueError("Для модели ChatGPT необходим API_KEY_1")
    elif model_name == "llama":
        if not model_path:
            raise ValueError("Для модели Llama необходим путь к модели model_path")
        if not os.path.exists(model_path):
            if not llama_url:
                raise ValueError("Для загрузки модели Llama необходима ссылка llama_url")
            r = requests.get(llama_url)
            r.raise_for_status() #Проверка статуса ответа
            with open(model_path, 'wb') as out:
                out.write(r.content)
        llama = Llama(
            model_path=model_path,
            n_ctx=8192,
            n_threads=8,
            n_gpu_layers=35,
            chat_format="llama-2"
        )
        return llama
    else:
        raise ValueError(f"Неизвестное имя модели: {model_name}")


def evaluate_bot_response(response_text):
    match = re.search(r'Оценка:\s*(\d)', response_text)  # Ищем 0 или 1 после "Оценка:"
    
    if match:
        return int(match.group(1))  # Возвращаем оценку как целое число
    else:
        return None  # Если оценка не найдена

# Функция для безопасной генерации контента
def safe_generate_content(model, prompt):
    global use_second_api

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        if "Rate limit exceeded" in str(e) or "429" in str(e):  # Пример обработки ошибки лимита API
            print(f"Превышен лимит для API-ключа {API_KEY_1}, переключаемся на второй API...")
            use_second_api = True  # Переключаемся на второе API
            time.sleep(30)  # Пауза перед использованием второго API
            return safe_generate_content(configure_model(API_KEY_2), prompt)  # Повторный вызов функции с вторым API
        else:
            print(f"Ошибка при запросе к модели: {e}")
            time.sleep(30)  # Пауза в 30 секунд перед повторной попыткой
            return safe_generate_content(model, prompt)  # Повторный вызов функции