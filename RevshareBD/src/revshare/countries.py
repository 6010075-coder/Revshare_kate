COUNTRY_NAMES = [
    ("Albania", "Албания", "Албанія"),
    ("Algeria", "Алжир", "Алжир"),
    ("Andorra", "Андорра", "Андорра"),
    ("Angola", "Ангола", "Ангола"),
    ("Argentina", "Аргентина", "Аргентина"),
    ("Armenia", "Армения", "Вірменія"),
    ("Australia", "Австралия", "Австралія"),
    ("Austria", "Австрия", "Австрія"),
    ("Azerbaijan", "Азербайджан", "Азербайджан"),
    ("Bahrain", "Бахрейн", "Бахрейн"),
    ("Bangladesh", "Бангладеш", "Бангладеш"),
    ("Belarus", "Беларусь", "Білорусь"),
    ("Belgium", "Бельгия", "Бельгія"),
    ("Bolivia", "Боливия", "Болівія"),
    ("Bosnia and Herzegovina", "Босния и Герцеговина", "Боснія і Герцеговина"),
    ("Brazil", "Бразилия", "Бразилія"),
    ("Bulgaria", "Болгария", "Болгарія"),
    ("Canada", "Канада", "Канада"),
    ("Chile", "Чили", "Чилі"),
    ("China", "Китай", "Китай"),
    ("Colombia", "Колумбия", "Колумбія"),
    ("Costa Rica", "Коста-Рика", "Коста-Рика"),
    ("Croatia", "Хорватия", "Хорватія"),
    ("Cyprus", "Кипр", "Кіпр"),
    ("Czechia", "Чехия", "Чехія"),
    ("Denmark", "Дания", "Данія"),
    ("Dominican Republic", "Доминиканская Республика", "Домініканська Республіка"),
    ("Ecuador", "Эквадор", "Еквадор"),
    ("Egypt", "Египет", "Єгипет"),
    ("Estonia", "Эстония", "Естонія"),
    ("Finland", "Финляндия", "Фінляндія"),
    ("France", "Франция", "Франція"),
    ("Georgia", "Грузия", "Грузія"),
    ("Germany", "Германия", "Німеччина"),
    ("Greece", "Греция", "Греція"),
    ("Hong Kong", "Гонконг", "Гонконг"),
    ("Hungary", "Венгрия", "Угорщина"),
    ("Iceland", "Исландия", "Ісландія"),
    ("India", "Индия", "Індія"),
    ("Indonesia", "Индонезия", "Індонезія"),
    ("Ireland", "Ирландия", "Ірландія"),
    ("Israel", "Израиль", "Ізраїль"),
    ("Italy", "Италия", "Італія"),
    ("Japan", "Япония", "Японія"),
    ("Jordan", "Иордания", "Йорданія"),
    ("Kazakhstan", "Казахстан", "Казахстан"),
    ("Kenya", "Кения", "Кенія"),
    ("Kuwait", "Кувейт", "Кувейт"),
    ("Kyrgyzstan", "Кыргызстан", "Киргизстан"),
    ("Latvia", "Латвия", "Латвія"),
    ("Lebanon", "Ливан", "Ліван"),
    ("Lithuania", "Литва", "Литва"),
    ("Luxembourg", "Люксембург", "Люксембург"),
    ("Malaysia", "Малайзия", "Малайзія"),
    ("Malta", "Мальта", "Мальта"),
    ("Mexico", "Мексика", "Мексика"),
    ("Moldova", "Молдова", "Молдова"),
    ("Monaco", "Монако", "Монако"),
    ("Montenegro", "Черногория", "Чорногорія"),
    ("Morocco", "Марокко", "Марокко"),
    ("Netherlands", "Нидерланды", "Нідерланди"),
    ("New Zealand", "Новая Зеландия", "Нова Зеландія"),
    ("Nigeria", "Нигерия", "Нігерія"),
    ("North Macedonia", "Северная Македония", "Північна Македонія"),
    ("Norway", "Норвегия", "Норвегія"),
    ("Oman", "Оман", "Оман"),
    ("Pakistan", "Пакистан", "Пакистан"),
    ("Panama", "Панама", "Панама"),
    ("Peru", "Перу", "Перу"),
    ("Philippines", "Филиппины", "Філіппіни"),
    ("Poland", "Польша", "Польща"),
    ("Portugal", "Португалия", "Португалія"),
    ("Qatar", "Катар", "Катар"),
    ("Romania", "Румыния", "Румунія"),
    ("Russia", "Россия", "Росія"),
    ("Saudi Arabia", "Саудовская Аравия", "Саудівська Аравія"),
    ("Serbia", "Сербия", "Сербія"),
    ("Singapore", "Сингапур", "Сінгапур"),
    ("Slovakia", "Словакия", "Словаччина"),
    ("Slovenia", "Словения", "Словенія"),
    ("South Africa", "ЮАР", "ПАР"),
    ("South Korea", "Южная Корея", "Південна Корея"),
    ("Spain", "Испания", "Іспанія"),
    ("Sweden", "Швеция", "Швеція"),
    ("Switzerland", "Швейцария", "Швейцарія"),
    ("Tajikistan", "Таджикистан", "Таджикистан"),
    ("Thailand", "Таиланд", "Таїланд"),
    ("Turkey", "Турция", "Туреччина"),
    ("Turkmenistan", "Туркменистан", "Туркменістан"),
    ("Ukraine", "Украина", "Україна"),
    ("United Arab Emirates", "ОАЭ", "ОАЕ"),
    ("United Kingdom", "Великобритания", "Велика Британія"),
    ("United States", "США", "США"),
    ("Uruguay", "Уругвай", "Уругвай"),
    ("Uzbekistan", "Узбекистан", "Узбекистан"),
    ("Vietnam", "Вьетнам", "В’єтнам"),
]

LANG_INDEX = {"en": 0, "ru": 1, "uk": 2}

ALTEGIO_LEGAL_ENTITIES = [
    "Open HC",
    "Altegio KFT",
    "Altegio LTD",
    "Альт тех",
]

REPORT_CHANNELS = ("telegram",)

APP_CATEGORIES = (
    "ai_bots",
    "analytics",
    "cash_register",
    "chat_bot",
    "client_acquisition",
    "crm",
    "fiscal_documents",
    "inventory",
    "loyalty",
    "notifications",
    "online_payments",
    "other",
    "payments",
    "promotion",
    "sms",
    "team",
    "telephony",
)

APP_CATEGORY_ALIASES = {
    "ai_bot": "ai_bots",
    "chatbot": "chat_bot",
    "chatbots": "chat_bot",
    "messaging": "notifications",
    "online_booking": "client_acquisition",
    "marketing": "promotion",
    "reviews": "other",
    "finance": "other",
    "hr": "team",
    "automation": "other",
}


def _normalize_category_token(value: str) -> str:
    token = value.strip()
    if token.startswith("\u0421") and len(token) > 1:
        token = "C" + token[1:]
    return token.casefold().replace(" ", "_").replace("-", "_")


def canonical_app_category(value: str | None) -> str:
    token = _normalize_category_token(value or "")
    if not token:
        return ""
    category = APP_CATEGORY_ALIASES.get(token, token)
    return category if category in APP_CATEGORIES else ""


def country_label(name: str, language: str) -> str:
    stored = (name or "").strip()
    if not stored:
        return ""
    index = LANG_INDEX.get(language, 0)
    folded = stored.casefold()
    for names in COUNTRY_NAMES:
        if any(label.casefold() == folded for label in names):
            return names[index]
    return stored


def canonical_country(name: str) -> str:
    stored = (name or "").strip()
    if not stored:
        return ""
    folded = stored.casefold()
    for names in COUNTRY_NAMES:
        if any(label.casefold() == folded for label in names):
            return names[0]
    return stored


def countries_for(language: str) -> list[str]:
    index = LANG_INDEX.get(language, 0)
    labels = [names[index] for names in COUNTRY_NAMES]
    return sorted(labels, key=lambda label: label.casefold())
