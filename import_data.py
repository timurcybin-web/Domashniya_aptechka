import pandas as pd
from app import app, db, Directory


def import_medications(file_path):
    """
    Загружает данные из Excel или CSV файла в таблицу Directory.
    """
    try:
        if file_path.endswith('.xlsx'):
            # Файл ГРЛС: заголовки со строки 4, данные с строки 6
            # Колонка 8 = название препарата, колонка 13 = фармакологическая группа
            df = pd.read_excel(file_path, header=None, skiprows=4)
        elif file_path.endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            print("Ошибка: Поддерживаются только форматы .xlsx и .csv")
            return
    except Exception as e:
        print(f"Ошибка при чтении файла: {e}")
        return

    with app.app_context():
        print("Начинаю импорт данных в базу...")

        count = 0
        for index, row in df.iterrows():
            # Пропускаем строки заголовков и пустые строки
            if index < 2:
                continue

            name = str(row.iloc[8]).strip() if pd.notna(row.iloc[8]) else ''
            desc = str(row.iloc[13]).strip() if pd.notna(row.iloc[13]) else 'Описание отсутствует'

            # Пропускаем пустые, служебные строки и строки без нормального названия
            if not name or name in ('nan', '~', 'None') or len(name) < 2:
                continue

            exists = Directory.query.filter_by(name=name).first()
            if not exists:
                db.session.add(Directory(name=name, description=desc))
                count += 1

            if count % 100 == 0 and count > 0:
                db.session.commit()
                print(f"  Добавлено {count} записей...")

        db.session.commit()
        print(f"Готово! Успешно добавлено {count} новых записей в справочник.")


if __name__ == '__main__':
    import_medications('grls2026-02-10-1-Действующий.xlsx')
