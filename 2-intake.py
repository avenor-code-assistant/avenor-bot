"""Manual application intake. No broker transactions or access grants."""
import json
import re
import time
import uuid
from pathlib import Path
from telegram.constants import ChatType
from telegram.error import TelegramError

COPY = json.loads(Path(__file__).with_name('intake_copy.json').read_text())
ACCOUNT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{2,39}\Z')


def wording(key, reference='', uid='', account=''):
    return (COPY[key].replace('{مرجع الطلب}', str(reference))
            .replace('{رقم المستخدم}', str(uid)).replace('{رقم حساب التداول}', str(account)))


class Intake:
    def __init__(self, store, admin, contact):
        self.store, self.admin, self.contact = store, admin, contact
        self.store.db.executescript('''CREATE TABLE IF NOT EXISTS applications(
          user_id INTEGER PRIMARY KEY, reference TEXT NOT NULL, status TEXT NOT NULL,
          updated INTEGER NOT NULL, admin_message_id INTEGER);''')
        self.store.db.commit()

    def row(self, uid):
        return self.store.db.execute('SELECT * FROM applications WHERE user_id=?', (uid,)).fetchone()

    def put(self, uid, ref, status, mid=None):
        with self.store.db:
            self.store.db.execute('''INSERT INTO applications VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET reference=excluded.reference,
            status=excluded.status,updated=excluded.updated,admin_message_id=excluded.admin_message_id''',
            (uid, ref, status, int(time.time()), mid))

    async def apply(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            await update.effective_message.reply_text(wording('محاولة التقديم في الجروب'))
            return
        if not self.admin:
            await update.effective_message.reply_text(wording('التقديم غير متاح أثناء الإعداد'))
            return
        uid = update.effective_user.id
        prior = self.row(uid)
        if prior and prior['status'] in ('submitted', 'sending', 'uncertain'):
            await self.status(update, context)
            return
        self.put(uid, uuid.uuid4().hex[:12], 'awaiting')
        await update.effective_message.reply_text(wording('/apply'))

    async def photo(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            return
        uid = update.effective_user.id
        row = self.row(uid)
        msg = update.effective_message
        if not row or row['status'] != 'awaiting':
            await msg.reply_text(wording('صورة بدون طلب نشط'))
            return
        if getattr(msg, 'media_group_id', None):
            await msg.reply_text(wording('ألبوم صور أثناء التقديم'))
            return
        account = (msg.caption or '').strip()
        if not ACCOUNT.fullmatch(account):
            await msg.reply_text(wording('وصف صورة غير مقبول'))
            return
        # Metadata only. Never save account identifiers, captions or photo IDs.
        self.put(uid, row['reference'], 'sending')
        try:
            sent = await context.bot.send_photo(
                chat_id=self.admin, photo=msg.photo[-1].file_id, protect_content=True,
                caption=wording('إشعار الطلب في خاص علي، مع الصورة', row['reference'], uid, account))
        except TelegramError:
            self.put(uid, row['reference'], 'uncertain')
            await msg.reply_text(wording('تعذر تأكيد الإرسال', row['reference']))
            return
        self.put(uid, row['reference'], 'submitted', sent.message_id)
        await msg.reply_text(wording('تأكيد إرسال الطلب', row['reference']))

    async def status(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            return
        row = self.row(update.effective_user.id)
        keys = {'awaiting': '/application: في انتظار الصورة',
                'sending': '/application: وصول غير مؤكد',
                'uncertain': '/application: وصول غير مؤكد',
                'submitted': '/application: تم الإرسال'}
        await update.effective_message.reply_text(
            wording(keys[row['status']], row['reference']) if row else wording('/application: لا يوجد طلب'))

    async def cancel(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            return
        uid = update.effective_user.id
        row = self.row(uid)
        if row and row['status'] in ('submitted', 'sending', 'uncertain'):
            await update.effective_message.reply_text(wording('/cancel: تم الإرسال أو وصول غير مؤكد', row['reference']))
            return
        with self.store.db:
            self.store.db.execute('DELETE FROM applications WHERE user_id=?', (uid,))
        await update.effective_message.reply_text(wording('/cancel: طلب غير مرسل'))

    async def other(self, update, context):
        if update.effective_chat.type != ChatType.PRIVATE:
            return
        row = self.row(update.effective_user.id)
        if not row or row['status'] != 'awaiting':
            return
        key = ('ملف أو صورة كمستند أثناء التقديم' if getattr(update.effective_message, 'document', None)
               else 'رسالة نصية أثناء انتظار الصورة')
        await update.effective_message.reply_text(wording(key))
