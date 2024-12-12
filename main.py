from parser import get_html, get_job_links, get_job_text, sanitize_filename, load_processed_links, save_processed_link, setup_driver
from model import  configure_model, get_model, evaluate_bot_response, safe_generate_content, generate_prompt
import telebot
import os
from dotenv import load_dotenv
import random

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
    base_urls = [
        'https://ekaterinburg.hh.ru/vacancy/112567531?hhtmFromLabel=suitable_vacancies_sidebar&hhtmFrom=vacancy'
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&education=not_required_or_not_specified&experience=between1And3&professional_role=165&text=",
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&education=not_required_or_not_specified&experience=noExperience&professional_role=165&text=",
        "https://hh.ru/search/vacancy?education=not_required_or_not_specified&experience=noExperience&ored_clusters=true&text=ML&order_by=relevance",
        "https://hh.ru/search/vacancy?education=not_required_or_not_specified&ored_clusters=true&experience=between1And3&search_field=name&search_field=company_name&search_field=description&text=ML&enable_snippets=false",
        "https://hh.ru/search/vacancy?education=not_required_or_not_specified&ored_clusters=true&experience=between1And3&search_field=name&search_field=company_name&search_field=description&text=ML&enable_snippets=false&page=1&searchSessionId=d6ae1f2f-b297-4ff0-bad5-3a62cd71c20d",
        "https://hh.ru/search/vacancy?education=not_required_or_not_specified&ored_clusters=true&experience=between1And3&search_field=name&search_field=company_name&search_field=description&text=ML&enable_snippets=false&page=2&searchSessionId=d6ae1f2f-b297-4ff0-bad5-3a62cd71c20d",
        "https://hh.ru/search/vacancy?education=not_required_or_not_specified&ored_clusters=true&experience=between1And3&search_field=name&search_field=company_name&search_field=description&text=ML&enable_snippets=false&page=3&searchSessionId=d6ae1f2f-b297-4ff0-bad5-3a62cd71c20d",
        "https://hh.ru/search/vacancy?from=suggest_post&ored_clusters=true&education=not_required_or_not_specified&hhtmFrom=vacancy_search_list&hhtmFromLabel=vacancy_search_line&experience=noExperience&search_field=name&search_field=company_name&search_field=description&text=Data+science&enable_snippets=false",
        "https://hh.ru/search/vacancy?from=suggest_post&ored_clusters=true&education=not_required_or_not_specified&hhtmFrom=vacancy_search_list&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&experience=between1And3&text=Data+science",
        "https://hh.ru/search/vacancy?from=suggest_post&ored_clusters=true&education=not_required_or_not_specified&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&experience=between1And3&text=Data+science&page=1&searchSessionId=1049b1bb-b5e6-4b73-b15f-5cc9f2ad9467",
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&schedule=remote&education=not_required_or_not_specified&experience=noExperience&text=python", 
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&schedule=remote&education=not_required_or_not_specified&experience=between1And3&text=python", 
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&education=not_required_or_not_specified&experience=noExperience&text=NLP", 
        "https://ekaterinburg.hh.ru/search/vacancy?hhtmFrom=main&hhtmFromLabel=vacancy_search_line&search_field=name&search_field=company_name&search_field=description&enable_snippets=false&L_save_area=true&education=not_required_or_not_specified&experience=between1And3&text=NLP",

    ]
    
    all_job_links = []
    all_results = []  

    processed_links = load_processed_links()

    driver = setup_driver()

    for base_url in base_urls:
        print(f"Обрабатываем базовый URL: {base_url}")
        job_links = get_job_links(base_url, driver)

        for job_link in job_links:
            if job_link in processed_links:
                print(f"Ссылка {job_link} уже обработана. Пропускаем.")
                continue  # Пропускаем уже обработанные ссылки
            
            print(f"Открываем вакансию: {job_link}")
            job_title, job_description = get_job_text(job_link)
            
            prompt = generate_prompt(job_title, job_description)
            model = get_model()  
            response_text = safe_generate_content(model, prompt)

            evaluation_score = evaluate_bot_response(response_text)

            all_job_links.append(job_link)
            all_results.append(response_text)

            if evaluation_score is not None:
                evaluate_and_notify(job_link, job_title, response_text, evaluation_score)
                
            save_processed_link(job_link)
    driver.quit()

    print(f"Обработано {len(all_job_links)} вакансий.")

if __name__ == "__main__":
    main()