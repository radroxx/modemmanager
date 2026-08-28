"""Кодировка GSM 7-бит и её распаковка.

Формат хранит септеты плотно упакованными в октеты, младший бит первого септета
на месте младшего бита первого октета. После служебного заголовка PDU обычно
идёт от нуля до шести бит выравнивания, чтобы первый септет полезной нагрузки
начинался на границе септета -- иначе весь текст сдвинулся бы.
"""

from __future__ import annotations

#: Основная таблица GSM 03.38. Позиция 0x1B зарезервирована под "escape" --
#: следующий септет ищется в ``EXTENSION_TABLE``, а не в основной таблице.
GSM7_TABLE: tuple[str, ...] = (
    # 0x00..0x0F
    "@", "£", "$", "¥", "è", "é", "ù", "ì", "ò", "Ç", "\n", "Ø", "ø", "\r", "Å", "å",
    # 0x10..0x1F
    "Δ", "_", "Φ", "Γ", "Λ", "Ω", "Π", "Ψ", "Σ", "Θ", "Ξ", "\x1b", "Æ", "æ", "ß", "É",
    # 0x20..0x2F
    " ", "!", "\"", "#", "¤", "%", "&", "'", "(", ")", "*", "+", ",", "-", ".", "/",
    # 0x30..0x3F
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", ":", ";", "<", "=", ">", "?",
    # 0x40..0x4F
    "¡", "A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O",
    # 0x50..0x5F
    "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "Ä", "Ö", "Ñ", "Ü", "§",
    # 0x60..0x6F
    "¿", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m", "n", "o",
    # 0x70..0x7F
    "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z", "ä", "ö", "ñ", "ü", "à",
)


#: Таблица расширения GSM 03.38. Пары ``ESC + <код>``; отсутствующие коды
#: заменяются на пробел.
EXTENSION_TABLE: dict[int, str] = {
    0x0A: "\x0c",  # form feed
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}


#: Символ ``0x1B`` в основной таблице обозначает переход к расширению.
ESCAPE = 0x1B


# --------------------------------------------------------------- обратные таблицы

_ENCODE: dict[str, int] = {char: index for index, char in enumerate(GSM7_TABLE) if char != "\x1b"}
_ENCODE_EXT: dict[str, int] = {char: code for code, char in EXTENSION_TABLE.items()}


# --------------------------------------------------------------- распаковка

def unpack_septets(data: bytes, count: int, skip_bits: int = 0) -> bytes:
    """Извлекает ``count`` септетов из плотно упакованного потока.

    ``skip_bits`` -- сколько битов пропустить перед первым септетом; это нужно,
    когда полезная нагрузка идёт после служебного заголовка с ненулевым
    выравниванием.

    Возвращает объект ``bytes``, в каждом байте лежит семибитное значение.
    """
    if count < 0:
        raise ValueError("count не может быть отрицательным")
    if skip_bits < 0:
        raise ValueError("skip_bits не может быть отрицательным")

    result = bytearray(count)
    bit_position = skip_bits
    for index in range(count):
        byte_index = bit_position // 8
        bit_offset = bit_position % 8
        if byte_index >= len(data):
            # Не хватает данных: остальные септеты остаются нулями. Это лучше,
            # чем поднять исключение: пусть текст будет короче, а не потеряется
            # целиком из-за одного повреждённого октета.
            break
        low = (data[byte_index] >> bit_offset) & 0x7F
        remaining = 7 - (8 - bit_offset)
        if remaining > 0 and byte_index + 1 < len(data):
            high = data[byte_index + 1] & ((1 << remaining) - 1)
            low |= high << (7 - remaining)
        result[index] = low & 0x7F
        bit_position += 7
    return bytes(result)


def pack_septets(septets: bytes, skip_bits: int = 0) -> bytes:
    """Обратная операция для тестов: собирает поток из септетов.

    В приложении не используется -- отправка сообщений не реализуется, -- но
    без обратной пары сборку и разбор трудно проверить одновременно.
    """
    if skip_bits < 0:
        raise ValueError("skip_bits не может быть отрицательным")
    total_bits = skip_bits + 7 * len(septets)
    byte_count = (total_bits + 7) // 8
    out = bytearray(byte_count)
    bit_position = skip_bits
    for septet in septets:
        if septet & ~0x7F:
            raise ValueError("септет должен помещаться в 7 бит")
        byte_index = bit_position // 8
        bit_offset = bit_position % 8
        out[byte_index] |= (septet << bit_offset) & 0xFF
        remaining = 7 - (8 - bit_offset)
        if remaining > 0:
            out[byte_index + 1] |= (septet >> (7 - remaining)) & ((1 << remaining) - 1)
        bit_position += 7
    return bytes(out)


# --------------------------------------------------------------- декодирование

def decode(septets: bytes) -> str:
    """Переводит поток септетов в строку с учётом таблицы расширения."""
    out: list[str] = []
    escape = False
    for value in septets:
        code = value & 0x7F
        if escape:
            out.append(EXTENSION_TABLE.get(code, GSM7_TABLE[code]))
            escape = False
            continue
        if code == ESCAPE:
            escape = True
            continue
        out.append(GSM7_TABLE[code])
    if escape:
        # Висячий ESC ничего не значит; в стриме, обрезанном по границе части,
        # такое не должно происходить -- сборка ведётся до декодирования.
        out.append(GSM7_TABLE[ESCAPE])
    return "".join(out)


def encode(text: str) -> bytes:
    """Обратно кодирует строку в поток септетов. Только для тестов сборки."""
    out = bytearray()
    for char in text:
        code = _ENCODE.get(char)
        if code is not None:
            out.append(code)
            continue
        ext = _ENCODE_EXT.get(char)
        if ext is not None:
            out.append(ESCAPE)
            out.append(ext)
            continue
        raise ValueError(f"символ {char!r} не входит в GSM7")
    return bytes(out)


def fill_bits_after_udh(udh_length: int) -> int:
    """Сколько битов пропустить между служебным заголовком и первым септетом.

    ``udh_length`` -- значение поля ``UDHL`` (без учёта самого байта UDHL);
    полная длина заголовка в битах равна ``(udh_length + 1) * 8``. Первый
    септет должен начинаться на кратной семи границе битов.
    """
    total_bits = (udh_length + 1) * 8
    return (7 - total_bits % 7) % 7


def septets_for_udh(udh_length: int) -> int:
    """Сколько септетов TP-UDL уходит на служебный заголовок с выравниванием."""
    total_bits = (udh_length + 1) * 8
    fill = fill_bits_after_udh(udh_length)
    return (total_bits + fill) // 7
