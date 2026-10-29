import asyncio
import logging
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher
from aiogram.types import Message, BusinessConnection
from groq import AsyncGroq

# =========================================================
# НАСТРОЙКИ
# =========================================================
BOT_TOKEN = os.getenv("API_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

AI_PROMPT = (
    "Ты — ассистент Ирины Прохорчук, премиального риелтора и основателя аукционного метода продажи в СПб. "
    "Ты общаешься с клиентами от ее имени в мессенджере. "
    "ПРАВИЛА ОБЩЕНИЯ:\n"
    "1. Если человек пишет только приветствие (например, 'Привет', 'Здравствуйте') и не указывает суть, "
    "вежливо поздоровайся от лица ассистента Ирины и спроси, по какому вопросу он обращается (покупка, продажа, инвестиции и т.д.).\n"
    "2. Если человек сразу пишет суть (например, 'хочу купить квартиру' или 'сколько стоит дом'), не спрашивай, что ему нужно. "
    "Ответь на его вопрос и предложи связаться с Ириной или воспользоваться ее ботом @IrinaProhorchyk_bot.\n"
    "3. Если человек просит передать диалог Ирине или заканчивает общение, попрощайся: 'Да, конечно, приятно было с вами пообщаться! Всего доброго!'\n"
    "4. Пиши коротко, как живой человек. Без длинных тире и сложного форматирования. Не пиши шаблонными фразами, веди себя естественно."
    "5: Всегда гоаори сто ты асистент ну при первом соо и спрашивай именно с каким вопросм обращаеться человек а не используй покупка продажа инвестиции и тп"
    "6: первое сообщение всегла начинай с того чтобы поприветствовать представится и подсказать"
    "7: Когда пррсят перевести на ирину обязательно лобавляй в игнор лист и все говори приятно было с вами пообщаться и тд"
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# Хранилище истории
chat_histories = defaultdict(list)
# Список тех, с кем попрощались
finished_chats = set()

# Кэш владельцев бизнес-аккаунтов (чтобы не дергать API каждый раз)
business_owners = {}

# =========================================================
# ОБРАБОТЧИКИ
# =========================================================

async def handle_business_connection(connection: BusinessConnection):
    logger.info(f"Подключение: {connection.id} (enabled={connection.is_enabled}, user_id={connection.user.id})")
    # Запоминаем владельца бизнес-аккаунта для этого подключения
    business_owners[connection.id] = connection.user.id

async def handle_business_message(message: Message):
    if not message.text or (message.from_user and message.from_user.is_bot):
        return

    connection_id = message.business_connection_id
    if not connection_id:
        logger.warning("Сообщение без business_connection_id, пропускаем.")
        return

    user_id = message.from_user.id
    logger.info(f"Получено сообщение от {user_id} (conn={connection_id}): {message.text[:50]}...")

    # --- ПРОВЕРКА 1: Игнорируем сообщения от самого владельца бизнес-аккаунта ---
    owner_id = business_owners.get(connection_id)
    if owner_id is None:
        # Если кэш пуст (например, бот перезапустился), получаем данные вручную
        try:
            conn = await bot.get_business_connection(connection_id)
            owner_id = conn.user.id
            business_owners[connection_id] = owner_id
            logger.info(f"Получен владелец для {connection_id}: {owner_id}")
        except Exception as e:
            logger.error(f"Не удалось получить данные о подключении {connection_id}: {e}")

    if owner_id and user_id == owner_id:
        logger.info(f"Сообщение от владельца бизнес-аккаунта ({user_id}). Игнорируем.")
        return

    # --- ПРОВЕРКА 2: Игнорируем, если диалог завершен ---
    if user_id in finished_chats:
        logger.info(f"Сообщение от {user_id} проигнорировано (мы уже попрощались).")
        return

    user_text = message.text.strip()
    chat_histories[user_id].append({"role": "user", "content": user_text})
    history_to_send = chat_histories[user_id][-10:]

    # --- ПРОВЕРКА 3: Статус "печатает" (ошибка здесь не должна ломать логику) ---
    try:
        await bot.send_chat_action(
            chat_id=user_id,
            action="typing",
            business_connection_id=connection_id,
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить статус 'печатает': {type(e).__name__}: {e}")

    # --- ЗАПРОС К GROQ ---
    try:
        response = await groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": AI_PROMPT},
                *history_to_send
            ],
            temperature=0.7,
            max_tokens=1024,
        )
        answer = response.choices[0].message.content
        if not answer:
            logger.warning("Groq вернул пустой ответ.")
            return

        answer = answer.replace("—", "-").strip()
        logger.info(f"Ответ Groq: {answer[:100]}...")

    except Exception as e:
        logger.error(f"Ошибка при запросе к Groq: {type(e).__name__}: {e}", exc_info=True)
        return

    # --- ОТПРАВКА ОТВЕТА В TELEGRAM ---
    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=answer,
            business_connection_id=connection_id,
        )
        logger.info(f"Telegram OK: message_id={sent.message_id}, chat_id={sent.chat.id}, user_id={user_id}")
        
        # Сохраняем ответ бота
        chat_histories[user_id].append({"role": "assistant", "content": answer})

    except Exception as e:
        # Здесь мы увидим точную причину, если Telegram отклонил отправку
        logger.error(
            f"Telegram FAIL при отправке {user_id} (conn={connection_id}): "
            f"{type(e).__name__}: {e}",
            exc_info=True
        )
        return

    # --- ПРОВЕРКА 4: Завершение диалога ---
    if ("воспользоваться ботом Ирины" in answer or
        "свяжется с вами" in answer or
        "до свидания" in answer.lower() or
        "всего доброго" in answer.lower()):

        logger.info(f"Диалог с {user_id} завершен. Добавляю в игнор-лист.")
        finished_chats.add(user_id)
        chat_histories.pop(user_id, None)

# Регистрация
dp.business_connection.register(handle_business_connection)
dp.business_message.register(handle_business_message)

# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    logger.info("Ассистент запущен...")
    await dp.start_polling(
        bot,
        allowed_updates=["business_connection", "business_message"],
    )

if __name__ == "__main__":
    asyncio.run(main())
