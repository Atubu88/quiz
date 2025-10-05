# Contributing Guide

Этот проект использует **Supabase (PostgreSQL)** как основную базу данных.  
Ниже представлена актуальная структура таблиц и их полей, чтобы вы понимали логику хранения данных.

---

## 📌 Таблицы базы данных

### 1. users — игроки
- `id` (serial, PK)  
- `telegram_id` (bigint, unique, not null)  
- `username` (varchar)  
- `first_name` (varchar)  
- `last_name` (varchar)  

---

### 2. quizzes — викторины
- `id` (serial, PK)  
- `title` (varchar, not null)  
- `description` (text)  
- `category_id` (int, FK → categories.id)  
- `is_active` (boolean, default true)  

---

### 3. questions — вопросы викторины
- `id` (serial, PK)  
- `quiz_id` (int, FK → quizzes.id, on delete cascade)  
- `text` (text, not null)  
- `explanation` (text)  

---

### 4. options — варианты ответов
- `id` (serial, PK)  
- `question_id` (int, FK → questions.id, on delete cascade)  
- `text` (text, not null)  
- `is_correct` (boolean, not null)  

---

5. teams — команды

id uuid, PK, default gen_random_uuid()

name text, not null

code text, unique, not null — короткий код для подключения

captain_id bigint, FK → users.telegram_id

created_at timestamptz, default now()

start_time timestamptz

ready boolean, default false — флаг готовности команды к игре

quiz_id text — идентификатор викторины, которую проходит команда

### 6. team_members — участники команд
- `id` (uuid, PK, default gen_random_uuid())  
- `team_id` (uuid, FK → teams.id, on delete cascade)  
- `user_id` (int, FK → users.id, on delete cascade)  
- `is_captain` (boolean, default false)  
- `joined_at` (timestamptz, default now())  
- `unique(team_id, user_id)`  

---

### 7. quiz_results — индивидуальные результаты игроков
- `id` (serial, PK)  
- `user_id` (bigint, FK → users.telegram_id)  
- `quiz_id` (int, FK → quizzes.id)  
- `is_correct` (boolean)  
- `time_taken` (double precision)  
- `created_at` (timestamptz, default now())  

---

### 8. team_results — результаты команд
- `id` (uuid, PK, default gen_random_uuid())  
- `team_id` (uuid, FK → teams.id, on delete cascade)  
- `quiz_id` (int, FK → quizzes.id, on delete cascade)  
- `score` (int, not null)  
- `time_taken` (double precision)  
- `created_at` (timestamptz, default now())  

---

## 📌 Примечания
- Все UUID генерируются через `gen_random_uuid()`.  
- Внешние ключи (`FK`) обеспечивают целостность данных.  
- При удалении викторины/вопроса каскадно удаляются связанные варианты и результаты.  
- Логика «капитана команды» хранится как `is_captain = true` в таблице `team_members`.  

---

## 📌 Работа с базой
- В проекте **не используется ORM** (например, SQLAlchemy).  
- Все операции выполняются **напрямую через Supabase REST API** с использованием `SUPABASE_URL` и `SUPABASE_API_KEY`.  
- Это упрощает архитектуру и снижает нагрузку на сервер.  

---

📖 Используйте эту схему как справочник при работе с API и при внесении изменений в базу.
