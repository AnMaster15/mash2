import os
import sys
import yt_dlp
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import subprocess
import shutil

num_cores = multiprocessing.cpu_count()

def search_youtube_videos(query, max_results=20):
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'default_search': 'ytsearch',
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        search_results = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
        
    videos = []
    if 'entries' in search_results:
        for entry in search_results['entries']:
            if entry.get('url'):
                videos.append(entry['url'])
    
    return videos

def download_single_audio(url, index, download_path):
    output_file = os.path.join(download_path, f'song_{index}.mp3')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        downloaded_files = [f for f in os.listdir(download_path) if f.startswith(f"song_{index}.") and f.endswith(".mp3")]
        if downloaded_files:
            return os.path.join(download_path, downloaded_files[0])
        else:
            print(f"Downloaded file not found for {url}")
            return None
    except Exception as e:
        print(f"Error downloading audio: {e}")
        return None

def download_all_audio(video_urls, download_path):
    downloaded_files = []
    with ThreadPoolExecutor(max_workers=num_cores) as executor:
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for future in as_completed(futures):
            try:
                mp3_file = future.result()
                if mp3_file:
                    downloaded_files.append(mp3_file)
            except Exception as e:
                print(f"Error occurred: {e}")

    return downloaded_files

def create_mashup_ffmpeg(audio_files, output_file, trim_duration):
    """Create mashup using ffmpeg directly instead of pydub"""
    concat_file = "concat_list.txt"
    with open(concat_file, "w") as f:
        for audio_file in audio_files:
            trimmed_file = f"{audio_file}.trimmed.mp3"
            subprocess.run([
                "ffmpeg", "-y", "-i", audio_file,
                "-t", str(trim_duration),
                "-acodec", "copy",
                trimmed_file
            ], capture_output=True)
            f.write(f"file '{trimmed_file}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-c", "copy",
        output_file
    ], capture_output=True)

    # Clean up temporary files
    os.remove(concat_file)
    for audio_file in audio_files:
        trimmed_file = f"{audio_file}.trimmed.mp3"
        if os.path.exists(trimmed_file):
            os.remove(trimmed_file)

def check_dependencies():
    """Check if required dependencies are installed"""
    if not shutil.which('ffmpeg'):
        print("Error: ffmpeg is not installed. Please install ffmpeg first.")
        print("On macOS, you can install it using: brew install ffmpeg")
        print("On Ubuntu/Debian: sudo apt-get install ffmpeg")
        sys.exit(1)

def main():
    if len(sys.argv) != 5:
        print("Usage: python script.py <singer_name> <number_of_videos> <trim_duration> <output_file>")
        sys.exit(1)

    check_dependencies()

    singer_name = sys.argv[1]
    num_videos = int(sys.argv[2])
    trim_duration = int(sys.argv[3])
    output_file = sys.argv[4]

    if num_videos <= 10:
        print("Error: Number of videos must be greater than 10.")
        sys.exit(1)

    if trim_duration <= 20:
        print("Error: Trim duration must be greater than 20 seconds.")
        sys.exit(1)

    try:
        temp_dir = "temp_audio_files"
        os.makedirs(temp_dir, exist_ok=True)

        print(f"Searching for {num_videos} videos of {singer_name}...")
        video_urls = search_youtube_videos(singer_name, num_videos)

        if not video_urls:
            print("No videos found. Exiting.")
            sys.exit(1)

        print("Downloading and converting videos to audio...")
        audio_files = download_all_audio(video_urls, temp_dir)

        if not audio_files:
            print("No audio files were successfully downloaded. Exiting.")
            sys.exit(1)

        print(f"Creating mashup with {trim_duration} seconds from each audio...")
        create_mashup_ffmpeg(audio_files, output_file, trim_duration)

        print(f"Mashup created successfully: {output_file}")


        for file in audio_files:
            if os.path.exists(file):
                os.remove(file)
        os.rmdir(temp_dir)

    except Exception as e:
        print(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
