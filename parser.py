import requests
from bs4 import BeautifulSoup
import os
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver import FirefoxOptions

PROCESSED_LINKS_FILE = "store.txt"

def setup_driver():
    options = FirefoxOptions()
    options.add_argument('--headless')  
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36')

    driver = webdriver.Firefox(options=options)
    return driver

def get_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"
    }
    response = requests.get(url, headers=headers)
    return response.text

def scroll_to_bottom(driver):
    last_height = driver.execute_script("return document.body.scrollHeight")
    
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)  # Ждем, чтобы страница успела подгрузиться
        new_height = driver.execute_script("return document.body.scrollHeight")
        
        if new_height == last_height:  # Если мы достигли конца
            break
        
        last_height = new_height

def get_job_links(base_url, driver):
    driver.get(base_url)
    scroll_to_bottom(driver=driver)
    vacancy_elements = driver.find_elements(By.CSS_SELECTOR, ".magritte-link___b4rEM_4-3-12.magritte-link_style_neutral___iqoW0_4-3-12.magritte-link_enable-visited___Biyib_4-3-12")
    
    vacancy_links = []
    for element in vacancy_elements:
        link = element.get_attribute('href')
        if link:
            vacancy_links.append(link)

    return vacancy_links


def get_job_text(job_url):
    html = get_html(job_url)
    soup = BeautifulSoup(html, 'html.parser')

    attempts = 0
    job_title = None
    while attempts < 5 and not job_title:
        job_title = soup.find('h1', {'class': 'magritte-text___gMq2l_5-0-9 magritte-text-overflow___UBrTV_5-0-9 magritte-text-typography-medium___cp79S_5-0-9 magritte-text-style-primary___8SAJp_5-0-9'})
        if not job_title:
            attempts += 1
            time.sleep(1)  
            html = get_html(job_url)
            soup = BeautifulSoup(html, 'html.parser')
    
    job_title = job_title.text.strip() if job_title else 'Untitled'
    
    attempts = 0
    job_description = None
    while attempts < 5 and not job_description:
        job_description = soup.find('div', {'class': 'vacancy-branded-user-content'})
        if not job_description:
            attempts += 1
            time.sleep(1)  
            html = get_html(job_url)
            soup = BeautifulSoup(html, 'html.parser')

    job_description = job_description.text.strip() if job_description else 'No description available'

    return job_title, job_description

def sanitize_filename(filename):
    invalid_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
    for char in invalid_chars:
        filename = filename.replace(char, '_')  
    return filename

def load_processed_links():
    if os.path.exists(PROCESSED_LINKS_FILE):
        with open(PROCESSED_LINKS_FILE, 'r', encoding='utf-8') as file:
            processed_links = file.read().splitlines()  # Читаем файл и возвращаем список ссылок
        print(f"Загружено {len(processed_links)} обработанных ссылок.")
        return set(processed_links)  # Возвращаем множество ссылок
    else:
        print("Файл с обработанными ссылками не найден. Создаем новый.")
        return set()

# Функция для сохранения обработанных ссылок
def save_processed_link(job_link):
    with open(PROCESSED_LINKS_FILE, 'a', encoding='utf-8') as file:
        file.write(job_link + '\n')
        print(f"Ссылка сохранена: {job_link}")
