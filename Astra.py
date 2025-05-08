# === 1. Imports ===
# === 1. Imports ===
import openai
import os
import sqlite3
import re
import time
from datetime import datetime, timedelta
from collections import Counter

# === 2. Config ===
# === 2. Config ===
ENV_PATH = ".env"
DB_PATH = "astra_v3_memory.db"
DEFAULT_MODEL = "gpt-4o"
TYPING_SPEED = 0.005
MAX_MEMORIES =10_000
# === 3. API Key Loader (Auto Programmatic) ===

# 1. Check if environment variable already set
if "OPENAI_API_KEY" not in os.environ:
    if os.path.exists(ENV_PATH):
        # Load from .env if it exists
        with open(ENV_PATH, "r") as f:
            for line in f:
                if line.startswith("OPENAI_API_KEY="):
                    os.environ["OPENAI_API_KEY"] = line.strip().split("=", 1)[1]
                    break
    else:
        # Create .env and set the API key
        with open(ENV_PATH, "w") as f:
            f.write(f"OPENAI_API_KEY={HARDCODED_API_KEY}\n")
        os.environ["OPENAI_API_KEY"] = HARDCODED_API_KEY

# 2. Always load API Key from environment
def load_api_key():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not found in environment variables!")
    return api_key

openai.api_key = load_api_key()
# === 4. Database Setup ===
conn = sqlite3.connect(DB_PATH)
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
conn.commit()

# === 5. Emotion Weights ===
EMOTION_WEIGHTS = {
    'fear': {'patterns': [r'\bscared\b', r'\bnervous\b', r'\banxious\b', r'\boverwhelmed\b'], 'score': -0.8},
    'anger': {'patterns': [r'\bangry\b', r'\bfurious\b', r'\bpissed\b'], 'score': -0.6},
    'joy': {'patterns': [r'\bhappy\b', r'\bexcited\b', r'\bgreat\b'], 'score': 0.7},
    'sadness': {'patterns': [r'\bsad\b', r'\bdepressed\b', r'\blonely\b', r'\bfeeling like shit\b'], 'score': -0.7}
}

# === 6. Utilities ===
def print_with_typing_effect(text, typing_speed=TYPING_SPEED):
    for char in text:
        print(char, end='', flush=True)
        time.sleep(typing_speed)
    print()

def calculate_emotion(prompt):
    score = 0.0
    for emotion, data in EMOTION_WEIGHTS.items():
        for pattern in data['patterns']:
            if re.search(pattern, prompt, re.IGNORECASE):
                score += data['score']
    return max(-1.0, min(1.0, score))

def update_memory_decay():
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
    cursor.execute('''
        INSERT INTO memories (user_name, subject, value, emotional_score, memory_type)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_name, subject, value, emotion_score, memory_type))

    # Enforce maximum memory limit per user
    cursor.execute('SELECT COUNT(*) FROM memories WHERE user_name = ?', (user_name,))
    count = cursor.fetchone()[0]

    if count > MAX_MEMORIES:
        cursor.execute('''
            DELETE FROM memories
            WHERE id IN (
                SELECT id FROM memories
                WHERE user_name = ?
                ORDER BY last_accessed ASC
                LIMIT ?
            )
        ''', (user_name, count - MAX_MEMORIES))

    conn.commit()
def forget_memory(user_name, subject_keyword):
    keyword = subject_keyword.lower()
    cursor.execute('''
        DELETE FROM memories 
        WHERE user_name = ?
        AND (
            LOWER(subject) LIKE ?
            OR LOWER(value) LIKE ?
        )
    ''', (user_name, f"%{keyword}%", f"%{keyword}%"))
    conn.commit()

def store_if_appropriate(user_name, prompt, response_text, emotion_score):
    lowered = prompt.lower()
    if lowered.startswith("remember ") or "i feel" in lowered or "i'm feeling" in lowered:
        memory_type = 1 if "i feel" in lowered else 0
        store_memory(user_name, prompt[:60], response_text, emotion_score, memory_type)

def get_relevant_memories(user_name, limit=5):
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

def get_existing_user():
    cursor.execute('SELECT DISTINCT user_name FROM memories')
    row = cursor.fetchone()
    if row:
        return row[0]
    return None

# === 7. Response Engine ===
def generate_response(user_name, prompt):
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

    store_if_appropriate(user_name, prompt, response['choices'][0]['message']['content'], emotion_score)

    return response['choices'][0]['message']['content']

# === 8. Main Loop ===
def main():
    user_name = get_existing_user()
    if user_name:
        user_name = user_name.capitalize()
        print_with_typing_effect(f"Astra: Welcome back, {user_name}!")
    else:
        print_with_typing_effect(
            "Astra: Hello! I'm Astra, your personal assistant. "
            "I'm here to listen, remember important things for you, and help you with anything you need.\n"
            "First things first—what's your name?"
        )
        user_name = input("\nYou: ").strip().capitalize()
        store_memory(user_name, "user_name", user_name, 0.0, memory_type=0)
        print_with_typing_effect(f"Astra: Nice to meet you, {user_name}!")

    while True:
        try:
            prompt = input("\nYou: ").strip()
            lowered_prompt = prompt.lower()

            if lowered_prompt in ['exit', 'quit', 'bye']:
                print_with_typing_effect("Astra: Goodbye! I'll remember you fondly.")
                break

            elif lowered_prompt.startswith("forget "):
                keyword = prompt[7:].strip()
                if keyword:
                    forget_memory(user_name, keyword)
                    # --- Enhancement: More natural forgetting reply ---
                    forget_responses = [
                        "Alright, forgetting that for you.",
                        "Got it. I've erased that memory.",
                        "No problem, I've forgotten it.",
                        "Consider it gone!"
                    ]
                    import random
                    print_with_typing_effect(f"\nAstra: {random.choice(forget_responses)}")
                else:
                    print_with_typing_effect("\nAstra: Please tell me what you want me to forget.")
                continue

            reply = generate_response(user_name, prompt)
            print_with_typing_effect(f"\nAstra: {reply}")

        except KeyboardInterrupt:
            print("\nAstra: Session ended manually. Take care!")
            break

    conn.close()

# === 9. Entry Point ===
if __name__ == "__main__":
    main()
