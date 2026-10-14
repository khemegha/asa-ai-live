"""Calendrier des sessions live ASA AI : chaque jeudi a 15h00 (Europe/Paris),
a partir du 26/11/2026, en sautant les jours feries francais et les
vacances scolaires de la zone C.

Pour une nouvelle annee scolaire : ajouter ses vacances dans SCHOOL_HOLIDAYS_ZONE_C.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")
FIRST_SESSION = date(2026, 11, 26)
SESSION_TIME = time(15, 0)

# (premier jour sans cours, jour de reprise) - source : service-public.gouv.fr / education.gouv.fr
SCHOOL_HOLIDAYS_ZONE_C = [
    (date(2026, 12, 19), date(2027, 1, 4)),   # Noel
    (date(2027, 2, 6), date(2027, 2, 22)),    # Hiver
    (date(2027, 4, 3), date(2027, 4, 19)),    # Printemps
    (date(2027, 7, 3), date(2027, 9, 1)),     # Ete (reprise a confirmer)
]


def _easter(y):
    a = y % 19; b = y // 100; c = y % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(y, month, day)


def public_holidays(y):
    e = _easter(y)
    return {
        date(y, 1, 1), e + timedelta(days=1), date(y, 5, 1), date(y, 5, 8),
        e + timedelta(days=39), e + timedelta(days=50), date(y, 7, 14),
        date(y, 8, 15), date(y, 11, 1), date(y, 11, 11), date(y, 12, 25),
    }


def is_excluded(d):
    if d in public_holidays(d.year):
        return True
    return any(start <= d < end for start, end in SCHOOL_HOLIDAYS_ZONE_C)


def upcoming_sessions(now=None, count=12, close_minutes=0):
    """Prochaines sessions (date) dont l'heure de debut n'est pas passee."""
    now = now or datetime.now(PARIS)
    d = FIRST_SESSION
    while d.weekday() != 3:
        d += timedelta(days=1)
    out = []
    while len(out) < count and d.year < now.year + 3:
        start = datetime.combine(d, SESSION_TIME, PARIS)
        if not is_excluded(d) and start - timedelta(minutes=close_minutes) > now:
            out.append(d)
        d += timedelta(days=7)
    return out


def session_start(d):
    return datetime.combine(d, SESSION_TIME, PARIS)


if __name__ == "__main__":
    for s in upcoming_sessions(datetime(2026, 10, 13, tzinfo=PARIS), 20):
        print(s.isoformat(), s.strftime("%a"))
