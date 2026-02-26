# NVPlayer

<img width="1202" height="1030" alt="스크린샷 2026-02-26 000849" src="https://github.com/user-attachments/assets/f98be411-b523-4668-aeb6-20000d603d51" />

안녕하세요. NVPlayer를 만든 ningning-voice 입니다. 
프로그램명에서 볼 수 있듯이 제가 좋아하는 에스파와 같은 케이팝 아이돌의 노래를 듣기 위해서 직접 만든 음악 플레이어입니다. 아직 미완성이지만 사용하는데 (아마) 지장은 없습니다. 지원하는 코덱은 flac, mp3, ogg, wav 입니다. 

<img width="1202" height="1030" alt="스크린샷 2026-02-26 142045" src="https://github.com/user-attachments/assets/df1f53f3-80f9-4d41-8808-a5a69673c420" />

음악 폴더 열기를 눌러서 음악이 저장되어 있는 폴더를 선택할 수 있습니다.
아티스트 탭에서는 아티스트를 a-z 순으로 정렬해주며 각 아티스트의 아이콘은 메타데이터 속 가장 최신 앨범의 커버로 설정됩니다. 아티스트를 클릭하면 앨범이 가장 최신 순으로 정렬되며 앨범을 클릭하면 해당 앨범의 전곡이 재생됩니다. 
전체 트랙 탭에서는 아티스트 - 발매일 - 트랙 넘버 순으로 모든 곡을 보여줍니다. 
재생 기록 탭에서는 가장 최근에 들었던 최대 100곡의 기록이 남습니다. 
재생목록은 말그대로입니다.
가사는 메타데이터 속 가사를 읽어오지만 아직 LRC(synced lyrics)를 지원하지는 않습니다. 
재생 버튼 탭에는 대부분 정상이지만 EQ가 아직 미완성이고, 볼륨바가 실제 볼륨이랑 제대로 동기화되지 않은 버그가 있습니다. 그리고 랜덤 재생은 재생목록을 섞어주는 것이 아니라 이전 혹은 다음 버튼을 눌렀을 때 랜덤으로 섞어주는 기능입니다. (한마디로 이전 곡으로 돌아가지 않는다는 뜻입니다.)

<img width="401" height="150" alt="스크린샷 2026-02-26 144020" src="https://github.com/user-attachments/assets/12d7f33b-94fc-41d4-b1ee-d84131aca17b" />

미니 플레이어 기능도 존재합니다.

============================================================================================

Hello, I'm the ningning-voice who created NVPlayer. 
As you can see from the name of the program, I made my own music player to listen to the songs of my favorite K-pop idols, like aespa. It's still incomplete, but it doesn't have a problem using it (probably). The supported codecs are flac, mp3, ogg, and wav. 

<img width="1202" height="1030" alt="스크린샷 2026-02-26 142045" src="https://github.com/user-attachments/assets/df1f53f3-80f9-4d41-8808-a5a69673c420" />

Click Open Music Folder to select the folder where the music is stored.
The Artists tab sorts artists in a-z order, and each artist's icon is set to cover the most recent album in the metadata. If you click an artist, the album will be sorted in the most current order, and if you click an album, the entire album will be played. 
The full track tab shows all songs in the order of artist - release date - track number. 
On the History of Play tab, the record of up to 100 songs that you listened to most recently is left. 
The playlist is literally what it is.
Lyrics read lyrics in metadata, but do not yet support synchronized lyrics (LRC). 
Most of the play button tabs are fine, but there is a bug where the EQ is still incomplete, and the volume bar is not properly synchronized with the actual volume. And random play is not about mixing playlists, it's about mixing randomly when you press the previous or next button. (In a nutshell, it doesn't go back to the previous song.)

<img width="401" height="150" alt="스크린샷 2026-02-26 144020" src="https://github.com/user-attachments/assets/12d7f33b-94fc-41d4-b1ee-d84131aca17b" />

There is also a mini-player feature.
