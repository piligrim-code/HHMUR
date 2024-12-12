import psycorg2
import pandas as pd

DATABASE_URL = "postgresql://postgres:1111@localhost:5432/postgres"

def import_dataframe_to_postgresql(df, table_name='vacancies'):
    try:
        conn = psycopg2.connect(DATABASE_URL) 
        cur = conn.cursor()

        create_table_query = """
            CREATE TABLE IF NOT EXISTS vacancies (
                id SERIAL PRIMARY KEY,
                Название_вакансии TEXT,
                Работодатель TEXT,
                Опыт_работы TEXT,
                Город TEXT,
                Требования TEXT,
                Описание_работы TEXT,
                Ссылка TEXT
            );
        """
        cur.execute(create_table_query)


        data_tuples = [tuple(row) for row in df.values]
        print(data_tuples)

        placeholders = ','.join(['%s'] * len(df.columns))
    
        insert_query = f"""
            INSERT INTO vacancies (Название_вакансии, Работодатель, Опыт_работы, Город, Требования, Описание_работы, Ссылка)
            VALUES ({placeholders})
        """
        print(insert_query)

        cur.executemany(insert_query, data_tuples) 

        conn.commit()  
        print(f"DataFrame успешно импортирован в таблицу '{table_name}'.")

    except psycopg2.Error as e:
        print(f"Ошибка при импорте DataFrame: {e}")
    finally:
        if conn:
            cur.close()
            conn.close() 

def export_dataframe_from_postgresql(table_name='vacancies', chunksize=1000):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        select_query = f"SELECT * FROM {table_name}"

        df = pd.DataFrame()
        cur.execute(select_query)
        while True:
            chunk = cur.fetchmany(chunksize)
            if not chunk:
                break
            chunk_df = pd.DataFrame(chunk, columns=[desc[0] for desc in cur.description])
            df = pd.concat([df, chunk_df], ignore_index=True)


        conn.commit()
        print(f"Данные успешно экспортированы из таблицы '{table_name}'.")
        return df

    except psycopg2.Error as e:
        print(f"Ошибка при экспорте данных: {e}")
        return None
    finally:
        if conn:
            cur.close()
            conn.close()

def import_dataframe_to_postgresql_ready(df, table_name='vacancies_ready'):
    try:
        conn = psycopg2.connect(DATABASE_URL) 
        cur = conn.cursor()

        create_table_query = """
            CREATE TABLE IF NOT EXISTS vacancies (
                id SERIAL PRIMARY KEY,
                Название_вакансии TEXT,
                Работодатель TEXT,
                Опыт_работы TEXT,
                Город TEXT,
                Требования TEXT,
                Описание_работы TEXT,
                Ссылка TEXT,
                Ответ,
                Оценка TEXT
            );
        """
        cur.execute(create_table_query)


        data_tuples = [tuple(row) for row in df.values]
        print(data_tuples)

        placeholders = ','.join(['%s'] * len(df.columns))
    
        insert_query = f"""
            INSERT INTO vacancies (Название_вакансии, Работодатель, Опыт_работы, Город, Требования, Описание_работы, Ссылка, Ответ, Оценка)
            VALUES ({placeholders})
        """
        print(insert_query)

        cur.executemany(insert_query, data_tuples) 

        conn.commit()  
        print(f"DataFrame успешно импортирован в таблицу '{table_name}'.")

    except psycopg2.Error as e:
        print(f"Ошибка при импорте DataFrame: {e}")
    finally:
        if conn:
            cur.close()
            conn.close() 