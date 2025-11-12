"""
Скрипт для застосування міграції limit_order_lifetime
"""
import asyncio
import asyncpg
from loguru import logger
from config import CONFIG

async def apply_migration():
    """Застосовує міграцію для додавання колонки limit_order_lifetime"""
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
        
        # Виконати міграцію - додати колонку
        await conn.execute("""
            ALTER TABLE settings 
            ADD COLUMN IF NOT EXISTS limit_order_lifetime TEXT
        """)
        
        logger.info("✅ Міграцію застосовано: додано колонку limit_order_lifetime")
        
        # Встановити значення за замовчуванням для існуючих записів
        await conn.execute("""
            UPDATE settings 
            SET limit_order_lifetime = '5' 
            WHERE limit_order_lifetime IS NULL
        """)
        
        logger.info("✅ Встановлено значення за замовчуванням: 5 хвилин")
        
        # Перевірити результат
        result = await conn.fetch("SELECT limit_order_lifetime FROM settings LIMIT 1")
        if result:
            logger.info(f"✅ Перевірка: limit_order_lifetime = {result[0]['limit_order_lifetime']}")
        
        await conn.close()
        logger.info("✅ З'єднання закрито")
        
    except Exception as e:
        logger.error(f"❌ Помилка застосування міграції: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(apply_migration())
