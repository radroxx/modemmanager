"""Таблицы ответов модемов для проб и обслуживания.

Собраны в одном месте, потому что и проба, и цикл сверки поднимают модем целиком:
опознание, инициализацию и первый опрос.
"""

from __future__ import annotations

#: Huawei, отвечающий на всё, что нужно для опознания, запуска и опроса.
HUAWEI: dict[str, object] = {
    "AT": "OK",
    "ATE0": "OK",
    "AT+CMEE=1": "OK",
    "ATI": [
        "Manufacturer: huawei",
        "Model: E3372",
        "Revision: 22.323.01.00.00",
        "IMEI: 861234567890123",
    ],
    "AT+CGMI": "huawei",
    "AT+CGMM": "E3372",
    "AT+CGSN": "861234567890123",
    "AT+CFUN=1": "OK",
    "AT+CLIP=1": "OK",
    "AT+CMGF=0": "OK",
    "AT+CNMI=2,1,0,0,0": "OK",
    "AT+CREG=1": "OK",
    "AT+CGREG=1": "OK",
    "AT^CURC=1": "OK",
    # Регулярный опрос.
    "AT+CSQ": "+CSQ: 17,99",
    "AT+CREG?": '+CREG: 1,1,"2B1A","1F2C3D"',
    "AT+CGREG?": "+CGREG: 1,1",
    "AT+COPS=3,2": "OK",
    "AT+COPS=3,0": "OK",
    "AT+COPS?": '+COPS: 0,2,"25002",7',
    "AT+CPMS?": '+CPMS: "SM",3,20,"SM",3,20,"SM",3,20',
}

#: Модем, отвечающий на AT, но не принадлежащий ни одному известному семейству.
UNKNOWN_MODEM: dict[str, object] = dict(
    HUAWEI,
    ATI=["Quectel", "EC25EFAR06A11M4G"],
    **{"AT+CGMI": "Quectel", "AT+CGMM": "EC25"},
)

#: Порт отвечает на опознание, но уведомления на нём не включаются.
NO_NOTIFICATIONS: dict[str, object] = dict(HUAWEI, **{"AT+CMGF=0": "ERROR"})

#: Второй модем -- отличается IMEI, чтобы события можно было различить.
HUAWEI_SECOND: dict[str, object] = dict(
    HUAWEI,
    ATI=[
        "Manufacturer: huawei",
        "Model: E3372",
        "Revision: 22.323.01.00.00",
        "IMEI: 861234567890999",
    ],
    **{"AT+CGSN": "861234567890999"},
)
