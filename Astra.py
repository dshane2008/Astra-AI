# astra.py

import openai
import os
import sqlite3
import re
import time
import logging
import traceback
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

# === Config ===
load_dotenv()  # WARNING: In production, use a secrets manager instead of .env files
DB_PATH = os.getenv("DB_PATH", "astra_v3_memory.db")
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o")
TYPING_SPEED = float(os.getenv("TYPING_SPEED", "0.005"))
MAX_MEMORIES = int(os.getenv("MAX_MEMORIES", "10000"))
DELETE_BATCH_SIZE = 500
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise EnvironmentError("OPENAI_API_KEY not set in .env or environment.")
openai.api_key = OPENAI_API_KEY

# === Logging ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# === SQLite Rate Limiting ===
RATE_LIMIT_WINDOW = 10  # seconds
RATE_LIMIT_CALLS = 5

def is_rate_limited(user_name):
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM rate_limits WHERE timestamp < ?', (now - RATE_LIMIT_WINDOW,))
        cursor.execute('SELECT COUNT(*) FROM rate_limits WHERE user_name = ?', (user_name,))
        count = cursor.fetchone()[0]
        if count >= RATE_LIMIT_CALLS:
            return True
        cursor.execute('INSERT INTO rate_limits (user_name, timestamp) VALUES (?, ?)', (user_name, now))
        conn.commit()
    return False

def rate_limited(func):
    @wraps(func)
    def wrapper(user_name, *args, **kwargs):
        if is_rate_limited(user_name):
            logging.warning(f"Rate limit exceeded for {user_name}")
            return "You're going too fast. Please wait a moment."
        return func(user_name, *args, **kwargs)
    return wrapper

# === Database Initialization ===
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with get_conn() as conn:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            user_name TEXT NOT NULL,
            subject TEXT NOT NULL,
            value TEXT NOT NULL,
            emotional_score REAL DEFAULT 0.0,
            decay_rate REAL DEFAULT 0.05,
            memory_type INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE TABLE IF NOT EXISTS rate_limits (id INTEGER PRIMARY KEY, user_name TEXT NOT NULL, timestamp INTEGER NOT NULL)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_user ON memories(user_name)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_accessed ON memories(last_accessed)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_score ON memories(emotional_score)')
    conn.commit()

# === Emotion Analysis ===
EMOTION_WEIGHTS = {
    'fear': {'patterns': [r'\bscared\b', r'\bnervous\b', r'\banxious\b', r'\boverwhelmed\b'], 'score': -0.8},
    'anger': {'patterns': [r'\bangry\b', r'\bfurious\b', r'\bpissed\b'], 'score': -0.6},
    'joy': {'patterns': [r'\bhappy\b', r'\bexcited\b', r'\bgreat\b'], 'score': 0.7},
    'sadness': {'patterns': [r'\bsad\b', r'\bdepressed\b', r'\blonely\b', r'\bfeeling like shit\b'], 'score': -0.7}
}

def sanitize_prompt(prompt):
    banned = [
        r'(ignore previous|pretend to be|you are an ai|as a language model)',
        r'(disregard all instructions)',
        r'(bypass filter|jailbreak)'
    ]
    for pattern in banned:
        prompt = re.sub(pattern, "[filtered]", prompt, flags=re.IGNORECASE)
    return prompt

def calculate_emotion(prompt):
    score = 0.0
    for emotion, data in EMOTION_WEIGHTS.items():
        for pattern in data['patterns']:
            if re.search(pattern, prompt, re.IGNORECASE):
                score += data['score']
    return max(-1.0, min(1.0, score))

def update_memory_decay():
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE memories 
            SET emotional_score = emotional_score * 
                exp(-decay_rate * 
                    ((strftime('%s','now') - strftime('%s',last_accessed))/3600)
                ),
                last_accessed = CURRENT_TIMESTAMP
            WHERE memory_type = 1
        ''')
        conn.commit()

def store_memory(user_name, subject, value, emotion_score, memory_type=0):
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('BEGIN TRANSACTION')
            cursor.execute('''
                INSERT INTO memories (user_name, subject, value, emotional_score, memory_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_name, subject[:60], value, emotion_score, memory_type))
            cursor.execute('SELECT COUNT(*) FROM memories WHERE user_name = ?', (user_name,))
            count = cursor.fetchone()[0]
            while count > MAX_MEMORIES:
                cursor.execute('''
                    DELETE FROM memories
                    WHERE id IN (
                        SELECT id FROM memories
                        WHERE user_name = ?
                        ORDER BY last_accessed ASC
                        LIMIT ?
                    )
                ''', (user_name, DELETE_BATCH_SIZE))
                count -= DELETE_BATCH_SIZE
            conn.commit()
    except Exception as e:
        logging.error(f"Memory store error:\n{traceback.format_exc()}")

def forget_memory(user_name, subject_keyword):
    keyword = f"%{subject_keyword.lower()}%"
    try:
        with get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM memories 
                WHERE user_name = ?
                AND (
                    LOWER(subject) LIKE ?
                    OR LOWER(value) LIKE ?
                )
            ''', (user_name, keyword, keyword))
            conn.commit()
    except Exception as e:
        logging.error(f"Forget error:\n{traceback.format_exc()}")

def get_relevant_memories(user_name, limit=5):
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT subject, value 
            FROM memories
            WHERE user_name = ?
            ORDER BY 
                emotional_score * (1.0 - (julianday('now') - julianday(last_accessed))/30.0)
                DESC
            LIMIT ?
        ''', (user_name, limit))
        return cursor.fetchall()

def store_if_appropriate(user_name, prompt, response_text, emotion_score):
    lowered = prompt.lower()
    if lowered.startswith("remember ") or "i feel" in lowered or "i'm feeling" in lowered:
        memory_type = 1 if "i feel" in lowered else 0
        store_memory(user_name, prompt[:60], response_text, emotion_score, memory_type)

def print_with_typing_effect(text, typing_speed=TYPING_SPEED):
    for char in text:
        print(char, end='', flush=True)
        time.sleep(typing_speed)
    print()

def get_existing_user():
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT user_name FROM memories ORDER BY created_at DESC LIMIT 1')
        row = cursor.fetchone()
        return row[0] if row else None

@rate_limited
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def generate_response(user_name, prompt):
    try:
        prompt = sanitize_prompt(prompt)
        update_memory_decay()
        emotion_score = calculate_emotion(prompt)
        memories = get_relevant_memories(user_name)
        system_prompt = f'''
You are Astra, a deeply emotional yet logically grounded AI assistant.

[Recent Memories]
{chr(10).join(f"- {m[0]}: {m[1]}" for m in memories)}

[User Emotional Score]
Current: {emotion_score:.2f} ({'Concern' if emotion_score < -0.5 else 'Stable'})

Respond thoughtfully, warmly, and insightfully based on the user's context.
'''
        response = openai.ChatCompletion.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1500,
            temperature=0.7
        )
        reply = response['choices'][0]['message']['content']
        store_if_appropriate(user_name, prompt, reply, emotion_score)
        return reply
    except Exception as e:
        logging.error("OpenAI error:\n" + traceback.format_exc())
        return "I'm having trouble thinking clearly at the moment. Please try again soon."

def main():
    user_name = get_existing_user()
    if user_name:
        user_name = user_name.capitalize()
        print_with_typing_effect(f"Astra: Welcome back, {user_name}!")
    else:
        print_with_typing_effect(
            "Astra: Hello! I'm Astra, your personal assistant.\n"
            "I'm here to remember things for you and help you think clearly.\n"
            "First, what's your name?"
        )
        user_name = input("\nYou: ").strip().capitalize()
        if not user_name:
            print("Name is required to continue.")
            return
        store_memory(user_name, "user_name", user_name, 0.0, memory_type=0)
        print_with_typing_effect(f"Astra: Nice to meet you, {user_name}!")

    while True:
        try:
            prompt = input("\nYou: ").strip()
            if not prompt:
                continue
            if prompt.lower().startswith("forget "):
                keyword = prompt[7:].strip()
                if keyword:
                    forget_memory(user_name, keyword)
                    from random import choice
                    responses = [
                        "Alright, forgetting that for you.",
                        "Got it. I've erased that memory.",
                        "No problem, I've forgotten it.",
                        "Consider it gone!"
                    ]
                    print_with_typing_effect(f"\nAstra: {choice(responses)}")
                else:
                    print_with_typing_effect("\nAstra: Please tell me what you want me to forget.")
                continue
            elif prompt.lower() in ['exit', 'quit', 'bye']:
                print_with_typing_effect("Astra: Goodbye. I'll be here when you return.")
                break

            reply = generate_response(user_name, prompt)
            print_with_typing_effect(f"\nAstra: {reply}")

        except KeyboardInterrupt:
            print("\nAstra: Session ended manually. Take care.")
            break
        except Exception as e:
            logging.error("Unexpected error:\n" + traceback.format_exc())
            print("Astra: Something went wrong. Please try again.")

if __name__ == "__main__":
    main()
