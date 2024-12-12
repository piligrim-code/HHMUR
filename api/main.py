from db import export_dataframe_from_postgresql, import_dataframe_to_postgresql, import_dataframe_to_postgresql_ready
from model import  configure_model, get_model, evaluate_bot_response, safe_generate_content, generate_prompt
import telebot
import os
from dotenv import load_dotenv
import pandas as pd

load_dotenv()

use_second_api = False
PROCESSED_LINKS_FILE = "store.txt"

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

bot = telebot.TeleBot(TELEGRAM_TOKEN)

def send_message_to_telegram(chat_id, message):
    bot.send_message(chat_id, message)

def evaluate_and_notify(job_link, job_title, response_text, evaluation_score):
    if evaluation_score == 1: 
        message_1 = f"Оценка: {evaluation_score}\nВакансия: {job_title}\nСсылка: {job_link}"
        message_2 = f"Письмо нейросети: {response_text}"
        
        # Отправляем первое сообщение с ссылкой и оценкой
        send_message_to_telegram(CHAT_ID, message_1)
        # Отправляем второе сообщение с описанием и ответом
        send_message_to_telegram(CHAT_ID, message_2)
    

def main():
    df = export_dataframe_from_postgresql()
    results = []
    for index, row in df.iterrows():
        job_title = row['Название вакансии']
        job_description = row['Описание работы']
        job_link = row['Ссылка']
        
        prompt = generate_prompt(job_title, job_description)
        model = get_model()  
        response_text = safe_generate_content(model, prompt)

        evaluation_score = evaluate_bot_response(response_text)

        if evaluation_score is not None:
            evaluate_and_notify(job_link, job_title, response_text, evaluation_score)

        results.append({'response_text': response_text, 'evaluation_score': evaluation_score or 0})
    results_df = pd.DataFrame(results)
    df['response_text'] = results_df['response_text']
    df['evaluation_score'] = results_df['evaluation_score']
    import_dataframe_to_postgresql_ready(df)

    if __name__ == "__main__":
    main()