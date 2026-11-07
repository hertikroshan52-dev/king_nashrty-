import os
import shutil
import json
import re
import asyncio
import logging
import time
import sqlite3
import random
import string
from datetime import datetime, timedelta
from threading import Thread
from queue import Queue

from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup,
    InputFile
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import DeletePhotosRequest, UploadProfilePhotoRequest
try:
    from telethon.tl.functions.stories import DeleteStoriesRequest, GetPeerStoriesRequest
except Exception:
    DeleteStoriesRequest = None
    GetPeerStoriesRequest = None
from telethon.errors import SessionPasswordNeededError, FloodWaitError

# تكوين البوت - بدون أي توكن أو آيدي افتراضي داخل الكود
# كل بوت يأخذ بياناته من أوامر export في Termux.
def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"❌ المتغير {name} غير موجود. استخدم export {name}=... قبل تشغيل البوت.")
    return value


def _safe_slug(value: str, fallback: str = "bot") -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return value[:64] or fallback


BOT_TOKEN = _required_env("BOT_TOKEN")
try:
    OWNER_ID = int(_required_env("OWNER_ID"))
except ValueError:
    raise SystemExit("❌ OWNER_ID يجب أن يكون رقماً فقط.")

# كل توكن/بوت يأخذ مجلد وقاعدة بيانات مستقلة تلقائياً.
# يمكنك تغييرها يدوياً عبر export DATA_DIR=... أو export DB_NAME=...
BOT_ID = _safe_slug(BOT_TOKEN.split(":", 1)[0], "bot")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join("bots_data", BOT_ID))
DB_NAME = os.environ.get("DB_NAME", os.path.join(DATA_DIR, "bot_database.db"))
ADS_DIR = os.path.join(DATA_DIR, "ads")
PROFILE_PHOTOS_DIR = os.path.join(DATA_DIR, "profile_photos")
GROUP_REPLIES_DIR = os.path.join(DATA_DIR, "group_replies")

for _directory in (DATA_DIR, ADS_DIR, PROFILE_PHOTOS_DIR, GROUP_REPLIES_DIR):
    os.makedirs(_directory, exist_ok=True)

# حالات المحادثة
(
    ADD_ACCOUNT, ADD_AD_TYPE, ADD_AD_TEXT, ADD_AD_MEDIA, ADD_GROUP, 
    ADD_PRIVATE_REPLY, ADD_GROUP_REPLY, ADD_ADMIN, 
    ADD_USERNAME, ADD_RANDOM_REPLY, ADD_PRIVATE_TEXT, ADD_GROUP_TEXT, 
    ADD_GROUP_PHOTO, SET_LINK_TARGETS, ARRANGE_ACCOUNT_NAME, ARRANGE_ACCOUNT_PHOTO
) = range(16)


# Telegram user credentials are supplied by the account owner; never hardcode example values.
def make_user_client(session_string):
    api_id = int(_required_env("TELEGRAM_API_ID"))
    api_hash = _required_env("TELEGRAM_API_HASH")
    return TelegramClient(StringSession(session_string), api_id, api_hash)

# تهيئة السجل
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class BotDatabase:
    def __init__(self):
        self.init_database()
    
    def init_database(self):
        """تهيئة قاعدة البيانات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # جدول الحسابات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_string TEXT UNIQUE,
                phone TEXT,
                name TEXT,
                username TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول الإعلانات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                text TEXT,
                media_path TEXT,
                file_type TEXT,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول المجموعات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT,
                status TEXT DEFAULT 'pending',
                join_date DATETIME,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول المشرفين
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                username TEXT,
                full_name TEXT,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_super_admin BOOLEAN DEFAULT 0
            )
        ''')
        
        # جدول الردود الخاصة
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS private_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reply_text TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول الردود الجماعية النصية
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_text_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT,
                reply_text TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول الردود الجماعية مع الصور
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_photo_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT,
                reply_text TEXT,
                media_path TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول الردود العشوائية في القروبات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_random_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reply_text TEXT,
                is_active BOOLEAN DEFAULT 1,
                added_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                admin_id INTEGER DEFAULT 0
            )
        ''')
        
        # جدول نشر الحسابات
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS account_publishing (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                status TEXT DEFAULT 'active',
                last_publish DATETIME,
                FOREIGN KEY (account_id) REFERENCES accounts (id)
            )
        ''')

        # إعدادات تجميع روابط واتساب وتليجرام
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS link_collection_settings (
                admin_id INTEGER PRIMARY KEY,
                telegram_target TEXT,
                whatsapp_target TEXT,
                updated_date DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # الروابط التي تم تجميعها مسبقاً لمنع التكرار
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS collected_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                link_type TEXT,
                link TEXT,
                source_dialog TEXT,
                source_account_id INTEGER,
                collected_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_id, link_type, link)
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_account(self, session_string, phone, name, username, admin_id=0):
        """إضافة حساب جديد"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO accounts (session_string, phone, name, username, admin_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (session_string, phone, name, username, admin_id))
            account_id = cursor.lastrowid
            
            cursor.execute('''
                INSERT INTO account_publishing (account_id)
                VALUES (?)
            ''', (account_id,))
            
            conn.commit()
            return True, "تم إضافة الحساب بنجاح"
        except sqlite3.IntegrityError:
            return False, "هذا الحساب مضاف مسبقاً"
        except Exception as e:
            return False, f"خطأ في إضافة الحساب: {str(e)}"
        finally:
            conn.close()
    
    def get_accounts(self, admin_id=None):
        """الحصول على جميع الحسابات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('''
                SELECT id, session_string, phone, name, username, is_active 
                FROM accounts 
                WHERE admin_id = ? OR admin_id = 0
                ORDER BY id
            ''', (admin_id,))
        else:
            cursor.execute('''
                SELECT id, session_string, phone, name, username, is_active 
                FROM accounts 
                ORDER BY id
            ''')
            
        accounts = cursor.fetchall()
        conn.close()
        return accounts
    

    def get_accounts_owned_by_admin(self, admin_id):
        """الحصول على الحسابات النشطة الخاصة بالمشرف فقط لتنفيذ العمليات الجماعية."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, session_string, phone, name, username, is_active
            FROM accounts
            WHERE admin_id = ? AND is_active = 1
            ORDER BY id
        ''', (admin_id,))

        accounts = cursor.fetchall()
        conn.close()
        return accounts

    def update_account_profile_cache(self, account_id, name, username):
        """تحديث الاسم واليوزرنيم المخزن محلياً بعد تعديل الحساب في تليجرام."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE accounts
            SET name = ?, username = ?
            WHERE id = ?
        ''', (name, username, account_id))

        conn.commit()
        conn.close()
        return True

    def delete_account(self, account_id, admin_id=None):
        """حذف حساب"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id:
            cursor.execute('DELETE FROM accounts WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (account_id, admin_id))
        else:
            cursor.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
            
        cursor.execute('DELETE FROM account_publishing WHERE account_id = ?', (account_id,))
        
        conn.commit()
        conn.close()
        return True
    
    def add_ad(self, ad_type, text, media_path=None, file_type=None, admin_id=0):
        """إضافة إعلان"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO ads (type, text, media_path, file_type, admin_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (ad_type, text, media_path, file_type, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_ads(self, admin_id=None):
        """الحصول على جميع الإعلانات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM ads WHERE admin_id = ? OR admin_id = 0 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM ads ORDER BY id')
            
        ads = cursor.fetchall()
        conn.close()
        return ads
    
    def delete_ad(self, ad_id, admin_id=None):
        """حذف إعلان"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id:
            cursor.execute('DELETE FROM ads WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (ad_id, admin_id))
        else:
            cursor.execute('DELETE FROM ads WHERE id = ?', (ad_id,))
            
        conn.commit()
        conn.close()
        return True
    
    def add_group(self, link, admin_id=0):
        """إضافة مجموعة"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO groups (link, admin_id)
            VALUES (?, ?)
        ''', (link, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_groups(self, admin_id=None):
        """الحصول على جميع المجموعات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM groups WHERE admin_id = ? OR admin_id = 0 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM groups ORDER BY id')
            
        groups = cursor.fetchall()
        conn.close()
        return groups
    
    def update_group_status(self, group_id, status):
        """تحديث حالة المجموعة"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE groups 
            SET status = ?, join_date = CURRENT_TIMESTAMP 
            WHERE id = ?
        ''', (status, group_id))
        
        conn.commit()
        conn.close()
        return True
    
    def add_admin(self, user_id, username, full_name, is_super_admin=False):
        """إضافة مشرف"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO admins (user_id, username, full_name, is_super_admin)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, full_name, is_super_admin))
            conn.commit()
            return True, "تم إضافة المشرف بنجاح"
        except sqlite3.IntegrityError:
            return False, "هذا المشرف مضاف مسبقاً"
        finally:
            conn.close()
    
    def get_admins(self):
        """الحصول على جميع المشرفين"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM admins ORDER BY id')
        admins = cursor.fetchall()
        conn.close()
        return admins
    
    def delete_admin(self, admin_id):
        """حذف مشرف"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM admins WHERE id = ?', (admin_id,))
        conn.commit()
        conn.close()
        return True
    
    def is_admin(self, user_id):
        """التحقق إذا كان المستخدم مشرف"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('SELECT id FROM admins WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    
    def is_super_admin(self, user_id):
        """التحقق إذا كان المستخدم مشرف رئيسي"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('SELECT id FROM admins WHERE user_id = ? AND is_super_admin = 1', (user_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    
    def add_private_reply(self, reply_text, admin_id=0):
        """إضافة رد خاص"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO private_replies (reply_text, admin_id)
            VALUES (?, ?)
        ''', (reply_text, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_private_replies(self, admin_id=None):
        """الحصول على الردود الخاصة"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM private_replies WHERE (admin_id = ? OR admin_id = 0) AND is_active = 1 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM private_replies WHERE is_active = 1 ORDER BY id')
            
        replies = cursor.fetchall()
        conn.close()
        return replies

    def delete_private_reply(self, reply_id, admin_id=None):
        """حذف رد خاص."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if admin_id is not None:
            cursor.execute('DELETE FROM private_replies WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (reply_id, admin_id))
        else:
            cursor.execute('DELETE FROM private_replies WHERE id = ?', (reply_id,))

        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    
    def add_group_text_reply(self, trigger, reply_text, admin_id=0):
        """إضافة رد نصي جماعي"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO group_text_replies (trigger, reply_text, admin_id)
            VALUES (?, ?, ?)
        ''', (trigger, reply_text, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_group_text_replies(self, admin_id=None):
        """الحصول على الردود النصية الجماعية"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM group_text_replies WHERE (admin_id = ? OR admin_id = 0) AND is_active = 1 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM group_text_replies WHERE is_active = 1 ORDER BY id')
            
        replies = cursor.fetchall()
        conn.close()
        return replies

    def delete_group_text_reply(self, reply_id, admin_id=None):
        """حذف رد نصي جماعي."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if admin_id is not None:
            cursor.execute('DELETE FROM group_text_replies WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (reply_id, admin_id))
        else:
            cursor.execute('DELETE FROM group_text_replies WHERE id = ?', (reply_id,))

        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    
    def add_group_photo_reply(self, trigger, reply_text, media_path, admin_id=0):
        """إضافة رد جماعي مع صورة"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO group_photo_replies (trigger, reply_text, media_path, admin_id)
            VALUES (?, ?, ?, ?)
        ''', (trigger, reply_text, media_path, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_group_photo_replies(self, admin_id=None):
        """الحصول على الردود الجماعية مع الصور"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM group_photo_replies WHERE (admin_id = ? OR admin_id = 0) AND is_active = 1 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM group_photo_replies WHERE is_active = 1 ORDER BY id')
            
        replies = cursor.fetchall()
        conn.close()
        return replies

    def delete_group_photo_reply(self, reply_id, admin_id=None):
        """حذف رد جماعي مع صورة وإرجاع مسار الصورة إن وجد."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if admin_id is not None:
            cursor.execute('SELECT media_path FROM group_photo_replies WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (reply_id, admin_id))
        else:
            cursor.execute('SELECT media_path FROM group_photo_replies WHERE id = ?', (reply_id,))
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False, None

        if admin_id is not None:
            cursor.execute('DELETE FROM group_photo_replies WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (reply_id, admin_id))
        else:
            cursor.execute('DELETE FROM group_photo_replies WHERE id = ?', (reply_id,))

        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted, row[0]
    
    def add_group_random_reply(self, reply_text, admin_id=0):
        """إضافة رد عشوائي في القروبات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO group_random_replies (reply_text, admin_id)
            VALUES (?, ?)
        ''', (reply_text, admin_id))
        
        conn.commit()
        conn.close()
        return True
    
    def get_group_random_replies(self, admin_id=None):
        """الحصول على الردود العشوائية في القروبات"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('SELECT * FROM group_random_replies WHERE (admin_id = ? OR admin_id = 0) AND is_active = 1 ORDER BY id', (admin_id,))
        else:
            cursor.execute('SELECT * FROM group_random_replies WHERE is_active = 1 ORDER BY id')
            
        replies = cursor.fetchall()
        conn.close()
        return replies

    def delete_group_random_reply(self, reply_id, admin_id=None):
        """حذف رد عشوائي في القروبات."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        if admin_id is not None:
            cursor.execute('DELETE FROM group_random_replies WHERE id = ? AND (admin_id = ? OR admin_id = 0)', (reply_id, admin_id))
        else:
            cursor.execute('DELETE FROM group_random_replies WHERE id = ?', (reply_id,))

        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

    def factory_reset(self, owner_id):
        """إرجاع قاعدة البيانات إلى وضع البداية مع إبقاء المالك الرئيسي فقط."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()

        tables = [
            'account_publishing',
            'accounts',
            'ads',
            'groups',
            'private_replies',
            'group_text_replies',
            'group_photo_replies',
            'group_random_replies',
            'link_collection_settings',
            'collected_links',
            'admins'
        ]

        for table in tables:
            cursor.execute(f'DELETE FROM {table}')

        placeholders = ','.join('?' for _ in tables)
        cursor.execute(f'DELETE FROM sqlite_sequence WHERE name IN ({placeholders})', tables)
        cursor.execute(
            'INSERT INTO admins (user_id, username, full_name, is_super_admin) VALUES (?, ?, ?, ?)',
            (owner_id, '@owner', 'المالك الرئيسي', True)
        )

        conn.commit()
        conn.close()
        return True
    
    def set_link_collection_settings(self, admin_id, telegram_target, whatsapp_target):
        """حفظ وجهات تجميع روابط تليجرام وواتساب."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO link_collection_settings (admin_id, telegram_target, whatsapp_target, updated_date)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(admin_id) DO UPDATE SET
                telegram_target = excluded.telegram_target,
                whatsapp_target = excluded.whatsapp_target,
                updated_date = CURRENT_TIMESTAMP
        ''', (admin_id, telegram_target, whatsapp_target))
        conn.commit()
        conn.close()
        return True

    def get_link_collection_settings(self, admin_id):
        """جلب وجهات تجميع الروابط للمشرف."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT telegram_target, whatsapp_target, updated_date
            FROM link_collection_settings
            WHERE admin_id = ?
        ''', (admin_id,))
        result = cursor.fetchone()
        conn.close()
        return result

    def add_collected_link(self, admin_id, link_type, link, source_dialog=None, source_account_id=None):
        """حفظ رابط جديد، ويرجع False إذا كان الرابط موجوداً مسبقاً."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO collected_links (admin_id, link_type, link, source_dialog, source_account_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (admin_id, link_type, link, source_dialog, source_account_id))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def get_collected_links_count(self, admin_id):
        """إحصائيات الروابط المجمعة للمشرف."""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT link_type, COUNT(*)
            FROM collected_links
            WHERE admin_id = ?
            GROUP BY link_type
        ''', (admin_id,))
        rows = dict(cursor.fetchall())
        conn.close()
        return rows

    def get_active_publishing_accounts(self, admin_id=None):
        """الحصول على الحسابات النشطة للنشر"""
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        if admin_id is not None:
            cursor.execute('''
                SELECT a.id, a.session_string, a.name, a.username
                FROM accounts a
                JOIN account_publishing ap ON a.id = ap.account_id
                WHERE ap.status = 'active' AND a.is_active = 1 
                AND (a.admin_id = ? OR a.admin_id = 0)
            ''', (admin_id,))
        else:
            cursor.execute('''
                SELECT a.id, a.session_string, a.name, a.username
                FROM accounts a
                JOIN account_publishing ap ON a.id = ap.account_id
                WHERE ap.status = 'active' AND a.is_active = 1
            ''')
            
        accounts = cursor.fetchall()
        conn.close()
        return accounts

class TelegramBotManager:
    def __init__(self, db):
        self.db = db
        self.publishing_active = False
        self.publishing_thread = None
        self.private_reply_active = False
        self.private_reply_thread = None
        self.group_reply_active = False
        self.group_reply_thread = None
        self.random_reply_active = False
        self.random_reply_thread = None
        self.private_replied_messages = set()
        self.group_replied_messages = set()
        self.random_replied_messages = set()
        self.link_collection_active = False
        self.link_collection_thread = None
        self.link_collection_admin_id = None
        self.link_collection_seen_messages = set()
        self._max_seen_messages = 5000
        # كاش لوجهات الإرسال حتى لا يعمل Telethon ResolveUsernameRequest عند كل رابط.
        self._send_target_cache = {}
        # تبريد مؤقت عند حدوث FloodWait حتى لا يكرر نفس الخطأ آلاف المرات.
        self._send_target_flood_until = {}

    def _remember_message(self, storage, key):
        """حفظ الرسائل التي تمت معالجتها لمنع تكرار الردود."""
        if key in storage:
            return False
        storage.add(key)
        if len(storage) > self._max_seen_messages:
            # تنظيف بسيط للذاكرة عند امتلاء القائمة
            for old_key in list(storage)[:1000]:
                storage.discard(old_key)
        return True

    async def _controlled_sleep(self, seconds, flag_name):
        """انتظار قابل للإيقاف حتى لا تتجمد أزرار الإيقاف."""
        for _ in range(int(seconds)):
            if not getattr(self, flag_name):
                return False
            await asyncio.sleep(1)
        return True
    
    def extract_group_links(self, text):
        """استخراج روابط مجموعات واتساب وتليجرام فقط من نص الرسالة."""
        if not text:
            return {"telegram": set(), "whatsapp": set()}

        results = {"telegram": set(), "whatsapp": set()}
        trailing = '.,;:!?)"]}،؛\n\r\t '

        # روابط مجموعات واتساب تكون غالباً بهذا الشكل: chat.whatsapp.com/<code>
        whatsapp_pattern = re.compile(
            r'(?:https?://)?chat\.whatsapp\.com/[A-Za-z0-9_-]{10,}',
            re.IGNORECASE
        )
        for match in whatsapp_pattern.findall(text):
            link = match.strip().strip(trailing)
            if not link.startswith(('http://', 'https://')):
                link = 'https://' + link
            results["whatsapp"].add(link)

        # روابط تليجرام: خاص t.me/+hash أو joinchat أو عام t.me/username
        telegram_pattern = re.compile(
            r'(?:https?://)?(?:t\.me|telegram\.me)/[^\s<>"\']+',
            re.IGNORECASE
        )
        blocked_first_segments = {
            'share', 'addstickers', 'addemoji', 'proxy', 'socks', 'iv', 'login',
            'bg', 'boost', 'c', 'invoice', 'giftcode', 'nft'
        }

        for match in telegram_pattern.findall(text):
            raw = match.strip().strip(trailing)
            clean = re.sub(r'^https?://', '', raw, flags=re.IGNORECASE)
            clean = clean.split('?', 1)[0].split('#', 1)[0].strip('/')
            parts = clean.split('/')
            if len(parts) < 2:
                continue

            path_parts = [part for part in parts[1:] if part]
            if not path_parts:
                continue

            first = path_parts[0]
            first_lower = first.lower()
            if first_lower in blocked_first_segments:
                continue

            if first.startswith('+') and len(first) > 3:
                results["telegram"].add(f"https://t.me/{first}")
                continue

            if first_lower == 'joinchat' and len(path_parts) >= 2 and len(path_parts[1]) > 3:
                results["telegram"].add(f"https://t.me/joinchat/{path_parts[1]}")
                continue

            # للروابط العامة نأخذ اسم المستخدم فقط ونتجاهل روابط الرسائل /123
            if re.fullmatch(r'[A-Za-z0-9_]{5,32}', first):
                results["telegram"].add(f"https://t.me/{first}")

        return results

    async def _is_telegram_group_or_channel_link(self, client, link):
        """دالة قديمة لم تعد مستخدمة: تم إلغاء التحقق الإضافي لتقليل FloodWait."""
        if '/+' in link or '/joinchat/' in link:
            return True
        try:
            entity = await client.get_entity(link)
            return hasattr(entity, 'megagroup') or hasattr(entity, 'broadcast') or entity.__class__.__name__ in ('Chat', 'Channel')
        except Exception:
            # إذا لم نستطع التحقق لا نحذف الرابط؛ قد يكون خاصاً أو غير قابل للوصول من الحساب الحالي.
            return True

    async def _resolve_send_target(self, client, account_id, target):
        """حل وجهة الإرسال مرة واحدة فقط لكل حساب/وجهة لتقليل ResolveUsernameRequest."""
        target = str(target).strip()
        cache_key = (account_id, target)

        if cache_key in self._send_target_cache:
            return self._send_target_cache[cache_key]

        # دعم الآيدي الرقمي إذا أدخله المستخدم بدلاً من @username.
        if re.fullmatch(r"-?\d+", target):
            resolved = int(target)
        else:
            # هنا قد يحدث ResolveUsernameRequest، لذلك نجعله مرة واحدة فقط ونحترم FloodWait.
            resolved = await client.get_input_entity(target)

        self._send_target_cache[cache_key] = resolved
        return resolved

    async def _send_collected_link(self, client, account_id, target, link_type, link, source_dialog, account_name):
        """إرسال الرابط للوجهة المحددة مع كاش للوجهة واحترام FloodWait."""
        target = str(target).strip()
        label = "تليجرام" if link_type == 'telegram' else "واتساب"
        message = (
            f"🔗 رابط {label} جديد\n"
            f"{link}\n\n"
            f"📌 المصدر: {source_dialog or 'غير معروف'}\n"
            f"👤 الحساب: {account_name or 'غير معروف'}"
        )

        flood_key = (account_id, target)
        now = time.time()
        flood_until = self._send_target_flood_until.get(flood_key, 0)
        if flood_until > now:
            remaining = int(flood_until - now)
            logger.warning(
                f"تخطي الإرسال مؤقتاً إلى {target} للحساب #{account_id}. "
                f"الوقت المتبقي بسبب FloodWait: {remaining} ثانية"
            )
            return False

        try:
            resolved_target = await self._resolve_send_target(client, account_id, target)
            await client.send_message(resolved_target, message)
            return True

        except FloodWaitError as e:
            wait_seconds = int(getattr(e, 'seconds', 0) or 0)
            # لا نحاول مرة أخرى لنفس الحساب/الوجهة قبل انتهاء المهلة التي طلبها تيليجرام.
            self._send_target_flood_until[flood_key] = time.time() + max(wait_seconds, 60)
            wait_hours = round(wait_seconds / 3600, 2)
            logger.error(
                f"FloodWait عند الإرسال/حل الوجهة {target} للحساب #{account_id}: "
                f"يجب الانتظار {wait_seconds} ثانية ≈ {wait_hours} ساعة"
            )
            # إذا حدث FloodWait أثناء حل اليوزرنيم، نمسح الكاش حتى لا يحتفظ بوجهة غير مكتملة.
            self._send_target_cache.pop((account_id, target), None)
            return False

        except Exception as e:
            logger.error(f"فشل الإرسال إلى الوجهة {target} للحساب #{account_id}: {str(e)}")
            return False

    async def collect_links_from_accounts(self, admin_id=None):
        """تجميع روابط واتساب وتليجرام من مجموعات وقنوات الحسابات المضافة."""
        scan_limit = int(os.environ.get('LINK_COLLECT_SCAN_LIMIT', '10'))
        scan_interval = int(os.environ.get('LINK_COLLECT_INTERVAL', '900'))

        while self.link_collection_active:
            try:
                settings = self.db.get_link_collection_settings(admin_id)
                if not settings:
                    logger.warning("لا توجد وجهات محفوظة لتجميع الروابط")
                    await self._controlled_sleep(30, 'link_collection_active')
                    continue

                telegram_target, whatsapp_target, _updated_date = settings
                accounts = self.db.get_active_publishing_accounts(admin_id)

                if not accounts:
                    logger.warning("لا توجد حسابات نشطة لتجميع الروابط")
                    await self._controlled_sleep(60, 'link_collection_active')
                    continue

                for account in accounts:
                    if not self.link_collection_active:
                        break

                    account_id, session_string, name, username = account
                    client = None

                    try:
                        client = make_user_client(session_string)
                        await client.connect()

                        if not await client.is_user_authorized():
                            await client.disconnect()
                            continue

                        dialogs = await client.get_dialogs()
                        for dialog in dialogs:
                            if not self.link_collection_active:
                                break

                            # التجميع فقط من المجموعات والقنوات، وتجاهل الخاص وأي شيء آخر.
                            if not (dialog.is_group or dialog.is_channel):
                                continue

                            try:
                                async for message in client.iter_messages(dialog.id, limit=scan_limit):
                                    if not self.link_collection_active:
                                        break

                                    msg_key = (account_id, dialog.id, message.id)
                                    if not self._remember_message(self.link_collection_seen_messages, msg_key):
                                        continue

                                    text = getattr(message, 'message', None) or getattr(message, 'text', None) or ''
                                    links = self.extract_group_links(text)

                                    for link in sorted(links['telegram']):
                                        # تم إلغاء التحقق الإضافي عبر get_entity/ResolveUsernameRequest
                                        # لتقليل FloodWait. الآن يتم اعتماد الرابط كما استخرجه regex فقط،
                                        # مع منع التكرار عبر قاعدة البيانات.
                                        if self.db.add_collected_link(admin_id, 'telegram', link, dialog.name, account_id):
                                            sent = await self._send_collected_link(client, account_id, telegram_target, 'telegram', link, dialog.name, name)
                                            if sent:
                                                logger.info(f"تم إرسال رابط تليجرام جديد: {link}")
                                                await asyncio.sleep(5)
                                            else:
                                                await asyncio.sleep(2)

                                    for link in sorted(links['whatsapp']):
                                        if self.db.add_collected_link(admin_id, 'whatsapp', link, dialog.name, account_id):
                                            sent = await self._send_collected_link(client, account_id, whatsapp_target, 'whatsapp', link, dialog.name, name)
                                            if sent:
                                                logger.info(f"تم إرسال رابط واتساب جديد: {link}")
                                                await asyncio.sleep(5)
                                            else:
                                                await asyncio.sleep(2)

                            except Exception as e:
                                logger.error(f"فشل فحص {dialog.name}: {str(e)}")
                                continue

                        await client.disconnect()

                    except Exception as e:
                        logger.error(f"خطأ أثناء تجميع الروابط من الحساب {name}: {str(e)}")
                        if client:
                            try:
                                await client.disconnect()
                            except Exception:
                                pass
                        continue

                await self._controlled_sleep(scan_interval, 'link_collection_active')

            except Exception as e:
                logger.error(f"خطأ عام في تجميع الروابط: {str(e)}")
                await self._controlled_sleep(60, 'link_collection_active')

    def start_link_collection(self, admin_id=None):
        """بدء تجميع الروابط."""
        if not self.link_collection_active:
            self.link_collection_active = True
            self.link_collection_admin_id = admin_id
            self.link_collection_thread = Thread(
                target=lambda: asyncio.run(self.collect_links_from_accounts(admin_id)),
                daemon=True
            )
            self.link_collection_thread.start()
            return True
        return False

    def stop_link_collection(self):
        """إيقاف تجميع الروابط."""
        if self.link_collection_active:
            self.link_collection_active = False
            if self.link_collection_thread:
                self.link_collection_thread.join(timeout=3)
            self.link_collection_admin_id = None
            return True
        return False

    async def test_session(self, session_string):
        """اختبار جلسة تيليجرام"""
        try:
            client = make_user_client(session_string)
            await client.connect()
            
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                return True, me
            else:
                await client.disconnect()
                return False, None
        except Exception as e:
            logger.error(f"خطأ في اختبار الجلسة: {str(e)}")
            return False, None
    

    def _split_profile_name(self, full_name):
        """تقسيم الاسم إلى first_name و last_name بما يناسب Telegram."""
        clean_name = re.sub(r"\s+", " ", str(full_name or "").strip())
        if not clean_name:
            return "Account", ""

        if len(clean_name) <= 64:
            return clean_name, ""

        parts = clean_name.split(" ", 1)
        first_name = parts[0][:64] or clean_name[:64]
        last_name = parts[1][:64] if len(parts) > 1 else ""
        return first_name, last_name

    def _extract_story_ids(self, obj):
        """استخراج معرفات القصص من استجابة Telethon باختلاف شكلها بين الإصدارات."""
        story_ids = []
        visited = set()

        def walk(value):
            if value is None:
                return
            value_id = id(value)
            if value_id in visited:
                return
            visited.add(value_id)
            if isinstance(value, (str, bytes, int, float, bool)):
                return
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    walk(item)
                return

            class_name = value.__class__.__name__.lower()
            if "storyitem" in class_name and hasattr(value, "id"):
                try:
                    story_id = int(value.id)
                    if story_id not in story_ids:
                        story_ids.append(story_id)
                except Exception:
                    pass

            for attr in ("stories", "peer_stories", "items"):
                if hasattr(value, attr):
                    walk(getattr(value, attr))

        walk(obj)
        return story_ids

    async def _delete_all_own_stories(self, client):
        """حذف قصص الحساب إن كانت نسخة Telethon الحالية تدعم Stories API."""
        if DeleteStoriesRequest is None or GetPeerStoriesRequest is None:
            return 0, "نسخة Telethon الحالية لا توفر دوال حذف القصص"

        try:
            try:
                peer = await client.get_input_entity("me")
            except Exception:
                peer = "me"

            result = await client(GetPeerStoriesRequest(peer=peer))
            story_ids = self._extract_story_ids(result)
            if not story_ids:
                return 0, None

            await client(DeleteStoriesRequest(peer=peer, id=story_ids))
            return len(story_ids), None
        except Exception as e:
            return 0, str(e)

    async def arrange_accounts_profiles(self, admin_id, new_name, photo_path):
        """
        ترتيب حسابات المشرف:
        - تغيير الاسم.
        - حذف كل صور الملف الشخصي القديمة.
        - رفع الصورة الجديدة.
        - حذف القصص إن وجدت وكان ذلك مدعوماً في Telethon.
        """
        accounts = self.db.get_accounts_owned_by_admin(admin_id)
        delay_seconds = int(os.environ.get("PROFILE_ARRANGE_DELAY", "8"))
        summary = {
            "total": len(accounts),
            "success": 0,
            "failed": 0,
            "details": []
        }

        if not accounts:
            return summary

        first_name, last_name = self._split_profile_name(new_name)

        for account in accounts:
            account_id, session_string, phone, old_name, old_username, is_active = account
            client = None
            try:
                client = make_user_client(session_string)
                await client.connect()

                if not await client.is_user_authorized():
                    summary["failed"] += 1
                    summary["details"].append(f"#{account_id} {old_name}: الجلسة غير مفعلة")
                    await client.disconnect()
                    continue

                deleted_photos_count = 0
                try:
                    old_photos = await client.get_profile_photos("me")
                    if old_photos:
                        await client(DeletePhotosRequest(old_photos))
                        deleted_photos_count = len(old_photos)
                except Exception as e:
                    logger.error(f"فشل حذف صور البروفايل للحساب #{account_id}: {str(e)}")

                await client(UpdateProfileRequest(first_name=first_name, last_name=last_name))

                uploaded_file = await client.upload_file(photo_path)
                await client(UploadProfilePhotoRequest(file=uploaded_file))

                deleted_stories_count, story_error = await self._delete_all_own_stories(client)
                if story_error:
                    logger.warning(f"تعذر حذف القصص للحساب #{account_id}: {story_error}")

                me = await client.get_me()
                saved_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or new_name
                saved_username = f"@{me.username}" if me.username else "لا يوجد"
                self.db.update_account_profile_cache(account_id, saved_name, saved_username)

                summary["success"] += 1
                detail = (
                    f"#{account_id} {old_name}: تم التحديث"
                    f" | صور محذوفة: {deleted_photos_count}"
                    f" | قصص محذوفة: {deleted_stories_count}"
                )
                if story_error:
                    detail += " | القصص: غير مدعومة/تعذر حذفها"
                summary["details"].append(detail)

                await client.disconnect()
                await asyncio.sleep(delay_seconds)

            except FloodWaitError as e:
                wait_seconds = int(getattr(e, "seconds", 0) or 0)
                summary["failed"] += 1
                summary["details"].append(
                    f"#{account_id} {old_name}: FloodWait يجب الانتظار {wait_seconds} ثانية"
                )
                logger.error(f"FloodWait أثناء ترتيب الحساب #{account_id}: {wait_seconds} ثانية")
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            except Exception as e:
                summary["failed"] += 1
                summary["details"].append(f"#{account_id} {old_name}: فشل - {str(e)}")
                logger.error(f"فشل ترتيب الحساب #{account_id}: {str(e)}")
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

        return summary

    async def join_groups(self, admin_id=None):
        """الانضمام إلى المجموعات"""
        groups = self.db.get_groups(admin_id)
        pending_groups = [g for g in groups if g[2] == 'pending']
        
        accounts = self.db.get_active_publishing_accounts(admin_id)
        
        for group in pending_groups:
            group_id, group_link, status, join_date, added_date, group_admin_id = group
            
            for account in accounts:
                account_id, session_string, name, username = account
                
                try:
                    client = make_user_client(session_string)
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        try:
                            if 't.me/+' in group_link:
                                invite_hash = group_link.split('+')[1]
                                await client(ImportChatInviteRequest(invite_hash))
                            else:
                                await client(JoinChannelRequest(group_link))
                            
                            self.db.update_group_status(group_id, 'joined')
                            logger.info(f"انضم الحساب {name} إلى المجموعة {group_link}")
                            
                        except Exception as e:
                            logger.error(f"فشل الانضمام للمجموعة {group_link}: {str(e)}")
                            self.db.update_group_status(group_id, 'failed')
                    
                    await client.disconnect()
                    await asyncio.sleep(120)
                    
                except Exception as e:
                    logger.error(f"خطأ في الحساب {name}: {str(e)}")
                    continue
    
    async def publish_to_groups(self, admin_id=None):
        """النشر في المجموعات"""
        while self.publishing_active:
            try:
                accounts = self.db.get_active_publishing_accounts(admin_id)
                ads = self.db.get_ads(admin_id)
                
                if not accounts or not ads:
                    await self._controlled_sleep(60, 'publishing_active')
                    continue
                
                for account in accounts:
                    if not self.publishing_active:
                        break
                        
                    account_id, session_string, name, username = account
                    
                    try:
                        client = make_user_client(session_string)
                        await client.connect()
                        
                        if await client.is_user_authorized():
                            dialogs = await client.get_dialogs()
                            
                            for dialog in dialogs:
                                if not self.publishing_active:
                                    break
                                    
                                if dialog.is_group or dialog.is_channel:
                                    try:
                                        for ad in ads:
                                            if not self.publishing_active:
                                                break
                                                
                                            ad_id, ad_type, ad_text, media_path, file_type, added_date, ad_admin_id = ad
                                            
                                            try:
                                                if ad_type == 'text':
                                                    await client.send_message(dialog.id, ad_text)
                                                elif ad_type == 'photo' and media_path and os.path.exists(media_path):
                                                    await client.send_file(dialog.id, media_path, caption=ad_text)
                                                elif ad_type == 'video' and media_path and os.path.exists(media_path):
                                                    await client.send_file(dialog.id, media_path, caption=ad_text)
                                                elif ad_type == 'document' and media_path and os.path.exists(media_path):
                                                    await client.send_file(dialog.id, media_path, caption=ad_text)
                                                elif ad_type == 'contact' and media_path and os.path.exists(media_path):
                                                    await client.send_file(dialog.id, media_path, caption=ad_text)
                                                
                                                logger.info(f"تم النشر في {dialog.name} بواسطة {name}")
                                                await asyncio.sleep(1)
                                                
                                            except Exception as e:
                                                logger.error(f"فشل نشر الإعلان {ad_id} في {dialog.name}: {str(e)}")
                                                continue
                                                
                                    except Exception as e:
                                        logger.error(f"فشل النشر في {dialog.name}: {str(e)}")
                                        continue
                        
                        await client.disconnect()
                        
                    except Exception as e:
                        logger.error(f"خطأ في الحساب {name}: {str(e)}")
                        continue
                
                await self._controlled_sleep(300, 'publishing_active')
                
            except Exception as e:
                logger.error(f"خطأ في عملية النشر: {str(e)}")
                await self._controlled_sleep(60, 'publishing_active')
    
    def start_publishing(self, admin_id=None):
        """بدء النشر التلقائي"""
        if not self.publishing_active:
            self.publishing_active = True
            self.publishing_thread = Thread(target=lambda: asyncio.run(self.publish_to_groups(admin_id)), daemon=True)
            self.publishing_thread.start()
            return True
        return False
    
    def stop_publishing(self):
        """إيقاف النشر التلقائي"""
        if self.publishing_active:
            self.publishing_active = False
            if self.publishing_thread:
                self.publishing_thread.join(timeout=3)
            return True
        return False
    
    async def handle_private_messages(self, admin_id=None):
        """معالجة الرسائل الخاصة"""
        while self.private_reply_active:
            try:
                accounts = self.db.get_active_publishing_accounts(admin_id)
                private_replies = self.db.get_private_replies(admin_id)
                
                if not accounts or not private_replies:
                    await self._controlled_sleep(60, 'private_reply_active')
                    continue
                
                for account in accounts:
                    if not self.private_reply_active:
                        break
                        
                    account_id, session_string, name, username = account
                    
                    try:
                        client = make_user_client(session_string)
                        await client.connect()
                        
                        if await client.is_user_authorized():
                            async for message in client.iter_messages(None, limit=10):
                                if not self.private_reply_active:
                                    break
                                    
                                if message.is_private and not message.out:
                                    message_key = (account_id, message.sender_id, message.id)
                                    if not self._remember_message(self.private_replied_messages, message_key):
                                        continue

                                    for reply in private_replies:
                                        reply_id, reply_text, is_active, added_date, reply_admin_id = reply
                                        await client.send_message(message.sender_id, reply_text)
                                        logger.info(f"تم الرد على رسالة خاصة بواسطة {name}")
                                        break
                                    await asyncio.sleep(2)
                        
                        await client.disconnect()
                        
                    except Exception as e:
                        logger.error(f"خطأ في الحساب {name}: {str(e)}")
                        continue
                
                await self._controlled_sleep(30, 'private_reply_active')
                
            except Exception as e:
                logger.error(f"خطأ في معالجة الرسائل الخاصة: {str(e)}")
                await self._controlled_sleep(60, 'private_reply_active')
    
    def start_private_reply(self, admin_id=None):
        """بدء الرد على الرسائل الخاصة"""
        if not self.private_reply_active:
            self.private_reply_active = True
            self.private_reply_thread = Thread(target=lambda: asyncio.run(self.handle_private_messages(admin_id)), daemon=True)
            self.private_reply_thread.start()
            return True
        return False
    
    def stop_private_reply(self):
        """إيقاف الرد على الرسائل الخاصة"""
        if self.private_reply_active:
            self.private_reply_active = False
            if self.private_reply_thread:
                self.private_reply_thread.join(timeout=3)
            return True
        return False
    
    async def handle_group_replies(self, admin_id=None):
        """معالجة الردود في المجموعات"""
        while self.group_reply_active:
            try:
                accounts = self.db.get_active_publishing_accounts(admin_id)
                text_replies = self.db.get_group_text_replies(admin_id)
                photo_replies = self.db.get_group_photo_replies(admin_id)
                
                if not accounts or (not text_replies and not photo_replies):
                    await self._controlled_sleep(60, 'group_reply_active')
                    continue
                
                for account in accounts:
                    if not self.group_reply_active:
                        break
                        
                    account_id, session_string, name, username = account
                    
                    try:
                        client = make_user_client(session_string)
                        await client.connect()
                        
                        if await client.is_user_authorized():
                            dialogs = await client.get_dialogs()
                            
                            for dialog in dialogs:
                                if not self.group_reply_active:
                                    break
                                    
                                if dialog.is_group:
                                    try:
                                        async for message in client.iter_messages(dialog.id, limit=10):
                                            if not self.group_reply_active:
                                                break
                                                
                                            if message.text and not message.out:
                                                message_key = (account_id, dialog.id, message.id)
                                                if message_key in self.group_replied_messages:
                                                    continue

                                                handled = False

                                                # الردود النصية
                                                for reply in text_replies:
                                                    reply_id, trigger, reply_text, is_active, added_date, reply_admin_id = reply
                                                    
                                                    if trigger.lower() in message.text.lower():
                                                        if self._remember_message(self.group_replied_messages, message_key):
                                                            await client.send_message(dialog.id, reply_text, reply_to=message.id)
                                                            logger.info(f"تم الرد على رسالة في {dialog.name} بواسطة {name}")
                                                            await asyncio.sleep(2)
                                                        handled = True
                                                        break

                                                if handled:
                                                    continue
                                                
                                                # الردود مع الصور
                                                for reply in photo_replies:
                                                    reply_id, trigger, reply_text, media_path, is_active, added_date, reply_admin_id = reply
                                                    
                                                    if trigger.lower() in message.text.lower() and os.path.exists(media_path):
                                                        if self._remember_message(self.group_replied_messages, message_key):
                                                            await client.send_file(dialog.id, media_path, caption=reply_text, reply_to=message.id)
                                                            logger.info(f"تم الرد بصورة على رسالة في {dialog.name} بواسطة {name}")
                                                            await asyncio.sleep(2)
                                                        break
                                        
                                    except Exception as e:
                                        logger.error(f"فشل الرد في {dialog.name}: {str(e)}")
                                        continue
                        
                        await client.disconnect()
                        
                    except Exception as e:
                        logger.error(f"خطأ في الحساب {name}: {str(e)}")
                        continue
                
                await self._controlled_sleep(30, 'group_reply_active')
                
            except Exception as e:
                logger.error(f"خطأ في معالجة الردود الجماعية: {str(e)}")
                await self._controlled_sleep(60, 'group_reply_active')
    
    def start_group_reply(self, admin_id=None):
        """بدء الردود في المجموعات"""
        if not self.group_reply_active:
            self.group_reply_active = True
            self.group_reply_thread = Thread(target=lambda: asyncio.run(self.handle_group_replies(admin_id)), daemon=True)
            self.group_reply_thread.start()
            return True
        return False
    
    def stop_group_reply(self):
        """إيقاف الردود في المجموعات"""
        if self.group_reply_active:
            self.group_reply_active = False
            if self.group_reply_thread:
                self.group_reply_thread.join(timeout=3)
            return True
        return False
    
    async def handle_random_replies(self, admin_id=None):
        """معالجة الردود العشوائية في القروبات"""
        while self.random_reply_active:
            try:
                accounts = self.db.get_active_publishing_accounts(admin_id)
                random_replies = self.db.get_group_random_replies(admin_id)
                
                if not accounts or not random_replies:
                    await self._controlled_sleep(60, 'random_reply_active')
                    continue
                
                for account in accounts:
                    if not self.random_reply_active:
                        break
                        
                    account_id, session_string, name, username = account
                    
                    try:
                        client = make_user_client(session_string)
                        await client.connect()
                        
                        if await client.is_user_authorized():
                            dialogs = await client.get_dialogs()
                            
                            for dialog in dialogs:
                                if not self.random_reply_active:
                                    break
                                    
                                if dialog.is_group:
                                    try:
                                        # مراقبة الرسائل الجديدة في المجموعة
                                        async for message in client.iter_messages(dialog.id, limit=20):
                                            if not self.random_reply_active:
                                                break
                                                
                                            # الرد على أي رسالة من الأعضاء (ليست من الحساب نفسه) بنسبة 100%
                                            if message.text and not message.out:
                                                message_key = (account_id, dialog.id, message.id)
                                                if not self._remember_message(self.random_replied_messages, message_key):
                                                    continue

                                                random_reply = random.choice(random_replies)
                                                reply_id, reply_text, is_active, added_date, reply_admin_id = random_reply
                                                
                                                await client.send_message(dialog.id, reply_text, reply_to=message.id)
                                                logger.info(f"تم الرد العشوائي على عضو في {dialog.name} بواسطة {name}")
                                                await asyncio.sleep(5)  # تأخير بين الردود
                                                break
                                        
                                    except Exception as e:
                                        logger.error(f"فشل الرد العشوائي في {dialog.name}: {str(e)}")
                                        continue
                        
                        await client.disconnect()
                        
                    except Exception as e:
                        logger.error(f"خطأ في الحساب {name}: {str(e)}")
                        continue
                
                await self._controlled_sleep(20, 'random_reply_active')  # فحص المجموعات كل 20 ثانية
                
            except Exception as e:
                logger.error(f"خطأ في معالجة الردود العشوائية: {str(e)}")
                await self._controlled_sleep(60, 'random_reply_active')
    
    def start_random_reply(self, admin_id=None):
        """بدء الردود العشوائية في القروبات"""
        if not self.random_reply_active:
            self.random_reply_active = True
            self.random_reply_thread = Thread(target=lambda: asyncio.run(self.handle_random_replies(admin_id)), daemon=True)
            self.random_reply_thread.start()
            return True
        return False
    
    def stop_random_reply(self):
        """إيقاف الردود العشوائية في القروبات"""
        if self.random_reply_active:
            self.random_reply_active = False
            if self.random_reply_thread:
                self.random_reply_thread.join(timeout=3)
            return True
        return False

    def stop_all_services(self):
        """إيقاف كل العمليات العاملة قبل ضبط المصنع."""
        self.stop_publishing()
        self.stop_private_reply()
        self.stop_group_reply()
        self.stop_random_reply()
        self.stop_link_collection()

        self.private_replied_messages.clear()
        self.group_replied_messages.clear()
        self.random_replied_messages.clear()
        self.link_collection_seen_messages.clear()
        self._send_target_cache.clear()
        self._send_target_flood_until.clear()

class BotHandler:
    def __init__(self):
        self.db = BotDatabase()
        self.manager = TelegramBotManager(self.db)
        self.application = None

    def _is_owner(self, user_id):
        """السماح بعمليات إدارة المشرفين للمالك فقط."""
        return int(user_id) == int(OWNER_ID)

    def _main_keyboard(self, user_id):
        """لوحة التحكم الرئيسية مع إخفاء إدارة المشرفين عن غير المالك."""
        keyboard = [
            [InlineKeyboardButton("👥 إدارة الحسابات", callback_data="manage_accounts")],
            [InlineKeyboardButton("📢 إدارة الإعلانات", callback_data="manage_ads")],
            [InlineKeyboardButton("👥 إدارة المجموعات", callback_data="manage_groups")],
            [InlineKeyboardButton("💬 إدارة الردود", callback_data="manage_replies")],
            [InlineKeyboardButton("🔗 تجميع الروابط", callback_data="manage_link_collector")],
        ]

        if self._is_owner(user_id):
            keyboard.append([InlineKeyboardButton("👨‍💼 إدارة المشرفين", callback_data="manage_admins")])

        keyboard.append([InlineKeyboardButton("⚙️ الإعدادات", callback_data="settings")])
        return keyboard
    
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء البوت"""
        user = update.effective_user
        user_id = user.id
        
        if not self.db.is_admin(user_id):
            await update.message.reply_text("❌ ليس لديك صلاحية للوصول إلى هذا البوت.")
            return
        
        if context.user_data.get('conversation_active'):
            context.user_data['conversation_active'] = False
        
        keyboard = self._main_keyboard(user_id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🎮 **لوحة تحكم البوت المتكامل**\n\n"
            "اختر القسم الذي تريد إدارته:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إلغاء الأمر الحالي"""
        user_id = update.message.from_user.id
        if not self.db.is_admin(user_id):
            await update.message.reply_text("❌ ليس لديك صلاحية للوصول إلى هذا البوت.")
            return
        
        context.user_data['conversation_active'] = False
        await update.message.reply_text("❌ تم إلغاء الأمر.")
        await self.start(update, context)
        return ConversationHandler.END
    
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة الأزرار"""
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        if not self.db.is_admin(user_id):
            await query.edit_message_text("❌ ليس لديك صلاحية للوصول إلى هذا البوت.")
            return
        
        data = query.data
        
        if context.user_data.get('conversation_active'):
            context.user_data['conversation_active'] = False

        admin_callbacks = {"manage_admins", "add_admin", "show_admins", "back_to_admins"}
        if data in admin_callbacks or data.startswith("delete_admin_"):
            if not self._is_owner(user_id):
                await query.edit_message_text("❌ إدارة المشرفين متاحة للمالك فقط.")
                return
        
        if data == "manage_accounts":
            await self.manage_accounts(query, context)
        elif data == "manage_ads":
            await self.manage_ads(query, context)
        elif data == "manage_groups":
            await self.manage_groups(query, context)
        elif data == "manage_replies":
            await self.manage_replies(query, context)
        elif data == "manage_link_collector":
            await self.manage_link_collector(query, context)
        elif data == "manage_admins":
            await self.manage_admins(query, context)
        elif data == "settings":
            await self.settings_menu(query, context)
        elif data == "bot_status":
            await self.bot_status(query, context)
        elif data == "factory_reset_warning":
            await self.factory_reset_warning(query, context)
        elif data == "factory_reset_confirm":
            await self.factory_reset_confirm(query, context)
        elif data == "factory_reset_cancel":
            await self.bot_status(query, context)
        
        # إدارة الحسابات
        elif data == "add_account":
            await self.add_account_start(update, context)
        elif data == "show_accounts":
            await self.show_accounts(query, context)
        elif data == "arrange_accounts":
            await self.arrange_accounts_start(update, context)
        elif data.startswith("delete_account_"):
            account_id = int(data.split("_")[2])
            await self.delete_account(query, context, account_id)
        
        # إدارة الإعلانات
        elif data == "add_ad":
            await self.add_ad_start(query, context)
        elif data == "show_ads":
            await self.show_ads(query, context)
        elif data.startswith("delete_ad_"):
            ad_id = int(data.split("_")[2])
            await self.delete_ad(query, context, ad_id)
        elif data.startswith("ad_type_"):
            await self.add_ad_type(update, context)
        
        # إدارة المجموعات
        elif data == "add_group":
            await self.add_group_start(update, context)
        elif data == "show_groups":
            await self.show_groups(query, context)
        elif data == "start_publishing":
            await self.start_publishing(query, context)
        elif data == "stop_publishing":
            await self.stop_publishing(query, context)

        # تجميع الروابط
        elif data == "set_link_targets":
            await self.set_link_targets_start(update, context)
        elif data == "start_link_collection":
            await self.start_link_collection(query, context)
        elif data == "stop_link_collection":
            await self.stop_link_collection(query, context)
        
        # إدارة الردود
        elif data == "private_replies":
            await self.manage_private_replies(query, context)
        elif data == "group_replies":
            await self.manage_group_replies(query, context)
        elif data == "show_random_replies":
            await self.show_random_replies(query, context)
        elif data == "add_private_reply":
            await self.add_private_reply_start(update, context)
        elif data == "add_group_text_reply":
            await self.add_group_text_reply_start(update, context)
        elif data == "add_group_photo_reply":
            await self.add_group_photo_reply_start(update, context)
        elif data == "add_random_reply":
            await self.add_random_reply_start(update, context)
        elif data == "start_private_reply":
            await self.start_private_reply(query, context)
        elif data == "stop_private_reply":
            await self.stop_private_reply(query, context)
        elif data == "start_group_reply":
            await self.start_group_reply(query, context)
        elif data == "stop_group_reply":
            await self.stop_group_reply(query, context)
        elif data == "start_random_reply":
            await self.start_random_reply(query, context)
        elif data == "stop_random_reply":
            await self.stop_random_reply(query, context)
        elif data.startswith("delete_private_reply_"):
            reply_id = int(data.rsplit("_", 1)[1])
            await self.delete_private_reply(query, context, reply_id)
        elif data.startswith("delete_group_text_reply_"):
            reply_id = int(data.rsplit("_", 1)[1])
            await self.delete_group_text_reply(query, context, reply_id)
        elif data.startswith("delete_group_photo_reply_"):
            reply_id = int(data.rsplit("_", 1)[1])
            await self.delete_group_photo_reply(query, context, reply_id)
        elif data.startswith("delete_random_reply_"):
            reply_id = int(data.rsplit("_", 1)[1])
            await self.delete_random_reply(query, context, reply_id)
        
        # إدارة المشرفين
        elif data == "add_admin":
            await self.add_admin_start(update, context)
        elif data == "show_admins":
            await self.show_admins(query, context)
        elif data.startswith("delete_admin_"):
            admin_id = int(data.split("_")[2])
            await self.delete_admin(query, context, admin_id)
        
        # الرجوع
        elif data == "back_to_main":
            await self.start_from_query(query, context)
        elif data == "back_to_accounts":
            await self.manage_accounts(query, context)
        elif data == "back_to_ads":
            await self.manage_ads(query, context)
        elif data == "back_to_groups":
            await self.manage_groups(query, context)
        elif data == "back_to_replies":
            await self.manage_replies(query, context)
        elif data == "back_to_link_collector":
            await self.manage_link_collector(query, context)
        elif data == "back_to_admins":
            await self.manage_admins(query, context)
    
    async def start_from_query(self, query, context):
        """بدء البوت من استعلام"""
        if context.user_data.get('conversation_active'):
            context.user_data['conversation_active'] = False
            
        keyboard = self._main_keyboard(query.from_user.id)
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "🎮 **لوحة تحكم البوت المتكامل**\n\n"
            "اختر القسم الذي تريد إدارته:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    # قسم إدارة الحسابات
    async def manage_accounts(self, query, context):
        """إدارة الحسابات"""
        keyboard = [
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("👥 عرض الحسابات", callback_data="show_accounts")],
            [InlineKeyboardButton("🧩 ترتيب الحسابات", callback_data="arrange_accounts")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "👥 **إدارة الحسابات**\n\n"
            "اختر الإجراء الذي تريد تنفيذه:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def add_account_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة حساب"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "📱 **إضافة حساب جديد**\n\n"
                "يرجى إرسال كود الجلسة (Session String):\n\n"
                "يمكنك الحصول على كود الجلسة من @SessionStringBot\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "📱 **إضافة حساب جديد**\n\n"
                "يرجى إرسال كود الجلسة (Session String):\n\n"
                "يمكنك الحصول على كود الجلسة من @SessionStringBot\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_ACCOUNT
    
    async def add_account_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة كود الجلسة"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        session_string = update.message.text
        admin_id = update.message.from_user.id
        
        success, me = await self.manager.test_session(session_string)
        
        if success:
            phone = me.phone if me.phone else "غير معروف"
            name = f"{me.first_name} {me.last_name}" if me.last_name else me.first_name
            username = f"@{me.username}" if me.username else "لا يوجد"
            
            result, message = self.db.add_account(session_string, phone, name, username, admin_id)
            
            if result:
                await update.message.reply_text(f"✅ {message}\n\n📱 الحساب: {name}\n📞 الهاتف: {phone}\n👤 المستخدم: {username}")
            else:
                await update.message.reply_text(f"❌ {message}")
        else:
            await update.message.reply_text("❌ كود الجلسة غير صالح أو الحساب غير مفعل")
        
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def show_accounts(self, query, context):
        """عرض الحسابات"""
        admin_id = query.from_user.id
        accounts = self.db.get_accounts(admin_id)
        
        if not accounts:
            await query.edit_message_text("❌ لا توجد حسابات مضافة")
            return
        
        text = "👥 **الحسابات المضافة:**\n\n"
        keyboard = []
        
        for account in accounts:
            account_id, session_string, phone, name, username, is_active = account
            status = "🟢 نشط" if is_active else "🔴 غير نشط"
            
            text += f"**#{account_id}** - {name}\n"
            text += f"📱 {phone} | {username}\n"
            text += f"الحالة: {status}\n"
            text += "─" * 20 + "\n"
            
            keyboard.append([InlineKeyboardButton(f"🗑️ حذف #{account_id}", callback_data=f"delete_account_{account_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_accounts")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def delete_account(self, query, context, account_id):
        """حذف حساب"""
        admin_id = query.from_user.id
        self.db.delete_account(account_id, admin_id)
        await query.edit_message_text(f"✅ تم حذف الحساب #{account_id}")
        await self.show_accounts(query, context)

    async def arrange_accounts_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء ترتيب حسابات المشرف."""
        context.user_data['conversation_active'] = True
        context.user_data.pop('arrange_new_name', None)
        admin_id = update.effective_user.id
        accounts_count = len(self.db.get_accounts_owned_by_admin(admin_id))

        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "🧩 **ترتيب الحسابات**\n\n"
                f"عدد الحسابات النشطة الخاصة بك: `{accounts_count}`\n\n"
                "أرسل الاسم الجديد الذي تريد وضعه لكل الحسابات.\n\n"
                "بعدها سأطلب منك الصورة الجديدة، ثم سيتم:\n"
                "1) حذف كل صور الملف الشخصي القديمة.\n"
                "2) تغيير الاسم إلى الاسم الجديد.\n"
                "3) رفع الصورة الجديدة كصورة ملف شخصي.\n"
                "4) محاولة حذف القصص/الستوري الموجودة إن كانت مدعومة.\n\n"
                "أو أرسل /cancel للإلغاء.",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "🧩 **ترتيب الحسابات**\n\n"
                "أرسل الاسم الجديد الذي تريد وضعه لكل الحسابات.\n\n"
                "أو أرسل /cancel للإلغاء.",
                parse_mode='Markdown'
            )

        return ARRANGE_ACCOUNT_NAME

    async def arrange_accounts_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """حفظ الاسم الجديد وطلب الصورة."""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END

        new_name = re.sub(r"\s+", " ", update.message.text.strip())
        if not new_name:
            await update.message.reply_text("❌ الاسم فارغ. أرسل الاسم الجديد مرة أخرى.")
            return ARRANGE_ACCOUNT_NAME

        context.user_data['arrange_new_name'] = new_name
        await update.message.reply_text(
            "✅ تم حفظ الاسم الجديد.\n\n"
            "الآن أرسل الصورة الجديدة التي تريد تعيينها كصورة ملف شخصي لكل الحسابات.\n\n"
            "يفضل إرسالها كصورة عادية وليس كملف.\n"
            "أو أرسل /cancel للإلغاء."
        )
        return ARRANGE_ACCOUNT_PHOTO

    async def arrange_accounts_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """استقبال الصورة ثم تطبيق ترتيب الحسابات."""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END

        new_name = context.user_data.get('arrange_new_name')
        if not new_name:
            await update.message.reply_text("❌ لم يتم حفظ الاسم. ابدأ العملية من جديد.")
            context.user_data['conversation_active'] = False
            await self.start(update, context)
            return ConversationHandler.END

        file_id = None
        extension = ".jpg"

        if update.message.photo:
            file_id = update.message.photo[-1].file_id
        elif update.message.document:
            document = update.message.document
            mime_type = (document.mime_type or "").lower()
            if not mime_type.startswith("image/"):
                await update.message.reply_text("❌ الملف المرسل ليس صورة. أرسل صورة فقط أو /cancel للإلغاء.")
                return ARRANGE_ACCOUNT_PHOTO

            file_id = document.file_id
            if document.file_name:
                suffix = os.path.splitext(document.file_name)[1].lower()
                if suffix in (".jpg", ".jpeg", ".png", ".webp"):
                    extension = suffix

        if not file_id:
            await update.message.reply_text("❌ لم يتم التعرف على الصورة. أرسل صورة واضحة أو /cancel للإلغاء.")
            return ARRANGE_ACCOUNT_PHOTO

        await update.message.reply_text("⏳ تم استلام الصورة. بدأ تطبيق الترتيب على حساباتك...")

        file = await context.bot.get_file(file_id)
        safe_file_id = re.sub(r"[^A-Za-z0-9_-]+", "_", file_id)[:80]
        photo_path = os.path.join(
            PROFILE_PHOTOS_DIR,
            f"arrange_{update.message.from_user.id}_{safe_file_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{extension}"
        )
        await file.download_to_drive(photo_path)

        admin_id = update.message.from_user.id
        summary = await self.manager.arrange_accounts_profiles(admin_id, new_name, photo_path)

        context.user_data['conversation_active'] = False
        context.user_data.pop('arrange_new_name', None)

        details = "\n".join(summary["details"][:15])
        if summary["total"] > 15:
            details += f"\n... وتم إخفاء {summary['total'] - 15} نتيجة إضافية."

        result_text = (
            "✅ انتهت عملية ترتيب الحسابات.\n\n"
            f"📌 الاسم الجديد: {new_name}\n"
            f"👥 إجمالي الحسابات: {summary['total']}\n"
            f"✅ نجح: {summary['success']}\n"
            f"❌ فشل: {summary['failed']}\n\n"
            f"{details or 'لا توجد تفاصيل.'}\n\n"
            "ملاحظة: إذا ظهرت رسالة FloodWait فهذا حد مؤقت من Telegram، ويمكنك إعادة المحاولة لاحقاً."
        )

        await update.message.reply_text(result_text)
        await self.start(update, context)
        return ConversationHandler.END

    # قسم إدارة الإعلانات
    async def manage_ads(self, query, context):
        """إدارة الإعلانات"""
        keyboard = [
            [InlineKeyboardButton("➕ إضافة إعلان", callback_data="add_ad")],
            [InlineKeyboardButton("📋 عرض الإعلانات", callback_data="show_ads")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📢 **إدارة الإعلانات**\n\n"
            "اختر الإجراء الذي تريد تنفيذه:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def add_ad_start(self, query, context):
        """بدء إضافة إعلان"""
        keyboard = [
            [InlineKeyboardButton("📝 نص فقط", callback_data="ad_type_text")],
            [InlineKeyboardButton("🖼️ صورة مع نص", callback_data="ad_type_photo")],
            [InlineKeyboardButton("🎥 فيديو مع نص", callback_data="ad_type_video")],
            [InlineKeyboardButton("📄 ملف مع نص", callback_data="ad_type_document")],
            [InlineKeyboardButton("📞 جهة اتصال (VCF)", callback_data="ad_type_contact")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_ads")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "📢 **إضافة إعلان جديد**\n\n"
            "اختر نوع الإعلان:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def add_ad_type(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة نوع الإعلان"""
        query = update.callback_query
        await query.answer()
        
        context.user_data['conversation_active'] = True
        ad_type = query.data.replace("ad_type_", "")
        context.user_data['ad_type'] = ad_type
        
        await query.edit_message_text(
            "📝 **إضافة نص الإعلان**\n\n"
            "يرجى إرسال نص الإعلان:\n\n"
            "أو أرسل /cancel للإلغاء",
            parse_mode='Markdown'
        )
        return ADD_AD_TEXT
    
    async def add_ad_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة نص الإعلان"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        context.user_data['ad_text'] = update.message.text
        ad_type = context.user_data['ad_type']
        admin_id = update.message.from_user.id
        
        if ad_type == 'text':
            self.db.add_ad('text', update.message.text, admin_id=admin_id)
            await update.message.reply_text("✅ تم إضافة الإعلان النصي بنجاح")
            context.user_data['conversation_active'] = False
            await self.start(update, context)
            return ConversationHandler.END
        else:
            file_type_text = {
                'photo': 'صورة',
                'video': 'فيديو', 
                'document': 'ملف',
                'contact': 'ملف جهة اتصال (VCF)'
            }
            await update.message.reply_text(
                f"📎 **إضافة {file_type_text.get(ad_type, 'ملف')}**\n\n"
                f"يرجى إرسال {file_type_text.get(ad_type, 'الملف')}:\n\n"
                f"أو أرسل /cancel للإلغاء"
            )
            return ADD_AD_MEDIA
    
    async def add_ad_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة ملف الإعلان"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        ad_type = context.user_data['ad_type']
        ad_text = context.user_data['ad_text']
        admin_id = update.message.from_user.id
        
        file_id = None
        file_type = None
        
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            file_type = 'photo'
        elif update.message.video:
            file_id = update.message.video.file_id
            file_type = 'video'
        elif update.message.document:
            file_id = update.message.document.file_id
            file_type = 'document'
        
        if file_id:
            file = await context.bot.get_file(file_id)
            file_path = os.path.join(ADS_DIR, f"{file_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            await file.download_to_drive(file_path)
            
            self.db.add_ad(ad_type, ad_text, file_path, file_type, admin_id)
            await update.message.reply_text(f"✅ تم إضافة الإعلان بنجاح")
        else:
            await update.message.reply_text("❌ لم يتم التعرف على الملف")
        
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def show_ads(self, query, context):
        """عرض الإعلانات"""
        admin_id = query.from_user.id
        ads = self.db.get_ads(admin_id)
        
        if not ads:
            await query.edit_message_text("❌ لا توجد إعلانات مضافة")
            return
        
        text = "📢 **الإعلانات المضافة:**\n\n"
        keyboard = []
        
        for ad in ads:
            ad_id, ad_type, ad_text, media_path, file_type, added_date, ad_admin_id = ad
            type_emoji = {"text": "📝", "photo": "🖼️", "video": "🎥", "document": "📄", "contact": "📞"}

            text += f"**#{ad_id}** - {type_emoji.get(ad_type, '📄')} {ad_type}\n"
            text += f"📋 {ad_text[:50]}...\n"
            text += "─" * 20 + "\n"
            
            keyboard.append([InlineKeyboardButton(f"🗑️ حذف #{ad_id}", callback_data=f"delete_ad_{ad_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_ads")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def delete_ad(self, query, context, ad_id):
        """حذف إعلان"""
        admin_id = query.from_user.id
        self.db.delete_ad(ad_id, admin_id)
        await query.edit_message_text(f"✅ تم حذف الإعلان #{ad_id}")
        await self.show_ads(query, context)
    
    # قسم إدارة المجموعات
    async def manage_groups(self, query, context):
        """إدارة المجموعات"""
        keyboard = [
            [InlineKeyboardButton("➕ إضافة مجموعة", callback_data="add_group")],
            [InlineKeyboardButton("📊 عرض المجموعات", callback_data="show_groups")],
            [InlineKeyboardButton("🚀 بدء النشر التلقائي", callback_data="start_publishing")],
            [InlineKeyboardButton("⏹️ إيقاف النشر", callback_data="stop_publishing")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "👥 **إدارة المجموعات**\n\n"
            "اختر الإجراء الذي تريد تنفيذه:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def add_group_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة مجموعة"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "👥 **إضافة مجموعة جديدة**\n\n"
                "يرجى إرسال رابط المجموعة:\n\n"
                "يمكنك إرسال رابط واحد أو عدة روابط مفصولة بمسافات\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "👥 **إضافة مجموعة جديدة**\n\n"
                "يرجى إرسال رابط المجموعة:\n\n"
                "يمكنك إرسال رابط واحد أو عدة روابط مفصولة بمسافات\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_GROUP
    
    async def add_group_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة رابط المجموعة"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        group_links = update.message.text.split()
        admin_id = update.message.from_user.id
        
        added_count = 0
        for link in group_links:
            if link.startswith('https://t.me/') or link.startswith('t.me/'):
                self.db.add_group(link, admin_id)
                added_count += 1
        
        if added_count > 0:
            asyncio.create_task(self.manager.join_groups(admin_id))
            await update.message.reply_text(f"✅ تم إضافة {added_count} مجموعة وبدأ عملية الانضمام")
        else:
            await update.message.reply_text("❌ لم يتم إضافة أي مجموعة، تأكد من صحة الروابط")
        
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def show_groups(self, query, context):
        """عرض المجموعات"""
        admin_id = query.from_user.id
        groups = self.db.get_groups(admin_id)
        
        if not groups:
            await query.edit_message_text("❌ لا توجد مجموعات مضافة")
            return
        
        text = "👥 **المجموعات المضافة:**\n\n"
        
        for group in groups:
            group_id, link, status, join_date, added_date, group_admin_id = group
            status_emoji = {"pending": "⏳", "joined": "✅", "failed": "❌"}
            
            text += f"**#{group_id}** - {link}\n"
            text += f"الحالة: {status_emoji.get(status, '❓')} {status}\n"
            
            if join_date:
                text += f"تاريخ الانضمام: {join_date}\n"
            
            text += "─" * 20 + "\n"
        
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_to_groups")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def start_publishing(self, query, context):
        """بدء النشر التلقائي"""
        admin_id = query.from_user.id
        if self.manager.start_publishing(admin_id):
            await query.edit_message_text("🚀 تم بدء النشر التلقائي في جميع الحسابات والمجموعات")
        else:
            await query.edit_message_text("⚠️ النشر التلقائي يعمل بالفعل")
    
    async def stop_publishing(self, query, context):
        """إيقاف النشر التلقائي"""
        if self.manager.stop_publishing():
            await query.edit_message_text("⏹️ تم إيقاف النشر التلقائي")
        else:
            await query.edit_message_text("⚠️ النشر التلقائي غير نشط")
    
    # قسم تجميع الروابط
    async def manage_link_collector(self, query, context):
        """إدارة تجميع روابط واتساب وتليجرام."""
        admin_id = query.from_user.id
        settings = self.db.get_link_collection_settings(admin_id)
        counts = self.db.get_collected_links_count(admin_id)
        telegram_count = counts.get('telegram', 0)
        whatsapp_count = counts.get('whatsapp', 0)
        status = "🟢 يعمل" if self.manager.link_collection_active else "🔴 متوقف"

        if settings:
            telegram_target, whatsapp_target, updated_date = settings
            targets_text = (
                f"📨 وجهة روابط تليجرام: `{telegram_target}`\n"
                f"📨 وجهة روابط واتساب: `{whatsapp_target}`\n"
                f"🕒 آخر تحديث: `{updated_date}`"
            )
        else:
            targets_text = "⚠️ لم يتم تحديد وجهات الإرسال بعد."

        text = (
            "🔗 **تجميع الروابط**\n\n"
            "هذه الخاصية تفحص رسائل المجموعات والقنوات الموجودة في الحسابات المضافة، "
            "وتستخرج فقط روابط مجموعات واتساب وروابط تليجرام العامة أو الخاصة.\n\n"
            f"الحالة: {status}\n"
            f"روابط تليجرام المجمعة: `{telegram_count}`\n"
            f"روابط واتساب المجمعة: `{whatsapp_count}`\n\n"
            f"{targets_text}\n\n"
            "ملاحظة: الحسابات المضافة يجب أن تكون قادرة على الإرسال في وجهات التجميع."
        )

        keyboard = [
            [InlineKeyboardButton("🎯 تحديد وجهات التجميع", callback_data="set_link_targets")],
            [InlineKeyboardButton("🚀 بدء تجميع الروابط", callback_data="start_link_collection")],
            [InlineKeyboardButton("⏹️ إيقاف تجميع الروابط", callback_data="stop_link_collection")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def set_link_targets_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء تحديد وجهات إرسال الروابط المجمعة."""
        context.user_data['conversation_active'] = True
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "🎯 **تحديد وجهات تجميع الروابط**\n\n"
                "أرسل وجهتين في رسالتين منفصلتين أو في رسالة واحدة من سطرين:\n\n"
                "السطر الأول: رابط/معرف القناة أو المجموعة التي تستقبل روابط تليجرام\n"
                "السطر الثاني: رابط/معرف القناة أو المجموعة التي تستقبل روابط واتساب\n\n"
                "مثال:\n"
                "`@telegram_links_channel`\n"
                "`@whatsapp_links_channel`\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "🎯 **تحديد وجهات تجميع الروابط**\n\n"
                "أرسل السطر الأول لوجهة روابط تليجرام، والسطر الثاني لوجهة روابط واتساب.\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return SET_LINK_TARGETS

    def _clean_target_line(self, line):
        """تنظيف بسيط لسطر الوجهة."""
        line = line.strip()
        prefixes = [
            'telegram:', 'tg:', 'تليجرام:', 'روابط تليجرام:',
            'whatsapp:', 'wa:', 'واتساب:', 'روابط واتساب:'
        ]
        lowered = line.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix.lower()):
                return line[len(prefix):].strip()
        return line

    async def set_link_targets_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """حفظ وجهات تجميع الروابط."""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END

        text = update.message.text.strip()
        lines = [self._clean_target_line(line) for line in text.replace(',', '\n').splitlines() if line.strip()]

        if len(lines) == 1:
            # دعم الإدخال على مرحلتين لمن يرسل كل وجهة في رسالة منفصلة.
            if 'telegram_links_target' not in context.user_data:
                context.user_data['telegram_links_target'] = lines[0]
                await update.message.reply_text(
                    "✅ تم حفظ وجهة روابط تليجرام مؤقتاً.\n\n"
                    "الآن أرسل وجهة روابط واتساب:",
                    parse_mode='Markdown'
                )
                return SET_LINK_TARGETS
            lines = [context.user_data.pop('telegram_links_target'), lines[0]]

        if len(lines) < 2:
            await update.message.reply_text(
                "❌ أرسل وجهتين: الأولى لروابط تليجرام والثانية لروابط واتساب.\n"
                "يمكنك إرسال كل وجهة في سطر مستقل."
            )
            return SET_LINK_TARGETS

        telegram_target = lines[0].strip()
        whatsapp_target = lines[1].strip()

        if not telegram_target or not whatsapp_target:
            await update.message.reply_text("❌ لا يمكن ترك أي وجهة فارغة.")
            return SET_LINK_TARGETS

        admin_id = update.message.from_user.id
        self.db.set_link_collection_settings(admin_id, telegram_target, whatsapp_target)
        context.user_data['conversation_active'] = False
        context.user_data.pop('telegram_links_target', None)

        await update.message.reply_text(
            "✅ تم حفظ وجهات التجميع بنجاح\n\n"
            f"روابط تليجرام ➜ `{telegram_target}`\n"
            f"روابط واتساب ➜ `{whatsapp_target}`\n\n"
            "يمكنك الآن الضغط على زر بدء تجميع الروابط.",
            parse_mode='Markdown'
        )
        await self.start(update, context)
        return ConversationHandler.END

    async def start_link_collection(self, query, context):
        """بدء تجميع الروابط من الحسابات المضافة."""
        admin_id = query.from_user.id
        settings = self.db.get_link_collection_settings(admin_id)
        if not settings:
            await query.edit_message_text(
                "❌ يجب تحديد وجهات التجميع أولاً من زر: 🎯 تحديد وجهات التجميع."
            )
            return

        if self.manager.start_link_collection(admin_id):
            await query.edit_message_text(
                "🚀 تم بدء تجميع الروابط.\n\n"
                "سيتم فحص المجموعات والقنوات في الحسابات المضافة وإرسال روابط تليجرام وواتساب إلى الوجهات المحددة."
            )
        else:
            await query.edit_message_text("⚠️ تجميع الروابط يعمل بالفعل.")

    async def stop_link_collection(self, query, context):
        """إيقاف تجميع الروابط."""
        if self.manager.stop_link_collection():
            await query.edit_message_text("⏹️ تم إيقاف تجميع الروابط.")
        else:
            await query.edit_message_text("⚠️ تجميع الروابط غير نشط حالياً.")

    # قسم إدارة الردود
    async def manage_replies(self, query, context):
        """إدارة الردود"""
        keyboard = [
            [InlineKeyboardButton("💬 الردود في الخاص", callback_data="private_replies")],
            [InlineKeyboardButton("👥 الردود في القروبات", callback_data="group_replies")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "💬 **إدارة الردود**\n\n"
            "اختر نوع الردود التي تريد إدارتها:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def manage_private_replies(self, query, context):
        """إدارة الردود الخاصة"""
        admin_id = query.from_user.id
        replies = self.db.get_private_replies(admin_id)
        
        text = "💬 **الردود في الخاص:**\n\n"
        keyboard = []
        
        if replies:
            for reply in replies:
                reply_id, reply_text, is_active, added_date, reply_admin_id = reply
                status = "🟢 نشط" if is_active else "🔴 غير نشط"
                
                text += f"**#{reply_id}**\n"
                text += f"📝 {reply_text[:50]}...\n"
                text += f"الحالة: {status}\n"
                text += "─" * 20 + "\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ حذف رد الخاص #{reply_id}", callback_data=f"delete_private_reply_{reply_id}")])
        else:
            text += "❌ لا توجد ردود مضافة\n"
        
        keyboard.append([InlineKeyboardButton("➕ إضافة رد", callback_data="add_private_reply")])
        keyboard.append([InlineKeyboardButton("🚀 بدء الرد التلقائي", callback_data="start_private_reply")])
        keyboard.append([InlineKeyboardButton("⏹️ إيقاف الرد التلقائي", callback_data="stop_private_reply")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_replies")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def delete_private_reply(self, query, context, reply_id):
        """حذف رد من ردود الخاص."""
        admin_id = query.from_user.id
        deleted = self.db.delete_private_reply(reply_id, admin_id)
        if deleted:
            await query.answer("✅ تم حذف الرد", show_alert=False)
        else:
            await query.answer("❌ لم يتم العثور على الرد أو لا تملك صلاحية حذفه", show_alert=True)
        await self.manage_private_replies(query, context)
    
    async def add_private_reply_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة رد خاص"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "💬 **إضافة رد في الخاص**\n\n"
                "يرجى إرسال نص الرد الذي سيتم إرساله للمستخدمين في الخاص:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "💬 **إضافة رد في الخاص**\n\n"
                "يرجى إرسال نص الرد الذي سيتم إرساله للمستخدمين في الخاص:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_PRIVATE_TEXT
    
    async def add_private_reply_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة نص الرد الخاص"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        reply_text = update.message.text
        admin_id = update.message.from_user.id
        
        self.db.add_private_reply(reply_text, admin_id=admin_id)
        await update.message.reply_text("✅ تم إضافة الرد في الخاص بنجاح")
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def start_private_reply(self, query, context):
        """بدء الرد التلقائي في الخاص"""
        admin_id = query.from_user.id
        if self.manager.start_private_reply(admin_id):
            await query.edit_message_text("🚀 تم بدء الرد التلقائي على الرسائل الخاصة")
        else:
            await query.edit_message_text("⚠️ الرد التلقائي على الرسائل الخاصة يعمل بالفعل")
    
    async def stop_private_reply(self, query, context):
        """إيقاف الرد التلقائي في الخاص"""
        if self.manager.stop_private_reply():
            await query.edit_message_text("⏹️ تم إيقاف الرد التلقائي على الرسائل الخاصة")
        else:
            await query.edit_message_text("⚠️ الرد التلقائي على الرسائل الخاصة غير نشط")
    
    async def manage_group_replies(self, query, context):
        """إدارة الردود في القروبات."""
        admin_id = query.from_user.id
        text_replies = self.db.get_group_text_replies(admin_id)
        photo_replies = self.db.get_group_photo_replies(admin_id)
        random_replies = self.db.get_group_random_replies(admin_id)

        text = "👥 **الردود في القروبات:**\n\n"
        keyboard = []

        text += "**الردود على رسائل محددة:**\n"
        if text_replies:
            for reply in text_replies:
                reply_id, trigger, reply_text, is_active, added_date, reply_admin_id = reply
                status = "🟢 نشط" if is_active else "🔴 غير نشط"
                text += f"**#{reply_id}** - {trigger}\n"
                text += f"➡️ {reply_text[:30]}...\n"
                text += f"الحالة: {status}\n"
                text += "─" * 20 + "\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ حذف رد محدد #{reply_id}", callback_data=f"delete_group_text_reply_{reply_id}")])
        else:
            text += "❌ لا توجد ردود نصية محددة\n"

        text += "\n**الردود مع الصور:**\n"
        if photo_replies:
            for reply in photo_replies:
                reply_id, trigger, reply_text, media_path, is_active, added_date, reply_admin_id = reply
                status = "🟢 نشط" if is_active else "🔴 غير نشط"
                text += f"**#{reply_id}** - {trigger}\n"
                text += f"➡️ {reply_text[:30]}...\n"
                text += f"الحالة: {status}\n"
                text += "─" * 20 + "\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ حذف رد صورة #{reply_id}", callback_data=f"delete_group_photo_reply_{reply_id}")])
        else:
            text += "❌ لا توجد ردود مع صور\n"

        text += f"\n🎲 الردود العشوائية المحفوظة: `{len(random_replies)}`\n"
        text += "اضغط زر عرض الردود العشوائية لحذف أي رد منها.\n"

        keyboard.extend([
            [InlineKeyboardButton("🎲 عرض الردود العشوائية", callback_data="show_random_replies")],
            [InlineKeyboardButton("➕ إضافة رد محدد", callback_data="add_group_text_reply")],
            [InlineKeyboardButton("➕ إضافة رد مع صورة", callback_data="add_group_photo_reply")],
            [InlineKeyboardButton("➕ إضافة رد عشوائي", callback_data="add_random_reply")],
            [InlineKeyboardButton("🚀 بدء الردود المحددة", callback_data="start_group_reply")],
            [InlineKeyboardButton("⏹️ إيقاف الردود المحددة", callback_data="stop_group_reply")],
            [InlineKeyboardButton("🚀 بدء الردود العشوائية", callback_data="start_random_reply")],
            [InlineKeyboardButton("⏹️ إيقاف الردود العشوائية", callback_data="stop_random_reply")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_replies")]
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def show_random_replies(self, query, context):
        """عرض الردود العشوائية فقط مع أزرار الحذف."""
        admin_id = query.from_user.id
        random_replies = self.db.get_group_random_replies(admin_id)

        text = "🎲 **الردود العشوائية في القروبات:**\n\n"
        keyboard = []

        if random_replies:
            for reply in random_replies:
                reply_id, reply_text, is_active, added_date, reply_admin_id = reply
                status = "🟢 نشط" if is_active else "🔴 غير نشط"
                text += f"**#{reply_id}**\n"
                text += f"📝 {reply_text[:80]}...\n"
                text += f"الحالة: {status}\n"
                text += "─" * 20 + "\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ حذف رد عشوائي #{reply_id}", callback_data=f"delete_random_reply_{reply_id}")])
        else:
            text += "❌ لا توجد ردود عشوائية مضافة.\n"

        keyboard.extend([
            [InlineKeyboardButton("➕ إضافة رد عشوائي", callback_data="add_random_reply")],
            [InlineKeyboardButton("🚀 بدء الردود العشوائية", callback_data="start_random_reply")],
            [InlineKeyboardButton("⏹️ إيقاف الردود العشوائية", callback_data="stop_random_reply")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="group_replies")]
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def delete_group_text_reply(self, query, context, reply_id):
        """حذف رد محدد نصي من القروبات."""
        admin_id = query.from_user.id
        deleted = self.db.delete_group_text_reply(reply_id, admin_id)
        if deleted:
            await query.answer("✅ تم حذف الرد المحدد", show_alert=False)
        else:
            await query.answer("❌ لم يتم العثور على الرد أو لا تملك صلاحية حذفه", show_alert=True)
        await self.manage_group_replies(query, context)

    async def delete_group_photo_reply(self, query, context, reply_id):
        """حذف رد صورة من القروبات مع حذف ملف الصورة إن وجد."""
        admin_id = query.from_user.id
        deleted, media_path = self.db.delete_group_photo_reply(reply_id, admin_id)
        if deleted:
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except Exception as e:
                    logger.warning(f"تعذر حذف ملف صورة الرد {media_path}: {str(e)}")
            await query.answer("✅ تم حذف رد الصورة", show_alert=False)
        else:
            await query.answer("❌ لم يتم العثور على الرد أو لا تملك صلاحية حذفه", show_alert=True)
        await self.manage_group_replies(query, context)

    async def delete_random_reply(self, query, context, reply_id):
        """حذف رد عشوائي من القروبات."""
        admin_id = query.from_user.id
        deleted = self.db.delete_group_random_reply(reply_id, admin_id)
        if deleted:
            await query.answer("✅ تم حذف الرد العشوائي", show_alert=False)
        else:
            await query.answer("❌ لم يتم العثور على الرد أو لا تملك صلاحية حذفه", show_alert=True)
        await self.show_random_replies(query, context)
    
    async def add_group_text_reply_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة رد نصي في القروبات"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "👥 **إضافة رد نصي في القروبات**\n\n"
                "يرجى إرسال النص الذي سيتم الرد عليه:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "👥 **إضافة رد نصي في القروبات**\n\n"
                "يرجى إرسال النص الذي سيتم الرد عليه:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_GROUP_TEXT
    
    async def add_group_text_reply_trigger(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة خطوات إضافة رد نصي في القروبات."""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END

        if 'group_text_trigger' not in context.user_data:
            context.user_data['group_text_trigger'] = update.message.text
            await update.message.reply_text(
                "👥 **إضافة رد نصي في القروبات**\n\n"
                "يرجى إرسال نص الرد:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
            return ADD_GROUP_TEXT

        trigger = context.user_data.pop('group_text_trigger')
        reply_text = update.message.text
        admin_id = update.message.from_user.id

        self.db.add_group_text_reply(trigger, reply_text, admin_id=admin_id)
        await update.message.reply_text("✅ تم إضافة الرد النصي في القروبات بنجاح")
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def add_group_text_reply_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """توافق قديم: يمرر للمعالج المرحلي الجديد."""
        return await self.add_group_text_reply_trigger(update, context)
    
    async def add_group_photo_reply_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة رد مع صورة في القروبات"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "👥 **إضافة رد مع صورة في القروبات**\n\n"
                "يرجى إرسال النص الذي سيتم الرد عليه:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "👥 **إضافة رد مع صورة في القروبات**\n\n"
                "يرجى إرسال النص الذي سيتم الرد عليه:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_GROUP_PHOTO
    
    async def add_group_photo_reply_trigger(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة خطوات إضافة رد مع صورة في القروبات."""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END

        if 'group_photo_trigger' not in context.user_data:
            context.user_data['group_photo_trigger'] = update.message.text
            await update.message.reply_text(
                "👥 **إضافة رد مع صورة في القروبات**\n\n"
                "يرجى إرسال نص الرد:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
            return ADD_GROUP_PHOTO

        if 'group_photo_text' not in context.user_data:
            context.user_data['group_photo_text'] = update.message.text
            await update.message.reply_text(
                "👥 **إضافة رد مع صورة في القروبات**\n\n"
                "يرجى إرسال الصورة:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
            return ADD_GROUP_PHOTO

        await update.message.reply_text("❌ يرجى إرسال صورة صالحة أو /cancel للإلغاء")
        return ADD_GROUP_PHOTO
    
    async def add_group_photo_reply_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """توافق قديم: يمرر للمعالج المرحلي الجديد."""
        return await self.add_group_photo_reply_trigger(update, context)
    
    async def add_group_photo_reply_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة صورة الرد"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        if update.message.photo:
            if 'group_photo_trigger' not in context.user_data or 'group_photo_text' not in context.user_data:
                await update.message.reply_text("❌ بيانات الرد غير مكتملة. ابدأ العملية من جديد.")
                context.user_data['conversation_active'] = False
                await self.start(update, context)
                return ConversationHandler.END

            trigger = context.user_data.pop('group_photo_trigger')
            reply_text = context.user_data.pop('group_photo_text')
            admin_id = update.message.from_user.id
            
            file_id = update.message.photo[-1].file_id
            file = await context.bot.get_file(file_id)
            file_path = os.path.join(GROUP_REPLIES_DIR, f"{file_id}.jpg")
            await file.download_to_drive(file_path)
            
            self.db.add_group_photo_reply(trigger, reply_text, file_path, admin_id=admin_id)
            await update.message.reply_text("✅ تم إضافة الرد مع الصورة في القروبات بنجاح")
        else:
            await update.message.reply_text("❌ يرجى إرسال صورة صالحة")
            return ADD_GROUP_PHOTO
        
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def add_random_reply_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة رد عشوائي"""
        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "🎲 **إضافة رد عشوائي في القروبات**\n\n"
                "يرجى إرسال نص الرد العشوائي:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "🎲 **إضافة رد عشوائي في القروبات**\n\n"
                "يرجى إرسال نص الرد العشوائي:\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_RANDOM_REPLY
    
    async def add_random_reply_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة نص الرد العشوائي"""
        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        reply_text = update.message.text
        admin_id = update.message.from_user.id
        
        self.db.add_group_random_reply(reply_text, admin_id=admin_id)
        await update.message.reply_text("✅ تم إضافة الرد العشوائي بنجاح")
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def start_group_reply(self, query, context):
        """بدء الرد التلقائي في القروبات"""
        admin_id = query.from_user.id
        if self.manager.start_group_reply(admin_id):
            await query.edit_message_text("🚀 تم بدء الرد التلقائي على الرسائل المحددة في القروبات")
        else:
            await query.edit_message_text("⚠️ الرد التلقائي على الرسائل المحددة في القروبات يعمل بالفعل")
    
    async def stop_group_reply(self, query, context):
        """إيقاف الرد التلقائي في القروبات"""
        if self.manager.stop_group_reply():
            await query.edit_message_text("⏹️ تم إيقاف الرد التلقائي على الرسائل المحددة في القروبات")
        else:
            await query.edit_message_text("⚠️ الرد التلقائي على الرسائل المحددة في القروبات غير نشط")
    
    async def start_random_reply(self, query, context):
        """بدء الردود العشوائية في القروبات"""
        admin_id = query.from_user.id
        if self.manager.start_random_reply(admin_id):
            await query.edit_message_text("🚀 تم بدء الردود العشوائية في القروبات (الرد على 100% من الرسائل)")
        else:
            await query.edit_message_text("⚠️ الردود العشوائية في القروبات تعمل بالفعل")
    
    async def stop_random_reply(self, query, context):
        """إيقاف الردود العشوائية في القروبات"""
        if self.manager.stop_random_reply():
            await query.edit_message_text("⏹️ تم إيقاف الردود العشوائية في القروبات")
        else:
            await query.edit_message_text("⚠️ الردود العشوائية في القروبات غير نشطة")
    
    # قسم إدارة المشرفين
    async def manage_admins(self, query, context):
        """إدارة المشرفين"""
        if not self._is_owner(query.from_user.id):
            await query.edit_message_text("❌ إدارة المشرفين متاحة للمالك فقط.")
            return

        keyboard = [
            [InlineKeyboardButton("➕ إضافة مشرف", callback_data="add_admin")],
            [InlineKeyboardButton("👨‍💼 عرض المشرفين", callback_data="show_admins")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "👨‍💼 **إدارة المشرفين**\n\n"
            "اختر الإجراء الذي تريد تنفيذه:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    
    async def add_admin_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء إضافة مشرف"""
        user_id = update.effective_user.id
        if not self._is_owner(user_id):
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ إضافة المشرفين متاحة للمالك فقط.")
            else:
                await update.message.reply_text("❌ إضافة المشرفين متاحة للمالك فقط.")
            return ConversationHandler.END

        context.user_data['conversation_active'] = True
        
        if update.callback_query:
            query = update.callback_query
            await query.edit_message_text(
                "👨‍💼 **إضافة مشرف جديد**\n\n"
                "يرجى إرسال معرف المستخدم (User ID) للمشرف الجديد:\n\n"
                "يمكنك الحصول على الـ User ID من @userinfobot\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                "👨‍💼 **إضافة مشرف جديد**\n\n"
                "يرجى إرسال معرف المستخدم (User ID) للمشرف الجديد:\n\n"
                "يمكنك الحصول على الـ User ID من @userinfobot\n\n"
                "أو أرسل /cancel للإلغاء",
                parse_mode='Markdown'
            )
        return ADD_ADMIN
    
    async def add_admin_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة معرف المشرف"""
        if not self._is_owner(update.message.from_user.id):
            context.user_data['conversation_active'] = False
            await update.message.reply_text("❌ إضافة المشرفين متاحة للمالك فقط.")
            return ConversationHandler.END

        if not context.user_data.get('conversation_active'):
            await update.message.reply_text("❌ تم إلغاء العملية. استخدم /start للبدء من جديد.")
            return ConversationHandler.END
            
        try:
            user_id = int(update.message.text)
            
            username = "يتم إضافته"
            full_name = "مشرف جديد"
            
            result, message = self.db.add_admin(user_id, username, full_name, False)
            await update.message.reply_text(f"✅ {message}\n\nتم إضافة المستخدم {user_id} كمشرف")
                
        except ValueError:
            await update.message.reply_text("❌ معرف المستخدم يجب أن يكون رقماً")
        
        context.user_data['conversation_active'] = False
        await self.start(update, context)
        return ConversationHandler.END
    
    async def show_admins(self, query, context):
        """عرض المشرفين"""
        if not self._is_owner(query.from_user.id):
            await query.edit_message_text("❌ عرض المشرفين متاح للمالك فقط.")
            return

        admins = self.db.get_admins()
        
        if not admins:
            await query.edit_message_text("❌ لا توجد مشرفين مضافة")
            return
        
        text = "👨‍💼 **المشرفين المضافين:**\n\n"
        keyboard = []
        
        for admin in admins:
            admin_id, user_id, username, full_name, added_date, is_super_admin = admin
            role = "🟢 مشرف رئيسي" if is_super_admin else "🔵 مشرف عادي"
            
            text += f"**#{admin_id}** - {full_name}\n"
            text += f"المعرف: {user_id} | {username}\n"
            text += f"الدور: {role}\n"
            text += "─" * 20 + "\n"
            
            keyboard.append([InlineKeyboardButton(f"🗑️ حذف #{admin_id}", callback_data=f"delete_admin_{admin_id}")])
        
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_admins")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    
    async def delete_admin(self, query, context, admin_id):
        """حذف مشرف مع حماية المالك الرئيسي من الحذف."""
        if not self._is_owner(query.from_user.id):
            await query.edit_message_text("❌ حذف المشرفين متاح للمالك فقط.")
            return

        for admin in self.db.get_admins():
            row_id, user_id, username, full_name, added_date, is_super_admin = admin
            if row_id == admin_id and int(user_id) == OWNER_ID:
                await query.edit_message_text("❌ لا يمكن حذف المالك الرئيسي من لوحة التحكم.")
                return

        self.db.delete_admin(admin_id)
        await query.edit_message_text(f"✅ تم حذف المشرف #{admin_id}")
        await self.show_admins(query, context)
    
    # قسم الإعدادات
    async def settings_menu(self, query, context):
        """قائمة الإعدادات"""
        keyboard = [
            [InlineKeyboardButton("📊 حالة البوت", callback_data="bot_status")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            "⚙️ **إعدادات البوت**\n\n"
            "اختر الإعداد الذي تريد تعديله:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def bot_status(self, query, context):
        """عرض حالة البوت والعمليات العاملة."""
        admin_id = query.from_user.id
        accounts_count = len(self.db.get_accounts(admin_id))
        ads_count = len(self.db.get_ads(admin_id))
        groups_count = len(self.db.get_groups(admin_id))
        admins_count = len(self.db.get_admins())

        publishing = "🟢 يعمل" if self.manager.publishing_active else "🔴 متوقف"
        private_reply = "🟢 يعمل" if self.manager.private_reply_active else "🔴 متوقف"
        group_reply = "🟢 يعمل" if self.manager.group_reply_active else "🔴 متوقف"
        random_reply = "🟢 يعمل" if self.manager.random_reply_active else "🔴 متوقف"
        link_collection = "🟢 يعمل" if self.manager.link_collection_active else "🔴 متوقف"

        text = (
            "📊 **حالة البوت**\n\n"
            f"👤 آيدي المالك: `{OWNER_ID}`\n"
            f"👥 الحسابات: `{accounts_count}`\n"
            f"📢 الإعلانات: `{ads_count}`\n"
            f"🔗 المجموعات: `{groups_count}`\n"
            f"👨‍💼 المشرفون: `{admins_count}`\n\n"
            f"🚀 النشر التلقائي: {publishing}\n"
            f"💬 ردود الخاص: {private_reply}\n"
            f"👥 ردود القروبات المحددة: {group_reply}\n"
            f"🎲 الردود العشوائية: {random_reply}\n"
            f"🔗 تجميع الروابط: {link_collection}"
        )

        keyboard = []
        if self._is_owner(query.from_user.id):
            keyboard.append([InlineKeyboardButton("🧨 ضبط المصنع", callback_data="factory_reset_warning")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data="settings")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

    async def factory_reset_warning(self, query, context):
        """رسالة تأكيد قبل ضبط المصنع."""
        if not self._is_owner(query.from_user.id):
            await query.edit_message_text("❌ ضبط المصنع متاح للمالك فقط.")
            return

        keyboard = [
            [InlineKeyboardButton("✅ نعم، امسح كل البيانات", callback_data="factory_reset_confirm")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="factory_reset_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "⚠️ **تأكيد ضبط المصنع**\n\n"
            "سيتم إيقاف كل العمليات العاملة ثم حذف كل البيانات التالية:\n"
            "• الحسابات والجلسات\n"
            "• الإعلانات والوسائط\n"
            "• المجموعات\n"
            "• ردود الخاص وردود القروبات والردود العشوائية\n"
            "• إعدادات تجميع الروابط والروابط المجمعة\n"
            "• المشرفون، مع إبقاء المالك الرئيسي فقط\n\n"
            "لا يمكن التراجع عن هذا الإجراء.",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def factory_reset_confirm(self, query, context):
        """تنفيذ ضبط المصنع."""
        if not self._is_owner(query.from_user.id):
            await query.edit_message_text("❌ ضبط المصنع متاح للمالك فقط.")
            return

        self.manager.stop_all_services()
        self.db.factory_reset(OWNER_ID)
        self._clear_data_directories()

        await query.edit_message_text(
            "✅ تم تنفيذ ضبط المصنع بنجاح.\n\n"
            "تم حذف كل البيانات السابقة وإبقاء المالك الرئيسي فقط. استخدم /start للبدء من جديد."
        )

    def _clear_data_directories(self):
        """حذف الملفات المحفوظة داخل مجلدات بيانات البوت مع إبقاء المجلدات نفسها."""
        for directory in (ADS_DIR, PROFILE_PHOTOS_DIR, GROUP_REPLIES_DIR):
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                continue

            for item in os.listdir(directory):
                path = os.path.join(directory, item)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except Exception as e:
                    logger.warning(f"تعذر حذف {path}: {str(e)}")
    
    def setup_handlers(self):
        """إعداد معالجات البوت"""
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("cancel", self.cancel))
        
        # معالجات المحادثة
        add_account_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_account_start, pattern="^add_account$")],
            states={
                ADD_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_session)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(add_account_conv)

        arrange_accounts_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.arrange_accounts_start, pattern="^arrange_accounts$")],
            states={
                ARRANGE_ACCOUNT_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.arrange_accounts_name)
                ],
                ARRANGE_ACCOUNT_PHOTO: [
                    MessageHandler(filters.PHOTO | filters.Document.ALL, self.arrange_accounts_photo)
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(arrange_accounts_conv)

        
        add_ad_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_ad_type, pattern="^ad_type_")],
            states={
                ADD_AD_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_ad_text)],
                ADD_AD_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL, self.add_ad_media)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(add_ad_conv)
        
        add_group_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_group_start, pattern="^add_group$")],
            states={
                ADD_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_group_link)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(add_group_conv)
        
        add_admin_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_admin_start, pattern="^add_admin$")],
            states={
                ADD_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_admin_id)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(add_admin_conv)
        
        private_reply_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_private_reply_start, pattern="^add_private_reply$")],
            states={
                ADD_PRIVATE_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_private_reply_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(private_reply_conv)
        
        group_text_reply_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_group_text_reply_start, pattern="^add_group_text_reply$")],
            states={
                ADD_GROUP_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_group_text_reply_trigger)
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(group_text_reply_conv)
        
        group_photo_reply_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_group_photo_reply_start, pattern="^add_group_photo_reply$")],
            states={
                ADD_GROUP_PHOTO: [
                    MessageHandler(filters.PHOTO, self.add_group_photo_reply_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_group_photo_reply_trigger)
                ]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(group_photo_reply_conv)
        
        random_reply_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.add_random_reply_start, pattern="^add_random_reply$")],
            states={
                ADD_RANDOM_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_random_reply_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(random_reply_conv)

        link_targets_conv = ConversationHandler(
            entry_points=[CallbackQueryHandler(self.set_link_targets_start, pattern="^set_link_targets$")],
            states={
                SET_LINK_TARGETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_link_targets_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)]
        )
        self.application.add_handler(link_targets_conv)
        
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))
    
    def run(self):
        """تشغيل البوت"""
        self.application = Application.builder().token(BOT_TOKEN).build()
        self.setup_handlers()
        
        self.db.add_admin(OWNER_ID, "@owner", "المالك الرئيسي", True)
        
        print("🤖 البوت يعمل الآن...")
        print(f"✅ تم إضافة الآيدي {OWNER_ID} كمشرف رئيسي")
        print("🎯 البوت جاهز للتشغيل بعد الإصلاحات")
        
        self.application.run_polling()


# =================== AI PRO SAFE ADMIN EXTENSION ===================
# No platform-limit bypass, forced folder cycling, or account-rotation features.
import urllib.request
import urllib.error
from contextlib import contextmanager
from telethon.tl import functions as tl_functions, types as tl_types

NEW_PROFILE_NAME = 101
NEW_PROFILE_BIO = 102
NEW_PROFILE_PHOTOS = 103
NEW_FOLDER_LINKS = 104
NEW_AI_MESSAGE = 105

PRIVACY_TEMPLATE = (
    ("Phone Number", "InputPrivacyKeyPhoneNumber", "nobody"),
    ("Last Seen & Online", "InputPrivacyKeyStatusTimestamp", "everybody"),
    ("Profile Photos", "InputPrivacyKeyProfilePhoto", "everybody"),
    ("Forwarded Messages", "InputPrivacyKeyForwards", "everybody"),
    ("Calls", "InputPrivacyKeyPhoneCall", "nobody"),
    ("Voice Messages", "InputPrivacyKeyVoiceMessages", "everybody"),
    ("Birthday", "InputPrivacyKeyBirthday", "contacts"),
    ("Bio", "InputPrivacyKeyAbout", "everybody"),
    ("Saved Music", "InputPrivacyKeySavedMusic", "everybody"),
    ("Invites", "InputPrivacyKeyChatInvite", "nobody"),
)


def query_ai_advice(prompt, statistics):
    """OpenAI Responses API: advice only, zero Telegram tool permissions."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return "⚠️ لا يوجد OPENAI_API_KEY. أضفه إلى متغيرات البيئة لتفعيل المساعد."
    endpoint = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    data = {
        "model": os.environ.get("OPENAI_MODEL", "gpt-6"),
        "instructions": (
            "أنت مساعد إداري عربي لبوت تيليجرام. تقدم التحليل والمساعدة في كتابة المحتوى "
            "وتشخيص الأخطاء العامة. أنت للقراءة والمشورة فقط ولا تملك أدوات لتنفيذ الأوامر. "
            "لا تدعِ أنك أصلحت ملفاً أو أرسلت رسالة أو فحصت خادماً مباشرة. "
            "لا تقترح تجاوز قيود تيليجرام أو نشر رسائل غير مرغوبة. "
            "لا تطلب ولا تكشف مفاتيح API أو رموز البوت أو بيانات الجلسات. "
            "اختر إجابة عملية موجزة وحدد ما تحتاج اختباره فعلياً."
        ),
        "input": "إحصاءات عامة آمنة: " + json.dumps(statistics, ensure_ascii=False) + "\nطلب المشرف: " + prompt[:3500],
        "store": False,
        "max_output_tokens": 850,
    }
    req = urllib.request.Request(
        endpoint + "/responses",
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=55) as res:
            parsed = json.load(res)
        if isinstance(parsed.get("output_text"), str) and parsed["output_text"].strip():
            return parsed["output_text"].strip()
        fragments = []
        for item in parsed.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text" and c.get("text"):
                    fragments.append(c["text"])
        return "\n".join(fragments).strip() or "تعذر استخراج نص الإجابة من الخدمة."
    except urllib.error.HTTPError as exc:
        # Deliberately never echo response bodies: they could include submitted context.
        return f"❌ تعذّر الاتصال بخدمة الذكاء الاصطناعي (HTTP {exc.code}). راجع المفتاح واسم النموذج."
    except Exception as exc:
        logger.warning("AI API request failed: %s", type(exc).__name__)
        return "❌ تعذر الاتصال بخدمة الذكاء الاصطناعي. تحقق من الإنترنت والإعدادات."


class ProBotHandler(BotHandler):
    """Non-destructive extension of the existing Telegram administration bot."""

    def __init__(self):
        super().__init__()
        self._init_pro_tables()

    @contextmanager
    def _db_conn(self):
        conn = sqlite3.connect(DB_NAME, timeout=20)
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_pro_tables(self):
        with self._db_conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_profile_templates (
                admin_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                bio TEXT NOT NULL DEFAULT '',
                photos_json TEXT NOT NULL DEFAULT '[]',
                auto_apply INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_folder_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_id,link)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS ai_pro_preferences (
                admin_id INTEGER PRIMARY KEY,
                folder_target INTEGER NOT NULL DEFAULT 2
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS profile_job_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )""")

    def _profile_get(self, admin_id):
        with self._db_conn() as conn:
            return conn.execute(
                "SELECT display_name, bio, photos_json, auto_apply FROM ai_profile_templates WHERE admin_id=?",
                (int(admin_id),)
            ).fetchone()

    def _profile_save(self, admin_id, name, bio, photos):
        with self._db_conn() as conn:
            conn.execute("""INSERT INTO ai_profile_templates (admin_id,display_name,bio,photos_json,auto_apply)
                VALUES(?,?,?,?,1) ON CONFLICT(admin_id) DO UPDATE SET
                display_name=excluded.display_name,bio=excluded.bio,
                photos_json=excluded.photos_json,updated_at=CURRENT_TIMESTAMP""",
                (int(admin_id), name, bio, json.dumps(photos, ensure_ascii=False)))

    def _profile_toggle(self, admin_id):
        with self._db_conn() as conn:
            conn.execute("UPDATE ai_profile_templates SET auto_apply=CASE WHEN auto_apply=1 THEN 0 ELSE 1 END WHERE admin_id=?", (admin_id,))

    def _folder_list(self, admin_id):
        with self._db_conn() as conn:
            return conn.execute("SELECT id,link FROM chat_folder_links WHERE admin_id=? ORDER BY id", (admin_id,)).fetchall()

    def _folder_target(self, admin_id):
        with self._db_conn() as conn:
            result = conn.execute("SELECT folder_target FROM ai_pro_preferences WHERE admin_id=?", (admin_id,)).fetchone()
        return result[0] if result else 2

    def _main_keyboard(self, user_id):
        keyboard = super()._main_keyboard(user_id)
        keyboard.insert(1, [InlineKeyboardButton("🧩 ترتيب الحسابات والخصوصية", callback_data="pro_profile_menu")])
        keyboard.insert(2, [InlineKeyboardButton("📁 روابط المجلدات", callback_data="pro_folders")])
        keyboard.insert(3, [InlineKeyboardButton("🧠 المساعد الذكي", callback_data="pro_ai")])
        return keyboard

    async def manage_accounts(self, query, context):
        keyboard = [
            [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
            [InlineKeyboardButton("👥 عرض الحسابات", callback_data="show_accounts")],
            [InlineKeyboardButton("🧩 إعداد ترتيب الحسابات", callback_data="pro_profile_menu")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")],
        ]
        await query.edit_message_text("👥 إدارة الحسابات\n\nاختر العملية:", reply_markup=InlineKeyboardMarkup(keyboard))

    def setup_handlers(self):
        # New state machines register before legacy generic CallbackQueryHandler.
        self.application.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(self.pro_template_begin, pattern=r"^pro_template_begin$")],
            states={
                NEW_PROFILE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.pro_template_name)],
                NEW_PROFILE_BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.pro_template_bio)],
                NEW_PROFILE_PHOTOS: [MessageHandler((filters.PHOTO | filters.Document.IMAGE | filters.TEXT) & ~filters.COMMAND, self.pro_template_photo)],
            },
            fallbacks=[CommandHandler("cancel", self.pro_cancel)],
            allow_reentry=True,
        ))
        self.application.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(self.pro_folder_begin, pattern=r"^pro_folder_begin$")],
            states={NEW_FOLDER_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.pro_folder_save)]},
            fallbacks=[CommandHandler("cancel", self.pro_cancel)],
        ))
        self.application.add_handler(ConversationHandler(
            entry_points=[CallbackQueryHandler(self.pro_ai_begin, pattern=r"^pro_ai_begin$")],
            states={NEW_AI_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.pro_ai_reply)]},
            fallbacks=[CommandHandler("cancel", self.pro_cancel)],
        ))
        super().setup_handlers()

    async def pro_cancel(self, update, context):
        context.user_data.pop("pro_template_name", None)
        context.user_data.pop("pro_template_bio", None)
        context.user_data.pop("pro_template_photos", None)
        await update.message.reply_text("تم إلغاء هذه العملية. افتح /start للعودة إلى القائمة.")
        return ConversationHandler.END

    async def handle_callback(self, update, context):
        q = update.callback_query
        action = q.data
        if action.startswith("pro_"):
            await q.answer()
            if not self.db.is_admin(q.from_user.id):
                await q.edit_message_text("ليس لديك صلاحية الوصول.")
                return
            uid = q.from_user.id
            if action == "pro_profile_menu":
                return await self.pro_profile_menu(q, context)
            elif action == "pro_profile_toggle":
                self._profile_toggle(uid)
                return await self.pro_profile_menu(q, context)
            elif action == "pro_apply_all_confirm":
                return await self.pro_apply_all_confirm(q, context)
            elif action == "pro_apply_all_do":
                return await self.pro_apply_all_do(q, context)
            elif action.startswith("pro_apply_one_"):
                return await self.pro_apply_one(q, context, int(action.rsplit("_", 1)[1]))
            elif action == "pro_choose_one":
                return await self.pro_choose_one(q, context)
            elif action == "pro_folders":
                return await self.pro_folder_menu(q, context)
            elif action.startswith("pro_folder_target_"):
                target = int(action.rsplit("_", 1)[1])
                with self._db_conn() as conn:
                    conn.execute("""INSERT INTO ai_pro_preferences (admin_id,folder_target) VALUES (?,?)
                        ON CONFLICT(admin_id) DO UPDATE SET folder_target=excluded.folder_target""", (uid, target))
                return await self.pro_folder_menu(q, context)
            elif action.startswith("pro_folder_del_"):
                row_id = int(action.rsplit("_", 1)[1])
                with self._db_conn() as conn:
                    conn.execute("DELETE FROM chat_folder_links WHERE id=? AND admin_id=?", (row_id, uid))
                return await self.pro_folder_menu(q, context)
            elif action == "pro_ai":
                await q.edit_message_text(
                    "🧠 المساعد الذكي\n\nتحليل تقني واقتراحات وكتابة إعلانات. "
                    "لا ينفذ الأوامر ولا يقرأ الجلسات أو كلمات المرور.\n\n" 
                    "يلزم OPENAI_API_KEY.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💬 اسأل المساعد", callback_data="pro_ai_begin")],
                        [InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")],
                    ])
                )
                return
            # The three *_begin buttons are owned by their ConversationHandlers.
            return await self.start_from_query(q, context)
        return await super().handle_callback(update, context)

    async def pro_profile_menu(self, query, context):
        p = self._profile_get(query.from_user.id)
        if p:
            photos = json.loads(p[2])
            state = "مفعّل" if p[3] else "متوقف"
            description = f"الاسم: {p[0]}\nالبايو: {p[1] or 'فارغ'}\nالصور: {len(photos)}\nالتلقائي عند إضافة الحساب: {state}"
        else:
            description = "لم تقم بإنشاء قالب ترتيب حسابات بعد."
        buttons = [
            [InlineKeyboardButton("✏️ إنشاء / تعديل القالب", callback_data="pro_template_begin")],
        ]
        if p:
            buttons.extend([
                [InlineKeyboardButton("🔄 تبديل الترتيب التلقائي", callback_data="pro_profile_toggle")],
                [InlineKeyboardButton("👤 اختيار حساب لتطبيق القالب", callback_data="pro_choose_one")],
                [InlineKeyboardButton("👥 تطبيق القالب على جميع الحسابات", callback_data="pro_apply_all_confirm")],
            ])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")])
        await query.edit_message_text(
            "🧩 ترتيب الحسابات والخصوصية\n\n" + description +
            "\n\nتشمل العملية الاسم والبايو والصور والقصص والخصوصية المدعومة. "
            "التنزيل التلقائي للوسائط ومزامنة جهات الاتصال وإعداد الاقتراحات الخاصة بالتطبيق تُضبط يدوياً في تيليجرام.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def pro_template_begin(self, update, context):
        await update.callback_query.answer()
        if not self.db.is_admin(update.effective_user.id):
            await update.callback_query.edit_message_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        context.user_data["pro_template_name"] = None
        context.user_data["pro_template_bio"] = None
        context.user_data["pro_template_photos"] = []
        await update.callback_query.edit_message_text(
            "✏️ إعداد القالب\n\nأرسل الاسم الكامل الذي تريده، أو /cancel للإلغاء."
        )
        return NEW_PROFILE_NAME

    async def pro_template_name(self, update, context):
        if not self.db.is_admin(update.effective_user.id):
            await update.message.reply_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        name = re.sub(r"\s+", " ", (update.message.text or "").strip())
        if not name or len(name) > 128:
            await update.message.reply_text("الاسم مطلوب ويجب ألا يزيد عن 128 حرفاً.")
            return NEW_PROFILE_NAME
        context.user_data["pro_template_name"] = name
        await update.message.reply_text("📝 أرسل البايو الجديد، أو اكتب علامة - لتركه فارغاً.")
        return NEW_PROFILE_BIO

    async def pro_template_bio(self, update, context):
        if not self.db.is_admin(update.effective_user.id):
            await update.message.reply_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        bio = (update.message.text or "").strip()
        if bio == "-":
            bio = ""
        if len(bio) > 70:
            await update.message.reply_text("البايو يتجاوز 70 حرفاً. أرسل بايو أقصر.")
            return NEW_PROFILE_BIO
        context.user_data["pro_template_bio"] = bio
        await update.message.reply_text(
            "🖼️ أرسل الصور واحدة تلو الأخرى (حتى 5 صور).\n"
            "بعد إرسال آخر صورة اكتب: تم\n"
            "ويمكنك كتابة تم دون أي صور للإبقاء على الصور القديمة.\n"
            "مهم: عند إرسال صور جديدة سيتم حذف صور الحساب القديمة أولاً."
        )
        return NEW_PROFILE_PHOTOS

    async def pro_template_photo(self, update, context):
        if not self.db.is_admin(update.effective_user.id):
            await update.message.reply_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        msg = update.message
        text = (msg.text or "").strip()
        if text == "تم":
            name = context.user_data.get("pro_template_name")
            bio = context.user_data.get("pro_template_bio", "")
            photos = context.user_data.get("pro_template_photos", [])
            if not name:
                await msg.reply_text("لم تُحفظ بيانات القالب، حاول مرة أخرى.")
                return ConversationHandler.END
            self._profile_save(msg.from_user.id, name, bio, photos)
            await msg.reply_text(
                f"✅ تم حفظ قالب الترتيب.\nالاسم: {name}\nالبايو: {bio or 'فارغ'}\n"
                f"الصور: {len(photos)}\n\nسيُطبق تلقائياً على الحسابات الجديدة. "
                "يمكنك تطبيقه على حساب حالي من القائمة."
            )
            for k in ("pro_template_name", "pro_template_bio", "pro_template_photos"):
                context.user_data.pop(k, None)
            return ConversationHandler.END
        if len(context.user_data.get("pro_template_photos", [])) >= 5:
            await msg.reply_text("الحد 5 صور. اكتب تم للحفظ.")
            return NEW_PROFILE_PHOTOS
        if msg.photo:
            file_id = msg.photo[-1].file_id
            suffix = ".jpg"
        elif msg.document and (msg.document.mime_type or "").lower() in ("image/jpeg", "image/png"):
            file_id = msg.document.file_id
            suffix = ".png" if msg.document.mime_type.lower() == "image/png" else ".jpg"
        else:
            await msg.reply_text("أرسل صورة JPG/PNG، أو اكتب تم للحفظ.")
            return NEW_PROFILE_PHOTOS
        try:
            file = await context.bot.get_file(file_id)
            base = f"profile_{msg.from_user.id}_{int(time.time())}_{len(context.user_data['pro_template_photos'])}_{random.randint(1000,9999)}"
            file_path = os.path.join(PROFILE_PHOTOS_DIR, base + suffix)
            await file.download_to_drive(file_path)
        except Exception as e:
            logger.warning("Profile download failed: %s", type(e).__name__)
            await msg.reply_text("تعذّر تحميل الصورة. حاول إرسالها من جديد.")
            return NEW_PROFILE_PHOTOS
        context.user_data["pro_template_photos"].append(file_path)
        await msg.reply_text(
            f"✅ تم حفظ الصورة رقم {len(context.user_data['pro_template_photos'])}. "
            "أرسل صورة أخرى، أو اكتب تم لإنهاء إعداد القالب."
        )
        return NEW_PROFILE_PHOTOS

    async def _apply_privacy(self, client):
        results = []
        for label, class_name, desired in PRIVACY_TEMPLATE:
            privacy_key = getattr(tl_types, class_name, None)
            if privacy_key is None:
                results.append(f"⚪ {label}: غير مدعوم في النسخة المثبتة")
                continue
            rule_class = {
                "nobody": tl_types.InputPrivacyValueDisallowAll,
                "everybody": tl_types.InputPrivacyValueAllowAll,
                "contacts": tl_types.InputPrivacyValueAllowContacts,
            }[desired]
            try:
                await client(tl_functions.account.SetPrivacyRequest(key=privacy_key(), rules=[rule_class()]))
                results.append(f"✅ {label}: {desired}")
            except FloodWaitError as e:
                results.append(f"⏸️ {label}: انتظر {e.seconds} ثانية")
                break
            except Exception as e:
                results.append(f"⚠️ {label}: {type(e).__name__}")
            await asyncio.sleep(0.6)
        results.extend([
            "⚪ Messages / Gifts: لا تُطبّق قيمة Everybody تلقائياً؛ مفاتيح API المتاحة لا تطابق معنى الواجهة مباشرة.",
            "⚪ Automatic media download / Sync Contacts / Suggest Frequent Contacts: إعدادات تطبيق تُضبط يدوياً.",
            "⚪ Delete Synced Contacts: لم تُنفّذ عملية حذف جهات الاتصال دون تأكيد منفصل.",
        ])
        return results

    async def _apply_profile_one(self, account, admin_id):
        account_id, session_string, phone, old_name, old_username, is_active = account
        p = self._profile_get(admin_id)
        if not p:
            return "❌ لم يتم حفظ قالب ترتيب بعد."
        display_name, bio, photos_json, _enabled = p
        photos = json.loads(photos_json)
        details = [f"الحساب #{account_id}:"]
        client = None
        try:
            client = make_user_client(session_string)
            await client.connect()
            if not await client.is_user_authorized():
                return f"❌ الحساب #{account_id}: الجلسة غير مفعلة."
            first_name, last_name = self.manager._split_profile_name(display_name)
            await client(UpdateProfileRequest(first_name=first_name, last_name=last_name, about=bio))
            details.append("✅ الاسم والبايو")
            if photos:
                try:
                    old_photos = await client.get_profile_photos("me")
                    if old_photos:
                        await client(DeletePhotosRequest(old_photos))
                    details.append(f"✅ تنظيف {len(old_photos)} صورة قديمة")
                except Exception as e:
                    details.append(f"⚠️ تعذّر حذف الصور القديمة: {type(e).__name__}")
                successes = 0
                for path in photos:
                    if not os.path.isfile(path):
                        details.append("⚠️ صورة من القالب غير موجودة على القرص")
                        continue
                    try:
                        uploaded = await client.upload_file(path)
                        await client(UploadProfilePhotoRequest(file=uploaded))
                        successes += 1
                        await asyncio.sleep(1)
                    except FloodWaitError as e:
                        details.append(f"⏸️ الصور: انتظر {e.seconds} ثانية")
                        break
                    except Exception as e:
                        details.append(f"⚠️ تعذّر رفع إحدى الصور: {type(e).__name__}")
                details.append(f"🖼️ صور رُفعت: {successes}/{len(photos)}")
            removed, story_error = await self.manager._delete_all_own_stories(client)
            details.append(f"{'⚠️' if story_error else '✅'} القصص: حُذفت {removed}" +
                           (f" ({story_error[:70]})" if story_error else ""))
            details.extend(await self._apply_privacy(client))
            me = await client.get_me()
            saved_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
            saved_username = f"@{me.username}" if me.username else "لا يوجد"
            self.db.update_account_profile_cache(account_id, saved_name, saved_username)
        except FloodWaitError as e:
            details.append(f"⏸️ حد تيليجرام: انتظر {e.seconds} ثانية؛ لا تُعاد المحاولة قبل انتهائه.")
        except Exception as e:
            details.append(f"❌ {type(e).__name__}: تعذّر إكمال الترتيب")
            logger.warning("Profile setup failed for account #%s: %s", account_id, type(e).__name__)
        finally:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        report = "\n".join(details)
        try:
            with self._db_conn() as conn:
                conn.execute("INSERT INTO profile_job_history (admin_id,account_id,details) VALUES (?,?,?)",
                             (admin_id, account_id, report))
        except sqlite3.Error:
            logger.exception("Could not write profile job log")
        return report

    async def pro_choose_one(self, query, context):
        rows = self.db.get_accounts_owned_by_admin(query.from_user.id)
        buttons = [[InlineKeyboardButton(f"#{r[0]} - {str(r[3])[:30]}", callback_data=f"pro_apply_one_{r[0]}")] for r in rows[:30]]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="pro_profile_menu")])
        await query.edit_message_text(
            "اختر حساباً لتطبيق القالب عليه.\n⚠️ سيتم تغيير الاسم والبايو وحذف الصور والقصص السابقة وفق القالب.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def pro_apply_one(self, query, context, account_id):
        accounts = self.db.get_accounts_owned_by_admin(query.from_user.id)
        account = next((a for a in accounts if a[0] == account_id), None)
        if account is None:
            await query.edit_message_text("الحساب غير موجود أو ليس ضمن الحسابات المصرح لك بها.")
            return
        await query.edit_message_text(f"جارٍ تطبيق قالب الملف الشخصي والخصوصية على الحساب #{account_id}...")
        result = await self._apply_profile_one(account, query.from_user.id)
        await query.message.reply_text(result[:4000])

    async def pro_apply_all_confirm(self, query, context):
        n = len(self.db.get_accounts_owned_by_admin(query.from_user.id))
        await query.edit_message_text(
            f"⚠️ تأكيد تطبيق قالب الملف الشخصي على {n} حساب.\n"
            "سيتم حذف صور وملفات تعريف قديمة وفق القالب، وقد تفشل بعض إعدادات الخصوصية.\n"
            "هل تؤكد؟",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ نعم، أؤكد", callback_data="pro_apply_all_do")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="pro_profile_menu")],
            ])
        )

    async def pro_apply_all_do(self, query, context):
        accounts = self.db.get_accounts_owned_by_admin(query.from_user.id)
        await query.edit_message_text(f"بدأ ترتيب {len(accounts)} حساب. ستحصل على تقارير للنتائج.")
        for idx, account in enumerate(accounts):
            result = await self._apply_profile_one(account, query.from_user.id)
            await query.message.reply_text(result[:4000])
            if idx < len(accounts) - 1:
                await asyncio.sleep(10)  # conservative pacing; does not bypass platform limits
        await query.message.reply_text("انتهت معالجة الحسابات المطلوبة. راجع تقارير كل حساب.")

    async def add_account_session(self, update, context):
        """Same legacy registration path; optionally apply saved profile to this new account."""
        if not context.user_data.get("conversation_active"):
            await update.message.reply_text("تم إلغاء الإضافة. افتح /start لإعادة المحاولة.")
            return ConversationHandler.END
        session = (update.message.text or "").strip()
        uid = update.message.from_user.id
        try:
            success, me = await self.manager.test_session(session)
        except Exception as exc:
            await update.message.reply_text(f"تعذّر التحقق من الجلسة: {type(exc).__name__}. تحقق من TELEGRAM_API_ID و TELEGRAM_API_HASH.")
            return ConversationHandler.END
        if not success:
            await update.message.reply_text("❌ الجلسة غير صالحة أو غير مفعلة؛ تحقق من بيانات API والجلسة.")
            return ConversationHandler.END
        phone = me.phone or "غير معروف"
        name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        username = f"@{me.username}" if me.username else "لا يوجد"
        added, result = self.db.add_account(session, phone, name, username, uid)
        await update.message.reply_text(("✅ " if added else "⚠️ ") + result + (f"\nالحساب: {name}" if added else ""))
        p = self._profile_get(uid)
        if added and p and p[3]:
            rows = self.db.get_accounts_owned_by_admin(uid)
            new_account = next((r for r in rows if r[1] == session), None)
            if new_account:
                await update.message.reply_text("🧩 بدأ تطبيق قالب ترتيب الحساب المحفوظ...")
                report = await self._apply_profile_one(new_account, uid)
                await update.message.reply_text(report[:4000])
        context.user_data["conversation_active"] = False
        await self.start(update, context)
        return ConversationHandler.END

    async def pro_folder_menu(self, query, context):
        uid = query.from_user.id
        links = self._folder_list(uid)
        target = self._folder_target(uid)
        buttons = [
            [InlineKeyboardButton("➕ حفظ روابط مجلدات", callback_data="pro_folder_begin")],
            [InlineKeyboardButton("🔢 تخطيط: مجلدان", callback_data="pro_folder_target_2"),
             InlineKeyboardButton("🔢 تخطيط: 5 مجلدات", callback_data="pro_folder_target_5")],
        ]
        for link_id, link in links[:15]:
            buttons.append([InlineKeyboardButton(f"🗑️ حذف الرابط #{link_id}", callback_data=f"pro_folder_del_{link_id}")])
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")])
        lines = "\n".join(f"#{i} {link}" for i, link in links[:12]) or "لا توجد روابط محفوظة"
        await query.edit_message_text(
            "📁 روابط مجلدات تيليجرام\n\n"
            f"عدد الروابط المحفوظة: {len(links)}\n"
            f"عدد المجلدات المرجعي للحساب: {target}\n\n{lines[:2200]}\n\n"
            "هذه قائمة تنظيمية فقط. الانضمام إلى المجلدات والمجموعات يتم بموافقة صاحب الحساب "
            "وبحدود تيليجرام، ولا توجد هنا عملية انضمام جماعي أو تدوير للمجلدات لتجاوز الحدود.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def pro_folder_begin(self, update, context):
        await update.callback_query.answer()
        if not self.db.is_admin(update.effective_user.id):
            await update.callback_query.edit_message_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        await update.callback_query.edit_message_text(
            "📁 أرسل روابط مجلدات تيليجرام، كل رابط في سطر مستقل.\n"
            "الصيغة: https://t.me/addlist/XXXXXXXX\n\nأرسل /cancel للإلغاء."
        )
        return NEW_FOLDER_LINKS

    async def pro_folder_save(self, update, context):
        if not self.db.is_admin(update.effective_user.id):
            await update.message.reply_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        uid = update.message.from_user.id
        pattern = re.compile(r"https?://(?:t\.me|telegram\.me)/addlist/([A-Za-z0-9_-]{4,100})(?:\S*)?", re.I)
        normalized = {f"https://t.me/addlist/{code}" for code in pattern.findall(update.message.text or "")}
        new_count = 0
        with self._db_conn() as conn:
            for url in sorted(normalized):
                result = conn.execute("INSERT OR IGNORE INTO chat_folder_links (admin_id,link) VALUES (?,?)", (uid, url))
                if result.rowcount:
                    new_count += 1
        await update.message.reply_text(
            f"✅ حفظت {new_count} رابط جديد من أصل {len(normalized)} رابط صحيح.\n"
            "يمكنك عرضها عبر زر 📁 روابط المجلدات."
        )
        return ConversationHandler.END

    async def pro_ai_begin(self, update, context):
        await update.callback_query.answer()
        if not self.db.is_admin(update.effective_user.id):
            await update.callback_query.edit_message_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        await update.callback_query.edit_message_text(
            "🧠 اكتب سؤالك للمساعد الذكي.\n\n"
            "يمكنه المساعدة في تحليل الأداء أو صياغة المحتوى وتشخيص الأخطاء، "
            "لكنه لا ينفذ أوامر مباشرة. أرسل /cancel للإلغاء."
        )
        return NEW_AI_MESSAGE

    async def pro_ai_reply(self, update, context):
        if not self.db.is_admin(update.effective_user.id):
            await update.message.reply_text("ليس لديك صلاحية.")
            return ConversationHandler.END
        uid = update.message.from_user.id
        statistics = {
            "accounts": len(self.db.get_accounts(uid)),
            "ads": len(self.db.get_ads(uid)),
            "groups": len(self.db.get_groups(uid)),
            "publishing_active": self.manager.publishing_active,
            "private_reply_active": self.manager.private_reply_active,
            "link_collection_active": self.manager.link_collection_active,
        }
        await update.message.reply_text("🧠 يجري تحليل السؤال...")
        response = await asyncio.to_thread(query_ai_advice, update.message.text or "", statistics)
        for i in range(0, len(response), 3900):
            await update.message.reply_text(response[i:i+3900])
        await update.message.reply_text("للعودة إلى القائمة: /start")
        return ConversationHandler.END

    async def factory_reset_confirm(self, query, context):
        await super().factory_reset_confirm(query, context)
        # The user's reset request explicitly resets all bot settings as well.
        with self._db_conn() as conn:
            for table in ("ai_profile_templates", "chat_folder_links", "ai_pro_preferences", "profile_job_history"):
                conn.execute(f"DELETE FROM {table}")


if __name__ == "__main__":
    print(f"📁 مجلد بيانات هذا البوت: {DATA_DIR}")
    print(f"🗄️ قاعدة بيانات هذا البوت: {DB_NAME}")
    print("🔐 يوصى بحماية مجلد البيانات بصلاحيات خاصة.")
    bot = ProBotHandler()
    bot.run()
