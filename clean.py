import os
import re

# Регулярка, що ловить усі емодзі
emoji_pattern = re.compile(
    "[" 
    "\U0001F600-\U0001F64F"  # смайли
    "\U0001F300-\U0001F5FF"  # символи та піктограми
    "\U0001F680-\U0001F6FF"  # транспорт
    "\U0001F1E0-\U0001F1FF"  # прапори
    "\U00002700-\U000027BF"  # різні символи
    "\U0001F900-\U0001F9FF"  # додаткові емодзі
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002B00-\U00002BFF"
    "]+",
    flags=re.UNICODE
)

def remove_emojis_from_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        cleaned = emoji_pattern.sub("", content)
        with open(path, "w", encoding="utf-8") as f:
            f.write(cleaned)
        print(f"[CLEANED] {path}")
    except Exception as e:
        print(f"[SKIPPED] {path} — {e}")

def clean_directory(root_dir="bybit-strategy"):
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            file_path = os.path.join(root, file)

            # Чистимо тільки текстові файли
            if file.endswith((
                ".py", ".txt", ".md", ".json", ".yaml", ".yml",
                ".ini", ".cfg", ".env"
            )):
                remove_emojis_from_file(file_path)

if __name__ == "__main__":
    clean_directory("bybit-strategy")
