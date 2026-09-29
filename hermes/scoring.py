"""Nota de compatibilidade vaga × perfil, com um LLM (OpenAI ou Gemini).

O rubric segue a ideia do career-ops (github.com/career-ops-hq/career-ops):
avaliar contra o perfil real, dizer "não aplique" sem rodeio e marcar vaga
fantasma. As últimas decisões do Gabriel entram como exemplo, para a nota se
calibrar pelo gosto dele sem precisar de memória externa.
"""

from typing import Literal

from pydantic import BaseModel, Field

MAX_DESCRIPTION = 6000  # caracteres; descrição maior é quase sempre benefício e boilerplate

INSTRUCTIONS = """\
Você avalia vagas de emprego para um candidato específico. O perfil dele vem abaixo,
seguido de decisões recentes que ele mesmo tomou sobre outras vagas.

Dê uma nota de 0 a 100 para o quanto vale a pena ele se candidatar:
- 80+: encaixe forte no nível, na área e nas condições; aplicar.
- 60-79: encaixe razoável com alguma ressalva.
- abaixo de 60: não vale o tempo (nível acima do dele, área diferente, condição que ele recusa).

Regras:
- O nível real vem do texto da vaga (requisitos, anos de experiência), não do rótulo.
  "Júnior" pedindo 3 anos de experiência é pleno.
- Use as decisões anteriores dele como sinal do que ele aprova e do que descarta.
- Não invente requisitos nem qualidades que não estão no texto.
- Marque em alertas: vaga fantasma ou banco de talentos, vaga só para um público do qual
  ele não faz parte, prazo vencendo, e qualquer pedido que ele não cumpre.
- Motivos e alertas curtos, em português, no máximo 3 de cada.

PERFIL:
{profile}

DECISÕES RECENTES DELE:
{examples}
"""


class FatalScoringError(RuntimeError):
    """Erro que vale para todas as vagas (sem crédito, chave inválida, cota): parar a rodada."""


class Score(BaseModel):
    score: int = Field(ge=0, le=100)
    recomendacao: Literal["aplicar", "talvez", "nao_aplicar"]
    nivel_detectado: Literal["estagio", "junior", "pleno", "senior", "indefinido"]
    modelo_trabalho: Literal["presencial", "hibrido", "remoto", "indefinido"]
    motivos: list[str]
    alertas: list[str]


def format_job(row) -> str:
    desc = (row["description"] or "(sem descrição)")[:MAX_DESCRIPTION]
    return (
        f"Título: {row['title']}\nEmpresa: {row['company']}\n"
        f"Local: {row['location'] or 'não informado'}\nFonte: {row['source']}\n\n{desc}"
    )


def format_examples(decisions) -> str:
    if not decisions:
        return "(nenhuma ainda)"
    return "\n".join(
        f"- {d['decision'].upper()}: {d['title']} — {d['company']} ({d['location'] or '?'})"
        for d in decisions
    )


class Scorer:
    """Monta o prompt e valida a resposta. Cada provedor só implementa _generate."""

    def __init__(self, client, model: str, profile: str):
        self._client = client
        self._model = model
        self._profile = profile

    def score(self, row, decisions) -> Score:
        instructions = INSTRUCTIONS.format(
            profile=self._profile, examples=format_examples(decisions)
        )
        try:
            result = self._generate(instructions, format_job(row))
        except Exception as exc:
            if self._is_fatal(exc):
                raise FatalScoringError(getattr(exc, "message", None) or str(exc)) from exc
            raise
        if result is None:
            raise ValueError(f"resposta sem JSON válido para a vaga {row['id']}")
        return result

    def _generate(self, instructions: str, text: str) -> Score | None:
        raise NotImplementedError

    def _is_fatal(self, exc: Exception) -> bool:
        return False


class OpenAIScorer(Scorer):
    def _generate(self, instructions, text):
        resp = self._client.responses.parse(
            model=self._model,
            instructions=instructions,
            input=text,
            text_format=Score,
            store=False,  # a OpenAI não guarda as vagas nem o perfil
        )
        return resp.output_parsed

    def _is_fatal(self, exc):
        import openai

        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return True
        return isinstance(exc, openai.RateLimitError) and (
            getattr(exc, "code", None) in ("insufficient_quota", "credit_balance_exhausted")
            or "insufficient_quota" in str(exc)
        )


class GeminiScorer(Scorer):
    def _generate(self, instructions, text):
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self._model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=instructions,
                response_mime_type="application/json",
                response_schema=Score,
                # a nota não usa ferramentas; sem isso o SDK avisa a cada chamada
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        return resp.parsed

    def _is_fatal(self, exc):
        from google.genai import errors

        # 401/403: chave inválida ou API desligada. 429: cota ou crédito — as próximas
        # vagas bateriam no mesmo limite, então a rodada para e tenta na próxima.
        return isinstance(exc, errors.ClientError) and getattr(exc, "code", None) in (401, 403, 429)


def make_scorer(provider: str, api_key: str, model: str, profile: str) -> Scorer:
    if provider == "openai":
        from openai import OpenAI

        return OpenAIScorer(OpenAI(api_key=api_key), model, profile)
    if provider == "gemini":
        from google import genai

        return GeminiScorer(genai.Client(api_key=api_key), model, profile)
    raise ValueError(f"provedor de LLM desconhecido: {provider!r}")
