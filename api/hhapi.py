import requests
import time
import os
from dotenv import load_dotenv
import json
import pandas as pd 

load_dotenv()

client_ID = os.getenv('client_ID')
client_secret = os.getenv('client_secret')
token_url = os.getenv('token_url')

data={'grant_type': 'client_credentials',
      'client_id': client_ID,
      'client_secret': client_secret}

response = requests.post(token_url, data=data)
access_token = response.json()['access_token']


def extract_vacancy_info(vacancy):
    return {
        'Название вакансии': vacancy['name'],
        'Работодатель': vacancy['employer'].get('name', 'N/A'),
        'Опыт работы': vacancy['experience'].get('name', 'N/A'),
        'Город': vacancy['area'].get('name', 'N/A'),
        'Требования': vacancy['snippet'].get('requirement'),
        'Описание работы': vacancy['snippet'].get('responsibility'),
        'Ссылка': vacancy['alternate_url']
    }

def fetch_vacancies(params):
    response = requests.get(url, params=params, headers=headers)
    response.raise_for_status() 
    data = json.loads(response.text)
    return data['items']

ex_params = [
    {'text': 'Data Scientist'},
    {'text': 'ML engineer'},
    {'experience': 'between1And3'},
    {'experience': 'noExperience'}
]

def get_hh_vacancies_df(experience_params, headers):

    all_vacancies = []
    for params_set in experience_params:
        params = {
            'text': 'Data Scientist',
            "area": 113,
            "per_page": 100,
            **params_set
        }

        page = 0
        while True:
            params['page'] = page
            vacancies, data = fetch_vacancies(params, headers)
            all_vacancies.extend(vacancies)
            time.sleep(1)
            page += 1
            if not data.get('pages'):
              break
            if page >= data['pages']:
                break

    vacancy_info = [extract_vacancy_info(item) for item in all_vacancies if item]
    if not vacancy_info:
      return None

    try:
        df = pd.DataFrame(vacancy_info)
        return df
    except Exception as e:
        print(f"Ошибка при создании DataFrame: {e}")
        return None