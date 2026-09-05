"""Translated labels for the permission registry (settings/reports spec §49, §50).

The registry in :mod:`utp.domain.permissions` holds **internal keys only**:
``"Payment Type"``, ``"EDIT"``, ``"MANAGE_EXCHANGE_RATE"``. Those keys are stored
in ``role_permissions`` rows, compared in ``effective_permissions`` and written
into audit records, so they must never change when a language changes (§50:
"Internal permission identifiers must never depend on language").

Display text therefore lives here, in a separate table keyed by those identifiers.
The split is what makes the guarantee real: a translator can edit every string in
this file without touching a single stored grant, and a permission can be renamed
in the UI of one language without splitting a role in two.

The registry's own ``label`` field stays as the English fallback so an untranslated
addition degrades to English rather than to a raw key.
"""

from __future__ import annotations

from ..core import i18n
from . import permissions as perms

#: Languages every label must cover. Same set the customer journey supports, so
#: an operator and a guest are never looking at different language coverage.
REQUIRED_LANGUAGES: tuple[str, ...] = i18n.BUILTIN_LANGUAGES


# --------------------------------------------------------------------------- #
# Verbs
# --------------------------------------------------------------------------- #

VERB_LABELS: dict[str, dict[str, str]] = {
    "VIEW": {"en": "View", "th": "ดู", "zh": "查看", "ja": "表示", "ru": "Просмотр"},
    "ADD": {"en": "Add", "th": "เพิ่ม", "zh": "新增", "ja": "追加", "ru": "Добавление"},
    "EDIT": {"en": "Edit", "th": "แก้ไข", "zh": "编辑", "ja": "編集", "ru": "Изменение"},
    "DELETE": {"en": "Delete", "th": "ลบ", "zh": "删除", "ja": "削除", "ru": "Удаление"},
}

#: What DELETE actually performs, per page (§17, §51). The confirmation dialog
#: must say "Disable" when that is what happens, in the operator's own language.
DELETE_SEMANTICS_LABELS: dict[str, dict[str, str]] = {
    "CANCEL": {"en": "Cancel", "th": "ยกเลิก", "zh": "取消", "ja": "キャンセル", "ru": "Отменить"},
    "VOID": {"en": "Void", "th": "ยกเลิกรายการ", "zh": "作废", "ja": "取消処理", "ru": "Аннулировать"},
    "ARCHIVE": {"en": "Archive", "th": "จัดเก็บ", "zh": "归档", "ja": "アーカイブ", "ru": "Архивировать"},
    "DEACTIVATE": {"en": "Deactivate", "th": "ปิดใช้งาน", "zh": "停用", "ja": "無効化", "ru": "Деактивировать"},
    "ANONYMIZE": {"en": "Anonymize", "th": "ลบข้อมูลส่วนบุคคล", "zh": "匿名化", "ja": "匿名化", "ru": "Анонимизировать"},
    "CREDIT_NOTE": {"en": "Issue credit note", "th": "ออกใบลดหนี้", "zh": "开具红字发票", "ja": "クレジットノート発行", "ru": "Выпустить кредит-ноту"},
    "RELEASE": {"en": "Release", "th": "ปล่อยที่นั่ง", "zh": "释放", "ja": "解放", "ru": "Освободить"},
    "DELETE": {"en": "Delete", "th": "ลบ", "zh": "删除", "ja": "削除", "ru": "Удалить"},
    "REVOKE": {"en": "Revoke", "th": "เพิกถอน", "zh": "吊销", "ja": "失効", "ru": "Отозвать"},
}


# --------------------------------------------------------------------------- #
# Settings categories (§11)
# --------------------------------------------------------------------------- #

CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "business": {"en": "Business", "th": "ข้อมูลธุรกิจ", "zh": "企业信息", "ja": "事業情報", "ru": "Бизнес"},
    "booking_ticketing": {
        "en": "Booking & Ticketing",
        "th": "การจองและบัตรเข้าชม",
        "zh": "预订与票务",
        "ja": "予約とチケット",
        "ru": "Бронирование и билеты",
    },
    "pricing_tax": {"en": "Pricing & Tax", "th": "ราคาและภาษี", "zh": "价格与税费", "ja": "料金と税", "ru": "Цены и налоги"},
    "payment": {"en": "Payments", "th": "การชำระเงิน", "zh": "支付方式", "ja": "決済", "ru": "Платежи"},
    "promotions": {"en": "Promotions", "th": "โปรโมชัน", "zh": "促销活动", "ja": "プロモーション", "ru": "Акции"},
    "customer_experience": {
        "en": "Customer Experience",
        "th": "ประสบการณ์ลูกค้า",
        "zh": "客户体验",
        "ja": "顧客体験",
        "ru": "Клиентский опыт",
    },
    "access_control": {
        "en": "Access Control",
        "th": "การควบคุมการเข้าชม",
        "zh": "入场控制",
        "ja": "入場管理",
        "ru": "Контроль доступа",
    },
    "shows_seating": {
        "en": "Shows & Seating",
        "th": "การแสดงและที่นั่ง",
        "zh": "表演与座位",
        "ja": "ショーと座席",
        "ru": "Шоу и места",
    },
    "staff_security": {
        "en": "Staff & Security",
        "th": "พนักงานและความปลอดภัย",
        "zh": "员工与安全",
        "ja": "スタッフとセキュリティ",
        "ru": "Персонал и безопасность",
    },
    "devices": {"en": "Devices", "th": "อุปกรณ์", "zh": "设备", "ja": "デバイス", "ru": "Устройства"},
    "system": {"en": "System", "th": "ระบบ", "zh": "系统", "ja": "システム", "ru": "Система"},
}

CATEGORY_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "business": {
        "en": "Organization, brand, venue, opening hours and time zone.",
        "th": "องค์กร แบรนด์ สถานที่ เวลาเปิดให้บริการ และเขตเวลา",
        "zh": "组织、品牌、场馆、营业时间与时区。",
        "ja": "組織、ブランド、施設、営業時間、タイムゾーン。",
        "ru": "Организация, бренд, объект, часы работы и часовой пояс.",
    },
    "booking_ticketing": {
        "en": "Ticket types, capacity, booking rules and ticket validity.",
        "th": "ประเภทบัตร ความจุ กฎการจอง และอายุการใช้งานบัตร",
        "zh": "票种、容量、预订规则与票券有效期。",
        "ja": "チケット種別、定員、予約ルール、有効期限。",
        "ru": "Типы билетов, вместимость, правила бронирования и срок действия.",
    },
    "pricing_tax": {
        "en": "VAT, service charge, currency, exchange rates and rounding.",
        "th": "ภาษีมูลค่าเพิ่ม ค่าบริการ สกุลเงิน อัตราแลกเปลี่ยน และการปัดเศษ",
        "zh": "增值税、服务费、货币、汇率与取整。",
        "ja": "付加価値税、サービス料、通貨、為替レート、丸め処理。",
        "ru": "НДС, сервисный сбор, валюта, курсы обмена и округление.",
    },
    "payment": {
        "en": "Customer payment options and the providers behind them.",
        "th": "ช่องทางการชำระเงินของลูกค้าและผู้ให้บริการที่เกี่ยวข้อง",
        "zh": "客户支付方式及其背后的服务商。",
        "ja": "顧客の支払方法と、その決済プロバイダー。",
        "ru": "Способы оплаты для клиентов и обслуживающие их провайдеры.",
    },
    "promotions": {
        "en": "Promotion rules, coupons, cash coupons, rewards and partner benefits.",
        "th": "กฎโปรโมชัน คูปอง คูปองเงินสด รางวัลสมาชิก และสิทธิพิเศษพาร์ตเนอร์",
        "zh": "促销规则、优惠券、现金券、会员奖励与合作伙伴权益。",
        "ja": "プロモーション規則、クーポン、金券、リワード、パートナー特典。",
        "ru": "Правила акций, купоны, денежные купоны, бонусы и партнёрские льготы.",
    },
    "customer_experience": {
        "en": "Languages, email and ticket templates, notifications and terms.",
        "th": "ภาษา เทมเพลตอีเมลและบัตร การแจ้งเตือน และข้อกำหนด",
        "zh": "语言、邮件与票券模板、通知与条款。",
        "ja": "言語、メール・チケットのテンプレート、通知、規約。",
        "ru": "Языки, шаблоны писем и билетов, уведомления и условия.",
    },
    "access_control": {
        "en": "Gates, access points, re-entry and scanner behaviour.",
        "th": "ประตูทางเข้า จุดตรวจบัตร การเข้าซ้ำ และการทำงานของเครื่องสแกน",
        "zh": "闸口、检票点、二次入场与扫描设备行为。",
        "ja": "ゲート、入場ポイント、再入場、スキャナーの動作。",
        "ru": "Входы, точки контроля, повторный вход и работа сканеров.",
    },
    "shows_seating": {
        "en": "Show master data, schedule, seat types, zones and layouts.",
        "th": "ข้อมูลการแสดง ตารางรอบ ประเภทที่นั่ง โซน และผังที่นั่ง",
        "zh": "表演主数据、排期、座位类型、区域与座位图。",
        "ja": "ショーのマスタ、スケジュール、座席種別、ゾーン、座席表。",
        "ru": "Справочник шоу, расписание, типы мест, зоны и схемы залов.",
    },
    "staff_security": {
        "en": "Staff accounts, roles, permissions, login security and audit.",
        "th": "บัญชีพนักงาน บทบาท สิทธิ์ ความปลอดภัยการเข้าสู่ระบบ และบันทึกตรวจสอบ",
        "zh": "员工账号、角色、权限、登录安全与审计日志。",
        "ja": "スタッフ アカウント、ロール、権限、ログインセキュリティ、監査ログ。",
        "ru": "Учётные записи сотрудников, роли, права, безопасность входа и аудит.",
    },
    "devices": {
        "en": "Kiosks, counter terminals, printers, gate scanners and their health.",
        "th": "ตู้บริการตนเอง เครื่องขายหน้าเคาน์เตอร์ เครื่องพิมพ์ เครื่องสแกนประตู และสถานะอุปกรณ์",
        "zh": "自助终端、柜台终端、打印机、闸口扫描器及其运行状态。",
        "ja": "キオスク、カウンター端末、プリンター、ゲートスキャナーと稼働状況。",
        "ru": "Киоски, терминалы кассы, принтеры, сканеры на входе и их состояние.",
    },
    "system": {
        "en": "Numbering, integrations, API access, webhooks and advanced switches.",
        "th": "รูปแบบเลขที่เอกสาร การเชื่อมต่อระบบ การเข้าถึง API เว็บฮุก และการตั้งค่าขั้นสูง",
        "zh": "编号规则、系统集成、API 访问、Webhook 与高级设置。",
        "ja": "採番、外部連携、API アクセス、Webhook、詳細設定。",
        "ru": "Нумерация, интеграции, доступ к API, вебхуки и расширенные настройки.",
    },
}


# --------------------------------------------------------------------------- #
# Permission groups (the matrix's collapsible sections, §19)
# --------------------------------------------------------------------------- #

GROUP_LABELS: dict[str, dict[str, str]] = {
    "Administration": {"en": "Administration", "th": "การจัดการระบบ", "zh": "系统管理", "ja": "管理", "ru": "Администрирование"},
    "Access": {"en": "Access", "th": "การเข้าชม", "zh": "入场", "ja": "入場", "ru": "Доступ"},
    "Authorization": {"en": "Authorization", "th": "การอนุมัติ", "zh": "授权审批", "ja": "承認", "ru": "Авторизация"},
    "Business": {"en": "Business", "th": "ข้อมูลธุรกิจ", "zh": "企业信息", "ja": "事業情報", "ru": "Бизнес"},
    "Catalog": {"en": "Catalog", "th": "แคตตาล็อกสินค้า", "zh": "商品目录", "ja": "カタログ", "ru": "Каталог"},
    "Commerce": {"en": "Commerce", "th": "การขายและลูกค้า", "zh": "销售与客户", "ja": "販売", "ru": "Продажи"},
    "Communications": {"en": "Communications", "th": "การสื่อสาร", "zh": "沟通与通知", "ja": "コミュニケーション", "ru": "Коммуникации"},
    "Configuration": {"en": "Configuration", "th": "การตั้งค่าโครงสร้าง", "zh": "基础配置", "ja": "構成設定", "ru": "Конфигурация"},
    "Currency": {"en": "Currency", "th": "สกุลเงิน", "zh": "货币", "ja": "通貨", "ru": "Валюта"},
    "Devices": {"en": "Devices", "th": "อุปกรณ์", "zh": "设备", "ja": "デバイス", "ru": "Устройства"},
    "Finance": {"en": "Finance", "th": "การเงิน", "zh": "财务", "ja": "会計", "ru": "Финансы"},
    "Insights": {"en": "Insights", "th": "รายงานและข้อมูลเชิงลึก", "zh": "分析洞察", "ja": "分析", "ru": "Аналитика"},
    "Money": {"en": "Money", "th": "รายการเงิน", "zh": "资金操作", "ja": "金銭処理", "ru": "Денежные операции"},
    "Operations": {"en": "Operations", "th": "การปฏิบัติงาน", "zh": "运营", "ja": "オペレーション", "ru": "Операции"},
    "Privacy": {"en": "Privacy", "th": "ความเป็นส่วนตัว", "zh": "隐私", "ja": "プライバシー", "ru": "Конфиденциальность"},
    "Promotions": {"en": "Promotions", "th": "โปรโมชัน", "zh": "促销活动", "ja": "プロモーション", "ru": "Акции"},
    "Schedule": {"en": "Schedule", "th": "ตารางรอบการแสดง", "zh": "排期", "ja": "スケジュール", "ru": "Расписание"},
    "Seating": {"en": "Seating", "th": "ที่นั่ง", "zh": "座位", "ja": "座席", "ru": "Места"},
    "System": {"en": "System", "th": "ระบบ", "zh": "系统", "ja": "システム", "ru": "Система"},
    "Tax & Charges": {"en": "Tax & Charges", "th": "ภาษีและค่าบริการ", "zh": "税费与服务费", "ja": "税と手数料", "ru": "Налоги и сборы"},
    "Ticket & Access": {"en": "Ticket & Access", "th": "บัตรและการเข้าชม", "zh": "票务与入场", "ja": "チケットと入場", "ru": "Билеты и доступ"},
}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

PAGE_LABELS: dict[str, dict[str, str]] = {
    "Dashboard": {"en": "Dashboard", "th": "แดชบอร์ดผู้บริหาร", "zh": "管理驾驶舱", "ja": "ダッシュボード", "ru": "Панель руководителя"},
    "Operations Dashboard": {
        "en": "Operations Dashboard",
        "th": "แดชบอร์ดปฏิบัติงาน",
        "zh": "运营看板",
        "ja": "運用ダッシュボード",
        "ru": "Операционная панель",
    },
    "Reports": {"en": "Reports", "th": "รายงาน", "zh": "报表", "ja": "レポート", "ru": "Отчёты"},
    "Bookings": {"en": "Bookings", "th": "การจอง", "zh": "预订记录", "ja": "予約", "ru": "Бронирования"},
    "Counter Sales": {"en": "Counter Sales", "th": "การขายหน้าเคาน์เตอร์", "zh": "柜台销售", "ja": "カウンター販売", "ru": "Продажи на кассе"},
    "Tickets": {"en": "Tickets", "th": "บัตรเข้าชม", "zh": "票券", "ja": "チケット", "ru": "Билеты"},
    "Customers": {"en": "Customers", "th": "ลูกค้า", "zh": "客户", "ja": "顧客", "ru": "Клиенты"},
    "Partners": {"en": "Partners", "th": "พาร์ตเนอร์", "zh": "合作伙伴", "ja": "パートナー", "ru": "Партнёры"},
    "Products": {"en": "Products", "th": "สินค้า", "zh": "商品", "ja": "商品", "ru": "Продукты"},
    "Ticket Types": {"en": "Ticket Types", "th": "ประเภทบัตร", "zh": "票种", "ja": "チケット種別", "ru": "Типы билетов"},
    "Customer Segments": {"en": "Customer Groups", "th": "กลุ่มลูกค้า", "zh": "客户分组", "ja": "顧客グループ", "ru": "Группы клиентов"},
    "Experiences": {"en": "Experiences", "th": "ประสบการณ์เข้าชม", "zh": "体验项目", "ja": "体験", "ru": "Впечатления"},
    "Pricing": {"en": "Pricing", "th": "การตั้งราคา", "zh": "价格设置", "ja": "価格設定", "ru": "Цены"},
    "Promotions": {"en": "Promotions", "th": "โปรโมชัน", "zh": "促销活动", "ja": "プロモーション", "ru": "Акции"},
    "Coupon Codes": {"en": "Coupon Codes", "th": "รหัสคูปอง", "zh": "优惠码", "ja": "クーポンコード", "ru": "Купонные коды"},
    "Cash Coupons": {"en": "Cash Coupons", "th": "คูปองเงินสด", "zh": "现金券", "ja": "金券", "ru": "Денежные купоны"},
    "Member Rewards": {"en": "Member Rewards", "th": "รางวัลสมาชิก", "zh": "会员奖励", "ja": "会員リワード", "ru": "Бонусы участников"},
    "Partner Benefits": {"en": "Partner Benefits", "th": "สิทธิพิเศษพาร์ตเนอร์", "zh": "合作伙伴权益", "ja": "パートナー特典", "ru": "Партнёрские льготы"},
    "Time Slots": {"en": "Time Slots", "th": "ช่วงเวลาเข้าชม", "zh": "时段", "ja": "時間枠", "ru": "Тайм-слоты"},
    "Capacity": {"en": "Capacity", "th": "ความจุ", "zh": "容量", "ja": "定員", "ru": "Вместимость"},
    "Shows": {"en": "Show Master", "th": "ข้อมูลการแสดง", "zh": "表演主数据", "ja": "ショー マスタ", "ru": "Справочник шоу"},
    "Show Schedule": {"en": "Show Schedule", "th": "ตารางรอบการแสดง", "zh": "表演排期", "ja": "ショー スケジュール", "ru": "Расписание шоу"},
    "Venues": {"en": "Venue", "th": "สถานที่", "zh": "场馆", "ja": "施設", "ru": "Объект"},
    "Areas": {"en": "Areas & Locations", "th": "พื้นที่และโซน", "zh": "区域与位置", "ja": "エリアと場所", "ru": "Зоны и локации"},
    "Access Points": {"en": "Access Points", "th": "จุดตรวจบัตร", "zh": "检票点", "ja": "入場ポイント", "ru": "Точки контроля"},
    "Kiosks": {"en": "Kiosks", "th": "ตู้บริการตนเอง", "zh": "自助终端", "ja": "キオスク", "ru": "Киоски"},
    "Devices": {"en": "Device Monitoring", "th": "การติดตามอุปกรณ์", "zh": "设备监控", "ja": "デバイス監視", "ru": "Мониторинг устройств"},
    "Email Templates": {"en": "Email Templates", "th": "เทมเพลตอีเมล", "zh": "邮件模板", "ja": "メール テンプレート", "ru": "Шаблоны писем"},
    "Tax Invoices": {"en": "Tax Invoices", "th": "ใบกำกับภาษี", "zh": "税务发票", "ja": "税務インボイス", "ru": "Налоговые счета"},
    "Staff": {"en": "Staff", "th": "พนักงาน", "zh": "员工", "ja": "スタッフ", "ru": "Сотрудники"},
    "Roles": {"en": "Roles", "th": "บทบาท", "zh": "角色", "ja": "ロール", "ru": "Роли"},
    "Settings": {"en": "Settings", "th": "การตั้งค่า", "zh": "设置", "ja": "設定", "ru": "Настройки"},
    "Audit Logs": {"en": "Audit Logs", "th": "บันทึกการตรวจสอบ", "zh": "审计日志", "ja": "監査ログ", "ru": "Журнал аудита"},
    "VAT Settings": {"en": "VAT", "th": "ภาษีมูลค่าเพิ่ม", "zh": "增值税", "ja": "付加価値税", "ru": "НДС"},
    "Service Charge Settings": {"en": "Service Charge", "th": "ค่าบริการ", "zh": "服务费", "ja": "サービス料", "ru": "Сервисный сбор"},
    "Time Zone Settings": {"en": "Time Zone", "th": "เขตเวลา", "zh": "时区", "ja": "タイムゾーン", "ru": "Часовой пояс"},
    "Ticket Validity Settings": {
        "en": "Ticket Validity",
        "th": "อายุการใช้งานบัตร",
        "zh": "票券有效期",
        "ja": "チケット有効期限",
        "ru": "Срок действия билета",
    },
    "Currency Settings": {"en": "Currency", "th": "สกุลเงิน", "zh": "货币", "ja": "通貨", "ru": "Валюта"},
    "Exchange Rates": {"en": "Exchange Rates", "th": "อัตราแลกเปลี่ยน", "zh": "汇率", "ja": "為替レート", "ru": "Курсы обмена"},
    "Payment Type": {"en": "Payment Types", "th": "ประเภทการชำระเงิน", "zh": "支付方式", "ja": "支払方法", "ru": "Способы оплаты"},
    "Seat Layout": {"en": "Seat Layout", "th": "ผังที่นั่ง", "zh": "座位图", "ja": "座席表", "ru": "Схема зала"},
    "Seat Type": {"en": "Seat Types", "th": "ประเภทที่นั่ง", "zh": "座位类型", "ja": "座席種別", "ru": "Типы мест"},
    "Seat Zone": {"en": "Seat Zones", "th": "โซนที่นั่ง", "zh": "座位区域", "ja": "座席ゾーン", "ru": "Зоны мест"},
    "Seat Reservation": {"en": "Seat Reservations", "th": "การจองที่นั่ง", "zh": "座位预订", "ja": "座席予約", "ru": "Бронирование мест"},
    # --- settings pages (§13) --- #
    "Organization": {"en": "Organization", "th": "องค์กร", "zh": "组织", "ja": "組織", "ru": "Организация"},
    "Brand": {"en": "Brand", "th": "แบรนด์", "zh": "品牌", "ja": "ブランド", "ru": "Бренд"},
    "Operating Hours": {"en": "Operating Hours", "th": "เวลาเปิดให้บริการ", "zh": "营业时间", "ja": "営業時間", "ru": "Часы работы"},
    "Last Admission": {"en": "Last Admission", "th": "เวลาเข้าชมรอบสุดท้าย", "zh": "最后入场时间", "ja": "最終入場時間", "ru": "Последний вход"},
    "Booking Rules": {"en": "Booking Rules", "th": "กฎการจอง", "zh": "预订规则", "ja": "予約ルール", "ru": "Правила бронирования"},
    "Advance Booking": {"en": "Advance Booking", "th": "การจองล่วงหน้า", "zh": "提前预订", "ja": "事前予約", "ru": "Раннее бронирование"},
    "QR Access Rules": {"en": "QR Access Rules", "th": "กฎการเข้าชมด้วย QR", "zh": "二维码入场规则", "ja": "QR 入場ルール", "ru": "Правила доступа по QR"},
    "Rounding": {"en": "Rounding", "th": "การปัดเศษ", "zh": "金额取整", "ja": "丸め処理", "ru": "Округление"},
    "Price Display": {"en": "Price Display", "th": "การแสดงราคา", "zh": "价格显示", "ja": "価格表示", "ru": "Отображение цен"},
    "Payment Providers": {
        "en": "Payment Providers",
        "th": "ผู้ให้บริการชำระเงิน",
        "zh": "支付服务商",
        "ja": "決済プロバイダー",
        "ru": "Платёжные провайдеры",
    },
    "Languages": {"en": "Languages", "th": "ภาษา", "zh": "语言", "ja": "言語", "ru": "Языки"},
    "Ticket Templates": {"en": "Ticket Templates", "th": "เทมเพลตบัตร", "zh": "票券模板", "ja": "チケット テンプレート", "ru": "Шаблоны билетов"},
    "Customer Notifications": {
        "en": "Customer Notifications",
        "th": "การแจ้งเตือนลูกค้า",
        "zh": "客户通知",
        "ja": "顧客通知",
        "ru": "Уведомления клиентам",
    },
    "Terms & Conditions": {
        "en": "Terms & Conditions",
        "th": "ข้อกำหนดและเงื่อนไข",
        "zh": "条款与条件",
        "ja": "利用規約",
        "ru": "Условия и положения",
    },
    "Gates": {"en": "Gates", "th": "ประตูทางเข้า", "zh": "闸口", "ja": "ゲート", "ru": "Входы"},
    "Re-entry Rules": {"en": "Re-entry Rules", "th": "กฎการเข้าซ้ำ", "zh": "二次入场规则", "ja": "再入場ルール", "ru": "Правила повторного входа"},
    "Scanner Configuration": {
        "en": "Scanner Configuration",
        "th": "การตั้งค่าเครื่องสแกน",
        "zh": "扫描设备配置",
        "ja": "スキャナー設定",
        "ru": "Настройка сканеров",
    },
    "Seat Reservation Rules": {
        "en": "Seat Reservation Rules",
        "th": "กฎการจองที่นั่ง",
        "zh": "座位预订规则",
        "ja": "座席予約ルール",
        "ru": "Правила бронирования мест",
    },
    "Permissions": {"en": "Permissions", "th": "สิทธิ์การใช้งาน", "zh": "权限", "ja": "権限", "ru": "Права доступа"},
    "Login Security": {"en": "Login Security", "th": "ความปลอดภัยการเข้าสู่ระบบ", "zh": "登录安全", "ja": "ログイン セキュリティ", "ru": "Безопасность входа"},
    "POS Devices": {"en": "POS Devices", "th": "เครื่องขายหน้าเคาน์เตอร์", "zh": "POS 设备", "ja": "POS 端末", "ru": "POS-терминалы"},
    "Printers": {"en": "Printers", "th": "เครื่องพิมพ์", "zh": "打印机", "ja": "プリンター", "ru": "Принтеры"},
    "Gate Devices": {"en": "Gate Devices", "th": "อุปกรณ์ประตูทางเข้า", "zh": "闸口设备", "ja": "ゲート機器", "ru": "Устройства на входе"},
    "Numbering": {"en": "Numbering", "th": "รูปแบบเลขที่เอกสาร", "zh": "编号规则", "ja": "採番設定", "ru": "Нумерация"},
    "Integrations": {"en": "Integrations", "th": "การเชื่อมต่อระบบภายนอก", "zh": "系统集成", "ja": "外部連携", "ru": "Интеграции"},
    "API Configuration": {"en": "API Configuration", "th": "การตั้งค่า API", "zh": "API 配置", "ja": "API 設定", "ru": "Настройка API"},
    "Webhooks": {"en": "Webhooks", "th": "เว็บฮุก", "zh": "Webhook", "ja": "Webhook", "ru": "Вебхуки"},
    "Advanced Configuration": {
        "en": "Advanced Configuration",
        "th": "การตั้งค่าขั้นสูง",
        "zh": "高级设置",
        "ja": "詳細設定",
        "ru": "Расширенные настройки",
    },
}


# --------------------------------------------------------------------------- #
# Action permissions
# --------------------------------------------------------------------------- #

ACTION_LABELS: dict[str, dict[str, str]] = {
    "APPROVE": {"en": "Approve", "th": "อนุมัติ", "zh": "审批", "ja": "承認", "ru": "Утверждать"},
    "REFUND": {"en": "Refund", "th": "คืนเงิน", "zh": "退款", "ja": "返金", "ru": "Возврат средств"},
    "VOID": {"en": "Void", "th": "ยกเลิกรายการขาย", "zh": "作废交易", "ja": "取消処理", "ru": "Аннулирование"},
    "REPRINT": {"en": "Reprint", "th": "พิมพ์ซ้ำ", "zh": "重新打印", "ja": "再印刷", "ru": "Повторная печать"},
    "EXPORT": {"en": "Export data", "th": "ส่งออกข้อมูล", "zh": "导出数据", "ja": "データ書き出し", "ru": "Экспорт данных"},
    "SCHEDULE_REPORT": {
        "en": "Schedule report delivery",
        "th": "ตั้งเวลาส่งรายงาน",
        "zh": "定时发送报表",
        "ja": "レポート定期配信",
        "ru": "Планирование отчётов",
    },
    "APPLY_MANUAL_DISCOUNT": {
        "en": "Apply manual discount",
        "th": "ให้ส่วนลดด้วยตนเอง",
        "zh": "手动折扣",
        "ja": "手動割引の適用",
        "ru": "Ручная скидка",
    },
    "ISSUE_COMPLIMENTARY": {
        "en": "Issue complimentary ticket",
        "th": "ออกบัตรอนุเคราะห์",
        "zh": "开具赠票",
        "ja": "招待券の発行",
        "ru": "Выдача бесплатного билета",
    },
    "RESCHEDULE": {"en": "Reschedule booking", "th": "เลื่อนวันจอง", "zh": "更改预订日期", "ja": "予約日の変更", "ru": "Перенос бронирования"},
    "CANCEL_BOOKING": {"en": "Cancel booking", "th": "ยกเลิกการจอง", "zh": "取消预订", "ja": "予約のキャンセル", "ru": "Отмена бронирования"},
    "ISSUE_TAX_INVOICE": {"en": "Issue tax invoice", "th": "ออกใบกำกับภาษี", "zh": "开具税务发票", "ja": "税務インボイス発行", "ru": "Выпуск налогового счёта"},
    "CLOSE_SHIFT": {"en": "Close cashier shift", "th": "ปิดกะแคชเชียร์", "zh": "结班", "ja": "レジ締め", "ru": "Закрытие смены"},
    "VIEW_COST": {"en": "View cost and margin", "th": "ดูต้นทุนและกำไร", "zh": "查看成本与毛利", "ja": "原価と利益の表示", "ru": "Просмотр себестоимости"},
    "VIEW_PII": {
        "en": "View unmasked personal data",
        "th": "ดูข้อมูลส่วนบุคคลแบบไม่ปิดบัง",
        "zh": "查看完整个人信息",
        "ja": "個人情報の完全表示",
        "ru": "Просмотр персональных данных",
    },
    "MANAGE_PERMISSION": {
        "en": "Manage roles and permissions",
        "th": "จัดการบทบาทและสิทธิ์",
        "zh": "管理角色与权限",
        "ja": "ロールと権限の管理",
        "ru": "Управление ролями и правами",
    },
    "PUBLISH_PROMOTION": {"en": "Publish a promotion", "th": "เผยแพร่โปรโมชัน", "zh": "发布促销", "ja": "プロモーション公開", "ru": "Публикация акции"},
    "PAUSE_PROMOTION": {
        "en": "Pause or resume a promotion",
        "th": "หยุดหรือเริ่มโปรโมชันต่อ",
        "zh": "暂停或恢复促销",
        "ja": "プロモーションの一時停止・再開",
        "ru": "Приостановка или возобновление акции",
    },
    "OVERRIDE_PROMOTION": {
        "en": "Override a promotion rule",
        "th": "ยกเว้นกฎโปรโมชัน",
        "zh": "覆盖促销规则",
        "ja": "プロモーション規則の上書き",
        "ru": "Переопределение правила акции",
    },
    "MANAGE_PROMOTION_BUDGET": {
        "en": "Manage promotion budget",
        "th": "จัดการงบโปรโมชัน",
        "zh": "管理促销预算",
        "ja": "プロモーション予算の管理",
        "ru": "Управление бюджетом акции",
    },
    "MANAGE_ACCOUNTING_TREATMENT": {
        "en": "Change coupon accounting treatment",
        "th": "เปลี่ยนวิธีบันทึกบัญชีของคูปอง",
        "zh": "更改优惠券会计处理方式",
        "ja": "クーポンの会計処理変更",
        "ru": "Изменение учёта купона",
    },
    "APPLY_PARTNER_DISCOUNT": {
        "en": "Apply a partner discount",
        "th": "ใช้ส่วนลดพาร์ตเนอร์",
        "zh": "使用合作伙伴折扣",
        "ja": "パートナー割引の適用",
        "ru": "Партнёрская скидка",
    },
    "APPLY_COMPLIMENTARY": {
        "en": "Apply a complimentary benefit",
        "th": "ใช้สิทธิอนุเคราะห์",
        "zh": "使用赠送权益",
        "ja": "無償特典の適用",
        "ru": "Применение бесплатной льготы",
    },
    "MANAGE_TAX_SETTINGS": {
        "en": "Manage VAT settings",
        "th": "จัดการการตั้งค่าภาษีมูลค่าเพิ่ม",
        "zh": "管理增值税设置",
        "ja": "付加価値税設定の管理",
        "ru": "Управление настройками НДС",
    },
    "MANAGE_SERVICE_CHARGE": {
        "en": "Manage service charge",
        "th": "จัดการค่าบริการ",
        "zh": "管理服务费",
        "ja": "サービス料の管理",
        "ru": "Управление сервисным сбором",
    },
    "MANAGE_TIMEZONE": {
        "en": "Manage venue time zone",
        "th": "จัดการเขตเวลาของสถานที่",
        "zh": "管理场馆时区",
        "ja": "施設タイムゾーンの管理",
        "ru": "Управление часовым поясом объекта",
    },
    "MANAGE_TICKET_VALIDITY": {
        "en": "Manage ticket validity",
        "th": "จัดการอายุการใช้งานบัตร",
        "zh": "管理票券有效期",
        "ja": "チケット有効期限の管理",
        "ru": "Управление сроком действия билета",
    },
    "MANAGE_CURRENCY": {"en": "Manage currencies", "th": "จัดการสกุลเงิน", "zh": "管理货币", "ja": "通貨の管理", "ru": "Управление валютами"},
    "MANAGE_EXCHANGE_RATE": {
        "en": "Manage exchange rates",
        "th": "จัดการอัตราแลกเปลี่ยน",
        "zh": "管理汇率",
        "ja": "為替レートの管理",
        "ru": "Управление курсами обмена",
    },
    "MANAGE_PAYMENT_TYPE": {
        "en": "Manage payment types",
        "th": "จัดการประเภทการชำระเงิน",
        "zh": "管理支付方式",
        "ja": "支払方法の管理",
        "ru": "Управление способами оплаты",
    },
    "MANAGE_PAYMENT_PROVIDER_CONFIG": {
        "en": "Manage payment provider credentials",
        "th": "จัดการข้อมูลรับรองผู้ให้บริการชำระเงิน",
        "zh": "管理支付服务商凭据",
        "ja": "決済プロバイダー資格情報の管理",
        "ru": "Управление учётными данными платёжного провайдера",
    },
    "OVERRIDE_ACCESS": {
        "en": "Override gate rejection",
        "th": "อนุญาตเข้าชมแม้ระบบปฏิเสธ",
        "zh": "覆盖闸口拒绝",
        "ja": "ゲート拒否の上書き",
        "ru": "Переопределение отказа на входе",
    },
    "PUBLISH_SHOW_SCHEDULE": {
        "en": "Publish show schedule",
        "th": "เผยแพร่ตารางรอบการแสดง",
        "zh": "发布表演排期",
        "ja": "ショー スケジュールの公開",
        "ru": "Публикация расписания шоу",
    },
    "CANCEL_SHOW": {"en": "Cancel show session", "th": "ยกเลิกรอบการแสดง", "zh": "取消表演场次", "ja": "ショー回のキャンセル", "ru": "Отмена сеанса шоу"},
    "CHANGE_SHOW_LOCATION": {
        "en": "Change show location",
        "th": "เปลี่ยนสถานที่การแสดง",
        "zh": "更改表演地点",
        "ja": "ショー会場の変更",
        "ru": "Изменение места шоу",
    },
    "OVERRIDE_CAPACITY": {"en": "Override capacity", "th": "เกินความจุที่กำหนด", "zh": "超出容量放行", "ja": "定員の上書き", "ru": "Превышение вместимости"},
    "BULK_UPDATE_SCHEDULE": {
        "en": "Bulk update schedule",
        "th": "แก้ไขตารางรอบเป็นชุด",
        "zh": "批量更新排期",
        "ja": "スケジュールの一括更新",
        "ru": "Массовое изменение расписания",
    },
    "EXPORT_SHOW_SCHEDULE": {
        "en": "Export show schedule",
        "th": "ส่งออกตารางรอบการแสดง",
        "zh": "导出表演排期",
        "ja": "ショー スケジュールの書き出し",
        "ru": "Экспорт расписания шоу",
    },
    "PUBLISH_SEAT_LAYOUT": {"en": "Publish seat layout", "th": "เผยแพร่ผังที่นั่ง", "zh": "发布座位图", "ja": "座席表の公開", "ru": "Публикация схемы зала"},
    "DUPLICATE_SEAT_LAYOUT": {
        "en": "Duplicate seat layout",
        "th": "ทำสำเนาผังที่นั่ง",
        "zh": "复制座位图",
        "ja": "座席表の複製",
        "ru": "Копирование схемы зала",
    },
    "BLOCK_SEAT": {"en": "Block seat", "th": "ปิดที่นั่ง", "zh": "锁定座位", "ja": "座席のブロック", "ru": "Блокировка места"},
    "UNBLOCK_SEAT": {"en": "Unblock seat", "th": "เปิดที่นั่ง", "zh": "解锁座位", "ja": "座席ブロック解除", "ru": "Разблокировка места"},
    "CHANGE_CUSTOMER_SEAT": {
        "en": "Change customer seat",
        "th": "เปลี่ยนที่นั่งลูกค้า",
        "zh": "更换客户座位",
        "ja": "顧客座席の変更",
        "ru": "Изменение места клиента",
    },
    "OVERRIDE_SEAT_PRICE": {
        "en": "Override seat price",
        "th": "แก้ราคาที่นั่งเป็นกรณีพิเศษ",
        "zh": "覆盖座位价格",
        "ja": "座席価格の上書き",
        "ru": "Переопределение цены места",
    },
    "OVERRIDE_SEAT_ELIGIBILITY": {
        "en": "Override seat eligibility",
        "th": "ยกเว้นเงื่อนไขสิทธิ์ที่นั่ง",
        "zh": "覆盖座位资格限制",
        "ja": "座席利用条件の上書き",
        "ru": "Переопределение условий выбора места",
    },
    "RELEASE_SEAT_HOLD": {"en": "Release seat hold", "th": "ปล่อยที่นั่งที่ถือไว้", "zh": "释放座位占用", "ja": "座席仮押えの解放", "ru": "Освобождение брони места"},
    # --- §18 --- #
    "RESET_ACCESS": {
        "en": "Reset staff access",
        "th": "รีเซ็ตการเข้าถึงของพนักงาน",
        "zh": "重置员工访问权限",
        "ja": "スタッフ アクセスの再設定",
        "ru": "Сброс доступа сотрудника",
    },
    "ASSIGN_ROLE": {"en": "Assign role", "th": "กำหนดบทบาท", "zh": "分配角色", "ja": "ロールの割り当て", "ru": "Назначение роли"},
    "APPROVE_EXCHANGE_RATE": {
        "en": "Approve exchange rate",
        "th": "อนุมัติอัตราแลกเปลี่ยน",
        "zh": "审批汇率",
        "ja": "為替レートの承認",
        "ru": "Утверждение курса обмена",
    },
    "APPROVE_TAX_CHANGE": {
        "en": "Approve tax change",
        "th": "อนุมัติการเปลี่ยนแปลงภาษี",
        "zh": "审批税率变更",
        "ja": "税率変更の承認",
        "ru": "Утверждение изменения налога",
    },
    "MANAGE_LOGIN_SECURITY": {
        "en": "Manage login security",
        "th": "จัดการความปลอดภัยการเข้าสู่ระบบ",
        "zh": "管理登录安全",
        "ja": "ログイン セキュリティの管理",
        "ru": "Управление безопасностью входа",
    },
    "MANAGE_INTEGRATION": {
        "en": "Manage integrations and API credentials",
        "th": "จัดการการเชื่อมต่อระบบและข้อมูลรับรอง API",
        "zh": "管理集成与 API 凭据",
        "ja": "外部連携と API 資格情報の管理",
        "ru": "Управление интеграциями и ключами API",
    },
}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def verb_label(verb: str, language: str) -> str:
    return i18n.text(VERB_LABELS.get(verb), language, fallback=verb)


def delete_semantics_label(semantics: str | None, language: str) -> str | None:
    if not semantics:
        return None
    return i18n.text(DELETE_SEMANTICS_LABELS.get(semantics), language, fallback=semantics)


def page_label(page: str, language: str) -> str:
    """Localized page name, falling back to the registry's English label."""
    definition = perms.PAGES_BY_KEY.get(page)
    fallback = definition.label if definition else page
    return i18n.text(PAGE_LABELS.get(page), language, fallback=fallback)


def action_label(action: str, language: str) -> str:
    definition = perms.ACTIONS_BY_KEY.get(action)
    fallback = definition.label if definition else action
    return i18n.text(ACTION_LABELS.get(action), language, fallback=fallback)


def group_label(group: str, language: str) -> str:
    return i18n.text(GROUP_LABELS.get(group), language, fallback=group)


def category_label(category: str, language: str) -> str:
    definition = perms.SETTINGS_CATEGORIES_BY_KEY.get(category)
    fallback = definition.label if definition else category
    return i18n.text(CATEGORY_LABELS.get(category), language, fallback=fallback)


def category_description(category: str, language: str) -> str:
    definition = perms.SETTINGS_CATEGORIES_BY_KEY.get(category)
    fallback = definition.description if definition else ""
    return i18n.text(CATEGORY_DESCRIPTIONS.get(category), language, fallback=fallback)


def permission_label(key: str, language: str) -> str:
    """Localize any permission key, page or action.

    ``"Payment Type.EDIT"`` becomes "แก้ไข · ประเภทการชำระเงิน" in Thai. The
    composition is deliberately ``verb · page`` rather than a sentence, because a
    natural-language sentence would need per-language word order and this label
    appears in a matrix cell tooltip, not in prose.
    """
    if perms.is_action_key(key):
        return action_label(key[len(perms.ACTION_PREFIX) :], language)
    page, _, verb = key.rpartition(".")
    if not page:
        return key
    return f"{verb_label(verb, language)} · {page_label(page, language)}"


def coverage_gaps() -> dict[str, list[str]]:
    """Identifiers missing a translation, per language.

    Exposed rather than asserted here so a caller can decide what to do: the test
    suite fails on any gap, while the back office can render an "untranslated"
    badge (R69.5) instead of refusing to start.
    """
    gaps: dict[str, list[str]] = {lang: [] for lang in REQUIRED_LANGUAGES}
    checks: tuple[tuple[str, dict[str, dict[str, str]], list[str]], ...] = (
        ("page", PAGE_LABELS, [p.key for p in perms.PAGES]),
        ("action", ACTION_LABELS, [a.key for a in perms.ACTIONS]),
        ("group", GROUP_LABELS, sorted({p.group for p in perms.PAGES} | {a.group for a in perms.ACTIONS})),
        ("category", CATEGORY_LABELS, [c.key for c in perms.SETTINGS_CATEGORIES]),
        ("category_description", CATEGORY_DESCRIPTIONS, [c.key for c in perms.SETTINGS_CATEGORIES]),
        ("verb", VERB_LABELS, list(perms.ALL_VERBS)),
        (
            "delete_semantics",
            DELETE_SEMANTICS_LABELS,
            sorted({p.delete_semantics for p in perms.PAGES if p.delete_semantics}),
        ),
    )
    for kind, table, identifiers in checks:
        for identifier in identifiers:
            entry = table.get(identifier) or {}
            for language in REQUIRED_LANGUAGES:
                if not (entry.get(language) or "").strip():
                    gaps[language].append(f"{kind}:{identifier}")
    return {lang: missing for lang, missing in gaps.items() if missing}


__all__ = [
    "ACTION_LABELS",
    "CATEGORY_DESCRIPTIONS",
    "CATEGORY_LABELS",
    "DELETE_SEMANTICS_LABELS",
    "GROUP_LABELS",
    "PAGE_LABELS",
    "REQUIRED_LANGUAGES",
    "VERB_LABELS",
    "action_label",
    "category_description",
    "category_label",
    "coverage_gaps",
    "delete_semantics_label",
    "group_label",
    "page_label",
    "permission_label",
    "verb_label",
]
