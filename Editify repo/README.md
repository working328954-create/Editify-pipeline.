# Editify Core: AI-Driven Automated Video Editing Pipeline

**Editify** is an open-source bridge and automation pipeline connecting Large Language Models (via Model Context Protocol) and local AI processing tools directly to professional video editing software (DaVinci Resolve). 

It bridges the gap between technical video ingestion/transcription layers and creative guidance layers, allowing automated, natural language-driven video assembly.

---

## 🏗️ Architecture Overview

Editify separates execution into two primary domains: a **Technical Pipeline Layer** (handling file I/O, transcription, processing, and rendering) and a **Creative Guidance Layer** (handling pacing, cut logic, and styling rules).
