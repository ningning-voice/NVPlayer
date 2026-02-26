# NVPlayer

<img width="1202" height="1030" alt="스크린샷 2026-02-26 142045" src="https://github.com/user-attachments/assets/166312d9-7743-4036-91f8-9a489793984c" />

안녕하세요. NVPlayer를 만든 ningning-voice입니다.

NVPlayer는 개인적으로 사용하기 위해 제작한 메타데이터 기반 음악 플레이어입니다.  
2025년 8월 5일부터 개발을 시작했으며, aespa와 같은 제가 좋아하는 K-pop 아티스트의 음악을 보다 편리하게 감상하기 위해 만들었습니다.

이 프로젝트는 취미 목적이자, Python GUI 개발과 오디오 처리 및 메타데이터 관리 구조를 학습하기 위한 프로젝트이기도 합니다.

---

## 기술 스택

- Python
- PyQt5
- Mutagen (메타데이터 처리)
- 오디오 재생 모듈 기반 플레이어 구조

---

## 지원 포맷

- FLAC
- MP3
- OGG
- WAV

---

## 주요 특징

### 1. 메타데이터 기반 정렬 시스템
단순 폴더 구조가 아닌, 음원 파일의 메타데이터를 기반으로:

- 아티스트
- 앨범
- 발매일
- 트랙 번호

를 자동 분석하여 라이브러리를 구성합니다.

---

### 2. Artists 탭
- 아티스트를 A–Z 순으로 정렬
- 가장 최근 앨범의 커버 이미지 자동 적용
- 앨범은 발매일 기준 최신순 정렬
- 앨범 클릭 시 전체 재생

---

### 3. All Tracks 탭
정렬 기준:
아티스트 → 발매일 → 트랙 번호

---

### 4. History 탭
- 최근 재생한 곡 최대 100곡까지 저장

---

### 5. Lyrics
- 메타데이터 내 가사 표시
- LRC 동기화 가사는 미지원

---

### 6. Mini Player
- 별도 미니 플레이어 모드 제공

<img width="401" height="150" alt="스크린샷 2026-02-26 144020" src="https://github.com/user-attachments/assets/3fe74065-5145-4f1e-8d78-af9288e76b3c" />

---

## 알려진 문제

- EQ 기능 미완성
- 볼륨 슬라이더 동기화 문제
- 셔플 알고리즘이 일반적인 플레이리스트 셔플과 다름

---

## 실행 방법

1. Python 3.x 설치
2. 필요한 라이브러리 설치

============================================================================================

# NVPlayer

<img width="1202" height="1030" alt="스크린샷 2026-02-26 142045" src="https://github.com/user-attachments/assets/bb3ff702-4681-478f-9902-3d50b76d3b6a" />


Hello! I'm ningning-voice, the creator of NVPlayer.

NVPlayer is a metadata-based music player that I started developing on August 5, 2025.  
I originally built it to better enjoy music from my favorite K-pop artists, such as aespa.

This project is both a personal hobby project and a way for me to practice Python GUI development, audio playback handling, and metadata-driven library organization.

The application is still a work in progress, but it is generally usable.

---

## Tech Stack

- Python
- PyQt5 (GUI)
- Mutagen (metadata processing)
- Audio playback module-based player structure

---

## Supported Formats

- FLAC
- MP3
- OGG
- WAV

---

## Core Features

### 1. Metadata-Based Library System

Unlike simple folder-based music players, NVPlayer organizes your music library using file metadata:

- Artist
- Album
- Release Date
- Track Number

The library is automatically structured based on this information.

---

### 2. Artists Tab

- Artists are sorted in A–Z order.
- Each artist icon automatically uses the cover image of the most recent album (based on metadata).
- Albums are sorted by release date (newest first).
- Clicking an album plays the entire album.

---

### 3. All Tracks Tab

Tracks are sorted by:

Artist → Release Date → Track Number

---

### 4. History Tab

- Stores up to 100 recently played tracks.

---

### 5. Lyrics

- Displays embedded lyrics from metadata.
- Synchronized lyrics (LRC) are not supported yet.

---

### 6. Mini Player

- Includes a separate mini-player mode for compact playback control.

<img width="401" height="150" alt="스크린샷 2026-02-26 144020" src="https://github.com/user-attachments/assets/829b16c2-8a44-4eab-bee4-c2b3892eeb74" />

---

## Known Issues / Limitations

- The EQ feature is unfinished.
- The volume slider is not perfectly synchronized with the actual system volume.
- Shuffle mode does not reshuffle the entire playlist.
  Instead, it randomly selects a track when pressing previous or next.
  (It does not always return to the exact previous track.)

---

## How to Run

1. Install Python 3.x
2. Install required libraries:

============================================================================================

P.S.

I'm a total stranger to coding (I can't even say I'm a developer). Any good ideas would be appreciated.

P.P.S

To be honest, I can't add an English version. It's hard.
