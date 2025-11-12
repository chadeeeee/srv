"""
Скрипт для застосування міграції trigger_logic
Додає налаштування для логіки trigger (Gravity2 SHORT)
"""
import asyncio
import asyncpg
from loguru import logger
from config import CONFIG

async def apply_migration():
    """Застосовує міграцію для додавання колонок trigger_logic"""
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
        
        # Виконати міграцію - читаємо з файлу SQL
        with open('migrations/add_trigger_logic_settings.sql', 'r', encoding='utf-8') as f:
            migration_sql = f.read()
        
        # Розділяємо на окремі команди (по крапці з комою)
        commands = [cmd.strip() for cmd in migration_sql.split(';') if cmd.strip() and not cmd.strip().startswith('--')]
        
        for cmd in commands:
            try:
                await conn.execute(cmd)
                logger.info(f"✅ Виконано команду: {cmd[:50]}...")
            except Exception as e:
                logger.warning(f"⚠ Команда вже виконана або помилка: {e}")
        
        logger.info("✅ Міграцію застосовано: додано колонки trigger_logic")
        
        # Перевірити результат
        result = await conn.fetch("""
            SELECT 
                trigger_wait_enabled, 
                trigger_wait_seconds, 
                trigger_candle_check_enabled,
                trigger_timeout_minutes
            FROM settings LIMIT 1
        """)
        
        if result:
            row = result[0]
            logger.info("✅ Перевірка нових колонок:")
            logger.info(f"   - trigger_wait_enabled: {row['trigger_wait_enabled']}")
            logger.info(f"   - trigger_wait_seconds: {row['trigger_wait_seconds']}")
            logger.info(f"   - trigger_candle_check_enabled: {row['trigger_candle_check_enabled']}")
            logger.info(f"   - trigger_timeout_minutes: {row['trigger_timeout_minutes']}")
        
        await conn.close()
        logger.info("✅ З'єднання закрито")
        
    except Exception as e:
        logger.error(f"❌ Помилка застосування міграції: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    asyncio.run(apply_migration())
