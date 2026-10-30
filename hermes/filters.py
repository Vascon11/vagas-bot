"""Pré-filtro barato: descarta o óbvio antes de gastar tokens com a nota.

Só descarta o que é certo pelo título ou pelo tipo da vaga. Qualquer dúvida
(nível ambíguo, local, stack) fica para o LLM.

Aqui também fica `classify_level`, que decide em qual thread do Discord a vaga cai.
"""

import html
import re
from dataclasses import dataclass, field

from hermes.models import normalize


@dataclass(frozen=True)
class FilterRules:
    # Termos que, no título, tornam a vaga incompatível. Comparados sem acento e
    # como palavra inteira: "sr" pega "Dev Sr" mas não "Srta".
    exclude_title: tuple[str, ...] = ()
    exclude_levels: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict | None) -> "FilterRules":
        data = data or {}
        return cls(
            exclude_title=tuple(data.get("excluir_no_titulo", ())),
            exclude_levels=tuple(data.get("excluir_niveis", ())),
        )

    def __post_init__(self):
        terms = [re.escape(normalize(t)) for t in self.exclude_title if normalize(t)]
        pattern = re.compile(rf"\b({'|'.join(terms)})\b") if terms else None
        object.__setattr__(self, "_title_re", pattern)


# Vaga de teste que a empresa esquece publicada na Gupy: "[Modelo] - Dev Fullstack -
# TESTE CONVOCAÇÃO", "[TESTE CF] Regressivo V0". O colchete só existe no título cru.
TEST_POSTING = re.compile(r"\[\s*(teste|test|modelo)\b", re.IGNORECASE)
TEST_POSTING_WORDS = re.compile(r"\b(vaga teste|teste convocacao|nao se candidate)\b")


def prefilter(title: str, level: str | None, rules: FilterRules) -> str | None:
    """Devolve o motivo do descarte, ou None se a vaga segue para a nota."""
    if TEST_POSTING.search(title) or TEST_POSTING_WORDS.search(normalize(title)):
        return "vaga de teste"
    if level and level in rules.exclude_levels:
        return f"nível {level}"
    if rules._title_re and (m := rules._title_re.search(normalize(title))):
        return f"título contém {m.group(1)!r}"
    return None


# --- nível da vaga -----------------------------------------------------------
# Tudo comparado depois do normalize (sem acento, minúsculo, pontuação vira espaço)
# e como palavra inteira. Em cada lista, o primeiro nível que bater ganha.


def _words(*terms: str) -> re.Pattern:
    return re.compile(rf"\b({'|'.join(terms)})\b")


# No título. "pl" não pega "PL/SQL".
TITLE_LEVELS = (
    ("estagio", _words("estagio", "estagiario", "estagiaria", "intern", "internship")),
    ("aprendiz", _words("aprendiz", "apprentice")),
    ("trainee", _words("trainee")),
    ("junior", _words("junior", "jr", "entry level", "iniciante")),
    ("pleno", _words("pleno", "pl(?! sql)", "mid level", "intermediario")),
    ("senior", _words("senior", "sr", "especialista", "lead")),
    # Romanos só depois das palavras: "Analista PL I" é pleno I, não júnior. Seguem o
    # costume daqui (Analista I/II/III = júnior/pleno/sênior). O "i" solto não pega
    # "T.I." nem "I.A.".
    ("junior", _words(r"(?<!\bt )i(?! a\b)")),
    ("pleno", _words("ii")),
    ("senior", _words("iii", "iv")),
)
# Cargo de entrada, quando nada mais diz o nível. Vem depois da descrição.
TITLE_ENTRY = _words("assistente", "auxiliar", "assistant")

# Nível declarado pela fonte. Os confiáveis vêm antes da descrição; o "entry level"
# do LinkedIn, só no fim (o "mid-senior level" dele vem errado demais para usar).
SOURCE_LEVELS = {"internship": "estagio", "apprentice": "aprendiz", "trainee": "trainee"}
SOURCE_WEAK = {"entry level": "junior"}

# Na descrição, frases que só aparecem num tipo de vaga. Aprendiz antes de estágio:
# vaga de aprendiz também fala em bolsa-auxílio.
DESC_LEVELS = (
    ("aprendiz", _words("jovem aprendiz", "menor aprendiz", "programa de aprendizagem")),
    ("estagio", _words("bolsa auxilio", "bolsa estagio", "programa de estagio",
                       "vaga de estagio", "oportunidade de estagio", "contrato de estagio")),
    ("trainee", _words("programa de trainee", "programa trainee")),
)
# "nível pleno", "senioridade: júnior", "pessoa desenvolvedora pleno", "vaga para
# analista sênior": o nível até duas palavras depois de uma dessas âncoras.
DESC_NAMED = re.compile(
    r"\b(?:nivel|senioridade|posicao|cargo|vaga|profissional|perfil|pessoa)"
    r"(?: \w+){0,2}? (junior|jr|pleno|senior|sr)\b"
)
NAMED = {"junior": "junior", "jr": "junior", "pleno": "pleno", "senior": "senior", "sr": "senior"}
# Anos de experiência pedidos: "3 anos de experiência", "experiência mínima de 2 a 4
# anos", "5+ years of experience". Conta o menor número de cada menção.
DESC_YEARS = re.compile(
    r"\b(\d{1,2})(?: (?:a|ate|ou) \d{1,2})? anos? (?:de )?(?:experiencia|vivencia|atuando)"
    r"|\bexperiencia (?:\w+ ){0,4}?(\d{1,2})(?: (?:a|ate|ou) \d{1,2})? anos?\b"
    r"|\b(\d{1,2}) years? (?:of )?(?:professional )?experience"
)
# Mais que isso é a idade da empresa ("30 anos de experiência no mercado"), não requisito.
MAX_YEARS = 10
# Responsabilidade de sênior. Juntas acertam 84% em ~450 vagas com o nível no título,
# e cada uma aparece em dezenas de empresas (não é o texto padrão de uma só).
DESC_SENIOR = _words("referencia tecnica", "liderar", "lideranca tecnica", "experiencia solida",
                     "solida experiencia", "visao estrategica", "mentoria")
# Início de carreira. "Cursando"/"em andamento" ficaram de fora de propósito: "superior
# completo ou cursando" aparece em vaga pleno e sênior o tempo todo.
DESC_ENTRY = _words("recem formad[oa]s?", "primeiro emprego", "inicio de carreira",
                    "inicio da carreira", "sem experiencia", r"\d{1,2} meses de experiencia")


def years_to_level(years: int) -> str:
    """Até 1 ano é júnior, 2 e 3 é pleno, 4 ou mais é sênior.

    Calibrado em ~750 vagas da Gupy (out/2026): as empresas pedem menos anos do que
    o nome do nível sugere — vaga sênior pedindo "mínimo de 3 anos" é comum.
    """
    return "junior" if years <= 1 else "pleno" if years <= 3 else "senior"


def _first(text: str, levels) -> str | None:
    return next((name for name, pattern in levels if pattern.search(text)), None)


def level_from_description(description: str) -> str | None:
    # A Gupy cola os blocos ("...de históriaSomos...") e manda &nbsp;: separa e
    # decodifica antes de normalizar.
    text = normalize(re.sub(r"(?<=[a-zà-ÿ])(?=[A-ZÀ-Ý])", " ", html.unescape(description)))
    if found := _first(text, DESC_LEVELS):
        return found
    if m := DESC_NAMED.search(text):
        return NAMED[m.group(1)]
    years = [int(next(g for g in m.groups() if g)) for m in DESC_YEARS.finditer(text)]
    if years := [y for y in years if y <= MAX_YEARS]:
        # A exigência mais alta é a que define o nível.
        return years_to_level(max(years))
    if DESC_SENIOR.search(text):
        return "senior"
    return "junior" if DESC_ENTRY.search(text) else None


def explain_level(
    title: str, level: str | None, description: str | None = None
) -> tuple[str | None, str | None]:
    """O nível da vaga (estagio, junior, pleno…) e de onde ele veio, ou (None, None).

    Do sinal mais forte para o mais fraco: o título, o nível confiável da fonte, a
    descrição, cargo de entrada no título e, por último, o "entry level" do LinkedIn.
    """
    text = normalize(title)
    if found := _first(text, TITLE_LEVELS):
        return found, "pelo título"
    if found := SOURCE_LEVELS.get(level or ""):
        return found, "pela fonte"
    if description and (found := level_from_description(description)):
        return found, "pela descrição"
    if TITLE_ENTRY.search(text):
        return "junior", "pelo cargo"
    if found := SOURCE_WEAK.get(level or ""):
        return found, "pela fonte"
    return None, None


def classify_level(title: str, level: str | None, description: str | None = None) -> str | None:
    """Só o nível de `explain_level`."""
    return explain_level(title, level, description)[0]
