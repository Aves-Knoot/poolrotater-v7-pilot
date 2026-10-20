"""English-first, opt-in player help. No network, dependencies or SQLite DDL.

Only explicit choices and offer suppression are persisted. Country and chat
evidence stay in memory; shared announcements never inherit a player's locale.
"""
from collections import deque
from contextlib import contextmanager
from functools import wraps
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata

log = logging.getLogger("bot")
LANGUAGES = {"en": "English", "zh": "Chinese", "ko": "Korean", "es": "Spanish", "ru": "Russian", "ja": "Japanese", "pl": "Polish", "id": "Indonesian", "fr": "French", "de": "German"}
ALIASES = {"english": "en", "chinese": "zh", "mandarin": "zh", "中文": "zh",
           "korean": "ko", "한국어": "ko", "spanish": "es", "español": "es",
           "russian": "ru", "русский": "ru", "off": "en", "japanese": "ja", "日本語": "ja", "jp": "ja",
           "polish": "pl", "polski": "pl", "indonesian": "id", "indonesia": "id", "bahasa": "id",
           "french": "fr", "français": "fr", "francais": "fr", "german": "de", "deutsch": "de"}
# Deliberately omit multilingual/ambiguous countries. A country is an offer
# hint only, never a saved preference or proof of language/writing system.
COUNTRIES = {"CN": "zh", "KR": "ko", "RU": "ru", "JP": "ja", "PL": "pl", "ID": "id", "FR": "fr", "DE": "de",
             **{c: "es" for c in ("ES", "MX", "AR", "BO", "CL", "CO", "CR", "CU", "DO", "EC", "GT", "HN", "NI", "PA", "PE", "PY", "SV", "UY", "VE")}}
OFFER_INTERVAL = 300.0
QUIET_SECONDS = 20.0


class LanguagePreferences:
    """Versioned, per-user JSON sidecar; cross-worker atomicity via one flock.

Older workers never access it. This does not change any shared database or
the existing storage contract. The directory/files are private to the service.
"""
    def __init__(self, ratings_path):
        self.root = Path(ratings_path).resolve().parent / "player-language-v1"

    @staticmethod
    def _uid(uid):
        if isinstance(uid, bool) or int(uid) <= 0:
            raise ValueError("invalid player id")
        return str(int(uid))

    def read(self, uid):
        path = self.root / (self._uid(uid) + ".json")
        try:
            if path.is_symlink() or path.stat().st_size > 2048:
                raise ValueError("invalid language record")
            value = json.loads(path.read_text())
        except FileNotFoundError:
            return {"format": 1, "language": None, "offered": False}
        if (not isinstance(value, dict) or value.get("format") != 1
                or value.get("language") not in (None, *LANGUAGES)
                or type(value.get("offered")) is not bool):
            raise ValueError("invalid language record")
        return value

    @contextmanager
    def _lock(self):
        import fcntl
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            # Optional language UX must never stall the lobby/API state loop.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _write(self, name, value):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.root, prefix=".language-", delete=False) as f:
                temporary = Path(f.name)
                json.dump(value, f, allow_nan=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.root / name)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def choose(self, uid, language):
        if language not in LANGUAGES:
            raise ValueError("unsupported language")
        with self._lock():
            self.read(uid)  # Fail closed on corrupt records, preserve evidence.
            self._write(self._uid(uid) + ".json", {"format": 1, "language": language, "offered": True})

    def claim_offer(self, uid, now):
        if not math.isfinite(now):
            return False
        with self._lock():
            record = self.read(uid)
            if record["offered"] or record["language"] is not None:
                return False
            limiter = self.root / "offers.json"
            try:
                if limiter.is_symlink() or limiter.stat().st_size > 2048:
                    return False
                value = json.loads(limiter.read_text())
                previous = float(value["at"])
                if value.get("format") != 1 or not math.isfinite(previous):
                    return False
                if now - previous < OFFER_INTERVAL:
                    return False
            except FileNotFoundError:
                pass
            except (ValueError, TypeError, KeyError):
                return False
            record["offered"] = True
            # Claim before sending: an uncertain chat delivery is never retried.
            self._write(self._uid(uid) + ".json", record)
            self._write("offers.json", {"format": 1, "at": now})
            return True


def detect_language(text, prefix="."):
    """Conservative sentence evidence, not a statistical confidence score.

Requires language-specific words/grammar, not country, names, Unicode script
alone, commands, single greetings or quoted titles. Ambiguity means no offer.
"""
    text = unicodedata.normalize("NFKC", str(text)).strip().lower()
    if (text.startswith((prefix, ",", ">", '"', "'", "「", "『"))
            or "http://" in text or "https://" in text or len(text) > 500):
        return None
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return None
    candidates = []
    han = sum("\u4e00" <= c <= "\u9fff" for c in letters)
    kana = any("\u3040" <= c <= "\u30ff" for c in text)
    if (not kana and han >= 8 and han / len(letters) >= .7
            and sum(p in text for p in ("我", "你", "这个", "這個", "什么", "什麼", "怎么", "怎麼", "谱面", "譜面", "有点", "有點", "不知道", "可以", "喜欢", "喜歡", "了", "的是", "很有", "帮助", "幫助")) >= 2):
        candidates.append("zh")
    hangul = sum("\uac00" <= c <= "\ud7a3" for c in letters)
    if (hangul >= 8 and hangul / len(letters) >= .7
            and sum(p in text for p in ("어요", "습니다", "이거", "이게", "좋", "어떻게", "왜", "한국", "안녕", "맵", "너무", "하고", "나요", "설정")) >= 2):
        candidates.append("ko")
    words = set(re.findall(r"[^\W\d_]+", text))
    cyrillic = sum("\u0400" <= c <= "\u04ff" for c in letters)
    if (cyrillic >= 8 and cyrillic / len(letters) >= .7
            and not any(c in text for c in "іїєґў")
            and words & {"это", "как", "этот", "что", "карта", "карты", "команды", "работает", "понял", "нравится", "почему", "можно", "играть", "только", "русский"}
            and len(words & {"это", "как", "этот", "что", "карта", "карты", "команды", "работает", "понял", "люблю", "нравится", "почему", "можно", "играть", "только", "не", "русский"}) >= 2):
        candidates.append("ru")
    if (len(letters) >= 12 and len(words) >= 4 and cyrillic == 0 and han == 0 and hangul == 0
            and len(words & {"quiero", "puedes", "mapas", "está", "esto", "cómo", "gracias", "jugar", "necesito", "alguien", "porque", "entiendo", "gusta", "buenas", "comandos", "podemos", "cambiar", "dificultad"}) >= 2):
        candidates.append("es")
    kana_count = sum("\u3040" <= c <= "\u30ff" for c in letters)
    if (kana_count >= 6 and kana_count / len(letters) >= .4
            and cyrillic == 0 and hangul == 0
            and sum(p in text for p in ("これは", "この", "です", "ます", "どう", "とても", "ない", "たい", "ください", "わから", "わかり", "マップ", "コマンド")) >= 2):
        candidates.append("ja")
    if len(letters) >= 12 and len(words) >= 4 and cyrillic == 0 and han == 0 and hangul == 0 and not kana:
        markers = {
            "pl": {"nie", "rozumiem", "jak", "mogę", "mapy", "komendy", "grać", "możemy", "zmienić", "trudność", "proszę"},
            "id": {"saya", "kamu", "bisa", "tidak", "mengerti", "perintah", "ingin", "main", "tolong", "kesulitan", "ubah"},
            "fr": {"je", "ne", "pas", "ces", "cette", "cartes", "comment", "peux", "jouer", "comprends", "commandes", "changer", "difficulté"},
            "de": {"ich", "nicht", "diese", "karten", "kann", "bitte", "spielen", "verstehe", "schwierigkeit", "ändern", "befehle"},
        }
        for language, evidence in markers.items():
            if len(words & evidence) >= 2:
                candidates.append(language)
    return candidates[0] if len(candidates) == 1 else None


# Compact, prepared translations. Never translate player chat, map names,
# numbers, command tokens or operational/staff messages using a remote model.
TEXT = {
    "offer": ("需要中文提示？输入 {p}lang zh。", "한국어 안내는 {p}lang ko로 설정하세요.", "¿Prefieres español? {p}lang es.", "Нужен русский? {p}lang ru."),
    "chosen": ("已启用中文；英文始终保留。{p}lang en 返回纯英文。", "한국어 안내 설정 완료. 영어도 유지됩니다. {p}lang en: 영어만.", "Español activado; inglés siempre visible. {p}lang en: solo inglés.", "Русский включён; английский остаётся. {p}lang en: только английский."),
    "help_short": ("完整命令见上方链接；{p}help 命令名：查看说明。", "전체 명령어는 위 링크. {p}help 명령어: 사용법.", "Todos los comandos en el enlace; {p}help comando: ayuda.", "Все команды по ссылке; {p}help команда: справка."),
    "not_running": ("当前没有正在进行的谱面。", "진행 중인 맵이 없습니다.", "No hay un mapa en curso.", "Сейчас карта не играется."),
    "time_unknown": ("剩余时间未知；下一局轮到你。", "남은 시간은 알 수 없습니다. 다음 판에 참가합니다.", "Tiempo restante no disponible; entras en la próxima.", "Остаток времени неизвестен; играешь со следующей карты."),
    "finishing": ("即将结束；下一局轮到你。", "곧 끝납니다. 다음 판에 참가합니다.", "Está terminando; entras en la próxima.", "Карта заканчивается; играешь со следующей."),
    "remaining": ("约剩 {duration}；下一局轮到你。", "약 {duration} 남았습니다. 다음 판에 참가합니다.", "Quedan unos {duration}; entras en la próxima.", "Осталось примерно {duration}; играешь со следующей карты."),
    "join": ("下一局轮到你。{p}time 查看剩余时间。", "다음 판에 참가합니다. {p}time: 남은 시간.", "Entras en la próxima. {p}time: tiempo restante.", "Играешь со следующей карты. {p}time: остаток времени."),
    "welcome": ("准备好后即可开始。{p}help 查看命令。", "준비하면 시작합니다. {p}help: 명령어.", "Márcate listo para empezar. {p}help: comandos.", "Нажми готовность для старта. {p}help: команды."),
    "style_enabled": ("{style} 已启用：{count} 张可选谱面，仅影响后续选图。", "{style} 설정: 맵 {count}개. 이후 선택에만 적용.", "{style} activado: {count} mapas; solo próximas selecciones.", "{style} включён: {count} карт; только будущие подборы."),
    "style_active": ("{style} 已启用：{count} 张可选谱面。", "{style} 이미 설정됨: 맵 {count}개.", "{style} ya está activo: {count} mapas.", "{style} уже включён: {count} карт."),
    "style_empty": ("没有符合条件的谱面；保留原设置。", "조건에 맞는 맵이 없어 이전 설정을 유지합니다.", "No hay mapas elegibles; se mantiene la selección anterior.", "Подходящих карт нет; прежний режим сохранён."),
    "join_player": ("请以玩家身份加入后再更改风格。", "스타일 변경은 플레이어로 참가해야 합니다.", "Únete como jugador para cambiar el estilo.", "Войди как игрок, чтобы изменить стиль."),
}
_INDEX = {"zh": 0, "ko": 1, "es": 2, "ru": 3}

HELP = {
    "help": ("显示常用命令和完整列表链接，或单条说明。", "자주 쓰는 명령어와 전체 목록 링크 또는 개별 도움말.", "Comandos comunes y enlace a la lista completa, o ayuda de uno.", "Частые команды и ссылка на полный список, либо справка по одной."),
    "help2": ("显示完整命令列表。", "전체 명령어 목록을 표시합니다.", "Muestra la lista completa.", "Полный список команд."),
    "time": ("查看当前谱面的大致剩余时间。", "진행 중인 맵의 대략적인 남은 시간.", "Tiempo aproximado restante del mapa actual.", "Примерное время до конца текущей карты."),
    "variety": ("恢复混合选图；Mania 恢复多种风格。", "다양한 맵/패턴 선택으로 돌아갑니다.", "Vuelve a una mezcla de mapas y estilos.", "Возвращает смешанный подбор карт и стилей."),
    "style": ("选择 Mania 风格；可组合。{p}variety 恢复混合。Rice 与 Noodle 不能组合。", "Mania 패턴 선택/조합. {p}variety: 혼합 복귀. Rice와 Noodle은 조합 불가.", "Elige/combina estilos Mania. {p}variety: mezcla. Rice y Noodle no se combinan.", "Выбор/сочетание стилей Mania. {p}variety: смесь. Rice и Noodle несовместимы."),
    "farm": ("后续优先选择 Farm 谱面。", "이후 Farm 맵을 우선 선택합니다.", "Prefiere mapas Farm para próximas selecciones.", "Предпочитать Farm-карты для будущих подборов."),
    "stream": ("后续优先选择 Stream 谱面。", "이후 Stream 맵을 우선 선택합니다.", "Prefiere mapas Stream para próximas selecciones.", "Предпочитать Stream-карты для будущих подборов."),
    "jump": ("优先选择 Standard 大间距跳跃谱面，与 Farm 独立。", "Standard 점프 맵 우선. Farm과 별개입니다.", "Prefiere saltos de Standard; independiente de Farm.", "Предпочитать прыжковые Standard-карты; отдельно от Farm."),
    "rice": ("优先单点/和弦为主、少长条和切的 Mania 谱面。", "탭/코드 중심, 홀드와 스트림이 적은 Mania 맵.", "Mania de taps/acordes, con pocos holds y streams.", "Mania с тапами/аккордами, минимум холдов и стримов."),
    "noodle": ("优先长条为主的原生 Mania 谱面，比 LN 更严格。", "홀드 중심의 네이티브 Mania 맵. LN보다 엄격합니다.", "Mania nativa dominada por holds; más estricto que LN.", "Нативная Mania с преобладанием холдов; строже LN."),
    "ln": ("优先长条 Mania 谱面。", "롱노트 Mania 맵 우선.", "Prefiere notas largas de Mania.", "Предпочитать длинные ноты Mania."),
    "jack": ("优先同键重复的 Mania 谱面。", "같은 키 반복 Mania 맵 우선.", "Prefiere repeticiones en la misma tecla de Mania.", "Предпочитать повторы одной клавиши в Mania."),
    "chordjack": ("优先重复和弦的 Mania 谱面。", "반복 코드 Mania 맵 우선.", "Prefiere acordes repetidos de Mania.", "Предпочитать повторяющиеся аккорды Mania."),
    "technical": ("优先技术型 Mania 谱面。", "테크니컬 Mania 맵 우선.", "Prefiere mapas técnicos de Mania.", "Предпочитать технические Mania-карты."),
    "skip": ("投票跳过当前谱面。", "현재 맵을 스킵하도록 투표합니다.", "Vota para saltar el mapa actual.", "Голос за пропуск текущей карты."),
    "replay": ("投票重玩上一张已完成谱面。", "직전 완료 맵 재플레이 투표.", "Vota para repetir el último mapa completado.", "Голос за повтор последней завершённой карты."),
    "start": ("准备好后请求开始，仍使用正常倒计时。", "준비 후 시작 요청. 정상 카운트다운 유지.", "Pide iniciar tras marcarte listo; mantiene la cuenta atrás.", "Просьба начать после готовности; отсчёт сохраняется."),
    "wait": ("开始前增加 10 秒或取消倒计时。", "시작 전 10초 추가 또는 카운트다운 취소.", "Añade 10 segundos antes de empezar o cancela la cuenta atrás.", "Добавить 10 секунд перед стартом или отменить отсчёт."),
    "harder": ("投票提高后续难度，当前队列不变。", "이후 난이도 증가 투표. 현재 큐 유지.", "Vota por mayor dificultad futura; la cola visible no cambia.", "Голос за повышение будущей сложности; очередь не меняется."),
    "easier": ("投票降低后续难度，当前队列不变。", "이후 난이도 감소 투표. 현재 큐 유지.", "Vota por menor dificultad futura; la cola visible no cambia.", "Голос за снижение будущей сложности; очередь не меняется."),
    "good": ("为上一张已完成谱面给好评。", "직전 완료 맵에 긍정적 평가.", "Valora positivamente el último mapa completado.", "Положительная оценка последней завершённой карты."),
    "bad": ("为上一张已完成谱面给差评。", "직전 완료 맵에 부정적 평가.", "Valora negativamente el último mapa completado.", "Отрицательная оценка последней завершённой карты."),
    "ok": ("为上一张已完成谱面给中立评价。", "직전 완료 맵에 중립적 평가.", "Valoración neutral del último mapa completado.", "Нейтральная оценка последней завершённой карты."),
    "pool": ("查看当前图池、难度范围和筛选条件。", "현재 맵 풀, 난이도 범위와 필터.", "Muestra la selección, rango y filtros actuales.", "Текущий пул, диапазон сложности и фильтры."),
    "queue": ("查看当前谱面和后续队列。", "현재 맵과 다음 맵 큐.", "Muestra el mapa actual y los siguientes.", "Текущая карта и следующие в очереди."),
    "map": ("查看当前 Mania 谱面信息和链接。", "현재 Mania 맵 정보와 링크.", "Datos y enlace del mapa Mania actual.", "Описание и ссылка текущей Mania-карты."),
    "pp": ("估算当前谱面的 PP；可指定模组和准确率。", "현재 맵 PP 추정. 모드와 정확도 지정 가능.", "Estima PP del mapa actual con mods y precisión opcionales.", "Оценка PP текущей карты с модами и точностью."),
    "stats": ("查看已记录的玩家统计。", "기록된 플레이어 통계.", "Muestra estadísticas registradas del jugador.", "Записанная статистика игрока."),
    "lobby": ("查看此房间已记录的活动。", "이 로비의 기록된 활동.", "Muestra la actividad registrada del lobby.", "Записанная активность этого лобби."),
    "hidden": ("从公开查询中隐藏你的统计。", "공개 조회에서 내 통계를 숨깁니다.", "Oculta tus estadísticas de consultas públicas.", "Скрыть свою статистику из публичных запросов."),
    "show": ("重新公开你的统计。", "내 통계를 다시 공개합니다.", "Vuelve a mostrar tus estadísticas públicamente.", "Снова показывать свою статистику публично."),
    "discord": ("获取社区 Discord 邀请。", "커뮤니티 Discord 초대 링크.", "Obtén la invitación de Discord.", "Приглашение в Discord сообщества."),
    "ai": ("在启用时向可选聊天功能提问。", "활성화된 경우 선택적 채팅 기능에 질문합니다.", "Pregunta al chat opcional cuando esté habilitado.", "Вопрос опциональному чату, если он включён."),
    "lang": ("设置个人提示语言；英文始终保留。{p}lang en 返回纯英文并关闭推荐。", "개인 안내 언어 설정. 영어 항상 유지. {p}lang en: 영어만/추천 끄기.", "Idioma personal; inglés siempre visible. {p}lang en: solo inglés y sin ofertas.", "Личный язык; английский всегда виден. {p}lang en: только английский, без предложений."),
}


# Every prepared entry covers the same languages; keep existing tuple positions.
HELP["start"] = ("2人以上时，1人准备即可开始；无人准备则等待。", "2명 이상이면 1명 준비로 시작. 아무도 준비하지 않으면 대기.", "Con 2+ jugadores basta 1 listo; si nadie está listo, espera.", "При 2+ игроках достаточно 1 готового; без готовых ждём.")
HELP["dt"] = ("设置 Standard DT 比例；{p}dt 100 优先全DT，必要时回退NoMod。", "Standard DT 비율 설정. {p}dt 100: 전체 DT 선호, 필요하면 NoMod.", "Ajusta la proporción DT Standard. {p}dt 100: todo DT, con respaldo NoMod.", "Доля DT Standard. {p}dt 100: предпочтение полного DT, резерв NoMod.")
HELP["nm"] = ("关闭 Standard DT；当前比赛继续。", "Standard DT 끄기. 진행 중 경기는 계속됩니다.", "Desactiva DT Standard; la partida actual continúa.", "Отключить DT Standard; текущая игра продолжается.")
TEXT.update({
    "dt_enabled": ("已启用混合DT。{p}nm 关闭。", "혼합 DT 켜짐. {p}nm: 끄기.", "DT mixto activado. {p}nm: desactivar.", "Смешанный DT включён. {p}nm: отключить."),
    "dt_full": ("优先全DT，必要时NoMod。{p}dt 混合，{p}nm 关闭。", "전체 DT 선호, 필요하면 NoMod. {p}dt: 혼합, {p}nm: 끄기.", "Preferencia todo DT; respaldo NoMod. {p}dt: mixto, {p}nm: desactivar.", "Полный DT предпочтителен; резерв NoMod. {p}dt: смешанный, {p}nm: отключить."),
    "dt_disabled": ("DT已关闭，后续仅NoMod；当前DT比赛继续。", "DT 꺼짐, 이후 NoMod. 진행 중 DT 경기는 계속됩니다.", "DT desactivado; próximas selecciones NoMod. La partida DT actual termina.", "DT отключён; далее NoMod. Текущая DT-игра завершится."),
    "dt_fixed_mods": ("此房间固定模组不支持混合DT。", "이 로비의 고정 모드는 혼합 DT를 지원하지 않습니다.", "Los mods fijos del lobby no permiten DT mixto.", "Фиксированные моды лобби не поддерживают смешанный DT."),
})
# Catch remains a neutral fixed-range native NM selection until variants/styles
# receive independent qualification. All prepared player languages explain this.
TEXT.update({
    "catch_nm_only": ("Catch 仅支持原生 NoMod，速率为 1.0。", "Catch는 네이티브 NoMod, 배속 1.0만 지원합니다.", "Catch solo admite mapas nativos NoMod a velocidad 1.0.", "Catch поддерживает только нативные карты NoMod со скоростью 1.0."),
    "catch_fixed_difficulty": ("Catch 使用固定星级范围；自动难度尚未启用。", "Catch는 고정 별 난이도 범위를 사용하며 자동 난이도는 지원하지 않습니다.", "Catch usa un rango fijo de estrellas; la dificultad automática no está habilitada.", "Catch использует фиксированный диапазон сложности; автонастройка не включена."),
    "catch_style_unavailable": ("Catch 风格和 Farm 筛选尚未验证；保持多样选图。", "Catch 패턴 및 Farm 필터는 아직 검증되지 않아 다양한 맵을 선택합니다.", "Los filtros de estilo y Farm de Catch aún no están validados; se mantiene la variedad.", "Фильтры стилей и Farm для Catch ещё не проверены; сохраняется разнообразный подбор."),
    "catch_research_only": ("Catch 图池仅供离线研究，尚未获准公开分发或在线启用。", "Catch 카탈로그는 오프라인 연구용이며 공개 배포나 실시간 사용은 아직 허용되지 않습니다.", "El catálogo Catch es solo para investigación sin conexión; su distribución y activación en vivo no están autorizadas.", "Каталог Catch предназначен для офлайн-исследования; публикация и запуск онлайн ещё не разрешены."),
})
from .language_packs import CODES as _EXTRA_CODES, TEXT as _EXTRA_TEXT, HELP as _EXTRA_HELP
for _key in TEXT:
    TEXT[_key] += _EXTRA_TEXT[_key]
for _key in HELP:
    HELP[_key] += _EXTRA_HELP[_key]
_INDEX.update({code: 4 + index for index, code in enumerate(_EXTRA_CODES)})
LANGUAGE_CODES = "|".join(LANGUAGES)


def help_translation(language, key, **values):
    try:
        return HELP[key][_INDEX[language]].format(**values)
    except (KeyError, IndexError, ValueError):
        return ""


def translated(language, key, **values):
    try:
        return TEXT[key][_INDEX[language]].format(**values)
    except (KeyError, IndexError, ValueError):
        return ""


def bilingual(english, native):
    """Never shorten English for a translation or emit a truncated fragment."""
    if native and len(english) + len(native) + 3 <= 400:
        return english + " / " + native
    return english


def player_language_command(method):
    """Scope personal localization to synchronous requested replies only."""
    @wraps(method)
    def wrapped(self, user_id, text, *args, **kwargs):
        previous = getattr(self, "_language_context", None)
        previous_help = getattr(self, "_language_help_sent", False)
        self._language_context = (user_id, str(text).split())
        self._language_help_sent = False
        try:
            return method(self, user_id, text, *args, **kwargs)
        finally:
            self._language_context = previous
            self._language_help_sent = previous_help
    return wrapped


class PlayerLanguages:
    def _init_player_languages(self):
        self._language_store = LanguagePreferences(self.ratings.path)
        self._language_evidence = {}
        self._language_candidates = {}

    def _language_record(self, uid):
        store = getattr(self, "_language_store", None)
        if store is None:
            return {"language": None, "offered": True}
        try:
            return store.read(uid)
        except (OSError, ValueError, TypeError):
            log.warning("language preference unavailable; using English")
            return {"language": "en", "offered": True}

    def _language_personal(self, uid, english, key, **values):
        language = self._language_record(uid)["language"]
        if language is None:
            language = self.cfg.get('default_response_language')
        return bilingual(english, translated(language, key, p=self.prefix, **values))

    def _handle_language_command(self, uid, parts):
        if len(parts) != 2:
            language = self._language_record(uid)["language"] or self.cfg.get('default_response_language') or "en"
            self.say(f"Language: {LANGUAGES.get(language, LANGUAGES['en'])}. {self.prefix}lang {LANGUAGE_CODES}. English always stays; en/off stops offers.")
            return
        language = ALIASES.get(parts[1].lower(), parts[1].lower())
        if language not in LANGUAGES:
            self.say(f"Use {self.prefix}lang {LANGUAGE_CODES}. English always stays; en/off stops offers.")
            return
        if uid not in self.players or uid in self.non_gameplay_user_ids():
            self.say("Join this lobby to set your own language.")
            return
        store = getattr(self, "_language_store", None)
        try:
            if store is None:
                raise OSError("language state unavailable")
            store.choose(uid, language)
        except (OSError, ValueError, TypeError):
            self.say("Language preference could not be saved. Please retry; English remains available.")
            return
        getattr(self, "_language_candidates", {}).pop(uid, None)
        if language == "en":
            self.say("English-only replies enabled; language offers disabled.")
        else:
            self.say(bilingual(f"{LANGUAGES[language]} replies enabled; English stays. {self.prefix}lang en: English only.", translated(language, "chosen", p=self.prefix)))

    def _observe_player_language(self, uid, text, message_time):
        if (not hasattr(self, "_language_store") or uid not in self.players
                or uid not in self.gameplay_player_ids()
                or self.cfg.get("bot_chat_silenced", False)
                or not self.cfg.get("user_commands_enabled", True)
                or abs(time.time() - message_time) > 30):
            return
        record = self._language_record(uid)
        if record["offered"] or record["language"] is not None:
            return
        language = detect_language(text, self.prefix)
        if language is None:
            return
        evidence = self._language_evidence.setdefault(uid, deque(maxlen=2))
        evidence.append((message_time, language))
        if len(evidence) == 2 and evidence[0][1] == language and 0 < message_time - evidence[0][0] <= 120:
            self._language_candidates[uid] = (message_time, language)
        # Bound stale per-visit evidence without retaining raw text.
        for player in list(self._language_evidence):
            if player not in self.players:
                self._language_evidence.pop(player, None)
                self._language_candidates.pop(player, None)

    def _claim_language_offer(self, uid, language, english, fallback=None):
        fallback = english if fallback is None else fallback
        native = translated(language, "offer", p=self.prefix)
        line = bilingual(english, native)
        if line == english:
            return fallback
        try:
            if self._language_store.claim_offer(uid, time.time()):
                return line
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("language offer unavailable; using English")
        return fallback

    def _maybe_language_offer(self):
        if (not hasattr(self, "_language_store") or self.state != "IDLE"
                or self.cfg.get("bot_chat_silenced", False)
                or not self.cfg.get("user_commands_enabled", True)
                or time.time() < getattr(self, "_language_offer_retry_at", 0)
                or time.time() - getattr(self, "_last_non_hint_sent_at", 0) < QUIET_SECONDS):
            return False
        for uid, (at, language) in list(self._language_candidates.items()):
            record = self._language_record(uid)
            if (uid not in self.gameplay_player_ids() or time.time() - at > 600
                    or record["offered"] or record["language"] is not None):
                self._language_candidates.pop(uid, None)
                continue
            name = str(self.players[uid].get("name") or uid)[:32].replace("\n", " ").replace("\r", " ")
            english = f"{name}: Prefer {LANGUAGES[language]}? {self.prefix}lang {language}. English stays; {self.prefix}lang en dismisses offers."
            line = self._claim_language_offer(uid, language, english)
            self._language_offer_retry_at = time.time() + 30
            if line != english:
                self._language_candidates.pop(uid, None)
                self.say(line, automatic=True)
                return True
        return False

    def _localize_command_reply(self, english, *, automatic=False, lobby_only=False):
        context = getattr(self, "_language_context", None)
        if (context is None or automatic or lobby_only
                or getattr(self, "_discord_command_reply_channel_id", None)):
            return english
        uid, parts = context
        if not parts or uid not in self.players:
            return english
        cmd = parts[0].removeprefix(self.prefix).lower()
        if cmd == "t":
            cmd = "time"
        if cmd == "aim":
            cmd = "jump"
        record = self._language_record(uid)
        language = record["language"]
        if language is None:
            default = self.cfg.get('default_response_language')
            # An English room default does not opt the player out of offers.
            language = default if default != 'en' else None
        if cmd in ("help", "commands"):
            if getattr(self, "_language_help_sent", False):
                return english
            self._language_help_sent = True
            if language in _INDEX:
                if len(parts) > 1 and english.startswith(self.prefix) and " — " in english:
                    from .bot import _COMMAND_HELP_ALIASES
                    key = _COMMAND_HELP_ALIASES.get(parts[1].lower().removeprefix(self.prefix), parts[1].lower().removeprefix(self.prefix))
                    native = help_translation(language, key, p=self.prefix)
                elif len(parts) == 1:
                    native = translated(language, "help_short", p=self.prefix)
                else:
                    native = ""
                return bilingual(english, native)
            if language is None and not record["offered"] and len(parts) == 1:
                detected = getattr(self, "_language_candidates", {}).get(uid)
                hint = detected[1] if detected and time.time() - detected[0] <= 600 else COUNTRIES.get(self.players[uid].get("country_code"))
                if hint:
                    offer = f"Prefer {LANGUAGES[hint]}? {self.prefix}lang {hint}."
                    # Append one bilingual offer to the existing help line.
                    return self._claim_language_offer(uid, hint, english + " | " + offer, fallback=english) if len(english + " | " + offer) + len(translated(hint, "offer", p=self.prefix)) + 3 <= 400 else english
            return english
        if language not in _INDEX:
            return english
        if cmd in {"lang", "language"} and english.startswith(("Language: ", "Use ")):
            return bilingual(english, help_translation(language, "lang", p=self.prefix))
        if cmd in {"dt", "nm"}:
            key = {
                "dt enabled, buckle up gamers | Disable with .nm": "dt_enabled",
                "DT 100% enabled; NoMod fallback | Mixed with .dt | Disable with .nm": "dt_full",
                "DT disabled; NoMod picks only. A running DT map will finish.": "dt_disabled",
                "DT mixing is unavailable with this lobby's fixed mods.": "dt_fixed_mods",
            }.get(english)
            if key:
                return bilingual(english, translated(language, key, p=self.prefix))
            if english.startswith("Usage: " + self.prefix + cmd):
                return bilingual(english, help_translation(language, cmd, p=self.prefix))
        if cmd == "time":
            key = {"No map running.": "not_running", "Time remaining unavailable—you're in next.": "time_unknown", "Finishing—you're in next.": "finishing"}.get(english)
            if key:
                return bilingual(english, translated(language, key, p=self.prefix))
            match = re.fullmatch(r"About (.+) left—you're in next\.", english)
            if match:
                return bilingual(english, translated(language, "remaining", duration=match[1], p=self.prefix))
        if cmd in {"style", "farm", "stream", "variety", "jump", "jack", "chordjack", "ln", "technical", "rice", "noodle", "noodles"}:
            match = re.fullmatch(r"(.+) (enabled|already active)—(\d+) eligible maps?\.(?: Future picks only\.)?", english)
            if match:
                style, status, count = match.groups()
                return bilingual(english, translated(language, "style_enabled" if status == "enabled" else "style_active", style=style, count=count, p=self.prefix))
            if english.startswith("No eligible maps") or english.startswith("No verified jump maps"):
                return bilingual(english, translated(language, "style_empty", p=self.prefix))
            if english == "Join as a player to change this lobby's style.":
                return bilingual(english, translated(language, "join_player", p=self.prefix))
            if cmd == "style" and (english.startswith(self.prefix + "style <styles>:") or english.startswith("Usage: " + self.prefix + "style")):
                return bilingual(english, HELP["style"][_INDEX[language]].format(p=self.prefix))
        return english
