#!/usr/bin/env python3
"""Community + private-channel access bot. Run exactly one instance."""
import asyncio
import hashlib
import logging
import os
import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ChatMemberStatus, ChatType, MessageEntityType
from telegram.error import TelegramError, Forbidden
from telegram.ext import (Application, CommandHandler, MessageHandler, ChatJoinRequestHandler,
                          ChatMemberHandler, PreCheckoutQueryHandler, filters)
from storage import Store

os.umask(0o077)
load_dotenv(Path(__file__).with_name('.env'))
TOKEN = os.getenv('BOT_TOKEN', '')
ADMIN = int(os.getenv('ADMIN_ID', '0'))
GROUP = int(os.getenv('GROUP_ID', '0'))
CHANNEL = int(os.getenv('CHANNEL_ID', '0'))
DAYS = int(os.getenv('SUBSCRIPTION_DAYS', '30'))
PRICE = int(os.getenv('STAR_PRICE', '0'))
SALES = os.getenv('ENABLE_STARS', 'false').lower() == 'true'
READY = bool(ADMIN and GROUP)
ACCESS = os.getenv('ENABLE_CHANNEL_ACCESS', 'false').lower() == 'true'
WEBHOOK_URL = os.getenv('RENDER_EXTERNAL_URL', '').rstrip('/')
CONTACT = os.getenv('ADMIN_CONTACT', 'ابعت /support في الخاص')
COMMUNITY = os.getenv('COMMUNITY_URL', '')
RULES = os.getenv('GROUP_RULES', 'احترام الجميع. ممنوع السبام والروابط والتحويلات من غير إذن. التداول فيه مخاطرة ومفيش ربح مضمون.')
FAQ = os.getenv('SIGNALS_FAQ', 'القناة فيها توصيات تداول. التوصية مش ضمان للربح، وممكن تخسر رأس مالك. إدارة المخاطر مسؤوليتك.')
TERMS = os.getenv('TERMS_TEXT', 'لم يتم تحديد شروط البيع بعد.')
REFUNDS = os.getenv('REFUND_TEXT', 'لم يتم تحديد سياسة الاسترداد بعد.')
DB_PATH = os.getenv('DB_PATH', './data/bot.sqlite3')
BACKUP = Path(os.getenv('BACKUP_DIR', './backups'))
STORE = Store(DB_PATH)
LOCK = asyncio.Lock()
LOG = logging.getLogger('community')
# Suppress HTTP URLs, which contain the token. Never log complete updates.
logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logging.getLogger('httpx').setLevel(logging.CRITICAL)
logging.getLogger('httpcore').setLevel(logging.CRITICAL)
FLOOD = defaultdict(deque)
COOLDOWN = {}
ALERTS = {}
LAST_BACKUP = 0
URL = re.compile(r'(?:https?://|www\.|t\.me/|telegram\.me/|tg://|(?:[a-z0-9-]+\.)+(?:com|net|org|io|me|eg)\b)', re.I)


def stamp(ts):
    # Explicit UTC in messages avoids server/user timezone ambiguity.
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def keyboard():
    # Commands are used instead of callback buttons to keep navigation simple.
    return InlineKeyboardMarkup([[InlineKeyboardButton('الجروب المجاني', url=COMMUNITY)]]) if COMMUNITY else None


async def alert(bot, key, text):
    now = time.time()
    if not ADMIN or now - ALERTS.get(key, 0) < 3600:
        return
    ALERTS[key] = now
    try:
        await bot.send_message(ADMIN, text)
    except TelegramError:
        LOG.warning('Admin alert could not be delivered: %s', key)


async def dm(bot, uid, text, **kwargs):
    try:
        await bot.send_message(uid, text, **kwargs)
        return True
    except TelegramError:
        return False


async def owner(update):
    return bool(READY and update.effective_user and update.effective_user.id == ADMIN
                and update.effective_chat.type == ChatType.PRIVATE)


async def group_admin(bot, uid):
    if uid == ADMIN:
        return True
    member = await bot.get_chat_member(GROUP, uid)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def identify(update, context):
    msg = update.effective_message
    user = update.effective_user
    text = f'User ID: {user.id if user else "unknown"}\nChat ID: {update.effective_chat.id}'
    origin = msg.forward_origin
    if origin and getattr(origin, 'chat', None):
        text += f'\nForwarded channel ID: {origin.chat.id}'
    await msg.reply_text(text)


async def start(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    STORE.remember(update.effective_user.id, update.effective_user.full_name)
    if not ACCESS:
        await update.message.reply_text('أهلاً بيك في Avenor.\n/faq معلومات عن التوصيات\n/support تواصل مع الإدارة\n/id رقم حسابك\n/help المساعدة\nإدارة دخول القناة والاشتراكات لسه مش مفعلة.', reply_markup=keyboard())
        return
    await update.message.reply_text(
        'أهلاً بيك. الجروب مجاني، والقناة باشتراك.\n'
        '/faq معلومات عن التوصيات\n/subscribe تفاصيل الاشتراك\n/status حالة اشتراكك\n'
        '/join لينك دخول القناة\n/support تواصل مع الإدارة\n/terms الشروط\n'
        '/privacy الخصوصية\n/id رقم حسابك\n/help المساعدة', reply_markup=keyboard())


async def help_cmd(update, context):
    if not ACCESS:
        text = '/start /faq /support /privacy /id /help\nإدارة دخول القناة والاشتراكات غير مفعلة. لا يوجد بيع أو منح دخول أو إزالة تلقائية عند انتهاء المدة.'
        if await owner(update):
            text += '\nأوامر إدارة الجروب في الخاص: /announce /mute /unmute /ban /unban /health'
        await update.effective_message.reply_text(text)
        return
    if await owner(update):
        text = ('أوامر الإدارة في الخاص:\n'
                '/grant USER_ID DAYS REFERENCE\n/approve USER_ID DAYS REFERENCE (نفس grant)\n'
                '/revoke USER_ID\n/subs\n/announce نص الإعلان\n'
                '/mute USER_ID MINUTES\n/unmute USER_ID\n/ban USER_ID\n/unban USER_ID\n'
                '/refund CHARGE_ID\n/health\n'
                'REFERENCE سبب أو مرجع فريد، وإعادة نفس الأمر لا تزود المدة مرتين.\n'
                'الإعلان رسالة واحدة للجروب، مش منشن لكل الأعضاء. /grant للهدية أو استحقاق قائم، مش تحصيل خارجي داخل البوت.')
    else:
        text = '/start /faq /subscribe /status /join /support /paysupport /terms /privacy /id'
    await update.effective_message.reply_text(text)


async def faq(update, context):
    if READY:
        await update.effective_message.reply_text(FAQ)


async def support(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text('ابعت الأمر في خاص البوت عشان خصوصيتك.')
        return
    uid = update.effective_user.id
    STORE.remember(uid, update.effective_user.full_name)
    if uid != ADMIN:
        await alert(context.bot, f'support-{uid}', f'طلب دعم من User ID: {uid}. المستخدم يقدر يبعث لك التفاصيل مباشرة. التواصل المعروض: {CONTACT}')
    await update.effective_message.reply_text(f'التواصل مع الإدارة: {CONTACT}\nرقم حسابك: {uid}\nللدفع ابعت رقم عملية Stars للإدارة، مش بيانات بطاقة أو كلمة سر.')


async def terms(update, context):
    await update.effective_message.reply_text(f'{TERMS}\n\nسياسة الاسترداد:\n{REFUNDS}\n\n{FAQ}')


async def privacy(update, context):
    await update.effective_message.reply_text(
        'البوت بيحفظ رقم حسابك واسم تيليجرام بعد /start، ومدة الاشتراك وروابط الدخول '
        'ومراجع المنح ومعرّفات عمليات Stars إذا تم تفعيل إدارة الاشتراكات مستقبلاً. '
        'بيفحص رسائل الجروب للحماية والردود، لكن مش بيحفظ نص المحادثات في قاعدة البيانات. '
        'البيانات محفوظة عند مزود قاعدة بيانات خارجي. الإدارة مسؤولة عن حماية البيانات '
        'وتحديد مدة الاحتفاظ القانونية. لطلب مراجعة أو حذف البيانات اتواصل مع الإدارة. '
        'ممنوع تبعت كلمات سر أو بيانات بطاقات أو إثبات دفع فيه بيانات حساسة.')


async def subscribe(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not READY:
        await update.effective_message.reply_text('البوت لسه تحت الإعداد.')
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        me = context.bot.username
        await update.effective_message.reply_text(f'تفاصيل الاشتراك في خاص البوت: @{me} ثم /subscribe')
        return
    STORE.remember(update.effective_user.id, update.effective_user.full_name)
    text = f'مدة الدخول: {DAYS} يوم من التفعيل، والتجديد بيضاف على الوقت المتبقي.\nالتجديد مش تلقائي.\n'
    text += f'السعر: {PRICE} Telegram Stars.\nاقرأ /terms وبعد الموافقة ابعت /buy.' if SALES else 'البيع لسه مش مفعّل. ابعت /support للاستفسار.'
    await update.message.reply_text(text)


async def status(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    row = STORE.sub(update.effective_user.id)
    text = f'اشتراكك شغال لحد {stamp(row["expires"])}' if row and STORE.active(update.effective_user.id) else 'مفيش اشتراك ساري. /subscribe'
    await update.message.reply_text(text)


async def issue_invite(bot, uid):
    """Caller holds LOCK. Link is bound to the approved user, not first clicker."""
    row = STORE.sub(uid)
    if not STORE.active(uid):
        raise ValueError('No active subscription')
    if row['invite'] and row['invite_until'] > time.time() + 30:
        return row['invite']
    if row['invite']:
        try:
            await bot.revoke_chat_invite_link(CHANNEL, row['invite'])
        except TelegramError:
            pass  # Expired links cannot admit anyone. Unknown links are identity-gated too.
    await bot.unban_chat_member(CHANNEL, uid, only_if_banned=True)
    until = min(int(time.time()) + 86400, row['expires'])
    link = await bot.create_chat_invite_link(
        CHANNEL, name=f'u{uid}', expire_date=datetime.fromtimestamp(until, timezone.utc),
        creates_join_request=True)
    STORE.set_invite(uid, link.invite_link, until)
    return link.invite_link


async def send_access(bot, uid, end):
    try:
        link = await issue_invite(bot, uid)
        ok = await dm(bot, uid, f'الدخول مفعّل لحد {stamp(end)}.\n'
                      f'ده لينك طلب دخول خاص بحسابك، صالح لمدة أقصاها 24 ساعة:\n{link}\n'
                      'لو أنت في القناة بالفعل، مش محتاج تدخل تاني. /join لتجديد اللينك.')
        if not ok:
            await alert(bot, f'dm-{uid}', f'الاشتراك محفوظ للحساب {uid} لكن رسالة الدخول ماوصلتش. خليه يفتح البوت ويبعت /join.')
    except TelegramError:
        await alert(bot, f'invite-{uid}', f'الاشتراك محفوظ للحساب {uid} لكن إنشاء لينك الدخول فشل. راجع صلاحية دعوة الأعضاء وطردهم، وبعدها المستخدم يبعت /join.')


async def join(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not READY or update.effective_chat.type != ChatType.PRIVATE:
        return
    uid = update.effective_user.id
    async with LOCK:
        if not STORE.active(uid):
            await update.message.reply_text('مفيش اشتراك ساري. /subscribe')
            return
        await send_access(context.bot, uid, STORE.sub(uid)['expires'])


async def grant(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not await owner(update):
        return
    try:
        uid, days = map(int, context.args[:2])
        reason = ' '.join(context.args[2:]).strip()
        if uid <= 0 or not 1 <= days <= 3660 or not reason or len(reason) > 150:
            raise ValueError()
        if not STORE.known(uid):
            await update.message.reply_text('المستخدم لازم يفتح خاص البوت ويبعت /start الأول. بعدها خد منه /id.')
            return
    except (ValueError, TypeError):
        await update.message.reply_text('استخدم /grant USER_ID DAYS UNIQUE_REFERENCE مثال: /grant 12345678 30 gift-001')
        return
    async with LOCK:
        # Same reference for the same member is idempotent, even in a new message.
        end, fresh = STORE.grant(uid, days, f'admin:{uid}:{reason}', reason)
        await send_access(context.bot, uid, end)
    await update.message.reply_text(f'{"تم التفعيل" if fresh else "المرجع ده متسجل قبل كده، المدة ماتزودتش"}. {uid}\nالنهاية: {stamp(end)}')


async def join_request(update, context):
    if not ACCESS:
        return
    req = update.chat_join_request
    if not READY or req.chat.id != CHANNEL:
        return
    uid = req.from_user.id
    async with LOCK:
        row = STORE.sub(uid)
        seen_link = req.invite_link.invite_link if req.invite_link else None
        if row and STORE.active(uid) and row['invite'] and row['invite'] == seen_link:
            await context.bot.approve_chat_join_request(CHANNEL, uid)
            try:
                await context.bot.revoke_chat_invite_link(CHANNEL, row['invite'])
            finally:
                STORE.set_invite(uid, None, 0)
        else:
            await context.bot.decline_chat_join_request(CHANNEL, uid)


async def channel_member(update, context):
    if not ACCESS:
        return
    change = update.chat_member
    if not READY or change.chat.id != CHANNEL:
        return
    member = change.new_chat_member
    if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    uid = member.user.id
    async with LOCK:
        if uid != ADMIN and uid != context.bot.id and not STORE.active(uid):
            await context.bot.ban_chat_member(CHANNEL, uid)
            await alert(context.bot, f'untracked-{uid}', f'تم منع دخول حساب {uid} للقناة لأنه مش مسجل باشتراك ساري. لو ده عضو قديم، اعمله /grant الأول.')


async def buy(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not READY or not SALES or update.effective_chat.type != ChatType.PRIVATE:
        await subscribe(update, context)
        return
    uid = update.effective_user.id
    STORE.remember(uid, update.effective_user.full_name)
    payload = uuid.uuid4().hex
    STORE.invoice(payload, uid, PRICE, DAYS)
    await update.message.reply_invoice(
        title=f'دخول القناة {DAYS} يوم', description='اشتراك غير متجدد تلقائياً. بعد قراءة /terms، الدفع يعني الموافقة على الشروط. التداول ينطوي على خسارة.',
        payload=payload, currency='XTR', prices=[LabeledPrice(f'{DAYS} يوم', PRICE)], provider_token='')


async def checkout(update, context):
    if not ACCESS:
        await update.pre_checkout_query.answer(ok=False, error_message='البيع وإدارة الدخول غير مفعلين.')
        return
    q = update.pre_checkout_query
    inv = STORE.get_invoice(q.invoice_payload)
    valid = bool(READY and SALES and inv and inv['user_id'] == q.from_user.id
                 and q.currency == 'XTR' and inv['amount'] == q.total_amount
                 and time.time() - inv['created'] < 86400)
    if valid:
        try:
            rights = await asyncio.wait_for(context.bot.get_chat_member(CHANNEL, context.bot.id), timeout=4)
            valid = bool(rights.status == ChatMemberStatus.ADMINISTRATOR and rights.can_invite_users and rights.can_restrict_members)
        except (TelegramError, asyncio.TimeoutError):
            valid = False
    await q.answer(ok=valid, error_message=None if valid else 'الفاتورة انتهت أو البيع مش متاح. افتح /buy من جديد.')


async def successful_payment(update, context):
    if not ACCESS:
        return
    payment = update.message.successful_payment
    uid = update.effective_user.id
    async with LOCK:
        if payment.currency != 'XTR':
            raise ValueError('Unexpected payment currency')
        end, fresh = STORE.paid(payment.telegram_payment_charge_id, uid, payment.total_amount, payment.invoice_payload)
        await send_access(context.bot, uid, end)
    if fresh:
        await dm(context.bot, ADMIN, f'دفع Stars ناجح للحساب {uid}: {payment.total_amount} Stars.\nرقم العملية للاسترداد:\n{payment.telegram_payment_charge_id}\nالنهاية: {stamp(end)}')


async def refund(update, context):
    if not await owner(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text('/refund CHARGE_ID يرجع العملية بالكامل ويلغي كل مدة العضو المتبقية. استخدمه بعد مراجعة السياسة والعضو.')
        return
    charge = context.args[0]
    row = STORE.payment(charge)
    if not row or row['refunded']:
        await update.message.reply_text('العملية مش موجودة أو اتردت قبل كده.')
        return
    async with LOCK:
        await context.bot.refund_star_payment(row['user_id'], charge)
        STORE.mark_refunded(charge)
        STORE.expire(row['user_id'])
        await remove_expired(context.bot, row['user_id'])
    await update.message.reply_text('تم رد العملية بالكامل وإلغاء دخول العضو. راجع أي عمليات تانية له لو فيه تجديدات متعددة.')


async def remove_expired(bot, uid):
    row = STORE.sub(uid)
    if not row or STORE.active(uid) or row['removed']:
        return
    member = await bot.get_chat_member(CHANNEL, uid)
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await alert(bot, f'protected-{uid}', f'انتهى اشتراك {uid} لكنه أدمن/مالك. لازم تشيل صلاحياته بنفسك قبل الإزالة.')
        return
    # Stay banned after expiry. /join after renewal unbans only that member.
    await bot.ban_chat_member(CHANNEL, uid)
    STORE.flag(uid, 'removed')
    if row['invite']:
        try:
            await bot.revoke_chat_invite_link(CHANNEL, row['invite'])
        except TelegramError:
            pass
        STORE.set_invite(uid, None, 0)
    await dm(bot, uid, 'مدة دخولك للقناة انتهت واتوقف الدخول. للتجديد /subscribe. الجروب المجاني متاح.')


async def maintenance(context):
    global LAST_BACKUP
    if not READY or not ACCESS:
        return
    async with LOCK:
        now = int(time.time())
        for row in STORE.subscriptions():
            uid = row['user_id']
            try:
                if row['expires'] <= now:
                    await remove_expired(context.bot, uid)
                elif row['expires'] - now <= 2 * 86400 and not row['warned']:
                    delivered = await dm(context.bot, uid, f'تنبيه: اشتراكك بينتهي {stamp(row["expires"])}. التجديد من /subscribe.')
                    # A blocked bot cannot send warnings. Notify owner once, do not spam retries.
                    STORE.flag(uid, 'warned')
                    if not delivered:
                        await alert(context.bot, f'warning-{uid}', f'تعذر إرسال تنبيه انتهاء الاشتراك للحساب {uid}.')
            except TelegramError:
                await alert(context.bot, f'expiry-{uid}', f'تعذر فحص/إزالة اشتراك {uid}. راجع صلاحيات البوت واتصال السيرفر. هيعيد المحاولة خلال دقيقة.')
        if now - LAST_BACKUP >= 86400:
            BACKUP.mkdir(parents=True, exist_ok=True)
            path = BACKUP / f'bot-{datetime.now(timezone.utc):%Y-%m-%d}.sqlite3'
            STORE.backup(str(path))
            os.chmod(path, 0o600)
            for old in BACKUP.glob('bot-*.sqlite3'):
                if old.stat().st_mtime < time.time() - 7 * 86400:
                    old.unlink()
            LAST_BACKUP = now


async def revoke(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not await owner(update):
        return
    try:
        uid = int(context.args[0])
    except (ValueError, IndexError):
        await update.message.reply_text('/revoke USER_ID')
        return
    if not STORE.sub(uid):
        await update.message.reply_text('الحساب مش مسجل باشتراك.')
        return
    async with LOCK:
        STORE.expire(uid)
        await remove_expired(context.bot, uid)
    await update.message.reply_text('تم إلغاء المدة. الإزالة بتتكرر لو حصل خطأ. ده مش استرداد فلوس.')


async def subs(update, context):
    if not ACCESS:
        await update.effective_message.reply_text('إدارة دخول القناة والاشتراكات غير مفعلة في النسخة الحالية.')
        return
    if not await owner(update):
        return
    rows = STORE.subscriptions()
    active = sum(STORE.active(r['user_id']) for r in rows)
    await update.message.reply_text(f'الساري: {active} | المسجل: {len(rows)}\nآخر 40 سجل حسب تاريخ الانتهاء:\n' + '\n'.join(f'{r["user_id"]}: {stamp(r["expires"])}' for r in rows[:40]))


async def announce(update, context):
    if not await owner(update):
        return
    text = update.message.text.partition(' ')[2].strip()
    if not text:
        await update.message.reply_text('/announce نص الإعلان (رسالة واحدة للجروب من غير منشن جماعي)')
        return
    await context.bot.send_message(GROUP, text[:4000])
    await update.message.reply_text('الإعلان اتنشر في الجروب.')


async def moderate(update, context):
    if not await owner(update):
        return
    cmd = update.message.text.split()[0].split('@')[0][1:]
    try:
        uid = int(context.args[0])
        if uid <= 0 or uid == context.bot.id or await group_admin(context.bot, uid):
            await update.message.reply_text('مش هطبق الأمر على أدمن أو البوت.')
            return
        if cmd == 'mute':
            minutes = int(context.args[1]) if len(context.args) > 1 else 60
            if not 1 <= minutes <= 525000:
                raise ValueError()
            await context.bot.restrict_chat_member(GROUP, uid, ChatPermissions.no_permissions(),
                until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes), use_independent_chat_permissions=True)
        elif cmd == 'unmute':
            chat = await context.bot.get_chat(GROUP)
            await context.bot.restrict_chat_member(GROUP, uid, chat.permissions or ChatPermissions.all_permissions(), use_independent_chat_permissions=True)
        elif cmd == 'ban':
            await context.bot.ban_chat_member(GROUP, uid)
        elif cmd == 'unban':
            await context.bot.unban_chat_member(GROUP, uid, only_if_banned=True)
        STORE.event(cmd, uid, 'owner command')
        await update.message.reply_text('تم تنفيذ الأمر في الجروب المجاني.')
    except (ValueError, IndexError):
        await update.message.reply_text(f'/{cmd} USER_ID' + (' MINUTES (1 أو أكتر)' if cmd == 'mute' else ''))


def blocked_content(msg):
    entities = tuple(msg.entities or ()) + tuple(msg.caption_entities or ())
    return bool(msg.forward_origin or URL.search(msg.text or msg.caption or '') or
                any(e.type in (MessageEntityType.URL, MessageEntityType.TEXT_LINK) for e in entities))


async def group_message(update, context):
    msg = update.effective_message
    if not READY or update.effective_chat.id != GROUP:
        return
    # Linked channel discussion auto-forwards are explicitly allowed.
    if msg.sender_chat:
        if msg.sender_chat.id == GROUP or msg.sender_chat.id == CHANNEL:
            return
        # Other channel identities are not verifiable group admins, so block them.
        await msg.delete()
        return
    user = update.effective_user
    if not user or user.is_bot or await group_admin(context.bot, user.id):
        return
    now = time.time()
    bucket = FLOOD[user.id]
    while bucket and bucket[0] < now - 10:
        bucket.popleft()
    bucket.append(now)
    if blocked_content(msg) or len(bucket) > 5:
        await msg.delete()
        return
    text = (msg.text or msg.caption or '').strip().lower()
    if now - COOLDOWN.get(user.id, 0) < 30:
        return
    answer = None
    if any(k in text for k in ('ازاي اشترك', 'إزاي اشترك', 'اشتراك القناه', 'اشتراك القناة', 'سعر الاشتراك')):
        answer = f'تفاصيل القناة في خاص البوت @{context.bot.username} ثم /subscribe'
    elif any(k in text for k in ('ايه التوصيات', 'إيه التوصيات', 'يعني ايه توصيات')):
        answer = FAQ
    elif text in ('الادمن', 'الأدمن', 'تواصل مع الادمن', 'الدعم'):
        answer = f'التواصل مع الإدارة: {CONTACT}'
    if answer:
        COOLDOWN[user.id] = now
        await msg.reply_text(answer)
    # Bound memory even in busy groups.
    if len(FLOOD) > 10000:
        for key in list(FLOOD):
            if not FLOOD[key] or FLOOD[key][-1] < now - 60:
                FLOOD.pop(key, None)
                COOLDOWN.pop(key, None)


async def welcome(update, context):
    if not READY or update.effective_chat.id != GROUP:
        return
    for user in update.message.new_chat_members:
        if not user.is_bot:
            await update.message.reply_text(f'أهلاً يا {user.first_name[:80]} في الكوميونتي.\n{RULES}\n/faq عن التوصيات\n/subscribe القناة المدفوعة')


async def health(update, context):
    if not await owner(update):
        return
    result = []
    for cid, label in (((GROUP, 'الجروب'), (CHANNEL, 'القناة')) if ACCESS else ((GROUP, 'الجروب'),)):
        chat = await context.bot.get_chat(cid)
        me = await context.bot.get_chat_member(cid, context.bot.id)
        result.append(f'{label}: {chat.title} | {me.status} | حذف={getattr(me, "can_delete_messages", False)} | تقييد={getattr(me, "can_restrict_members", False)} | دعوة={getattr(me, "can_invite_users", False)}')
    await update.message.reply_text('\n'.join(result) + '\nالبوت شغال دلوقتي. راجع صلاحياته ونتائج اختبار الدخول والإزالة، ده مش ضمان لاستمرار السيرفر.')


async def errors(update, context):
    # No raw exceptions: request URLs can contain the bot token.
    LOG.error('Handler failed: %s', type(context.error).__name__)
    await alert(context.bot, 'handler-error', f'حصل خطأ من نوع {type(context.error).__name__}. راجع /health وسجل الخدمة. متبعتش التوكن لحد.')
    if update and update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        await dm(context.bot, update.effective_chat.id, 'حصل خطأ. لو دفعت بالفعل ماتدفعش تاني قبل مراجعة الإدارة. لو اشتراكك محفوظ، جرب /join. /support')


async def post_init(app):
    me = await app.bot.get_me()
    # Do not discard pending successful_payment or member updates after a restart.
    if not WEBHOOK_URL:
        await app.bot.delete_webhook(drop_pending_updates=False)
    if READY:
        await dm(app.bot, ADMIN, f'البوت @{me.username} اشتغل. ابعت /health واختبره قبل البيع.')
        if ACCESS:
            app.job_queue.run_repeating(maintenance, interval=60, first=5)
    else:
        LOG.warning('Discovery mode: /id and forwarded channel ID only; access/sales disabled.')


def main():
    if not TOKEN or ':' not in TOKEN or TOKEN.startswith('PASTE_'):
        raise SystemExit('Set BOT_TOKEN privately in .env first. Never send it in a chat.')
    if DAYS < 1 or DAYS > 3660 or (SALES and (not READY or PRICE < 1 or TERMS.startswith('لم يتم') or REFUNDS.startswith('لم يتم'))):
        raise SystemExit('Invalid config: Stars requires IDs, STAR_PRICE > 0, TERMS_TEXT and REFUND_TEXT.')
    if READY and (ADMIN <= 0 or GROUP >= 0 or (ACCESS and (CHANNEL >= 0 or GROUP == CHANNEL))):
        raise SystemExit('Check ADMIN_ID positive; GROUP_ID and CHANNEL_ID negative and different.')
    if ACCESS:
        raise SystemExit('Channel access requires a verified external scheduler; disabled in this deployment.')
    if not os.getenv('TURSO_DATABASE_URL'):
        raise SystemExit('A durable TURSO_DATABASE_URL is required for this deployment.')
    if not os.getenv('TURSO_AUTH_TOKEN'):
        raise SystemExit('Set TURSO_AUTH_TOKEN privately.')
    app = Application.builder().token(TOKEN).concurrent_updates(False).post_init(post_init).build()
    commands = {'start': start, 'help': help_cmd, 'id': identify, 'faq': faq, 'support': support,
                'paysupport': support, 'terms': terms, 'privacy': privacy, 'subscribe': subscribe,
                'status': status, 'join': join, 'grant': grant, 'approve': grant, 'buy': buy,
                'refund': refund, 'revoke': revoke, 'subs': subs, 'announce': announce,
                'mute': moderate, 'unmute': moderate, 'ban': moderate, 'unban': moderate, 'health': health}
    # Moderation sees commands too. A command containing a blocked link is still removed.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, group_message), group=-1)
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(PreCheckoutQueryHandler(checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(ChatMemberHandler(channel_member, ChatMemberHandler.CHAT_MEMBER))
    # Forward any channel post in discovery mode to get its ID without third-party bots.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.FORWARDED, identify))
    app.add_error_handler(errors)
    if WEBHOOK_URL:
        secret = hashlib.sha256(('avenor-webhook:' + TOKEN).encode()).hexdigest()
        app.run_webhook(listen='0.0.0.0', port=int(os.getenv('PORT', '10000')),
                        url_path='telegram', webhook_url=WEBHOOK_URL + '/telegram',
                        secret_token=secret, max_connections=1,
                        allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == '__main__':
    main()
