#!/usr/bin/env python3
"""
Скрипт для очистки старих Telegram сесій
Використовується при помилках AuthKeyDuplicatedError
"""

import os
import glob
import sys
from pathlib import Path

def cleanup_sessions():
    """Видаляє всі файли сесій Telegram"""
    script_dir = Path(__file__).parent
    os.chdir(script_dir)
    
    # Шукаємо всі файли сесій
    session_patterns = [
        "userbot*.session",
        "userbot*.session-journal",
        "*.session",
        "*.session-journal"
    ]
    
    deleted_files = []
    
    for pattern in session_patterns:
        for session_file in glob.glob(pattern):
            try:
                os.remove(session_file)
                deleted_files.append(session_file)
                print(f" Видалено: {session_file}")
            except Exception as e:
                print(f" Помилка видалення {session_file}: {e}")
    
    if deleted_files:
        print(f"\n🧹 Всього видалено файлів: {len(deleted_files)}")
    else:
        print("📁 Файли сесій не знайдено")
    
    return len(deleted_files)

if __name__ == "__main__":
    print(" Cleanup Telegram Sessions")
    print("=" * 30)
    
    deleted_count = cleanup_sessions()
    
    if deleted_count > 0:
        print("\n Очистка завершена успішно!")
        print(" Тепер можна перезапустити бота")
    else:
        print("\n Нічого не потрібно очищати")
    
    sys.exit(0)