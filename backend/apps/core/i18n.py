"""Localization.

Translatable content is stored as a per-language map on a single record, so one
Show/Product/SeatType row serves every language (R69.3). ``tr`` resolves a map
against a requested language and records nothing implicitly: when a language is
missing it falls back to the configured default *and reports that it did*, which
is how the back office can show operators exactly which content is untranslated
(R69.5).

System messages live in :data:`MESSAGES` keyed by the ``message_key`` on each
domain error, so no user-facing string is assembled by concatenating translated
fragments (R66.7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_LANGUAGE = "en"

#: Languages shipped with the platform. More are added as configuration (R69.2).
#: The customer booking journey supports these five (update spec §1); additional
#: languages need only translation data, not code changes.
BUILTIN_LANGUAGES: tuple[str, ...] = ("en", "th", "zh", "ja", "ru")

#: Display metadata for the customer language selector (update spec §1). The name is
#: shown in its own script alongside a visual indicator — never a flag alone.
LANGUAGE_DISPLAY: tuple[dict[str, str], ...] = (
    {"code": "en", "name": "English", "indicator": "🇬🇧"},
    {"code": "th", "name": "ไทย", "indicator": "🇹🇭"},
    {"code": "zh", "name": "中文", "indicator": "🇨🇳"},
    {"code": "ja", "name": "日本語", "indicator": "🇯🇵"},
    {"code": "ru", "name": "Русский", "indicator": "🇷🇺"},
)


@dataclass(frozen=True, slots=True)
class Translated:
    """A resolved translatable value plus whether a fallback was used."""

    value: str
    language: str
    fell_back: bool

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


def as_map(value: Any, *, language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Normalize a translatable field into a language map.

    Accepts an existing map, a bare string (assigned to ``language``) or ``None``.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    return {language: str(value)}


def tr(
    value: Any,
    language: str,
    *,
    default_language: str = DEFAULT_LANGUAGE,
    fallback: str = "",
) -> Translated:
    """Resolve a translatable value for ``language`` (R69.5)."""
    mapping = as_map(value, language=default_language)
    if not mapping:
        return Translated(fallback, default_language, True)
    if language in mapping and mapping[language].strip():
        return Translated(mapping[language], language, False)
    base = language.split("-")[0]
    if base in mapping and mapping[base].strip():
        return Translated(mapping[base], base, language != base)
    if default_language in mapping and mapping[default_language].strip():
        return Translated(mapping[default_language], default_language, True)
    first_lang, first_value = next(iter(mapping.items()))
    return Translated(first_value, first_lang, True)


def text(value: Any, language: str, *, default_language: str = DEFAULT_LANGUAGE, fallback: str = "") -> str:
    """``tr`` when only the string is needed."""
    return tr(value, language, default_language=default_language, fallback=fallback).value


def untranslated_languages(value: Any, required: tuple[str, ...] = BUILTIN_LANGUAGES) -> list[str]:
    """Languages for which this field has no content — surfaced in the back office."""
    mapping = as_map(value)
    return [lang for lang in required if not mapping.get(lang, "").strip()]


#: User-facing system messages. Keys match ``PlatformError.message_key``.
MESSAGES: dict[str, dict[str, str]] = {
    "error.generic": {
        "en": "Something went wrong. Please try again.",
        "th": "เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้ง",
    },
    "error.authentication_required": {
        "en": "Please sign in to continue.",
        "th": "กรุณาเข้าสู่ระบบเพื่อดำเนินการต่อ",
    },
    "error.authorization_denied": {
        "en": "You do not have permission to perform this action.",
        "th": "คุณไม่มีสิทธิ์ดำเนินการนี้",
    },
    "error.not_found": {
        "en": "We could not find what you were looking for.",
        "th": "ไม่พบข้อมูลที่คุณต้องการ",
    },
    "error.rate_limited": {
        "en": "Too many attempts. Please wait a moment and try again.",
        "th": "มีการพยายามมากเกินไป กรุณารอสักครู่แล้วลองใหม่",
    },
    "error.validation_failed": {
        "en": "Please check the highlighted fields and try again.",
        "th": "กรุณาตรวจสอบข้อมูลที่ทำเครื่องหมายไว้",
    },
    "error.rule_violation": {
        "en": "That option is not available.",
        "th": "ไม่สามารถเลือกตัวเลือกนี้ได้",
    },
    "error.not_available": {
        "en": "That is not available for the date you selected.",
        "th": "ไม่พร้อมให้บริการในวันที่คุณเลือก",
    },
    "error.just_sold_out": {
        "en": "That has just sold out. Please choose another date or time.",
        "th": "รอบนี้เต็มแล้ว กรุณาเลือกวันหรือเวลาอื่น",
    },
    "error.seat_just_taken": {
        "en": "That seat has just been taken. Please choose another one.",
        "th": "ที่นั่งนี้ถูกจองแล้ว กรุณาเลือกที่นั่งอื่น",
    },
    "error.hold_expired": {
        "en": "Your reservation time ran out. Please confirm your choices again.",
        "th": "เวลาถือที่นั่งหมดลง กรุณายืนยันรายการของคุณอีกครั้ง",
    },
    "error.consent_required": {
        "en": (
            "We cannot continue until you accept the processing needed to create your "
            "booking, issue your tickets and take payment."
        ),
        "th": "ไม่สามารถดำเนินการต่อได้ จนกว่าคุณจะยอมรับการประมวลผลข้อมูลที่จำเป็นสำหรับการจอง การออกบัตร และการชำระเงิน",
    },
    "error.conflict": {
        "en": "That change conflicts with the current state. Please review and retry.",
        "th": "การเปลี่ยนแปลงขัดแย้งกับสถานะปัจจุบัน กรุณาตรวจสอบและลองใหม่",
    },
    "error.confirmation_required": {
        "en": "Please confirm this action before it can be applied.",
        "th": "กรุณายืนยันการดำเนินการนี้",
    },
    "error.immutable_record": {
        "en": "This record is kept for audit and cannot be deleted.",
        "th": "รายการนี้ถูกเก็บไว้เพื่อการตรวจสอบและไม่สามารถลบได้",
    },
    "error.payment_failed": {
        "en": "The payment could not be completed. Please try another method.",
        "th": "การชำระเงินไม่สำเร็จ กรุณาลองวิธีอื่น",
    },
    "error.configuration_error": {
        "en": "This configuration is incomplete. Please review the highlighted items.",
        "th": "การตั้งค่ายังไม่สมบูรณ์ กรุณาตรวจสอบรายการที่ทำเครื่องหมายไว้",
    },
    "success.booking_confirmed": {
        "en": "Booking confirmed successfully.",
        "th": "ยืนยันการจองเรียบร้อยแล้ว",
    },
    "calendar.state.AVAILABLE": {"en": "Available", "th": "ว่าง", "zh": "可预订", "ja": "予約可能", "ru": "Доступно"},
    "calendar.state.LIMITED": {"en": "Limited availability", "th": "เหลือน้อย", "zh": "名额有限", "ja": "残りわずか", "ru": "Мало мест"},
    "calendar.state.SOLD_OUT": {"en": "Sold out", "th": "เต็ม", "zh": "已售罄", "ja": "完売", "ru": "Распродано"},
    "calendar.state.CLOSED": {"en": "Closed", "th": "ปิดให้บริการ", "zh": "休息", "ja": "休館", "ru": "Закрыто"},
    "calendar.state.BLACKOUT": {"en": "Not available", "th": "งดให้บริการ", "zh": "不可预订", "ja": "利用不可", "ru": "Недоступно"},
    "calendar.state.NOT_YET_ON_SALE": {"en": "Not yet on sale", "th": "ยังไม่เปิดจำหน่าย", "zh": "尚未开售", "ja": "販売前", "ru": "Продажи ещё не открыты"},
    "calendar.state.PAST": {"en": "Past date", "th": "วันที่ผ่านมาแล้ว", "zh": "已过日期", "ja": "過去の日付", "ru": "Прошедшая дата"},
    "calendar.state.TODAY": {"en": "Today", "th": "วันนี้", "zh": "今天", "ja": "本日", "ru": "Сегодня"},
    "calendar.state.SELECTED": {"en": "Selected", "th": "เลือกไว้", "zh": "已选择", "ja": "選択済み", "ru": "Выбрано"},
    # Last admission has passed for today (update spec §5, §42). Customer-friendly,
    # no technical wording, in all five supported languages.
    "calendar.last_admission_passed": {
        "en": "Online booking for today is no longer available because the last admission time has passed. Please select another date.",
        "th": "ไม่สามารถจองสำหรับวันนี้ได้ เนื่องจากเลยเวลาเข้าชมรอบสุดท้ายแล้ว กรุณาเลือกวันอื่น",
        "zh": "由于今日最后入场时间已过，无法在线预订今天的门票，请选择其他日期。",
        "ja": "本日の最終入場時間を過ぎたため、本日分のオンライン予約はできません。別の日付をお選びください。",
        "ru": "Онлайн-бронирование на сегодня недоступно, так как время последнего входа уже прошло. Пожалуйста, выберите другую дату.",
    },
    "calendar.last_admission_short": {
        "en": "Last admission has passed",
        "th": "เลยเวลาเข้าชมรอบสุดท้ายแล้ว",
        "zh": "最后入场时间已过",
        "ja": "最終入場時間を過ぎました",
        "ru": "Время последнего входа прошло",
    },
}


def message(key: str, language: str = DEFAULT_LANGUAGE, *, fallback: str = "") -> str:
    """Resolve a system message key for a language (R66.7)."""
    entry = MESSAGES.get(key)
    if not entry:
        return fallback or key
    return tr(entry, language, fallback=fallback or key).value


def localize_error(error: Any, language: str) -> dict[str, Any]:
    """Localize a :class:`~utp.core.errors.PlatformError` payload for a channel.

    A raise site may pass a specific, actionable message (for example "Your selection
    expired. Please choose your tickets again.") that is more useful than the generic
    text behind ``message_key``. Such a message must survive localization: replacing it
    with the class default would hide *why* the request failed (R66.3). So we localize
    via ``message_key`` only when the error is carrying its class default message; a
    caller-supplied message is preserved as written.
    """
    payload = error.public_dict()
    default = getattr(error, "default_message", None)
    has_custom_message = bool(getattr(error, "message", None)) and error.message != default
    if not has_custom_message:
        payload["error"]["message"] = message(
            error.message_key, language, fallback=payload["error"]["message"]
        )
    return payload


__all__ = [
    "BUILTIN_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "LANGUAGE_DISPLAY",
    "MESSAGES",
    "Translated",
    "as_map",
    "localize_error",
    "message",
    "text",
    "tr",
    "untranslated_languages",
]
