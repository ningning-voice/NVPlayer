# NVPlayer  
### Metadata-Based Audio Player with Native FLAC & 10-Band Equalizer
<img width="1202" height="1030" alt="스크린샷 2026-03-04 152817" src="https://github.com/user-attachments/assets/f40fc168-6772-4717-8733-cc9c9548be1a" />

NVPlayer is a desktop music player built with Python and PyQt5.

I started developing this project on August 5, 2025 as a personal tool to better enjoy music from my favorite K-pop artists (such as aespa), while also learning GUI design, audio processing, and metadata-driven library management.

What began as a simple player has evolved into a structured audio engine project featuring native FLAC playback, real-time equalization, asynchronous scanning, and persistent state management.

---

## ✨ Key Features

### 🎵 Metadata-Based Library System
NVPlayer organizes your music using embedded metadata instead of folder structure.

The library is automatically structured by:

- Artist
- Album
- Release Year
- Track Number

Tracks are sorted consistently and intelligently across views.

---

### 🎧 Dual Audio Engine Support

NVPlayer supports two playback backends:

**1. Pygame Mixer (Compatibility Mode)**  
- Simple and stable playback

**2. SoundDevice Backend (Advanced Mode)**  
- Native FLAC decoding  
- Sample-level audio processing  
- Real-time 10-band equalizer  
- Accurate playback position tracking  

The SoundDevice backend enables more advanced audio handling and processing.

---

### 🎚 10-Band Graphic Equalizer
<img width="372" height="402" alt="스크린샷 2026-03-04 170356" src="https://github.com/user-attachments/assets/adfb367b-0b38-459b-8752-6fe66cfc5e57" />

Includes a real-time 10-band EQ covering:

31Hz – 16kHz

The equalizer operates in the frequency domain using FFT processing and applies gain adjustments per band during playback.

---

### 📚 Library Management

- SQLite-based metadata storage  
- Asynchronous scanning using QThread (non-blocking UI)  
- Incremental library updates  
- Automatic album cover extraction from metadata  

---

### 🖼 Album Cover Optimization

- Embedded cover extraction (FLAC / ID3 APIC)
- Rounded thumbnail rendering
- Disk-based thumbnail caching for performance

---

### 🕒 Playback History

- Stores up to 100 recently played tracks
- Persistent across sessions

---

### 📂 Playlist System

- Persistent user playlist (JSON-based storage)
- Automatically removes missing files
- Album-level and full-library playback support

---

### 🎛 Mini Player Mode
<img width="471" height="150" alt="스크린샷 2026-03-04 152820" src="https://github.com/user-attachments/assets/3ddd7b09-9481-411e-9168-564d3a995792" />

Compact playback window for lightweight control.

---

## 🗂 Supported Formats

- FLAC
- MP3
- OGG
- WAV

---

## 🛠 Tech Stack

- Python 3
- PyQt5 (GUI)
- SQLite (library database)
- Mutagen (metadata processing)
- pygame (basic playback)
- sounddevice + soundfile (native FLAC & advanced audio engine)
- NumPy (FFT-based equalizer)

---

## 🚀 How to Run

1. Install Python 3.x
2. Install dependencies: pip install PyQt5 mutagen pygame sounddevice soundfile numpy
3. Run: python NVPlayer.py

---

## ⚠ Known Limitations

- The EQ is a simple graphic implementation and not professionally tuned.
- Shuffle logic is not a full playlist reshuffle system.
- The project is currently structured in a single large file (modular refactoring planned).
- UI polishing and code cleanup are ongoing.

---

## 📌 Project Status

This is a personal learning and hobby project focused on:

- GUI architecture
- Audio engine experimentation
- Metadata-driven design
- Performance optimization techniques

The project continues to evolve.

Feedback, suggestions, and improvements are welcome.
===========================================================================

P.S.

I'm a total stranger to coding(I can't even say I'm a developer).

P.P.S

To be honest, I can't add an English version. It's hard.

P.P.P.S 

I will update the English version soon, please wait a little bit.
