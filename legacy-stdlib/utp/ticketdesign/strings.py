"""Translated labels for both ticket templates.

One template, translated strings — not one template per language
(ticketDesign.md). The QR payload never varies with language, so a guest can
switch language freely and the credential at the gate is unchanged.

These live beside the templates rather than in ``core.i18n.MESSAGES`` because
they are presentation copy for one artefact, not platform-wide system messages,
and keeping them here means a venue's ticket wording can be reviewed in one
place. Lookup falls back to English so a missing translation degrades to a
readable ticket rather than a blank label (R69.5).
"""

from __future__ import annotations

TICKET_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "confirmed_title": "Your booking is confirmed",
        "confirmed_subtitle": "Thank you for choosing us. We look forward to welcoming you!",
        "admission_ticket": "ADMISSION TICKET",
        "booking_number": "Booking Number",
        "visit_date": "Visit Date",
        "valid_time": "Valid Time",
        "last_admission": "Last admission",
        "customer_name": "Customer Name",
        "customer": "Customer",
        "ticket_number": "Ticket Number",
        "entry_location": "Entrance",
        "ticket_details": "Ticket Details",
        "ticket_type": "Ticket Type",
        "quantity": "Quantity",
        "entrance_access": "Entrance Access",
        "scan_at_entrance": "SCAN AT ENTRANCE",
        "scan_helper": "Present this QR code at the entrance. Valid according to your ticket access policy.",
        "entrance_note": "Present this QR code at the entrance. Keep your ticket ready before reaching the access gate.",
        "subtotal": "Subtotal",
        "discount": "Discount",
        "service_charge": "Service charge",
        "vat": "VAT",
        "total": "TOTAL",
        "included": "included",
        "need_help": "Need help?",
        "closing_message": "We can't wait to welcome you!",
        "thank_you": "Thank you for your visit.",
        "conditions": "Conditions",
        "qr_alt": "Entrance access QR code",
        "print_eticket": "Print e-ticket",
        "print_thermal": "Print gate ticket (80mm)",
        "ticket_of": "Ticket {index} of {total}",
        "entries": "Entries",
    },
    "th": {
        "confirmed_title": "การจองของคุณได้รับการยืนยันแล้ว",
        "confirmed_subtitle": "ขอบคุณที่เลือกเรา เราพร้อมต้อนรับคุณ",
        "admission_ticket": "บัตรเข้าชม",
        "booking_number": "หมายเลขการจอง",
        "visit_date": "วันที่เข้าชม",
        "valid_time": "ระยะเวลาที่ใช้ได้",
        "last_admission": "เข้าชมได้ถึง",
        "customer_name": "ชื่อผู้จอง",
        "customer": "ผู้จอง",
        "ticket_number": "หมายเลขบัตร",
        "entry_location": "ทางเข้า",
        "ticket_details": "รายละเอียดบัตร",
        "ticket_type": "ประเภทบัตร",
        "quantity": "จำนวน",
        "entrance_access": "การเข้าชม",
        "scan_at_entrance": "สแกนที่ทางเข้า",
        "scan_helper": "แสดง QR code นี้ที่ทางเข้า ใช้ได้ตามเงื่อนไขของบัตรที่คุณซื้อ",
        "entrance_note": "แสดง QR code นี้ที่ทางเข้า กรุณาเตรียมบัตรให้พร้อมก่อนถึงประตูตรวจ",
        "subtotal": "ยอดรวมย่อย",
        "discount": "ส่วนลด",
        "service_charge": "ค่าบริการ",
        "vat": "ภาษีมูลค่าเพิ่ม",
        "total": "ยอดรวมทั้งสิ้น",
        "included": "รวมแล้ว",
        "need_help": "ต้องการความช่วยเหลือ?",
        "closing_message": "เราพร้อมต้อนรับคุณแล้ว",
        "thank_you": "ขอบคุณที่มาเยี่ยมชม",
        "conditions": "เงื่อนไข",
        "qr_alt": "QR code สำหรับเข้าชม",
        "print_eticket": "พิมพ์บัตรอิเล็กทรอนิกส์",
        "print_thermal": "พิมพ์บัตรเข้าชม (80 มม.)",
        "ticket_of": "บัตรที่ {index} จาก {total}",
        "entries": "จำนวนครั้งที่เข้า",
    },
    "zh": {
        "confirmed_title": "您的预订已确认",
        "confirmed_subtitle": "感谢您的选择，我们期待您的到访！",
        "admission_ticket": "入场门票",
        "booking_number": "订单号",
        "visit_date": "参观日期",
        "valid_time": "有效时间",
        "last_admission": "最后入场",
        "customer_name": "客户姓名",
        "customer": "客户",
        "ticket_number": "门票号",
        "entry_location": "入口",
        "ticket_details": "门票详情",
        "ticket_type": "门票类型",
        "quantity": "数量",
        "entrance_access": "入场凭证",
        "scan_at_entrance": "入口处扫码",
        "scan_helper": "请在入口处出示此二维码。有效性依照您的门票使用规则。",
        "entrance_note": "请在入口处出示此二维码。到达闸口前请提前准备好门票。",
        "subtotal": "小计",
        "discount": "优惠",
        "service_charge": "服务费",
        "vat": "增值税",
        "total": "总计",
        "included": "已含",
        "need_help": "需要帮助？",
        "closing_message": "期待与您相见！",
        "thank_you": "感谢您的光临。",
        "conditions": "使用条款",
        "qr_alt": "入场二维码",
        "print_eticket": "打印电子门票",
        "print_thermal": "打印入场票（80毫米）",
        "ticket_of": "第 {index} 张，共 {total} 张",
        "entries": "入场次数",
    },
    "ja": {
        "confirmed_title": "ご予約が確定しました",
        "confirmed_subtitle": "お選びいただきありがとうございます。ご来場をお待ちしております。",
        "admission_ticket": "入場チケット",
        "booking_number": "予約番号",
        "visit_date": "ご来場日",
        "valid_time": "有効時間",
        "last_admission": "最終入場",
        "customer_name": "お客様名",
        "customer": "お客様",
        "ticket_number": "チケット番号",
        "entry_location": "入場口",
        "ticket_details": "チケット詳細",
        "ticket_type": "チケット種別",
        "quantity": "枚数",
        "entrance_access": "入場用コード",
        "scan_at_entrance": "入場口でスキャン",
        "scan_helper": "入場口でこのQRコードをご提示ください。有効期限はチケットの規定に従います。",
        "entrance_note": "入場口でこのQRコードをご提示ください。ゲートに到着する前にご準備ください。",
        "subtotal": "小計",
        "discount": "割引",
        "service_charge": "サービス料",
        "vat": "消費税",
        "total": "合計",
        "included": "込み",
        "need_help": "お困りですか？",
        "closing_message": "ご来場を心よりお待ちしております。",
        "thank_you": "ご来場ありがとうございます。",
        "conditions": "ご利用条件",
        "qr_alt": "入場用QRコード",
        "print_eticket": "Eチケットを印刷",
        "print_thermal": "入場券を印刷（80mm）",
        "ticket_of": "{total}枚中{index}枚目",
        "entries": "入場回数",
    },
    "ru": {
        "confirmed_title": "Ваше бронирование подтверждено",
        "confirmed_subtitle": "Спасибо, что выбрали нас. Будем рады вас видеть!",
        "admission_ticket": "ВХОДНОЙ БИЛЕТ",
        "booking_number": "Номер брони",
        "visit_date": "Дата посещения",
        "valid_time": "Время действия",
        "last_admission": "Последний вход",
        "customer_name": "Имя гостя",
        "customer": "Гость",
        "ticket_number": "Номер билета",
        "entry_location": "Вход",
        "ticket_details": "Детали билета",
        "ticket_type": "Тип билета",
        "quantity": "Количество",
        "entrance_access": "Доступ на вход",
        "scan_at_entrance": "СКАНИРУЙТЕ НА ВХОДЕ",
        "scan_helper": "Предъявите этот QR-код на входе. Действует согласно условиям вашего билета.",
        "entrance_note": "Предъявите этот QR-код на входе. Подготовьте билет заранее.",
        "subtotal": "Промежуточный итог",
        "discount": "Скидка",
        "service_charge": "Сервисный сбор",
        "vat": "НДС",
        "total": "ИТОГО",
        "included": "включено",
        "need_help": "Нужна помощь?",
        "closing_message": "Будем рады встрече!",
        "thank_you": "Спасибо за визит.",
        "conditions": "Условия",
        "qr_alt": "QR-код для входа",
        "print_eticket": "Печать электронного билета",
        "print_thermal": "Печать билета (80 мм)",
        "ticket_of": "Билет {index} из {total}",
        "entries": "Входов",
    },
}

DEFAULT_LANGUAGE = "en"


def ticket_text(key: str, language: str | None = None, **params: object) -> str:
    """Resolve one ticket label, falling back to English then to the key."""
    lang = language or DEFAULT_LANGUAGE
    table = TICKET_STRINGS.get(lang) or {}
    value = table.get(key) or TICKET_STRINGS[DEFAULT_LANGUAGE].get(key) or key
    for name, replacement in params.items():
        value = value.replace("{" + name + "}", str(replacement))
    return value


def translator(language: str | None = None):
    """Bind a language once so templates read as ``t("booking_number")``."""

    def _t(key: str, **params: object) -> str:
        return ticket_text(key, language, **params)

    return _t
