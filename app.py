import os
import tempfile
import logging
import re
import zipfile
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
from flask import Flask, render_template, request, jsonify
from googleapiclient.discovery import build
import yt_dlp
from pydub import AudioSegment

app = Flask(__name__)


# Load API key and email credentials from .env file
load_dotenv()
api_key = os.getenv('YOUTUBE_API_KEY')
sender_email = os.getenv('SENDER_EMAIL')
email_password = os.getenv('EMAIL_PASSWORD')

# Validate environment variables
if not all([api_key, sender_email, email_password]):
    raise ValueError("Missing environment variables. Please check your .env file.")

# Determine the number of CPU cores available
num_cores = multiprocessing.cpu_count()

# Function to validate email
def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

# Function to get YouTube links
def get_youtube_links(api_key, query, max_results=20):
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
        search_response = youtube.search().list(
            q=query,
            part='snippet',
            type='video',
            maxResults=max_results
        ).execute()

        videos = []
        for item in search_response['items']:
            video_id = item['id']['videoId']
            video_title = item['snippet']['title']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            videos.append((video_title, video_url))

        return videos
    except Exception as e:
        logging.error(f"Failed to fetch YouTube links: {e}")
        return []

# Function to download audio from YouTube
def download_single_audio(url, index, download_path):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}_%(title)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'retries': 10,
        'fragment_retries': 10,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        downloaded_files = [f for f in os.listdir(download_path) if f.startswith(f"song_{index}_") and f.endswith(".mp3")]
        if downloaded_files:
            return os.path.join(download_path, downloaded_files[0])
        else:
            logging.error(f"Downloaded file not found for {url}")
            return None
    except Exception as e:
        logging.error(f"Error downloading audio: {e}")
        return None

# Function to download all audio files in parallel
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
                logging.error(f"Error occurred: {e}")

    return downloaded_files

# Function to create a mashup from audio files
def create_mashup(audio_files, output_file, trim_duration):
    mashup = AudioSegment.silent(duration=0)
    total_trim_duration_per_file = trim_duration * 1000  # Convert to milliseconds

    for file in audio_files:
        try:
            audio = AudioSegment.from_file(file)
            if len(audio) < total_trim_duration_per_file:
                logging.warning(f"Audio file {file} is shorter than trim duration. Using full length.")
                part = audio
            else:
                part = audio[:total_trim_duration_per_file]
            mashup += part
        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")

    if len(mashup) == 0:
        logging.error("No audio files were successfully processed.")
        return None

    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)
    if len(mashup) < expected_mashup_duration:
        logging.warning(f"Mashup duration ({len(mashup)}ms) is less than expected ({expected_mashup_duration}ms).")
    else:
        mashup = mashup[:expected_mashup_duration]

    mashup.export(output_file, format="mp3", bitrate="128k")
    return output_file

def create_zip_file(file_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    return zip_path

# Function to send email
def send_email(sender_email, receiver_email, subject, body, attachment_path, password):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        with open(attachment_path, 'rb') as attachment:
            part = MIMEBase('application', 'zip')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f"attachment; filename= {os.path.basename(attachment_path)}")
            msg.attach(part)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, password)
        text = msg.as_string()
        server.sendmail(sender_email, receiver_email, text)
        server.quit()

        logging.info("Email sent successfully!")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/create_mashup', methods=['POST'])
def create_mashup_route():
    singer_name = request.form['singer_name']
    num_videos = int(request.form['num_videos'])
    trim_duration = int(request.form['trim_duration'])
    receiver_email = request.form['receiver_email']

    if not singer_name or not is_valid_email(receiver_email):
        return jsonify({'status': 'error', 'message': 'Please enter a valid singer name and email address.'})

    videos = get_youtube_links(api_key, singer_name, max_results=num_videos)

    if not videos:
        return jsonify({'status': 'error', 'message': f'No videos found for {singer_name}. Please try a different singer name.'})

    download_path = tempfile.mkdtemp()
    video_urls = [url for _, url in videos]
    audio_files = download_all_audio(video_urls, download_path)

    if not audio_files:
        return jsonify({'status': 'error', 'message': 'Failed to download audio files. Please try again.'})

    output_file = os.path.join(tempfile.gettempdir(), "mashup.mp3")
    mashup_file = create_mashup(audio_files, output_file, trim_duration)

    if not mashup_file:
        return jsonify({'status': 'error', 'message': 'Failed to create mashup. Please try again.'})

    zip_file = os.path.join(tempfile.gettempdir(), "mashup.zip")
    create_zip_file(mashup_file, zip_file)

    subject = f"Your {singer_name} YouTube Mashup"
    body = f"Please find attached your custom YouTube mashup of {singer_name} songs. Duration: {trim_duration * len(audio_files)} seconds."
    email_sent = send_email(sender_email, receiver_email, subject, body, zip_file, email_password)

    # Cleanup
    os.remove(mashup_file)
    os.remove(zip_file)
    for file in audio_files:
        os.remove(file)

    if email_sent:
        return jsonify({'status': 'success', 'message': 'Mashup created and sent successfully! Check your email.'})
    else:
        return jsonify({'status': 'error', 'message': 'Mashup created but failed to send email. Please try again.'})

if __name__ == '__main__':
    app.run(debug=True)