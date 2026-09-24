# config/prompts/__init__.py
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).parent

_EXTRACT_FILES = {
    "sout": "extract_sout.txt",
    "education": "extract_education.txt",
    "opr": "extract_opr.txt",
    "plk": "extract_plk.txt",
    "combined": "extract_combined.txt",
    "sout_opr": "extract_combined.txt",
}


def load_system_prompt() -> str:
    """Legacy: полный текст (для UI / отладки)."""
    path = _DIR / "system_prompt.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return load_core_prompt()


def load_core_prompt() -> str:
    path = _DIR / "core.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "Ты аналитик тендеров ОТ. Отвечай только валидным JSON без пояснений."


def load_extract_prompt(tender_type: str) -> str:
    name = _EXTRACT_FILES.get((tender_type or "").lower())
    if not name:
        return f"[TYPE_HINT: {tender_type}]\nИзвлеки ключевые параметры. Ответ — JSON."
    path = _DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"[TYPE_HINT: {tender_type}]\nИзвлеки параметры. JSON only."


def build_agent_system_prompt(tender_type: Optional[str] = None) -> str:
    """core + extract для известного типа — то, что уходит в role=system."""
    parts = [load_core_prompt()]
    if tender_type:
        parts.append(load_extract_prompt(tender_type))
    return "\n\n".join(parts)


__all__ = [
    "load_system_prompt",
    "load_core_prompt",
    "load_extract_prompt",
    "build_agent_system_prompt",
]
