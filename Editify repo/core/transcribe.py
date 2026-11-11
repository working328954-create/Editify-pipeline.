import os
import json
import sys
import torch
from faster_whisper import WhisperModel

# Configuration
MODEL_SIZE = "small"
CACHE_DIR = r"C:\Tools for editing\models"
OUTPUT_FILE = r"C:\Tools for editing\transcript_output.json"

# Ensure cache directory exists
os.makedirs(CACHE_DIR, exist_ok=True)

# Set environment variables for cache
os.environ["HF_HUB_CACHE"] = CACHE_DIR
os.environ["CTRANSLATE2_CACHE_DIR"] = CACHE_DIR

# CPU Thread Optimization
os.environ["OMP_NUM_THREADS"] = "4"
os.environ["MKL_NUM_THREADS"] = "4"

def log_error(message):
    sys.stderr.write(json.dumps({"error": message}) + "\n")

def transcribe_audio(audio_path):
    if not os.path.exists(audio_path):
        log_error(f"Audio file not found: {audio_path}")
        sys.exit(1)

    try:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        
        print("Loading model...", flush=True)
        if device == "cpu":
            model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type, 
                                 cpu_threads=4, num_workers=1, download_root=CACHE_DIR)
        else:
            model = WhisperModel(MODEL_SIZE, device=device, compute_type=compute_type, 
                                 download_root=CACHE_DIR)
        
        print("Processing transcription...", flush=True)
        segments, info = model.transcribe(audio_path, word_timestamps=True, beam_size=2, vad_filter=True)
        
        full_text = ""
        words_list = []
        
        for segment in segments:
            full_text += segment.text + " "
            if segment.words:
                for word in segment.words:
                    words_list.append({
                        "word": word.word,
                        "start": word.start,
                        "end": word.end,
                        "probability": word.probability
                    })
                    
        result = {
            "text": full_text.strip(),
            "words": words_list
        }
        
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
            
        print("Transcription complete.", flush=True)
        sys.exit(0)
        
    except Exception as e:
        log_error(f"Transcription failed: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        log_error("Usage: python transcribe.py <audio_path>")
        sys.exit(1)
    transcribe_audio(sys.argv[1])
