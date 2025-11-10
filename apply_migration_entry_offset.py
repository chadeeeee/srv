"""
Скрипт для застосування міграції entry_offset_percent
"""
import asyncio
import asyncpg
from loguru import logger
from config import CONFIG

async def apply_migration():
    """Застосовує міграцію для додавання колонки entry_offset_percent"""
    try:
        # Підключення до БД
        conn = await asyncpg.connect(
            host=CONFIG['DB_HOST'],
            port=CONFIG['DB_PORT'],
            database=CONFIG['DB_NAME'],
            user=CONFIG['DB_USER'],
            password=CONFIG['DB_PASS']
        )
        
        logger.info("✅ Підключено до БД")
        
        # Виконати міграцію
        await conn.execute("""
            ALTER TABLE settings 
            ADD COLUMN IF NOT EXISTS entry_offset_percent DECIMAL(10, 4) DEFAULT 0.01
        """)
        
        logger.info("✅ Міграцію застосовано: додано колонку entry_offset_percent")
        
        # Перевірити результат
        result = await conn.fetch("SELECT entry_offset_percent FROM settings LIMIT 1")
        if result:
            logger.info(f"✅ Перевірка: entry_offset_percent = {result[0]['entry_offset_percent']}")
        
        await conn.close()
        logger.info("✅ З'єднання закрито")
        
    except Exception as e:
        logger.error(f"❌ Помилка застосування міграції: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(apply_migration())
