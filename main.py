import os
import time
import shutil
import zipfile
import aiohttp
import requests
import sqlite3 as sl
import asyncio
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from yoomoney import Quickpay, Client
from aiogram import Bot, types
from aiogram.dispatcher import Dispatcher, FSMContext
from aiogram.utils import executor
from config import TOKEN, KEY_ACCESS, token_yoomoney
from aiogram.dispatcher.filters.state import StatesGroup, State

bot = Bot(token=TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
link_to_site = "https://antiznak.ru/api/v2.php?"
processing_lock = asyncio.Lock()

# словарь с ошибками при обработке запроса
# ключ содержит номер ошибки
# значение содержит 1) текстовое опичание ошибки 2) Если значение равно 1 то вычетаем из баланса попытку
error_code = {"102": ["Не указана ссылка на объявление", 0],
              "105": ["Отрицательный баланс ключа", 0],
              "210": ["Объявление не найдено", 0],
              "220": ["Не найдены фотографии в объявлении", 1],
              "230": ["Объявление более не актуально", 1],
              "225": ["При обработке фото произошла ошибка", 0],
              "500": ["Технические неполадки. Уже решаем проблему, повторите позднее...", 0]}


@dp.message_handler(commands=['start'])
async def process_start_command(message: types.Message):
    await bot.send_message(message.chat.id, "Привет")
    con = sl.connect("users.db")
    with con:
        data = con.execute(f"SELECT user_id FROM user WHERE user_id = '{message.from_user.id}'")
        if data.fetchone() is None:
            with con:
                con.execute(f"INSERT INTO user (user_id) VALUES ({message.from_user.id})")


@dp.message_handler(commands=['balance'])
async def balance_info(message: types.Message):
    con = sl.connect('users.db')
    with con:
        balance = con.execute(f"SELECT balance FROM user WHERE user_id = '{message.from_user.id}'").fetchone()[0]
        await bot.send_message(message.chat.id, f"Ваш баланс {balance}")


class BuyState(StatesGroup):
    count = State()


@dp.message_handler(commands=['buy'])
async def buy_info(message: types.Message):
    await bot.send_message(message.chat.id, "Введите количество объявлений вы хотите приобрести")
    await BuyState.count.set()


@dp.message_handler(state=BuyState.count)
async def get_link(message: types.Message, state: FSMContext):
    await state.update_data(username=message.text)
    if message.text.isdigit() and int(message.text) > 0:
        con = sl.connect('users.db')
        with con:
            label = con.execute(f"SELECT user_id, count_buy FROM user WHERE user_id = '{message.from_user.id}'").fetchone()
            quickpay = Quickpay(
                receiver="4100118561777997",
                quickpay_form="shop",
                targets="Sponsor this project",
                paymentType="SB",
                sum=10 * int(message.text),
                label=str(label[0]) + ":" + str(label[1])
            )
        await bot.send_message(message.chat.id, f"{quickpay.redirected_url}")
    else:
        await bot.send_message(message.chat.id, "Ошибка")
    await state.finish()


@dp.message_handler(commands=['confirm'])
async def confirm(message: types.Message):
    try:
        client = Client(token_yoomoney)
        con = sl.connect('users.db')
        label = ":"
        with con:
            data = con.execute(f"SELECT user_id, count_buy FROM user WHERE user_id = '{message.from_user.id}'").fetchone()
            label = str(data[0]) + label + str(data[1])
        history = client.operation_history(label=label)
        if history.operations == []:
            await bot.send_message(message.chat.id, "Запрос не найдет")
        else:
            for operation in history.operations:
                if operation.status == 'success':
                    con = sl.connect('users.db')
                    with con:
                        count = int(operation.amount) / 10
                        data = con.execute(f"UPDATE user SET balance = balance + {count}, count_buy = count_buy + 1 WHERE user_id = '{message.from_user.id}'")
                else:
                    await bot.send_message(message.chat.id, "Оплата не прошла")
    except Exception as e:
        await bot.send_message(message.chat.id, "Ошибка")


class RemoveState(StatesGroup):
    remove = State()


@dp.message_handler(commands=['remove'])
async def remove_znak(message: types.Message):
    await bot.send_message(message.chat.id, "Введите ссылку на объявление")
    await RemoveState.remove.set()


# функция получения json
def get_anti_znak(msg):
    response = requests.get(link_to_site + "k=" + KEY_ACCESS + "&u=" + msg)
    data = response.json()
    return data


@dp.message_handler(state=RemoveState.remove)
async def remove(msg: types.Message, state: FSMContext):
    if processing_lock.locked():
        await bot.send_message(msg.from_user.id, "Ваш заказ принят ,пожалуйста, подождите, пока дойдет ваша очередь"
                                                 " обработки заказа")
    async with processing_lock:
        con = sl.connect('users.db')
        with con:
            balance = con.execute(f"SELECT balance FROM user WHERE user_id = '{msg.from_user.id}'").fetchone()[0]

        if balance < 1:
            await bot.send_message(msg.from_user.id, "Пожалуйста пополните баланс")
            await state.finish()
            return
        file_json = get_anti_znak(msg.text)
        sent_massage = None

        while file_json["status"] != "error" and file_json["status"] != "done":
            time.sleep(4)
            file_json = get_anti_znak(msg.text)
            if sent_massage is not None:
                await bot.edit_message_text(chat_id=msg.from_user.id, message_id=sent_massage.message_id,
                                            text=f"Процесс удаление водяных знаков: {file_json['status']}")
            else:
                sent_massage = await bot.send_message(msg.from_user.id,
                                                      f"Процесс удаление водяных знаков: {file_json['status']}")

        if sent_massage is not None:
            await bot.delete_message(chat_id=msg.from_user.id, message_id=sent_massage.message_id)

        if file_json["status"] == "error":
            if file_json["err_code"] in error_code:
                await bot.send_message(msg.from_user.id, error_code[file_json["err_code"]][0])
                balance -= error_code[file_json["err_code"]][1]
                with con:
                    con.execute(f"UPDATE user SET balance = '{balance}' WHERE user_id = '{msg.from_user.id}'")
            else:
                await bot.send_message(msg.from_user.id, "При обработке фото произошла неизвестная ошибка")
            await state.finish()
        elif file_json["status"] == "done":
            sent_massage = await bot.send_message(msg.from_user.id, "Формирование архива фотографий началось")

            temp_dir = "temp" + file_json["ID"]

            os.makedirs(temp_dir, exist_ok=True)

            for photo in file_json["photos"]:
                async with aiohttp.ClientSession() as session:
                    async with session.get(file_json["photos"][photo]) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            with open(f"{temp_dir}/photo{photo}.png", "wb") as f:
                                f.write(data)

            zip_filename = f"photos{file_json['ID']}.zip"
            with zipfile.ZipFile(f"{zip_filename}", 'w') as zipf:
                for root, _, files in os.walk(temp_dir):
                    for file in files:
                        zipf.write(os.path.join(root, file), file)

            if sent_massage is not None:
                await bot.delete_message(chat_id=msg.from_user.id, message_id=sent_massage.message_id)

            with open(f"{zip_filename}", 'rb') as f:
                await bot.send_document(msg.chat.id, f)

            shutil.rmtree(temp_dir)
            os.remove(f"{zip_filename}")

            info_ad = file_json["title"] + "\n" + file_json["address"] + "\n" + file_json["price"]

            balance -= 1
            con = sl.connect('users.db')

            with con:
                con.execute(f"UPDATE user SET balance = '{balance}' WHERE user_id = '{msg.from_user.id}'")

            await bot.send_message(msg.chat.id, info_ad)
            await state.finish()


@dp.message_handler()
async def remove(msg: types.Message):
    await bot.send_message(msg.chat.id, "Выберите действие: \n"
                                        "/buy\n"
                                        "/remove\n"
                                        "/balance\n")


if __name__ == '__main__':
    executor.start_polling(dp)
