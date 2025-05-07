import sqlite3

DB_PATH = "astra_v3_memory.db"

def browse_memories(user_filter=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    if user_filter:
        cursor.execute("SELECT id, user_name, subject, value, emotional_score FROM memories WHERE user_name = ?", (user_filter,))
    else:
        cursor.execute("SELECT id, user_name, subject, value, emotional_score FROM memories")
    
    memories = cursor.fetchall()
    conn.close()

    if not memories:
        print("No memories found.")
    else:
        for mem in memories:
            print(f"\nID: {mem[0]}")
            print(f"User: {mem[1]}")
            print(f"Subject: {mem[2]}")
            print(f"Value: {mem[3]}")
            print(f"Emotion Score: {mem[4]:.2f}")

if __name__ == "__main__":
    user = input("Enter username to filter (or press Enter to show all): ").strip()
    if not user:
        browse_memories()
    else:
        browse_memories(user)
