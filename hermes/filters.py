"""Pré-filtro barato: descarta o óbvio antes de gastar tokens com a nota.

Só descarta o que é certo pelo título ou pelo tipo da vaga. Qualquer dúvida
(nível ambíguo, local, stack) fica para o LLM.
"""

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


def prefilter(title: str, level: str | None, rules: FilterRules) -> str | None:
    """Devolve o motivo do descarte, ou None se a vaga segue para a nota."""
    if level and level in rules.exclude_levels:
        return f"nível {level}"
    if rules._title_re and (m := rules._title_re.search(normalize(title))):
        return f"título contém {m.group(1)!r}"
    return None
