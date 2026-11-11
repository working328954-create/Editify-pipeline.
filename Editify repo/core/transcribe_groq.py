import json
import os
import sys
from pathlib import Path

from groq import Groq


OUTPUT_FILE = Path(os.getenv("GROQ_TRANSCRIPTION_OUTPUT", r"C:\Tools for editing\transcript_output.json"))
CONFIG_FILES = (
    Path(r"C:\Tools for editing\.env"),
    Path(r"C:\Tools for editing\config.json"),
)
MODEL = "whisper-large-v3-turbo"


def log_error(message: str) -> None:
    sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")


def read_local_api_key() -> str | None:
    key = os.getenv("GROQ_API_KEY")
    if key:
        return key.strip()

    for path in CONFIG_FILES:
        if not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                data = json.loads(path.read_text(encoding="utf-8"))
                value = data.get("GROQ_API_KEY") or data.get("groq_api_key")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            else:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("GROQ_API_KEY="):
                        value = line.split("=", 1)[1].strip().strip('"\'')
                        if value:
                            return value
        except (OSError, json.JSONDecodeError) as exc:
            log_error(f"Could not read local config {path}: {exc}")
            return None
    return None


def main() -> int:
    if len(sys.argv) < 2:
        log_error("Usage: python transcribe_groq.py <audio_path>")
        return 1

    audio_path = Path(sys.argv[1])
    if not audio_path.is_file():
        log_error(f"Audio file not found: {audio_path}")
        return 1

    api_key = read_local_api_key()
    if not api_key:
        log_error("GROQ_API_KEY is not set in the environment, .env, or config.json")
        return 1

    try:
        client = Groq(api_key=api_key)
        with audio_path.open("rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=(audio_path.name, audio_file),
                model=MODEL,
                response_format="verbose_json",
                timestamp_granularities=["word"],
            )

        words = []
        for item in (getattr(response, "words", None) or []):
            word = item if isinstance(item, dict) else item.model_dump()
            words.append({
                "word": word.get("word", ""),
                "start": word.get("start"),
                "end": word.get("end"),
            })

        result = {"text": getattr(response, "text", ""), "words": words}
        OUTPUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0
    except Exception as exc:
        log_error(f"Groq transcription failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
